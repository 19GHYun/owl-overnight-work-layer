"""OWL 워커(미니PC): 작업을 HTTP로 받아 git → Claude Code → 테스트 → push 순서로 실행."""
import hashlib
import hmac
import json
import os
import queue
import re
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from owl_common import (
    APP_VERSION,
    validate_identity,
    ensure_dir,
    find_tool,
    now_iso,
    results_dir,
    run_cmd,
    tail_lines,
)

TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
REQUIRED_TEXT_FIELDS = ("task_id", "repo", "base_branch", "instruction", "test_cmd")
DEFAULT_TIMEOUT_SEC = 1800


class StepError(Exception):
    def __init__(self, step: str, reason: str):
        super().__init__(f"{step}: {reason}")
        self.step = step
        self.reason = reason


def repo_key(url: str) -> str:
    name = url.rstrip("/\\").replace("\\", "/").split("/")[-1]
    if name.endswith(".git"):
        name = name[:-4]
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:40] or "repo"
    return f"{name}-{hashlib.sha1(url.encode('utf-8')).hexdigest()[:8]}"


def parse_porcelain_z(output: str):
    """git status --porcelain=v1 -z -uall 출력에서 바뀐 파일 경로 목록."""
    paths = []
    entries = output.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        paths.append(path)
        if status[0] in ("R", "C"):
            i += 1  # 다음 항목은 원래 경로
    return paths


class WorkerCore:
    def __init__(self, logger, work_root: str, token: str, commit_name: str = "",
                 commit_email: str = "", on_change=None):
        self.logger = logger
        self.work_root = work_root
        self.token = token
        self.commit_name = commit_name.strip()
        self.commit_email = commit_email.strip()
        self.on_change = on_change or (lambda task_id: None)
        self.tasks = {}
        self.lock = threading.Lock()
        self.q = queue.Queue()
        self.tools = {}
        self.httpd = None
        self._threads = []
        self.running = False

    # ------------------------------------------------------------ 시작/중지
    def check_tools(self) -> dict:
        self.tools = {name: find_tool(name) for name in ("claude", "git", "docker")}
        return self.tools

    def start(self, host: str, port: int) -> None:
        if not self.token:
            self.logger.log("-", "error", step="startup", reason="토큰이 비어 있음")
            raise RuntimeError("토큰이 비어 있습니다. OWL_TOKEN 값을 입력하세요.")
        problem = validate_identity(self.commit_name, self.commit_email)
        if problem:
            self.logger.log("-", "error", step="startup", reason=problem)
            raise RuntimeError(f"{problem}. 워커 설정의 '커밋 작성자'를 채우세요.")
        tools = self.check_tools()
        missing = [n for n in ("claude", "git") if not tools[n]]
        if missing:
            reason = f"필수 프로그램을 PATH에서 찾을 수 없음: {', '.join(missing)}"
            self.logger.log("-", "error", step="startup", reason=reason)
            raise RuntimeError(reason)
        ensure_dir(os.path.join(self.work_root, "repos"))
        ensure_dir(os.path.join(self.work_root, "worktrees"))
        try:
            self.httpd = ThreadingHTTPServer((host, port), make_handler(self))
        except OSError as e:
            reason = f"{host}:{port} 열기 실패 ({type(e).__name__}: {e})"
            self.logger.log("-", "error", step="startup", reason=reason)
            raise RuntimeError(reason) from e
        self.running = True
        http_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        exec_thread = threading.Thread(target=self._exec_loop, daemon=True)
        self._threads = [http_thread, exec_thread]
        http_thread.start()
        exec_thread.start()
        self.logger.log(
            "-",
            "worker_started",
            reason=f"{host}:{port}",
            tools=tools,
            commit_author=f"{self.commit_name} <{self.commit_email}>",
            work_root=self.work_root,
            version=APP_VERSION,
        )

    def stop(self) -> None:
        if not self.running:
            return
        self.running = False
        self.httpd.shutdown()
        self.httpd.server_close()
        self.q.put(None)
        self.logger.log(
            "-", "worker_stopped", reason="새 작업 접수 중단. 실행 중인 작업은 끝까지 진행"
        )

    # ------------------------------------------------------------ 접수/조회
    def submit(self, payload):
        if not isinstance(payload, dict):
            return self._reject(None, "요청 본문이 JSON 객체가 아님")
        task_id = payload.get("task_id")
        for field in REQUIRED_TEXT_FIELDS:
            value = payload.get(field)
            if not isinstance(value, str) or not value.strip():
                return self._reject(task_id, f"필수 필드 누락 또는 빈 값: {field}")
        if not TASK_ID_RE.match(task_id):
            return self._reject(task_id, "task_id 형식 오류(영문·숫자·._- 1~64자)")
        if not BRANCH_RE.match(payload["base_branch"]):
            return self._reject(task_id, "base_branch 형식 오류")
        timeout = payload.get("timeout_sec", DEFAULT_TIMEOUT_SEC)
        if not isinstance(timeout, int) or not (60 <= timeout <= 86400):
            return self._reject(task_id, "timeout_sec는 60~86400 사이 정수")
        request = {f: payload[f] for f in REQUIRED_TEXT_FIELDS}
        request["timeout_sec"] = timeout
        with self.lock:
            if task_id in self.tasks:
                dup = True
            else:
                dup = False
                self.tasks[task_id] = {
                    "task_id": task_id,
                    "status": "queued",
                    "step": "queued",
                    "received_at": now_iso(),
                    "request": request,
                    "result": None,
                    "error": None,
                }
        if dup:
            return self._reject(task_id, "이미 접수된 task_id", code=409)
        self.q.put(task_id)
        self.logger.log(task_id, "received", reason=f"repo={request['repo']} base={request['base_branch']}")
        self.on_change(task_id)
        return 202, {"task_id": task_id, "status": "queued"}

    def _reject(self, task_id, reason, code=400):
        self.logger.log(task_id, "error", step="receive", reason=reason)
        return code, {"error": reason}

    def get(self, task_id):
        with self.lock:
            st = self.tasks.get(task_id)
            return json.loads(json.dumps(st)) if st else None

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(list(self.tasks.values())))

    # ------------------------------------------------------------ 실행
    def _set(self, task_id, **kw):
        # 결과는 실행 스레드만 고치므로, 조회 스레드와 공유하지 않도록 복사본을 저장한다.
        if kw.get("result") is not None:
            kw["result"] = json.loads(json.dumps(kw["result"]))
        with self.lock:
            self.tasks[task_id].update(kw)
        self.on_change(task_id)

    def _exec_loop(self):
        while True:
            task_id = self.q.get()
            if task_id is None:
                break
            self._run_task(task_id)

    def _remaining(self, step, deadline):
        left = deadline - time.monotonic()
        if left <= 0:
            raise StepError(step, "timeout_sec 초과")
        return left

    def _run_task(self, task_id):
        with self.lock:
            req = dict(self.tasks[task_id]["request"])
        res = {
            "agent_exit_code": None,
            "test_exit_code": None,
            "test_log_tail": "",
            "changed_files": [],
            "branch": f"owl/{task_id}",
            "step_ms": {},
            "raw_paths": {},
            "cost_usd": None,
        }
        out_dir = ensure_dir(os.path.join(results_dir(), task_id))
        deadline = time.monotonic() + req["timeout_sec"]
        self._set(task_id, status="running", step="git", started_at=now_iso(), result=res)
        try:
            worktree = self._git_sync(task_id, req, res, deadline)
            self._agent(task_id, req, res, worktree, out_dir, deadline)
            self._test(task_id, req, res, worktree, out_dir, deadline)
            self._push(task_id, res, worktree, deadline)
            self._set(task_id, status="done", step="done", finished_at=now_iso(), result=res)
            self.logger.log(
                task_id,
                "done",
                reason=None if res["changed_files"] else "바뀐 파일 없음",
            )
        except StepError as e:
            self._fail(task_id, e.step, e.reason, res)
        except Exception as e:  # 예상 못 한 오류도 단계와 함께 남긴다
            tb_path = os.path.join(out_dir, "worker_traceback.txt")
            with open(tb_path, "w", encoding="utf-8") as f:
                f.write(traceback.format_exc())
            res["raw_paths"]["traceback"] = tb_path
            with self.lock:
                step = self.tasks[task_id].get("step", "unknown")
            self._fail(task_id, step, f"{type(e).__name__}: {e}", res)
        finally:
            with self.lock:
                final = json.loads(json.dumps(self.tasks[task_id]))
            with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
                json.dump(final, f, ensure_ascii=False, indent=2)

    def _fail(self, task_id, step, reason, res):
        self._set(
            task_id,
            status="failed",
            step=step,
            finished_at=now_iso(),
            result=res,
            error={"step": step, "reason": reason},
        )
        self.logger.log(task_id, "error", step=step, reason=reason)

    def _git(self, step, args, cwd, deadline, config=None):
        """config: {"user.name": ...} 처럼 이번 명령에만 적용할 git 설정 (-c)."""
        prefix = []
        for key, value in (config or {}).items():
            prefix += ["-c", f"{key}={value}"]
        r = run_cmd([self.tools["git"], *prefix, *args], cwd=cwd,
                    timeout=self._remaining(step, deadline))
        if r.timed_out:
            raise StepError(step, f"git {args[0]} 시간 초과")
        if r.exit_code != 0:
            detail = tail_lines(r.stderr or r.stdout, 5)
            raise StepError(step, f"git {' '.join(args[:2])} 실패(exit {r.exit_code}): {detail}")
        return r

    def _git_sync(self, task_id, req, res, deadline):
        t0 = time.monotonic()
        ensure_dir(os.path.join(self.work_root, "worktrees"))
        repo_dir = os.path.join(ensure_dir(os.path.join(self.work_root, "repos")), repo_key(req["repo"]))
        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            self._git("git", ["clone", req["repo"], repo_dir], self.work_root, deadline)
        else:
            self._git("git", ["fetch", "--prune", "origin"], repo_dir, deadline)
        worktree = os.path.join(self.work_root, "worktrees", task_id)
        if os.path.exists(worktree):
            raise StepError("git", f"작업 폴더가 이미 있음: {worktree}")
        self._git(
            "git",
            ["worktree", "add", "-b", res["branch"], worktree, f"origin/{req['base_branch']}"],
            repo_dir,
            deadline,
        )
        res["step_ms"]["git"] = int((time.monotonic() - t0) * 1000)
        self.logger.log(task_id, "git_synced", step_ms=res["step_ms"]["git"], reason=worktree)
        return worktree

    def _agent(self, task_id, req, res, worktree, out_dir, deadline):
        self._set(task_id, step="agent", result=res)
        self.logger.log(task_id, "agent_started")
        args = [
            self.tools["claude"],
            "-p",
            "--output-format",
            "json",
            "--permission-mode",
            "acceptEdits",
        ]
        r = run_cmd(args, cwd=worktree, input_text=req["instruction"],
                    timeout=self._remaining("agent", deadline))
        out_path = os.path.join(out_dir, "agent_output.json")
        err_path = os.path.join(out_dir, "agent_stderr.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(r.stdout)
        with open(err_path, "w", encoding="utf-8") as f:
            f.write(r.stderr)
        res["raw_paths"]["agent_output"] = out_path
        res["raw_paths"]["agent_stderr"] = err_path
        res["step_ms"]["agent"] = r.duration_ms
        res["agent_exit_code"] = None if r.timed_out else r.exit_code

        notes = []
        is_error = False
        try:
            data = json.loads(r.stdout)
        except json.JSONDecodeError:
            data = None
            notes.append("에이전트 출력이 JSON이 아님(agent_output.json 확인)")
        if isinstance(data, dict):
            for key in ("total_cost_usd", "cost_usd"):
                value = data.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    res["cost_usd"] = float(value)
                    break
            if data.get("is_error") is True:
                is_error = True
                notes.append(f"is_error=true: {tail_lines(str(data.get('result', '')), 3)}")

        status = run_cmd([self.tools["git"], "status", "--porcelain=v1", "-z", "-uall"],
                         cwd=worktree, timeout=60)
        if status.exit_code != 0:
            raise StepError("agent", f"git status 실패: {tail_lines(status.stderr, 3)}")
        res["changed_files"] = parse_porcelain_z(status.stdout)
        if not res["changed_files"]:
            notes.append("바뀐 파일 없음 — 수정 권한 또는 지시문 확인")

        self.logger.log(
            task_id,
            "agent_finished",
            exit_code=res["agent_exit_code"],
            step_ms=r.duration_ms,
            cost_usd=res["cost_usd"],
            changed_files=len(res["changed_files"]),
            reason="; ".join(notes) or None,
        )
        if r.timed_out:
            raise StepError("agent", "에이전트 시간 초과(timeout_sec)")
        if r.exit_code != 0 or is_error:
            if isinstance(data, dict) and data.get("result"):
                detail = tail_lines(str(data["result"]), 5)
            else:
                detail = tail_lines(r.stderr or r.stdout, 5)
            raise StepError("agent", f"에이전트 실패(exit {r.exit_code}): {detail}")

    def _test(self, task_id, req, res, worktree, out_dir, deadline):
        self._set(task_id, step="test", result=res)
        self.logger.log(task_id, "test_started", reason=req["test_cmd"])
        r = run_cmd(req["test_cmd"], cwd=worktree, shell=True,
                    timeout=self._remaining("test", deadline))
        combined = f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}\n"
        path = os.path.join(out_dir, "test_output.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(combined)
        res["raw_paths"]["test_output"] = path
        res["step_ms"]["test"] = r.duration_ms
        res["test_exit_code"] = None if r.timed_out else r.exit_code
        res["test_log_tail"] = tail_lines(r.stdout + "\n" + r.stderr, 50)
        self.logger.log(
            task_id,
            "test_finished",
            exit_code=res["test_exit_code"],
            step_ms=r.duration_ms,
            reason="시간 초과" if r.timed_out else None,
        )
        if r.timed_out:
            raise StepError("test", "테스트 시간 초과(timeout_sec)")
        # 종료 코드가 0이 아니어도 워커 오류가 아니다. 통과·반려 판정은 데스크톱이 한다.

    def _push(self, task_id, res, worktree, deadline):
        self._set(task_id, step="push", result=res)
        if not res["changed_files"]:
            return
        t0 = time.monotonic()
        # 테스트가 만든 파일은 빼고, 에이전트가 바꾼 파일만 커밋한다.
        self._git("push", ["add", "-A", "--", *res["changed_files"]], worktree, deadline)
        # 전역 git 설정을 건드리지 않고, 워커 설정의 작성자로 이 커밋만 만든다.
        self._git("push", ["commit", "-m", f"owl: {task_id}"], worktree, deadline,
                  config={"user.name": self.commit_name, "user.email": self.commit_email})
        self._git("push", ["push", "-u", "origin", res["branch"]], worktree, deadline)
        res["step_ms"]["push"] = int((time.monotonic() - t0) * 1000)
        self.logger.log(task_id, "pushed", step_ms=res["step_ms"]["push"], reason=res["branch"])


def make_handler(core: WorkerCore):
    task_path = re.compile(r"^/tasks/([A-Za-z0-9._-]{1,64})$")

    class Handler(BaseHTTPRequestHandler):
        server_version = f"OWLWorker/{APP_VERSION}"

        def log_message(self, fmt, *args):  # 정상 접근(5초 폴링)은 기록하지 않는다
            pass

        def log_error(self, fmt, *args):
            core.logger.log("-", "error", step="http", reason=fmt % args)

        def _send(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _auth(self):
            header = self.headers.get("Authorization", "")
            given = header[7:] if header.startswith("Bearer ") else ""
            if given and hmac.compare_digest(given.encode(), core.token.encode()):
                return True
            core.logger.log(
                "-", "auth_failed",
                reason=f"{self.client_address[0]} {self.command} {self.path}",
            )
            self._send(401, {"error": "unauthorized"})
            return False

        def do_GET(self):
            if not self._auth():
                return
            if self.path == "/health":
                self._send(200, {"ok": True, "version": APP_VERSION,
                                 "host": core.logger.host, "tools": core.tools})
                return
            m = task_path.match(self.path)
            if m:
                st = core.get(m.group(1))
                if st is None:
                    self._send(404, {"error": "unknown task_id"})
                else:
                    self._send(200, st)
                return
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._auth():
                return
            if self.path != "/tasks":
                self._send(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length <= 0 or length > 1_000_000:
                core.logger.log("-", "error", step="receive", reason=f"본문 길이 오류: {length}")
                self._send(400, {"error": "bad content length"})
                return
            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as e:
                core.logger.log("-", "error", step="receive", reason=f"JSON 파싱 실패: {e}")
                self._send(400, {"error": "invalid json"})
                return
            code, obj = core.submit(payload)
            self._send(code, obj)

    return Handler
