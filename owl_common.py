"""OWL 공용 모듈: 경로, 설정 파일, JSONL 로그, 외부 명령 실행."""
import datetime
import json
import locale
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback

APP_VERSION = "0.1.1"

# Windows에서 git/claude/docker 실행 시 콘솔 창이 뜨지 않게 한다.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- 경로/설정
def base_dir() -> str:
    """exe로 실행되면 exe 옆, 스크립트로 실행되면 스크립트 옆."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def logs_dir() -> str:
    return ensure_dir(os.path.join(base_dir(), "logs"))


def results_dir() -> str:
    return ensure_dir(os.path.join(base_dir(), "results"))


def tasks_dir() -> str:
    return ensure_dir(os.path.join(base_dir(), "tasks"))


def config_path() -> str:
    return os.path.join(base_dir(), "owl_config.json")


def load_config():
    """(설정 dict, 오류 메시지 또는 None)을 돌려준다. 파일이 없으면 빈 설정."""
    path = config_path()
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}, f"설정 파일 형식 오류(객체가 아님): {path}"
        return data, None
    except (OSError, json.JSONDecodeError) as e:
        return {}, f"설정 파일을 읽지 못함: {path} ({type(e).__name__}: {e})"


def save_config(cfg: dict) -> None:
    path = config_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def write_internal_error(name: str, text: str) -> str:
    """로그 시스템 자체나 GUI가 실패했을 때 남기는 별도 파일."""
    path = os.path.join(logs_dir(), f"{name}-internal.log")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"[{now_iso()}]\n{text}\n")
    return path


# ---------------------------------------------------------------- JSONL 로그
class JsonlLogger:
    """한 줄에 이벤트 하나. 파일은 logs/<pc>-YYYYMMDD.jsonl, 추가만 한다."""

    def __init__(self, pc: str):
        self.pc = pc
        self.host = socket.gethostname()
        self._lock = threading.Lock()
        self._listeners = []

    def add_listener(self, fn) -> None:
        self._listeners.append(fn)

    def log(self, task_id, event: str, **fields) -> dict:
        rec = {
            "ts": now_iso(),
            "task_id": task_id or "-",
            "pc": self.pc,
            "host": self.host,
            "event": event,
        }
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False)
        path = os.path.join(
            logs_dir(), f"{self.pc}-{datetime.date.today():%Y%m%d}.jsonl"
        )
        with self._lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        for fn in list(self._listeners):
            try:
                fn(rec)
            except Exception:  # 화면 표시 실패가 기록을 막으면 안 된다
                write_internal_error(self.pc, traceback.format_exc())
        return rec


def format_record(rec: dict) -> str:
    """로그 한 줄을 사람이 읽기 쉬운 형태로."""
    ts = rec.get("ts", "")
    clock = ts[11:19] if len(ts) >= 19 else ts
    parts = [clock, f"[{rec.get('task_id', '-')}]", rec.get("event", "")]
    if rec.get("step"):
        parts.append(f"step={rec['step']}")
    if rec.get("exit_code") is not None:
        parts.append(f"exit={rec['exit_code']}")
    if rec.get("step_ms") is not None:
        parts.append(f"{rec['step_ms'] / 1000:.1f}s")
    if "cost_usd" in rec:
        cost = rec["cost_usd"]
        parts.append("cost=기록 없음" if cost is None else f"cost=${cost:.4f}")
    text = " ".join(parts)
    if rec.get("reason"):
        text += f" — {rec['reason']}"
    return text


# ---------------------------------------------------------------- 외부 명령
def decode_bytes(data: bytes) -> str:
    if not data:
        return ""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False), errors="replace")


def tail_lines(text: str, n: int) -> str:
    lines = (text or "").rstrip().splitlines()
    return "\n".join(lines[-n:])


def find_tool(name: str):
    return shutil.which(name)


class CmdResult:
    def __init__(self, exit_code, stdout, stderr, timed_out, duration_ms):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.duration_ms = duration_ms


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_cmd(args, cwd=None, input_text=None, timeout=None, shell=False) -> CmdResult:
    """명령을 실행하고 출력 전체를 받는다. 시간 초과 시 자식 프로세스까지 종료."""
    start = time.monotonic()
    popen_kwargs = dict(
        cwd=cwd,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=shell,
    )
    if os.name == "nt":
        popen_kwargs["creationflags"] = CREATE_NO_WINDOW
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, **popen_kwargs)
    data = input_text.encode("utf-8") if input_text is not None else None
    timed_out = False
    try:
        out, err = proc.communicate(input=data, timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        out, err = proc.communicate()
    duration_ms = int((time.monotonic() - start) * 1000)
    return CmdResult(
        proc.returncode, decode_bytes(out), decode_bytes(err), timed_out, duration_ms
    )


def read_git_identity(git_path):
    """git 전역 설정의 user.name / user.email. 없으면 빈 문자열."""
    values = []
    for key in ("user.name", "user.email"):
        r = run_cmd([git_path, "config", "--global", "--get", key], timeout=15)
        # exit 1 = 값 없음(정상). 그 밖의 실패는 호출한 쪽이 알 수 있게 예외로 올린다.
        if r.exit_code not in (0, 1):
            raise RuntimeError(f"git config --global --get {key} 실패(exit {r.exit_code}): "
                               f"{tail_lines(r.stderr, 3)}")
        values.append(r.stdout.strip() if r.exit_code == 0 else "")
    return values[0], values[1]


def validate_identity(name: str, email: str):
    """문제가 없으면 None, 있으면 사유 문자열."""
    if not name.strip():
        return "커밋 작성자 이름이 비어 있음"
    if not email.strip():
        return "커밋 작성자 메일이 비어 있음"
    if "@" not in email or any(c in email for c in " <>"):
        return f"커밋 작성자 메일 형식 오류: {email}"
    if any(c in name for c in "<>\n"):
        return f"커밋 작성자 이름에 쓸 수 없는 문자: {name}"
    return None


def local_ipv4_addresses():
    addrs = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in addrs and not ip.startswith("127."):
                addrs.append(ip)
    except socket.gaierror:
        pass
    return addrs
