"""OWL GUI 진입점. 실행하면 이 PC의 역할(워커/디스패처)을 고른다.

    OWL.exe                  역할 선택 창
    OWL.exe --role worker    바로 워커 창
    OWL.exe --role dispatch  바로 디스패처 창
"""
import datetime
import json
import os
import sys
import threading
import time
import traceback

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from owl_common import (
    APP_VERSION,
    JsonlLogger,
    config_path,
    find_tool,
    format_record,
    load_config,
    local_ipv4_addresses,
    logs_dir,
    read_git_identity,
    results_dir,
    save_config,
    tasks_dir,
    validate_identity,
    write_internal_error,
)
from owl_dispatch import DispatchError, call, judge
from owl_worker import WorkerCore

POLL_INTERVAL_MS = 5000
POLL_FAIL_LIMIT = 12          # 5초 × 12 = 1분 동안 응답이 없으면 추적 중단
RESULT_GRACE_SEC = 120        # timeout_sec 이후 기다려 주는 여유 시간


class Bridge(QObject):
    """다른 스레드에서 GUI 스레드로 넘기는 통로."""
    log_rec = Signal(dict)
    task_changed = Signal(str)
    net_result = Signal(str, str, object)   # (종류, task_id, 결과)


def make_log_view() -> QPlainTextEdit:
    view = QPlainTextEdit()
    view.setReadOnly(True)
    view.setMaximumBlockCount(5000)
    view.setFont(QFont("Consolas", 9))
    return view


def make_table(headers) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def set_row(table: QTableWidget, key: str, values) -> int:
    """첫 열이 key인 행을 찾아 갱신하고, 없으면 새로 추가."""
    for row in range(table.rowCount()):
        item = table.item(row, 0)
        if item and item.text() == key:
            break
    else:
        row = table.rowCount()
        table.insertRow(row)
    for col, value in enumerate([key, *values]):
        table.setItem(row, col, QTableWidgetItem("" if value is None else str(value)))
    return row


def open_folder(path: str) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def safe_save_config(cfg, logger, parent) -> bool:
    try:
        save_config(cfg)
        return True
    except OSError as e:
        reason = f"설정 저장 실패: {config_path()} ({type(e).__name__}: {e})"
        logger.log("-", "error", step="config", reason=reason)
        QMessageBox.warning(parent, "설정 저장 실패", reason)
        return False


# =================================================================== 워커 창
class WorkerWindow(QMainWindow):
    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.wcfg = cfg.setdefault("worker", {})
        self.logger = JsonlLogger("minipc")
        self.bridge = Bridge()
        self.bridge.log_rec.connect(self._append_log)
        self.bridge.task_changed.connect(self._refresh_task)
        self.logger.add_listener(self.bridge.log_rec.emit)
        self.core = None

        self.setWindowTitle(f"OWL 워커 — {self.logger.host}  (v{APP_VERSION})")
        self.resize(1000, 720)

        self.host_combo = QComboBox()
        self.host_combo.setEditable(True)
        self.host_combo.addItems(local_ipv4_addresses() + ["0.0.0.0"])
        if self.wcfg.get("host"):
            self.host_combo.setCurrentText(self.wcfg["host"])
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65535)
        self.port_spin.setValue(int(self.wcfg.get("port", 8765)))
        self.token_edit = QLineEdit(self.wcfg.get("token") or os.environ.get("OWL_TOKEN", ""))
        self.token_edit.setEchoMode(QLineEdit.Password)
        self.root_edit = QLineEdit(
            self.wcfg.get("work_root") or os.path.join(os.path.expanduser("~"), "owl_work")
        )
        browse = QPushButton("폴더 선택")
        browse.clicked.connect(self._browse_root)
        root_row = QHBoxLayout()
        root_row.addWidget(self.root_edit)
        root_row.addWidget(browse)

        self.name_edit = QLineEdit(self.wcfg.get("commit_name", ""))
        self.name_edit.setPlaceholderText("예: OWL worker")
        self.email_edit = QLineEdit(self.wcfg.get("commit_email", ""))
        self.email_edit.setPlaceholderText("예: owl@example.com")
        load_id_btn = QPushButton("git 전역 설정 불러오기")
        load_id_btn.clicked.connect(lambda: self._load_git_identity(overwrite=True))
        id_row = QHBoxLayout()
        id_row.addWidget(self.name_edit, 1)
        id_row.addWidget(self.email_edit, 1)
        id_row.addWidget(load_id_btn)

        form = QFormLayout()
        form.addRow("받을 주소(이 PC의 LAN IP)", self.host_combo)
        form.addRow("포트", self.port_spin)
        form.addRow("토큰 (OWL_TOKEN)", self.token_edit)
        form.addRow("작업 폴더", root_row)
        form.addRow("커밋 작성자 (이름 / 메일)", id_row)
        settings = QGroupBox("워커 설정")
        settings.setLayout(form)

        self.tools_label = QLabel()
        self.tools_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.identity_label = QLabel()
        self.identity_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        recheck = QPushButton("다시 확인")
        recheck.clicked.connect(self._recheck)
        tools_box = QGroupBox("필요한 프로그램")
        info = QVBoxLayout()
        info.addWidget(self.tools_label)
        info.addWidget(self.identity_label)
        tl = QHBoxLayout()
        tl.addLayout(info, 1)
        tl.addWidget(recheck)
        tools_box.setLayout(tl)

        self.start_btn = QPushButton("워커 시작")
        self.start_btn.setMinimumHeight(34)
        self.start_btn.clicked.connect(self._toggle)
        self.state_label = QLabel("중지됨")
        logs_btn = QPushButton("로그 폴더 열기")
        logs_btn.clicked.connect(lambda: open_folder(logs_dir()))
        results_btn = QPushButton("결과 폴더 열기")
        results_btn.clicked.connect(lambda: open_folder(results_dir()))
        ctrl = QHBoxLayout()
        ctrl.addWidget(self.start_btn)
        ctrl.addWidget(self.state_label, 1)
        ctrl.addWidget(logs_btn)
        ctrl.addWidget(results_btn)

        self.table = make_table(["작업 ID", "상태", "단계", "접수 시각", "바뀐 파일", "비용(USD)"])
        self.log_view = make_log_view()
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(self.log_view)
        split.setSizes([260, 300])

        lay = QVBoxLayout()
        lay.addWidget(settings)
        lay.addWidget(tools_box)
        lay.addLayout(ctrl)
        lay.addWidget(split, 1)
        root = QWidget()
        root.setLayout(lay)
        self.setCentralWidget(root)
        self._show_tools()
        self._load_git_identity(overwrite=False)

    def _load_git_identity(self, overwrite: bool):
        """git 전역 설정의 이름/메일을 커밋 작성자 칸에 채운다.
        overwrite=False면 빈 칸만 채운다. 전역 설정은 바꾸지 않는다."""
        git = find_tool("git")
        if not git:
            self.identity_label.setText("git 사용자(전역 설정): git을 찾을 수 없음")
            return
        try:
            name, email = read_git_identity(git)
        except RuntimeError as e:
            self.logger.log("-", "error", step="git_identity", reason=str(e))
            self.identity_label.setText("git 사용자(전역 설정): 읽기 실패 — 로그 확인")
            return
        if name or email:
            self.identity_label.setText(f"git 사용자(전역 설정): {name or '이름 없음'} <{email or '메일 없음'}>")
        else:
            self.identity_label.setText(
                "git 사용자(전역 설정): 없음 — 위 '커밋 작성자'에 입력하면 OWL 커밋에만 쓰입니다")
        if overwrite or not self.name_edit.text().strip():
            if name:
                self.name_edit.setText(name)
        if overwrite or not self.email_edit.text().strip():
            if email:
                self.email_edit.setText(email)
        if overwrite and not (name or email):
            QMessageBox.information(self, "git 전역 설정 없음",
                                    "이 PC에는 git 사용자 이름/메일이 설정돼 있지 않습니다.\n"
                                    "'커밋 작성자' 칸에 직접 입력하세요. OWL이 만드는 커밋에만 쓰이고, "
                                    "git 전역 설정은 바뀌지 않습니다.")

    def _browse_root(self):
        path = QFileDialog.getExistingDirectory(self, "작업 폴더 선택", self.root_edit.text())
        if path:
            self.root_edit.setText(path)

    def _recheck(self):
        self._show_tools()
        self._load_git_identity(overwrite=False)

    def _show_tools(self):
        lines = []
        for name in ("claude", "git", "docker"):
            path = find_tool(name)
            need = "필수" if name in ("claude", "git") else "test_cmd에서 쓰면 필요"
            lines.append(f"{name}: {path if path else '찾을 수 없음'}  ({need})")
        self.tools_label.setText("\n".join(lines))

    def _toggle(self):
        if self.core and self.core.running:
            self.core.stop()
            self._set_running(False)
            return
        host = self.host_combo.currentText().strip()
        token = self.token_edit.text().strip()
        work_root = self.root_edit.text().strip()
        name = self.name_edit.text().strip()
        email = self.email_edit.text().strip()
        if not host or not token or not work_root:
            QMessageBox.warning(self, "입력 확인", "받을 주소, 토큰, 작업 폴더를 모두 채워 주세요.")
            return
        problem = validate_identity(name, email)
        if problem:
            QMessageBox.warning(self, "커밋 작성자 확인",
                                f"{problem}.\n\n결과 브랜치에 커밋할 때 쓰는 이름과 메일입니다. "
                                "'git 전역 설정 불러오기'를 누르거나 직접 입력하세요.")
            return
        self.wcfg.update(host=host, port=self.port_spin.value(), token=token,
                         work_root=work_root, commit_name=name, commit_email=email)
        self.cfg["role"] = "worker"
        safe_save_config(self.cfg, self.logger, self)
        self.core = WorkerCore(self.logger, work_root, token, commit_name=name,
                               commit_email=email, on_change=self.bridge.task_changed.emit)
        try:
            self.core.start(host, self.port_spin.value())
        except RuntimeError as e:
            QMessageBox.critical(self, "워커를 시작하지 못함", str(e))
            self._show_tools()
            return
        self._set_running(True)

    def _set_running(self, running: bool):
        self.start_btn.setText("워커 중지" if running else "워커 시작")
        for w in (self.host_combo, self.port_spin, self.token_edit, self.root_edit,
                  self.name_edit, self.email_edit):
            w.setEnabled(not running)
        if running:
            self.state_label.setText(
                f"작업 대기 중: {self.host_combo.currentText()}:{self.port_spin.value()}"
            )
        else:
            self.state_label.setText("중지됨 (실행 중이던 작업은 끝까지 진행)")

    def _refresh_task(self, task_id: str):
        if not self.core:
            return
        st = self.core.get(task_id)
        if not st:
            return
        res = st.get("result") or {}
        cost = res.get("cost_usd")
        set_row(self.table, task_id, [
            st.get("status"),
            st.get("step"),
            (st.get("received_at") or "")[11:19],
            len(res.get("changed_files") or []),
            "기록 없음" if cost is None else f"{cost:.4f}",
        ])

    def _append_log(self, rec: dict):
        self.log_view.appendPlainText(format_record(rec))

    def closeEvent(self, event):
        if self.core and self.core.running:
            self.core.stop()
        super().closeEvent(event)


# =================================================================== 디스패처 창
class DispatchWindow(QMainWindow):
    def __init__(self, cfg: dict):
        super().__init__()
        self.cfg = cfg
        self.dcfg = cfg.setdefault("dispatch", {})
        self.logger = JsonlLogger("desktop")
        self.bridge = Bridge()
        self.bridge.log_rec.connect(self._append_log)
        self.bridge.net_result.connect(self._on_net_result)
        self.logger.add_listener(self.bridge.log_rec.emit)
        self.tracked = {}

        self.setWindowTitle(f"OWL 디스패처 — {self.logger.host}  (v{APP_VERSION})")
        self.resize(1100, 820)

        self.url_edit = QLineEdit(self.dcfg.get("worker_url", "http://192.168.0.10:8765"))
        self.token_edit = QLineEdit(self.dcfg.get("token") or os.environ.get("OWL_TOKEN", ""))
        self.token_edit.setEchoMode(QLineEdit.Password)
        health_btn = QPushButton("연결 확인")
        health_btn.clicked.connect(self._health)
        url_row = QHBoxLayout()
        url_row.addWidget(self.url_edit, 1)
        url_row.addWidget(health_btn)
        conn_form = QFormLayout()
        conn_form.addRow("워커 주소", url_row)
        conn_form.addRow("토큰 (OWL_TOKEN)", self.token_edit)
        conn_box = QGroupBox("워커 연결")
        conn_box.setLayout(conn_form)

        self.id_edit = QLineEdit(self._new_id())
        new_id_btn = QPushButton("새 ID")
        new_id_btn.clicked.connect(lambda: self.id_edit.setText(self._new_id()))
        id_row = QHBoxLayout()
        id_row.addWidget(self.id_edit, 1)
        id_row.addWidget(new_id_btn)
        last = self.dcfg.get("last_task", {})
        self.repo_edit = QLineEdit(last.get("repo", ""))
        self.repo_edit.setPlaceholderText("미니PC가 clone/push할 수 있는 git 주소")
        self.branch_edit = QLineEdit(last.get("base_branch", "main"))
        self.instr_edit = QPlainTextEdit(last.get("instruction", ""))
        self.instr_edit.setPlaceholderText("Claude Code에 그대로 전달할 지시문")
        self.instr_edit.setMinimumHeight(90)
        self.test_edit = QLineEdit(last.get("test_cmd", ""))
        self.test_edit.setPlaceholderText("예: 지금 미니PC에서 쓰는 Docker 테스트 명령")
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(60, 86400)
        self.timeout_spin.setValue(int(last.get("timeout_sec", 1800)))
        self.timeout_spin.setSuffix(" 초")
        task_form = QFormLayout()
        task_form.addRow("작업 ID", id_row)
        task_form.addRow("저장소", self.repo_edit)
        task_form.addRow("기준 브랜치", self.branch_edit)
        task_form.addRow("지시문", self.instr_edit)
        task_form.addRow("테스트 명령", self.test_edit)
        task_form.addRow("제한 시간", self.timeout_spin)

        self.send_btn = QPushButton("작업 보내기")
        self.send_btn.setMinimumHeight(34)
        self.send_btn.clicked.connect(self._send)
        load_btn = QPushButton("작업 불러오기")
        load_btn.clicked.connect(self._load_task)
        save_btn = QPushButton("작업 저장")
        save_btn.clicked.connect(self._save_task)
        logs_btn = QPushButton("로그 폴더 열기")
        logs_btn.clicked.connect(lambda: open_folder(logs_dir()))
        btns = QHBoxLayout()
        btns.addWidget(self.send_btn)
        btns.addWidget(load_btn)
        btns.addWidget(save_btn)
        btns.addStretch(1)
        btns.addWidget(logs_btn)
        task_box = QGroupBox("보낼 작업")
        tb = QVBoxLayout()
        tb.addLayout(task_form)
        tb.addLayout(btns)
        task_box.setLayout(tb)

        self.table = make_table(["작업 ID", "상태", "단계", "판정", "사유", "보낸 시각"])
        self.table.itemSelectionChanged.connect(self._show_detail)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(QFont("Consolas", 9))
        self.log_view = make_log_view()
        lower = QSplitter(Qt.Horizontal)
        lower.addWidget(self.detail)
        lower.addWidget(self.log_view)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.table)
        split.addWidget(lower)
        split.setSizes([200, 300])

        lay = QVBoxLayout()
        lay.addWidget(conn_box)
        lay.addWidget(task_box)
        lay.addWidget(split, 1)
        root = QWidget()
        root.setLayout(lay)
        self.setCentralWidget(root)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_all)
        self.timer.start(POLL_INTERVAL_MS)

    # ------------------------------------------------------------ 입력/설정
    @staticmethod
    def _new_id() -> str:
        return f"t-{datetime.datetime.now():%m%d-%H%M%S}"

    def _conn(self):
        return self.url_edit.text().strip(), self.token_edit.text().strip()

    def _task_payload(self) -> dict:
        return {
            "task_id": self.id_edit.text().strip(),
            "repo": self.repo_edit.text().strip(),
            "base_branch": self.branch_edit.text().strip(),
            "instruction": self.instr_edit.toPlainText().strip(),
            "test_cmd": self.test_edit.text().strip(),
            "timeout_sec": self.timeout_spin.value(),
        }

    def _remember(self, payload=None):
        url, token = self._conn()
        self.dcfg.update(worker_url=url, token=token)
        if payload:
            self.dcfg["last_task"] = {k: v for k, v in payload.items() if k != "task_id"}
        self.cfg["role"] = "dispatch"
        safe_save_config(self.cfg, self.logger, self)

    def _load_task(self):
        path, _ = QFileDialog.getOpenFileName(self, "작업 불러오기", tasks_dir(), "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            QMessageBox.warning(self, "불러오기 실패", f"{path}\n{type(e).__name__}: {e}")
            return
        self.repo_edit.setText(str(data.get("repo", "")))
        self.branch_edit.setText(str(data.get("base_branch", "main")))
        self.instr_edit.setPlainText(str(data.get("instruction", "")))
        self.test_edit.setText(str(data.get("test_cmd", "")))
        if isinstance(data.get("timeout_sec"), int):
            self.timeout_spin.setValue(data["timeout_sec"])
        self.id_edit.setText(self._new_id())

    def _save_task(self):
        payload = self._task_payload()
        payload.pop("task_id")
        path, _ = QFileDialog.getSaveFileName(self, "작업 저장", tasks_dir(), "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        except OSError as e:
            QMessageBox.warning(self, "저장 실패", f"{path}\n{type(e).__name__}: {e}")

    # ------------------------------------------------------------ 네트워크 (백그라운드)
    def _bg(self, kind, task_id, method, path, body=None):
        url, token = self._conn()

        def work():
            try:
                code, obj = call(method, url, path, token, body)
                out = {"code": code, "body": obj}
            except DispatchError as e:
                out = {"error": str(e)}
            except Exception:  # 예상 못 한 오류도 화면과 로그로 넘긴다
                out = {"error": traceback.format_exc()}
            self.bridge.net_result.emit(kind, task_id, out)

        threading.Thread(target=work, daemon=True).start()

    def _health(self):
        url, token = self._conn()
        if not url or not token:
            QMessageBox.warning(self, "입력 확인", "워커 주소와 토큰을 채워 주세요.")
            return
        self._remember()
        self.statusBar().showMessage("연결 확인 중…")
        self._bg("health", "-", "GET", "/health")

    def _send(self):
        payload = self._task_payload()
        url, token = self._conn()
        missing = [k for k, v in payload.items() if v in ("", None)]
        if not url or not token:
            missing.append("워커 주소/토큰")
        if missing:
            QMessageBox.warning(self, "입력 확인", f"비어 있는 항목: {', '.join(missing)}")
            return
        if payload["task_id"] in self.tracked:
            QMessageBox.warning(self, "입력 확인", "이미 보낸 작업 ID입니다. '새 ID'를 누르세요.")
            return
        self._remember(payload)
        self.send_btn.setEnabled(False)
        self.statusBar().showMessage(f"{payload['task_id']} 보내는 중…")
        self._bg("send", payload["task_id"], "POST", "/tasks", payload)
        self._pending_payload = payload

    def _poll_all(self):
        now = time.monotonic()
        for task_id, t in self.tracked.items():
            if t["closed"] or t["polling"]:
                continue
            if now > t["sent_mono"] + t["timeout"] + RESULT_GRACE_SEC:
                self._close(task_id, "대기 초과")
                self.logger.log(task_id, "dispatch_error",
                                reason=f"결과 대기 시간 초과(timeout_sec + {RESULT_GRACE_SEC}초)")
                continue
            t["polling"] = True
            self._bg("poll", task_id, "GET", f"/tasks/{task_id}")

    def _on_net_result(self, kind: str, task_id: str, out: dict):
        if kind == "health":
            self._on_health(out)
        elif kind == "send":
            self._on_send(task_id, out)
        elif kind == "poll":
            self._on_poll(task_id, out)

    def _on_health(self, out):
        if "error" in out:
            self.logger.log("-", "dispatch_error", step="health", reason=out["error"])
            self.statusBar().showMessage("연결 실패", 8000)
            QMessageBox.warning(self, "연결 실패", out["error"])
            return
        code, body = out["code"], out["body"]
        if code != 200:
            reason = f"HTTP {code}: {body.get('error', body)}"
            self.logger.log("-", "dispatch_error", step="health", reason=reason)
            QMessageBox.warning(self, "연결 실패", reason)
            return
        tools = body.get("tools") or {}
        lines = [f"워커: {body.get('host')} (v{body.get('version')})"]
        lines += [f"{k}: {v or '찾을 수 없음'}" for k, v in tools.items()]
        self.statusBar().showMessage(f"연결됨: {body.get('host')}", 8000)
        QMessageBox.information(self, "연결됨", "\n".join(lines))

    def _on_send(self, task_id, out):
        self.send_btn.setEnabled(True)
        payload = getattr(self, "_pending_payload", {})
        if "error" in out:
            self.logger.log(task_id, "dispatch_error", step="send", reason=out["error"])
            self.statusBar().showMessage("보내기 실패", 8000)
            QMessageBox.warning(self, "보내기 실패", out["error"])
            return
        code, body = out["code"], out["body"]
        if code != 202:
            reason = f"HTTP {code}: {body.get('error', body)}"
            self.logger.log(task_id, "dispatch_error", step="send", reason=reason)
            QMessageBox.warning(self, "워커가 거절함", reason)
            return
        self.tracked[task_id] = {
            "sent_mono": time.monotonic(),
            "sent_at": datetime.datetime.now().strftime("%H:%M:%S"),
            "timeout": payload.get("timeout_sec", 1800),
            "polling": False,
            "closed": False,
            "fail_streak": 0,
            "state": None,
            "verdict": "",
            "reason": "",
        }
        self.logger.log(task_id, "dispatched",
                        reason=f"repo={payload.get('repo')} test_cmd={payload.get('test_cmd')}")
        self._update_row(task_id)
        self.statusBar().showMessage(f"{task_id} 접수됨", 5000)
        self.id_edit.setText(self._new_id())

    def _on_poll(self, task_id, out):
        t = self.tracked.get(task_id)
        if not t:
            return
        t["polling"] = False
        if t["closed"]:
            return
        if "error" in out:
            t["fail_streak"] += 1
            if t["fail_streak"] == 1:
                self.logger.log(task_id, "dispatch_error", step="poll", reason=out["error"])
            if t["fail_streak"] >= POLL_FAIL_LIMIT:
                self._close(task_id, "연결 끊김")
                self.logger.log(task_id, "dispatch_error", step="poll",
                                reason=f"{POLL_FAIL_LIMIT * POLL_INTERVAL_MS // 1000}초 동안 응답 없음 — 추적 중단")
            self._update_row(task_id)
            return
        t["fail_streak"] = 0
        code, body = out["code"], out["body"]
        if code != 200:
            reason = f"HTTP {code}: {body.get('error', body)}"
            self._close(task_id, "조회 실패")
            self.logger.log(task_id, "dispatch_error", step="poll", reason=reason)
            self._update_row(task_id)
            return
        t["state"] = body
        if body.get("status") in ("done", "failed"):
            self._finish(task_id, body)
        self._update_row(task_id)
        self._show_detail()

    def _finish(self, task_id, state):
        t = self.tracked[task_id]
        t["closed"] = True
        self.logger.log(task_id, "result_received", reason=f"status={state.get('status')} step={state.get('step')}")
        accepted, reason = judge(state)
        t["verdict"] = "accepted" if accepted else "rejected"
        t["reason"] = reason
        cost = (state.get("result") or {}).get("cost_usd")
        self.logger.log(task_id, t["verdict"], reason=reason, cost_usd=cost)
        path = os.path.join(results_dir(), f"{task_id}.json")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"state": state, "judgment": {"verdict": t["verdict"], "reason": reason}},
                          f, ensure_ascii=False, indent=2)
        except OSError as e:
            self.logger.log(task_id, "dispatch_error", step="save",
                            reason=f"결과 저장 실패: {path} ({type(e).__name__}: {e})")

    def _close(self, task_id, verdict):
        t = self.tracked[task_id]
        t["closed"] = True
        t["verdict"] = verdict

    # ------------------------------------------------------------ 화면
    def _update_row(self, task_id):
        t = self.tracked[task_id]
        st = t["state"] or {}
        status = st.get("status", "보냄")
        if t["fail_streak"]:
            status += f" (응답 없음 {t['fail_streak']}회)"
        set_row(self.table, task_id, [status, st.get("step", ""), t["verdict"], t["reason"], t["sent_at"]])

    def _show_detail(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        task_id = self.table.item(rows[0].row(), 0).text()
        t = self.tracked.get(task_id)
        if not t or not t["state"]:
            self.detail.setPlainText("아직 결과가 없습니다.")
            return
        self.detail.setPlainText(json.dumps(t["state"], ensure_ascii=False, indent=2))

    def _append_log(self, rec: dict):
        self.log_view.appendPlainText(format_record(rec))


# =================================================================== 역할 선택 / 시작
class RoleDialog(QDialog):
    def __init__(self, last_role):
        super().__init__()
        self.role = None
        self.setWindowTitle("OWL — 이 PC의 역할")
        lay = QVBoxLayout()
        lay.addWidget(QLabel("이 PC에서 무엇을 할지 고르세요. 다음에는 마지막 선택이 강조됩니다."))
        worker = QPushButton("워커 — 작업을 받아서 실행 (미니PC)")
        dispatch = QPushButton("디스패처 — 작업을 보내고 결과 확인 (메인 PC)")
        for btn, role in ((worker, "worker"), (dispatch, "dispatch")):
            btn.setMinimumHeight(44)
            btn.clicked.connect(lambda _=False, r=role: self._pick(r))
            if role == last_role:
                btn.setDefault(True)
                btn.setFocus()
            lay.addWidget(btn)
        self.setLayout(lay)

    def _pick(self, role):
        self.role = role
        self.accept()


def install_excepthook():
    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        path = write_internal_error("owl-gui", text)
        if QApplication.instance():
            QMessageBox.critical(None, "OWL 오류",
                                 f"예상하지 못한 오류가 발생했습니다.\n기록 파일: {path}\n\n"
                                 + "\n".join(text.strip().splitlines()[-4:]))
    sys.excepthook = hook


def parse_role(argv):
    if "--role" in argv:
        i = argv.index("--role")
        if i + 1 < len(argv) and argv[i + 1] in ("worker", "dispatch"):
            return argv[i + 1]
    return None


def main():
    install_excepthook()
    app = QApplication(sys.argv)
    cfg, err = load_config()
    if err:
        broken = config_path() + f".broken-{datetime.datetime.now():%Y%m%d%H%M%S}"
        os.replace(config_path(), broken)
        QMessageBox.warning(None, "설정 파일 오류", f"{err}\n\n기존 파일을 다음 이름으로 옮기고 새로 시작합니다.\n{broken}")
    role = parse_role(sys.argv)
    if role is None:
        dlg = RoleDialog(cfg.get("role"))
        if dlg.exec() != QDialog.Accepted or not dlg.role:
            return 0
        role = dlg.role
    window = WorkerWindow(cfg) if role == "worker" else DispatchWindow(cfg)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
