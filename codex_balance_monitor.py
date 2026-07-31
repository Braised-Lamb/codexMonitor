from __future__ import annotations

import json
import faulthandler
import logging
from logging.handlers import RotatingFileHandler
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, qInstallMessageHandler
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QFontMetrics,
    QIcon,
    QMouseEvent,
    QPainterPath,
    QPixmap,
    QRegion,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QScrollArea,
    QSizeGrip,
    QSlider,
    QStackedWidget,
    QSystemTrayIcon,
    QMenu,
    QVBoxLayout,
    QWidget,
)


POLL_INTERVAL_SECONDS = 60
ACTIVITY_POLL_INTERVAL_SECONDS = 1
SESSION_SCAN_INTERVAL_SECONDS = 2
SESSION_RECENT_SECONDS = 5 * 60
SESSION_SCAN_LIMIT = 80
SESSION_TAIL_BYTES = 256 * 1024
SNAPSHOT_SLOW_SECONDS = 12
LOW_BALANCE_THRESHOLD = 20.0
USAGE_PAGE = "https://chatgpt.com/codex/settings/usage"


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = application_dir()


def resource_path(relative_path):
    if getattr(sys, "frozen", False):
        if hasattr(sys, "_MEIPASS"):
            base_path = Path(sys._MEIPASS)
        else:
            base_path = Path(sys.executable).parent
    else:
        base_path = Path(__file__).parent
    return base_path / relative_path


def config_path(filename: str) -> Path:
    return APP_DIR / "config" / filename


SETTINGS_PATH = config_path("codex_balance_monitor_settings.json")
QUOTA_HISTORY_PATH = config_path("codex_balance_monitor_quota_history.json")
STARTUP_REG_NAME = "CodexBalanceMonitor"
CODEX_HOME = Path.home() / ".codex"
LOG_DIR = APP_DIR / "logs"
LOGGER = logging.getLogger("codex_monitor")
_NATIVE_CRASH_LOG_HANDLE = None


def _diagnostic_log_dir() -> Path:
    """Return a writable directory for Python and native crash diagnostics."""
    candidates = (
        LOG_DIR,
        CODEX_HOME / "codex-monitor" / "logs",
        Path.cwd() / "logs",
    )
    for directory in candidates:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            probe = directory / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return directory
        except OSError:
            continue
    return LOG_DIR


def _qt_message_handler(mode, context, message: str) -> None:
    mode_name = str(mode).split(".")[-1].lower()
    location = ""
    if context is not None:
        file_name = getattr(context, "file", None)
        line_number = getattr(context, "line", None)
        if file_name:
            location = f" ({file_name}:{line_number})"
    LOGGER.warning("Qt %s%s: %s", mode_name, location, message)


def _uncaught_exception_hook(exc_type, exc_value, exc_traceback) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    LOGGER.critical(
        "未捕获的主线程异常",
        exc_info=(exc_type, exc_value, exc_traceback),
    )


def _thread_exception_hook(args) -> None:
    LOGGER.critical(
        "未捕获的后台线程异常：%s",
        args.thread.name if args.thread else "unknown",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def _unraisable_exception_hook(args) -> None:
    LOGGER.error(
        "未处理的对象析构异常：%s",
        args.object,
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def setup_diagnostics() -> None:
    """Install file logging, Qt logging and faulthandler before QApplication starts."""
    global _NATIVE_CRASH_LOG_HANDLE
    directory = _diagnostic_log_dir()
    log_path = directory / "codex_monitor.log"
    native_log_path = directory / "codex_monitor_native.log"

    LOGGER.setLevel(logging.DEBUG)
    LOGGER.propagate = False
    if not LOGGER.handlers:
        try:
            handler = RotatingFileHandler(
                log_path,
                maxBytes=2 * 1024 * 1024,
                backupCount=3,
                encoding="utf-8",
            )
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
            )
        except OSError:
            handler = logging.StreamHandler()
        LOGGER.addHandler(handler)

    try:
        _NATIVE_CRASH_LOG_HANDLE = native_log_path.open(
            "a",
            encoding="utf-8",
            buffering=1,
        )
        faulthandler.enable(file=_NATIVE_CRASH_LOG_HANDLE, all_threads=True)
    except (OSError, RuntimeError) as exc:
        LOGGER.warning("无法启用 native 崩溃日志：%s", exc)

    sys.excepthook = _uncaught_exception_hook
    if hasattr(threading, "excepthook"):
        threading.excepthook = _thread_exception_hook
    if hasattr(sys, "unraisablehook"):
        sys.unraisablehook = _unraisable_exception_hook
    qInstallMessageHandler(_qt_message_handler)
    LOGGER.info("诊断日志已启用：%s", directory)
DEBUG_IPC_EVENTS = os.environ.get("CODEX_MONITOR_DEBUG_IPC", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DEBUG_ACCOUNT_EVENTS = os.environ.get("CODEX_MONITOR_DEBUG_ACCOUNT", "").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
DEFAULT_GEOMETRY = "520x430"
MIN_WINDOW_WIDTH = 430
MIN_WINDOW_HEIGHT = 300
WINDOW_CORNER_RADIUS = 12

BG_COLOR = "#17171c"
TITLE_BG = "#050506"
CARD_BG = "#2b2b31"
CARD_HEADER_BG = "#34343a"
TRACK_COLOR = "#3a3a40"
TEXT_COLOR = "#f1f1f4"
MUTED_TEXT_COLOR = "#b8bac6"
DIM_TEXT_COLOR = "#888b96"
ACCENT_COLOR = "#7b55d8"
GOOD_COLOR = "#49c86a"
WARN_COLOR = "#f0a23a"
BAD_COLOR = "#e86464"
BLUE_COLOR = "#2674d9"
PURPLE_COLOR = "#7b55d8"


class AppServerError(RuntimeError):
    pass


class CodexIpcActivityClient:
    """Optional listener for the VS Code / desktop Codex IPC router."""

    PIPE_NAME = r"\\.\pipe\codex-ipc"
    ACTIVE_METHODS = {
        "thread-stream-state-changed",
        "thread/status/changed",
        "turn/started",
        "thread/started",
        "thread-follower-start-turn",
    }
    IDLE_METHODS = {
        "turn/completed",
        "turn/aborted",
        "turn/cancelled",
        "turn/canceled",
        "turn/stopped",
        "turn/interrupted",
        "thread/closed",
        "thread/deleted",
        "thread/archived",
        "thread-follower-interrupt-turn",
    }

    def __init__(self) -> None:
        self.handle = None
        self.reader_thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.write_lock = threading.Lock()
        self.event_lock = threading.Lock()
        self.thread_events: dict[str, dict] = {}
        self.errors: deque[str] = deque(maxlen=5)
        self.connected = False
        self.client_id: str | None = None
        self.client_types: dict[str, str] = {}
        self._kernel32 = None

    def start(self) -> None:
        if os.name != "nt":
            return
        if self.reader_thread and self.reader_thread.is_alive():
            return
        self.stop_event.clear()
        self.reader_thread = threading.Thread(target=self._run, daemon=True)
        self.reader_thread.start()

    def close(self) -> None:
        self.stop_event.set()
        self.connected = False
        self.handle = None

    def snapshot(self) -> dict:
        with self.event_lock:
            items = [dict(thread) for thread in self.thread_events.values()]
            errors = list(self.errors)
        return {"data": items, "errors": errors, "source": "VS Code IPC"}

    def request_status(self) -> None:
        if not self.connected or self.handle is None:
            return
        self._send(
            {
                "type": "broadcast",
                "method": "thread-stream-following-status-requested",
                "version": 1,
                "params": {},
            }
        )

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                handle = self._connect_pipe()
                self.handle = handle
                self.connected = True
                self._send(
                    {
                        "type": "request",
                        "requestId": str(uuid.uuid4()),
                        "method": "initialize",
                        "params": {"clientType": "codex-monitor"},
                    }
                )
                self.request_status()
                while not self.stop_event.is_set():
                    message = self._read_frame(handle)
                    if not isinstance(message, dict):
                        continue
                    self._handle_ipc_message(message)
            except Exception as exc:
                with self.event_lock:
                    self.errors.append(str(exc))
                self.connected = False
                if self.handle is not None:
                    self._close_handle(self.handle)
                    self.handle = None
                time.sleep(3)

    def _handle_ipc_message(self, message: dict) -> None:
        if DEBUG_IPC_EVENTS:
            print(json.dumps(message, ensure_ascii=False, indent=2), flush=True)

        message_type = message.get("type")
        if message_type == "response" and message.get("method") == "initialize":
            result = message.get("result")
            if isinstance(result, dict):
                self.client_id = str(result.get("clientId") or "")
            return
        if message_type == "client-discovery-request":
            self._send(
                {
                    "type": "client-discovery-response",
                    "requestId": message.get("requestId"),
                    "response": {"canHandle": False},
                }
            )
            return
        if message_type != "broadcast":
            return
        if message.get("method") == "client-status-changed":
            params = message.get("params")
            if isinstance(params, dict):
                client_id = params.get("clientId")
                client_type = params.get("clientType")
                status = params.get("status")
                if client_id and status == "connected":
                    self.client_types[str(client_id)] = str(client_type or "codex")
                elif client_id and status == "disconnected":
                    self.client_types.pop(str(client_id), None)
            return
        self._handle_broadcast(message)

    def _handle_broadcast(self, message: dict) -> None:
        method = str(message.get("method") or "")
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}

        thread_id = find_first_value(
            params,
            (
                "threadId",
                "thread_id",
                "conversationId",
                "conversation_id",
                "sessionId",
                "id",
            ),
        )
        if not thread_id:
            return

        title = find_first_value(params, ("name", "title", "summary", "label"))
        status = find_first_value(
            params,
            (
                "status",
                "state",
                "streamState",
                "streamStatus",
                "turnStatus",
                "activityStatus",
                "phase",
            ),
        )
        thread: dict = {
            "id": str(thread_id),
            "name": str(title or "Chating"),
            "updatedAt": time.time(),
            "source": self._source_from_message(message),
            "_ipcMethod": method,
        }

        if method in self.IDLE_METHODS:
            thread["status"] = {"type": "idle"}
        elif method in self.ACTIVE_METHODS:
            thread["status"] = status or {"type": "active", "activeFlags": []}
        elif method == "thread-stream-following-changed":
            following = find_first_value(params, ("following", "isFollowing", "active"))
            if following is not True:
                return
            thread["status"] = status or {"type": "following"}
        else:
            return

        with self.event_lock:
            old = self.thread_events.get(str(thread_id), {})
            previous_status = old.get("status")
            old.update({key: value for key, value in thread.items() if key != "status"})
            if "status" in thread:
                default_following = (
                    method == "thread-stream-following-changed"
                    and thread.get("status") == {"type": "following"}
                    and previous_status is not None
                )
                old["status"] = previous_status if default_following else thread["status"]
            self.thread_events[str(thread_id)] = old

    def _source_from_message(self, message: dict) -> str:
        source_client_id = message.get("sourceClientId")
        if source_client_id:
            client_type = self.client_types.get(str(source_client_id))
            if client_type:
                return client_type
        return "codex-ipc"

    def _connect_pipe(self):
        import _winapi

        return _winapi.CreateFile(
            self.PIPE_NAME,
            _winapi.GENERIC_READ | _winapi.GENERIC_WRITE,
            0,
            0,
            _winapi.OPEN_EXISTING,
            0,
            0,
        )

    def _read_exact(self, handle, size: int) -> bytes:
        import _winapi

        chunks: list[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = _winapi.ReadFile(handle, remaining)[0]
            if not chunk:
                raise OSError("codex-ipc 已关闭")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_frame(self, handle) -> dict:
        header = self._read_exact(handle, 4)
        size = struct.unpack("<I", header)[0]
        if size <= 0 or size > 256 * 1024 * 1024:
            raise OSError(f"无效 IPC 帧长度：{size}")
        payload = self._read_exact(handle, size)
        return json.loads(payload.decode("utf-8"))

    def _send(self, message: dict) -> None:
        if self.handle is None:
            return
        payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        frame = struct.pack("<I", len(payload)) + payload
        self._write_all(self.handle, frame)

    def _write_all(self, handle, data: bytes) -> None:
        import _winapi

        with self.write_lock:
            offset = 0
            while offset < len(data):
                chunk = data[offset:]
                result = _winapi.WriteFile(handle, chunk)
                written = result[0] if isinstance(result, tuple) else result
                offset += written

    def _close_handle(self, handle) -> None:
        try:
            import _winapi

            _winapi.CloseHandle(handle)
        except Exception:
            pass


class CodexAppServerClient:
    """Minimal JSONL client for `codex app-server`."""

    def __init__(self) -> None:
        self.process: subprocess.Popen[str] | None = None
        self.pending: dict[int, queue.Queue[dict]] = {}
        self.pending_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.event_lock = threading.Lock()
        self.next_id = 1
        self.stderr_lines: deque[str] = deque(maxlen=30)
        self.server_requests: deque[str] = deque(maxlen=10)
        self.thread_events: dict[str, dict] = {}
        self.ipc_client = CodexIpcActivityClient()
        self.session_scan_cache: dict = {"data": [], "errors": []}
        self.session_scan_signature: tuple | None = None
        self.session_scan_checked_at = 0.0
        self.codex_path = ""
        self.codex_login_status = ""
        self.app_server_info: dict = {}

    @staticmethod
    def _wrap_codex_command(codex_path: str, args: list[str]) -> list[str]:
        if os.name == "nt" and codex_path.lower().endswith((".cmd", ".bat")):
            cmdline = subprocess.list2cmdline([codex_path, *args])
            return ["cmd.exe", "/d", "/s", "/c", cmdline]
        if os.name == "nt" and codex_path.lower().endswith(".ps1"):
            return [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                codex_path,
                *args,
            ]
        return [codex_path, *args]

    @staticmethod
    def _subprocess_env() -> dict[str, str]:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(CODEX_HOME)
        return env

    def _command(self) -> list[str]:
        codex_path = shutil.which("codex")
        if not codex_path:
            raise AppServerError(
                "未找到 codex 命令。请先安装 Codex CLI，并确认在终端中可以运行 codex。"
            )
        self.codex_path = codex_path
        return self._wrap_codex_command(codex_path, ["app-server"])

    def connection_diagnostics(self, command: list[str] | None = None) -> str:
        env = self._subprocess_env()
        writable, writable_error = self._check_codex_home_writable(CODEX_HOME)
        lines = [
            "连接诊断：",
            f"codex 路径：{self.codex_path or '未知'}",
            f"登录状态：{self.codex_login_status or '未读取'}",
            f"CODEX_HOME：{env.get('CODEX_HOME') or CODEX_HOME}",
            f"CODEX_HOME 可写：{'是' if writable else '否'}"
            + (f" ({writable_error})" if writable_error else ""),
            f"USERPROFILE：{env.get('USERPROFILE') or ''}",
            f"HOME：{env.get('HOME') or ''}",
        ]
        if self.app_server_info:
            lines.append(f"app-server codexHome：{self.app_server_info.get('codexHome')}")
        if command:
            lines.append("启动命令：" + subprocess.list2cmdline(command))
        if self.stderr_lines:
            lines.append("stderr：")
            lines.extend(list(self.stderr_lines)[-6:])
        if self.server_requests:
            lines.append("app-server 请求：" + self.recent_server_request_text())
        return "\n".join(lines)

    @staticmethod
    def _check_codex_home_writable(codex_home: Path) -> tuple[bool, str]:
        probe_dir = codex_home / "tmp" / "codex-monitor"
        probe_path = probe_dir / f"write-test-{os.getpid()}.tmp"
        try:
            probe_dir.mkdir(parents=True, exist_ok=True)
            probe_path.write_text("ok", encoding="utf-8")
            probe_path.unlink(missing_ok=True)
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return

        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            creationflags = subprocess.CREATE_NO_WINDOW

        self.stderr_lines.clear()
        command = self._command()
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            startupinfo=startupinfo,
            creationflags=creationflags,
            env=self._subprocess_env(),
        )

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

        try:
            self.app_server_info = self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex_balance_monitor",
                        "title": "Codex Balance Monitor",
                        "version": "0.2.0",
                    },
                    "capabilities": {
                        "experimentalApi": True,
                        "mcpServerOpenaiFormElicitation": True,
                        "requestAttestation": False,
                    },
                },
                timeout=15,
            )
        except Exception as exc:
            details = self.connection_diagnostics(command)
            message = str(exc)
            if "failed to initialize sqlite state runtime" in message.lower():
                message += (
                    "\n\napp-server 无法初始化 Codex 状态库。"
                    "这通常是当前进程对 CODEX_HOME 没有写权限，"
                    "请在普通 PowerShell / VS Code 终端里直接运行监控工具，"
                    "不要从 Codex 沙盒命令里启动。"
                )
            raise AppServerError(f"{message}\n\n{details}") from exc
        self.notify("initialized", {})

    def _read_stdout(self) -> None:
        assert self.process and self.process.stdout
        for raw_line in self.process.stdout:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue

            message_id = message.get("id")
            if isinstance(message_id, int):
                with self.pending_lock:
                    response_queue = self.pending.get(message_id)
                if response_queue:
                    response_queue.put(message)
                elif message.get("method"):
                    self._handle_server_request(message)
            else:
                self._handle_notification(message)
        LOGGER.warning(
            "Codex app-server stdout reader ended, returncode=%s",
            self.process.returncode if self.process else None,
        )

    def _handle_notification(self, message: dict) -> None:
        method = message.get("method")
        params = message.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            return

        thread_id = params.get("threadId") or params.get("thread_id")
        if not thread_id and isinstance(params.get("thread"), dict):
            thread_id = params["thread"].get("id")
        if not thread_id:
            return

        thread: dict = {
            "id": str(thread_id),
            "name": params.get("name") or params.get("title") or "Chating",
            "updatedAt": time.time(),
            "source": "通知",
        }
        if isinstance(params.get("thread"), dict):
            thread.update(params["thread"])

        if method == "thread/status/changed":
            thread["status"] = params.get("status") or params.get("state")
        elif method in ("turn/started", "thread/started"):
            thread["status"] = {"type": "active", "activeFlags": []}
        elif method in (
            "turn/completed",
            "turn/aborted",
            "turn/cancelled",
            "turn/canceled",
            "turn/stopped",
            "turn/interrupted",
            "thread-follower-interrupt-turn",
        ):
            turn = params.get("turn")
            status = turn.get("status") if isinstance(turn, dict) else params.get("status")
            thread["status"] = (
                {"type": "idle"}
                if status in (
                    None,
                    "",
                    "completed",
                    "aborted",
                    "cancelled",
                    "canceled",
                    "stopped",
                    "interrupted",
                )
                else {"type": "systemError" if status == "failed" else str(status)}
            )
        elif method in ("thread/closed", "thread/deleted", "thread/archived"):
            thread["status"] = {"type": "idle"}
        else:
            return

        with self.event_lock:
            self.thread_events[str(thread_id)] = thread

    def _handle_server_request(self, message: dict) -> None:
        request_id = message.get("id")
        method = str(message.get("method") or "")
        params = message.get("params")
        self.server_requests.append(method)
        if DEBUG_ACCOUNT_EVENTS:
            print("server request", json.dumps(message, ensure_ascii=False), flush=True)

        url = find_first_value(params, ("url", "authUrl", "authorizationUrl", "loginUrl"))
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            opened = webbrowser.open(url)
            self._send({"id": request_id, "result": {"opened": bool(opened)}})
            return

        self._send(
            {
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": f"Codex Monitor 暂不支持 app-server 请求：{method}",
                },
            }
        )

    def recent_server_request_text(self) -> str:
        if not self.server_requests:
            return ""
        return ", ".join(dict.fromkeys(self.server_requests))

    def _read_stderr(self) -> None:
        assert self.process and self.process.stderr
        for raw_line in self.process.stderr:
            line = raw_line.rstrip()
            if line:
                self.stderr_lines.append(line)

    def _send(self, message: dict) -> None:
        if not self.process or self.process.poll() is not None or not self.process.stdin:
            code = self.process.returncode if self.process else "未启动"
            details = "\n".join(self.stderr_lines)
            raise AppServerError(
                f"Codex app-server 已退出，退出码：{code}"
                + (f"\n\n{details}" if details else "")
            )

        payload = json.dumps(message, ensure_ascii=False) + "\n"
        with self.write_lock:
            self.process.stdin.write(payload)
            self.process.stdin.flush()

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"method": method, "params": params or {}})

    def request(
        self, method: str, params: dict | None = None, timeout: float = 15
    ) -> dict:
        with self.pending_lock:
            request_id = self.next_id
            self.next_id += 1
            response_queue: queue.Queue[dict] = queue.Queue(maxsize=1)
            self.pending[request_id] = response_queue

        message: dict = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params

        try:
            self._send(message)
            try:
                response = response_queue.get(timeout=timeout)
            except queue.Empty as exc:
                details = "\n".join(self.stderr_lines)
                raise AppServerError(
                    f"请求 {method} 超时。" + (f"\n\n{details}" if details else "")
                ) from exc
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

        if "error" in response:
            error = response["error"]
            raise AppServerError(f"{method} 返回错误：{error.get('message', error)}")
        return response.get("result", {})

    def read_account(self, refresh_token: bool = False) -> dict:
        if DEBUG_ACCOUNT_EVENTS:
            print(f"reading account refreshToken={refresh_token}", flush=True)
        account_result = self.request(
            "account/read",
            {"refreshToken": refresh_token},
            timeout=20 if refresh_token else 15,
        )
        if DEBUG_ACCOUNT_EVENTS:
            safe_result = dict(account_result)
            if safe_result.get("account"):
                safe_result["account"] = "<account>"
            print(json.dumps(safe_result, ensure_ascii=False, indent=2), flush=True)
        if not account_result.get("account") and account_result.get("requiresOpenaiAuth"):
            path_text = self.codex_path or "未知 codex 路径"
            status_text = self.codex_login_status or "未读取登录状态"
            codex_home_text = self.app_server_info.get("codexHome") or "未知"
            request_text = self.recent_server_request_text()
            raise AppServerError(
                (
                    "Codex 账号需要重新登录。请在终端运行 codex login，完成后回到监控工具点击刷新。"
                    f"\n\n工具使用的 codex：{path_text}"
                    f"\n登录状态：{status_text}"
                    f"\napp-server codexHome：{codex_home_text}"
                    + (f"\napp-server 请求：{request_text}" if request_text else "")
                )
            )
        return account_result

    def read_rate_limits(self) -> dict:
        try:
            return self.request("account/rateLimits/read", {}, timeout=20)
        except AppServerError as exc:
            message = str(exc).lower()
            if "authentication required" not in message and "auth" not in message:
                raise
            self.read_account(refresh_token=True)
            return self.request("account/rateLimits/read", {}, timeout=20)

    def read_activity_threads(self) -> dict:
        self.start()
        self.ipc_client.start()
        self.ipc_client.request_status()
        thread_sources = []
        session_scan = self.read_session_scan()
        if session_scan.get("data") or session_scan.get("errors"):
            thread_sources.append(
                {
                    "source": "本地 session",
                    "error": "; ".join(session_scan.get("errors") or []),
                    "result": {"data": session_scan.get("data") or []},
                }
            )
        try:
            thread_sources.append(
                {
                    "source": "当前会话",
                    "result": self.request("thread/loaded/list", {}, timeout=3),
                }
            )
        except Exception as exc:
            thread_sources.append(
                {"source": "当前会话", "error": str(exc), "result": {"data": []}}
            )

        for source_label, state_only in (
            ("客户端", True),
            ("网页/VS Code/插件", False),
        ):
            params = {
                "limit": 20,
                "sortKey": "updated_at",
                "sortDirection": "desc",
                "archived": False,
                "useStateDbOnly": state_only,
            }
            try:
                thread_sources.append(
                    {
                        "source": source_label,
                        "result": self.request("thread/list", params, timeout=4),
                    }
                )
            except Exception as exc:
                thread_sources.append(
                    {"source": source_label, "error": str(exc), "result": {"data": []}}
                )

        with self.event_lock:
            event_threads = list(self.thread_events.values())
        if event_threads:
            thread_sources.append(
                {"source": "实时通知", "result": {"data": event_threads}}
            )
        ipc_snapshot = self.ipc_client.snapshot()
        if ipc_snapshot.get("data") or ipc_snapshot.get("errors"):
            thread_sources.append(
                {
                    "source": "VS Code IPC",
                    "error": "; ".join(ipc_snapshot.get("errors") or []),
                    "result": {"data": ipc_snapshot.get("data") or []},
                }
            )
        return merge_thread_results(thread_sources)

    def read_realtime_activity_threads(self, base_result: dict | None = None) -> dict:
        self.ipc_client.start()
        self.ipc_client.request_status()
        time.sleep(0.12)
        thread_sources = []
        if isinstance(base_result, dict):
            thread_sources.append({"source": "已知对话", "result": base_result})

        session_scan = self.read_session_scan()
        if session_scan.get("data") or session_scan.get("errors"):
            thread_sources.append(
                {
                    "source": "本地 session",
                    "error": "; ".join(session_scan.get("errors") or []),
                    "result": {"data": session_scan.get("data") or []},
                }
            )

        with self.event_lock:
            event_threads = list(self.thread_events.values())
        if event_threads:
            thread_sources.append(
                {"source": "实时通知", "result": {"data": event_threads}}
            )

        ipc_snapshot = self.ipc_client.snapshot()
        if ipc_snapshot.get("data") or ipc_snapshot.get("errors"):
            thread_sources.append(
                {
                    "source": "Codex IPC",
                    "error": "; ".join(ipc_snapshot.get("errors") or []),
                    "result": {"data": ipc_snapshot.get("data") or []},
                }
            )
        return merge_thread_results(thread_sources)

    def read_passive_activity_threads(self, base_result: dict | None = None) -> dict:
        self.ipc_client.start()
        self.ipc_client.request_status()
        thread_sources = []
        if isinstance(base_result, dict):
            thread_sources.append({"source": "已知对话", "result": base_result})

        session_scan = self.read_session_scan()
        if session_scan.get("data") or session_scan.get("errors"):
            thread_sources.append(
                {
                    "source": "本地 session",
                    "error": "; ".join(session_scan.get("errors") or []),
                    "result": {"data": session_scan.get("data") or []},
                }
            )

        ipc_snapshot = self.ipc_client.snapshot()
        if ipc_snapshot.get("data") or ipc_snapshot.get("errors"):
            thread_sources.append(
                {
                    "source": "Codex IPC",
                    "error": "; ".join(ipc_snapshot.get("errors") or []),
                    "result": {"data": ipc_snapshot.get("data") or []},
                }
            )
        return merge_thread_results(thread_sources)

    def read_session_scan(self, force: bool = False) -> dict:
        now = time.time()
        if (
            not force
            and now - self.session_scan_checked_at < SESSION_SCAN_INTERVAL_SECONDS
            and isinstance(self.session_scan_cache, dict)
        ):
            return self.session_scan_cache

        files, signature = session_file_inventory()
        self.session_scan_checked_at = now
        if (
            not force
            and signature == self.session_scan_signature
            and isinstance(self.session_scan_cache, dict)
        ):
            return self.session_scan_cache

        self.session_scan_cache = scan_local_sessions(files=files)
        self.session_scan_signature = signature
        return self.session_scan_cache

    def get_snapshot(self) -> tuple[dict, dict, dict]:
        self.start()
        account = self.read_account(refresh_token=False)
        limits = self.read_rate_limits()
        threads = {"data": [], "errors": [], "sourceCount": 0}
        return account, limits, threads

    def restart_app_server(self) -> None:
        process = self.process
        self.process = None
        with self.pending_lock:
            self.pending.clear()
        with self.event_lock:
            self.thread_events.clear()
        self.stderr_lines.clear()
        if process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                process.kill()
        self.start()

    def close(self) -> None:
        self.ipc_client.close()
        process = self.process
        self.process = None
        if not process or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            process.kill()


class SnapshotSignals(QObject):
    loaded = Signal(dict, dict, dict)
    activity_loaded = Signal(dict)
    failed = Signal(str)


def load_settings() -> dict:
    try:
        with SETTINGS_PATH.open("r", encoding="utf-8") as handle:
            settings = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    return settings if isinstance(settings, dict) else {}


def save_settings(settings: dict) -> None:
    try:
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SETTINGS_PATH.open("w", encoding="utf-8") as handle:
            json.dump(settings, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


def load_quota_history() -> dict:
    try:
        with QUOTA_HISTORY_PATH.open("r", encoding="utf-8") as handle:
            history = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "snapshots": []}
    if not isinstance(history, dict):
        return {"version": 1, "snapshots": []}
    snapshots = history.get("snapshots")
    if not isinstance(snapshots, list):
        history["snapshots"] = []
    return history


def save_quota_history(history: dict) -> None:
    try:
        QUOTA_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with QUOTA_HISTORY_PATH.open("w", encoding="utf-8") as handle:
            json.dump(history, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return subprocess.list2cmdline([str(Path(sys.executable).resolve())])
    return subprocess.list2cmdline(
        [str(Path(sys.executable).resolve()), str(Path(__file__).resolve())]
    )


def is_startup_enabled() -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_READ,
        ) as key:
            value, _kind = winreg.QueryValueEx(key, STARTUP_REG_NAME)
    except OSError:
        return False
    return str(value or "") == startup_command()


def set_startup_enabled(enabled: bool) -> None:
    if os.name != "nt":
        raise OSError("当前系统不支持 Windows 开机自启注册表项。")
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        r"Software\Microsoft\Windows\CurrentVersion\Run",
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        if enabled:
            winreg.SetValueEx(key, STARTUP_REG_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, STARTUP_REG_NAME)
            except FileNotFoundError:
                pass


def coerce_opacity(value: object) -> float:
    try:
        opacity = float(value)
    except (TypeError, ValueError):
        return 1.0
    if opacity > 1:
        opacity /= 100
    return max(0.0, min(1.0, opacity))


def rgba(hex_color: str, opacity: float) -> str:
    text = hex_color.strip().lstrip("#")
    if len(text) != 6:
        return hex_color
    red = int(text[0:2], 16)
    green = int(text[2:4], 16)
    blue = int(text[4:6], 16)
    alpha = max(0.0, min(1.0, opacity))
    return f"rgba({red}, {green}, {blue}, {alpha:.3f})"


def parse_geometry(geometry: object) -> tuple[int, int, int | None, int | None]:
    text = str(geometry or DEFAULT_GEOMETRY)
    match = re.match(r"^(\d+)x(\d+)(?:\+(-?\d+)\+(-?\d+))?$", text)
    if not match:
        return 520, 430, None, None
    width = int(match.group(1))
    height = int(match.group(2))
    x = int(match.group(3)) if match.group(3) is not None else None
    y = int(match.group(4)) if match.group(4) is not None else None
    return width, height, x, y


def format_time_value(value: int | float | str | None) -> str:
    if value is None or value == "":
        return "未知"
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return "未知"
        try:
            value = float(text)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone()
                return parsed.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                return text

    timestamp = float(value)
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def format_until(timestamp: object) -> str:
    if timestamp in (None, ""):
        return "未知"
    try:
        raw = float(timestamp)
    except (TypeError, ValueError):
        return format_time_value(timestamp)
    if raw > 10_000_000_000:
        raw /= 1000
    seconds = int(raw - time.time())
    if seconds <= 0:
        return "已到期"
    minutes = max(1, seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d"


def format_age(timestamp: object) -> str:
    if timestamp in (None, ""):
        return "未知"
    try:
        raw = float(timestamp)
    except (TypeError, ValueError):
        return format_time_value(timestamp)
    if raw > 10_000_000_000:
        raw /= 1000
    seconds = max(0, int(time.time() - raw))
    if seconds < 60:
        return "刚刚"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m前"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h前"
    days = hours // 24
    return f"{days}d前"


def format_window_name(minutes: int | float | None, index: int) -> str:
    if minutes == 300:
        return "Session"
    if minutes == 10080:
        return "Weekly"
    return "Session" if index == 0 else "Weekly"


def find_first_value(source: object, keys: tuple[str, ...]) -> object | None:
    if isinstance(source, dict):
        for key in keys:
            if key in source and source[key] not in (None, ""):
                return source[key]
        values = source.values()
    elif isinstance(source, list):
        values = source
    else:
        return None

    for value in values:
        if isinstance(value, (dict, list)):
            found = find_first_value(value, keys)
            if found not in (None, ""):
                return found
    return None


def session_id_from_path(path: Path) -> str:
    match = re.search(
        r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})",
        path.stem,
    )
    return match.group(1) if match else path.stem


def session_source_from_meta(meta: dict) -> str:
    originator = str(meta.get("originator") or meta.get("thread_source") or "").lower()
    source = str(meta.get("source") or meta.get("client") or "").lower()
    combined = f"{originator} {source}"
    if "desktop" in originator or "codex desktop" in combined:
        return "codex-client-ipc"
    if "codex_vscode" in originator or "vscode" in originator or "vs code" in originator:
        return "vscode"
    if "plugin" in combined or "extension" in combined:
        return "plugin"
    if "web" in combined or "browser" in combined or "chatgpt" in combined:
        return "web"
    if "desktop" in source:
        return "codex-client-ipc"
    if "vscode" in source or "vs code" in source:
        return "vscode"
    if source:
        return source
    return "session-file"


def read_session_index() -> dict[str, dict]:
    index_path = CODEX_HOME / "session_index.jsonl"
    sessions: dict[str, dict] = {}
    try:
        with index_path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                session_id = item.get("id") or item.get("thread_id") or item.get("threadId")
                if not session_id:
                    continue
                sessions[str(session_id)] = item
    except OSError:
        pass
    return sessions


def read_session_meta(path: Path) -> dict:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(12):
                line = handle.readline()
                if not line:
                    break
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(item, dict):
                    continue
                payload = item.get("payload")
                if item.get("type") == "session_meta" and isinstance(payload, dict):
                    return payload
    except OSError:
        pass
    return {}


def read_session_tail_items(path: Path, max_items: int = 160) -> list[dict]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > SESSION_TAIL_BYTES:
                handle.seek(size - SESSION_TAIL_BYTES)
                handle.readline()
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    items: list[dict] = []
    for line in data.splitlines()[-max_items:]:
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            items.append(item)
    return items


SESSION_TERMINAL_EVENTS = {
    "task_complete",
    "task_completed",
    "turn_completed",
    "turn_complete",
    "turn_aborted",
    "turn_abort",
    "turn_cancelled",
    "turn_canceled",
    "turn_stopped",
    "turn_interrupted",
    "interrupt",
    "interrupted",
    "cancelled",
    "canceled",
    "aborted",
    "stopped",
}


def is_terminal_session_item(item_type: object, payload_type: object, payload: object) -> bool:
    labels = {str(item_type or "").lower(), str(payload_type or "").lower()}
    if isinstance(payload, dict):
        for key in ("status", "state", "reason", "result", "outcome"):
            value = payload.get(key)
            if value not in (None, ""):
                labels.add(str(value).lower())
    return any(label in SESSION_TERMINAL_EVENTS for label in labels)


def function_call_arguments(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    arguments = payload.get("arguments")
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def approval_text_from_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    arguments = function_call_arguments(payload)
    reason = arguments.get("justification") or arguments.get("reason")
    command = arguments.get("command") or arguments.get("cmd")
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    if reason and command:
        return clean_thread_text(f"{reason} · {command}")
    return clean_thread_text(reason or command or payload.get("name") or "等待手动批准")


def is_approval_session_item(item_type: object, payload_type: object, payload: object) -> bool:
    labels = {str(item_type or "").lower(), str(payload_type or "").lower()}
    if isinstance(payload, dict):
        labels.add(str(payload.get("name") or "").lower())
        arguments = function_call_arguments(payload)
        text = json.dumps(arguments, ensure_ascii=False).lower()
        if arguments.get("sandbox_permissions") == "require_escalated":
            return True
        if arguments.get("require_escalated") is True:
            return True
        if "require_escalated" in text or "waitingonapproval" in text:
            return True
        for key in ("status", "state", "reason", "message"):
            value = payload.get(key)
            if value not in (None, ""):
                labels.add(str(value).lower())
    return any(
        marker in label
        for label in labels
        for marker in (
            "approval_request",
            "approval-request",
            "permission_request",
            "permission-request",
            "waitingonapproval",
            "needs_approval",
            "manual_approval",
        )
    )


def session_text_from_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message")
    if isinstance(message, str):
        return clean_thread_text(message)

    content = payload.get("content")
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
    elif isinstance(content, str):
        parts.append(content)
    return clean_thread_text(" ".join(parts))


def session_activity_from_tail(path: Path, is_recent: bool) -> dict:
    items = read_session_tail_items(path)
    if not items:
        return {
            "status": {"type": "active"} if is_recent else {"type": "idle"},
            "preview": "",
            "kind": "recent" if is_recent else "idle",
        }

    preview = ""
    kind = "recent" if is_recent else "idle"
    status: dict | str = {"type": "active"} if is_recent else {"type": "idle"}

    for item in reversed(items):
        payload = item.get("payload")
        payload_type = payload.get("type") if isinstance(payload, dict) else None
        item_type = item.get("type")
        if item_type == "event_msg" and payload_type == "agent_message":
            preview = session_text_from_payload(payload)
            if preview:
                break
        if item_type == "response_item" and payload_type == "message":
            role = payload.get("role") if isinstance(payload, dict) else ""
            if role == "assistant":
                preview = session_text_from_payload(payload)
                if preview:
                    break

    for item in reversed(items):
        payload = item.get("payload")
        payload_type = payload.get("type") if isinstance(payload, dict) else None
        item_type = item.get("type")

        if is_terminal_session_item(item_type, payload_type, payload):
            kind = "idle"
            status = {"type": "idle"}
            break
        if is_approval_session_item(item_type, payload_type, payload):
            kind = "waiting"
            status = {"type": "active", "activeFlags": ["waitingOnApproval"]}
            approval_preview = approval_text_from_payload(payload)
            if approval_preview:
                preview = approval_preview
            break
        if item_type == "response_item" and payload_type == "reasoning":
            kind = "thinking"
            status = {"type": "thinking"}
            break
        if item_type == "response_item" and payload_type == "message":
            role = payload.get("role") if isinstance(payload, dict) else ""
            if role == "assistant":
                kind = "outputting"
                status = {"type": "outputting"}
                break
        if item_type == "event_msg" and payload_type == "agent_message":
            kind = "outputting"
            status = {"type": "outputting"}
            break
        if item_type == "response_item" and payload_type in (
            "function_call",
            "custom_tool_call",
        ):
            kind = "working"
            status = {"type": "working"}
            break
        if item_type == "event_msg" and payload_type in (
            "patch_apply_begin",
            "exec_command_begin",
            "tool_call_begin",
        ):
            kind = "working"
            status = {"type": "working"}
            break
        if item_type == "response_item" and payload_type in (
            "function_call_output",
            "custom_tool_call_output",
        ):
            kind = "working"
            status = {"type": "working"}
            break
        if item_type == "event_msg" and payload_type in ("task_started", "user_message"):
            kind = "thinking"
            status = {"type": "thinking"}
            break

    if not is_recent and kind not in ("waiting",):
        status = {"type": "idle"}
        kind = "idle"

    return {"status": status, "preview": preview, "kind": kind}


def session_file_inventory() -> tuple[list[Path], tuple]:
    """List session files and return a cheap change fingerprint.

    The fingerprint only stats files. The expensive tail parsing is performed
    by scan_local_sessions only when this fingerprint changes.
    """
    sessions_dir = CODEX_HOME / "sessions"
    if not sessions_dir.exists():
        return [], (("missing", str(sessions_dir)),)

    entries: list[tuple[Path, int, int]] = []
    try:
        for path in sessions_dir.rglob("*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            entries.append((path, int(stat.st_mtime_ns), int(stat.st_size)))
    except OSError as exc:
        return [], (("error", str(exc)),)

    entries.sort(key=lambda item: item[1], reverse=True)
    files = [item[0] for item in entries]
    signature = tuple((str(path), mtime_ns, size) for path, mtime_ns, size in entries)
    return files, signature


def scan_local_sessions(
    limit: int = SESSION_SCAN_LIMIT,
    files: list[Path] | None = None,
) -> dict:
    sessions_dir = CODEX_HOME / "sessions"
    if not sessions_dir.exists():
        return {"data": [], "errors": [f"未找到本地 sessions：{sessions_dir}"]}

    errors: list[str] = []
    if files is None:
        files, _signature = session_file_inventory()
    files = files[:limit]

    index = read_session_index()
    now = time.time()
    threads: list[dict] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError as exc:
            errors.append(str(exc))
            continue

        meta = read_session_meta(path)
        session_id = str(
            meta.get("id")
            or meta.get("session_id")
            or meta.get("thread_id")
            or meta.get("threadId")
            or session_id_from_path(path)
        )
        index_item = index.get(session_id, {})
        title = (
            index_item.get("thread_name")
            or index_item.get("title")
            or meta.get("thread_name")
            or meta.get("title")
            or meta.get("name")
            or "Chating"
        )
        age = max(0.0, now - stat.st_mtime)
        is_recent = age <= SESSION_RECENT_SECONDS
        activity = session_activity_from_tail(path, is_recent)
        threads.append(
            {
                "id": session_id,
                "name": str(title),
                "cwd": meta.get("cwd") or index_item.get("cwd"),
                "updatedAt": stat.st_mtime,
                "source": session_source_from_meta(meta),
                "status": activity.get("status"),
                "preview": activity.get("preview") or "",
                "_activityKind": activity.get("kind") or "",
                "_sessionPath": str(path),
                "_sessionInferredActive": is_recent,
                "_rawSource": meta.get("source"),
                "_originator": meta.get("originator"),
            }
        )

    return {"data": threads, "errors": errors}


def percent_color(percent: float) -> str:
    if percent >= 90:
        return BAD_COLOR
    if percent >= 70:
        return WARN_COLOR
    return GOOD_COLOR


def plan_badge_color(plan: object) -> str:
    plan_lower = str(plan or "").lower()
    if "pro" in plan_lower or "team" in plan_lower:
        return BLUE_COLOR
    if "max" in plan_lower or "enterprise" in plan_lower:
        return PURPLE_COLOR
    return "#555560"


def credits_text(credits: object) -> str:
    if not isinstance(credits, dict):
        return "未知"
    if credits.get("unlimited"):
        return "无限额度"
    balance = credits.get("balance", "0")
    return f"余额 {balance}" if credits.get("hasCredits") else f"无可用 · {balance}"


def limit_status(status: object) -> str:
    labels = {
        "rate_limit_reached": "已触发额度限制",
        "workspace_owner_credits_depleted": "工作区 owner credits 已耗尽",
        "workspace_member_credits_depleted": "工作区 member credits 已耗尽",
        "workspace_owner_usage_limit_reached": "工作区 owner 用量限制",
        "workspace_member_usage_limit_reached": "工作区 member 用量限制",
    }
    if status in (None, ""):
        return "正常"
    return labels.get(str(status), str(status))


def bucket_status(bucket: dict) -> tuple[str, str]:
    reached = bucket.get("rateLimitReachedType")
    if reached:
        return limit_status(reached), ACCENT_COLOR
    spend_control = bucket.get("spendControlReached")
    if spend_control is True:
        return "花费控制已触发", ACCENT_COLOR
    if spend_control is False:
        return "正常", GOOD_COLOR
    return "未返回限制状态", DIM_TEXT_COLOR


def snapshot_status(limits_result: dict) -> tuple[str, str]:
    statuses: list[tuple[str, str]] = []
    for bucket in iter_buckets(limits_result).values():
        if isinstance(bucket, dict):
            statuses.append(bucket_status(bucket))
    for label, color in statuses:
        if color == ACCENT_COLOR:
            return label, color
    for label, color in statuses:
        if color == GOOD_COLOR:
            return label, color
    return "状态未知", DIM_TEXT_COLOR


def iter_buckets(limits_result: dict) -> dict:
    buckets = limits_result.get("rateLimitsByLimitId")
    if buckets:
        return buckets
    single = limits_result.get("rateLimits")
    if isinstance(single, dict):
        return {single.get("limitId", "codex"): single}
    return {}


def coerce_number(value: object) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


def usage_period_from_text(text: str) -> str | None:
    lower = text.lower()
    if any(marker in lower for marker in ("monthly", "month", "30d", "30_d", "30 天", "月", "本月")):
        return "month"
    if any(marker in lower for marker in ("weekly", "week", "7d", "7_d", "周", "本周")):
        return "week"
    if any(marker in lower for marker in ("daily", "today", "24h", "24_h", "day", "日", "今天")):
        return "day"
    return None


def usage_period_from_dict(data: dict, path: tuple[str, ...]) -> str | None:
    text_parts = list(path)
    for key in ("period", "window", "range", "duration", "name", "title", "label"):
        value = data.get(key)
        if value not in (None, ""):
            text_parts.append(str(value))
    period = usage_period_from_text(" ".join(text_parts))
    if period:
        return period

    minutes = coerce_number(data.get("windowDurationMins") or data.get("durationMins"))
    if minutes is None:
        return None
    if 1200 <= minutes <= 1800:
        return "day"
    if 9000 <= minutes <= 11000:
        return "week"
    if 40000 <= minutes <= 46000:
        return "month"
    return None


def quota_used_percent(data: dict) -> float | None:
    used = coerce_number(data.get("usedPercent") or data.get("used_percent"))
    if used is not None:
        return max(0.0, min(100.0, used))

    remaining = coerce_number(
        data.get("remainingPercent")
        or data.get("remaining_percent")
        or data.get("remaining")
    )
    if remaining is not None:
        return max(0.0, min(100.0, 100.0 - remaining))

    used_count = coerce_number(data.get("used") or data.get("usage"))
    limit_count = coerce_number(data.get("limit") or data.get("quota") or data.get("total"))
    if used_count is not None and limit_count and limit_count > 0:
        return max(0.0, min(100.0, used_count * 100.0 / limit_count))
    return None


def quota_candidate_key(path: tuple[str, ...], period: str, data: dict) -> str:
    parts = [str(part) for part in path if not str(part).isdigit()]
    if len(parts) >= 3 and parts[0] == "rateLimitsByLimitId":
        return f"{parts[1]}.{parts[-1]}"
    if len(parts) >= 2 and parts[0] == "rateLimits":
        return f"codex.{parts[-1]}"
    return ".".join(parts) or str(data.get("limitId") or data.get("id") or period)


def quota_candidates(data: object, path: tuple[str, ...] = ()) -> list[dict]:
    candidates: list[dict] = []
    if isinstance(data, dict):
        used_percent = quota_used_percent(data)
        period = usage_period_from_dict(data, path)
        if period and used_percent is not None:
            candidates.append(
                {
                    "period": period,
                    "usedPercent": used_percent,
                    "key": quota_candidate_key(path, period, data),
                    "direct": True,
                }
            )
        for key, value in data.items():
            candidates.extend(quota_candidates(value, (*path, str(key))))
    elif isinstance(data, list):
        for index, value in enumerate(data):
            candidates.extend(quota_candidates(value, (*path, str(index))))
    return candidates


def direct_quota_summary(limits_result: dict) -> dict[str, float | None]:
    summary: dict[str, float | None] = {"day": None, "week": None, "month": None}
    for candidate in quota_candidates(limits_result):
        period = candidate.get("period")
        if period not in summary:
            continue
        if summary[period] is None:
            summary[period] = candidate.get("usedPercent")
    return summary


def quota_snapshot_items(limits_result: dict) -> list[dict]:
    items_by_key: dict[tuple[str, str], dict] = {}
    for candidate in quota_candidates(limits_result):
        used_percent = candidate.get("usedPercent")
        if used_percent is None:
            continue
        item = {
            "key": str(candidate.get("key") or candidate.get("period") or "quota"),
            "period": str(candidate.get("period") or ""),
            "usedPercent": round(float(used_percent), 4),
        }
        items_by_key[(item["key"], item["period"])] = item
    return list(items_by_key.values())


def update_quota_history(history: dict, limits_result: dict, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    items = quota_snapshot_items(limits_result)
    if not items:
        return history

    snapshots = history.setdefault("snapshots", [])
    if not isinstance(snapshots, list):
        snapshots = []
        history["snapshots"] = snapshots

    last_snapshot = snapshots[-1] if snapshots else {}
    last_items = last_snapshot.get("items") if isinstance(last_snapshot, dict) else None
    if isinstance(last_items, list) and last_items == items and now - float(last_snapshot.get("at", 0) or 0) < 45:
        return history

    snapshots.append({"at": now, "items": items})
    cutoff = now - 70 * 24 * 60 * 60
    history["snapshots"] = [
        snapshot
        for snapshot in snapshots[-10000:]
        if isinstance(snapshot, dict) and float(snapshot.get("at", 0) or 0) >= cutoff
    ]
    save_quota_history(history)
    return history


def period_start_timestamp(period: str, now: float | None = None) -> float:
    current = datetime.fromtimestamp(time.time() if now is None else now)
    if period == "day":
        start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        start = day_start - timedelta(days=day_start.weekday())
    elif period == "month":
        start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = current
    return start.timestamp()


def local_quota_delta(history: dict, period: str, now: float | None = None) -> float | None:
    snapshots = history.get("snapshots")
    if not isinstance(snapshots, list) or not snapshots:
        return None

    since = period_start_timestamp(period, now)
    sorted_snapshots = sorted(
        [snapshot for snapshot in snapshots if isinstance(snapshot, dict)],
        key=lambda snapshot: float(snapshot.get("at", 0) or 0),
    )

    previous_by_key: dict[str, float] = {}
    seen_after_start = False
    total = 0.0
    preferred_keys: set[str] = set()
    for snapshot in sorted_snapshots:
        for item in snapshot.get("items") or []:
            if isinstance(item, dict) and item.get("period") == "week":
                preferred_keys.add(str(item.get("key") or ""))

    for snapshot in sorted_snapshots:
        at = float(snapshot.get("at", 0) or 0)
        items = snapshot.get("items")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            if not key:
                continue
            if preferred_keys and key not in preferred_keys:
                continue
            used = coerce_number(item.get("usedPercent"))
            if used is None:
                continue
            previous = previous_by_key.get(key)
            if at < since:
                previous_by_key[key] = used
                continue
            seen_after_start = True
            if previous is None:
                previous_by_key[key] = used
                continue
            if used >= previous:
                total += used - previous
            else:
                total += used
            previous_by_key[key] = used

    if not seen_after_start:
        return None
    return max(0.0, total)


def quota_summary(
    limits_result: dict,
    history: dict | None = None,
    now: float | None = None,
) -> dict[str, dict]:
    direct = direct_quota_summary(limits_result)
    summary: dict[str, dict] = {}
    for period in ("day", "week", "month"):
        if direct.get(period) is not None:
            summary[period] = {"value": direct[period], "source": "direct"}
            continue
        local_value = local_quota_delta(history or {}, period, now)
        summary[period] = {"value": local_value, "source": "local" if local_value is not None else ""}
    return summary


def validate_account(account_result: dict) -> dict:
    account = account_result.get("account")
    if not account:
        raise AppServerError("Codex 尚未登录。请先在终端运行 codex login。")
    if account.get("type") != "chatgpt":
        raise AppServerError(
            "当前 Codex 使用的不是 ChatGPT 登录。ChatGPT 套餐额度查询需要通过 ChatGPT 登录。"
        )
    return account


def is_rate_limits_error(error: str) -> bool:
    text = str(error).lower()
    return "ratelimits" in text or "rate limits" in text or "/wham/usage" in text


def is_auth_error(error: str) -> bool:
    text = str(error).lower()
    return (
        "requiresopenaiauth" in text
        or "authentication required" in text
        or "需要重新登录" in text
        or "尚未登录" in text
        or "not logged in" in text
    )


def collect_low_items(limits_result: dict) -> list[str]:
    low_items: list[str] = []
    for bucket_id, bucket in iter_buckets(limits_result).items():
        if not isinstance(bucket, dict):
            continue
        label = bucket.get("limitName") or bucket_id
        shown = 0
        for window in (bucket.get("primary"), bucket.get("secondary")):
            if not isinstance(window, dict):
                continue
            used = float(window.get("usedPercent") or 0)
            remaining = max(0.0, min(100.0, 100.0 - used))
            name = format_window_name(window.get("windowDurationMins"), shown)
            if remaining <= LOW_BALANCE_THRESHOLD:
                low_items.append(f"{label} {name} 剩余 {remaining:.1f}%")
            shown += 1

        individual_limit = bucket.get("individualLimit")
        if isinstance(individual_limit, dict):
            remaining_percent = individual_limit.get("remainingPercent")
            if remaining_percent is not None:
                remaining = max(0.0, min(100.0, float(remaining_percent)))
                if remaining <= LOW_BALANCE_THRESHOLD:
                    low_items.append(f"{label} 个人限制 剩余 {remaining:.1f}%")
    return low_items


def available_reset_credits(reset_credits: dict) -> list[dict]:
    details = reset_credits.get("credits")
    if not isinstance(details, list):
        return []
    return [
        credit
        for credit in details
        if isinstance(credit, dict)
        and credit.get("status", "available") in ("available", "unknown")
    ]


def thread_identity(thread: dict) -> str:
    identity = find_first_value(
        thread,
        ("id", "threadId", "conversationId", "conversation_id", "sessionId"),
    )
    return str(identity or id(thread))


def thread_source_label(thread: dict, fallback: str = "") -> str:
    primary = thread.get("_primarySource")
    if primary:
        return str(primary)
    raw = find_first_value(
        thread,
        ("source", "origin", "surface", "client", "app", "channel", "entrypoint"),
    )
    text = str(raw or fallback or "未知").lower()
    if (
        "codex-client-ipc" in text
        or text == "codex"
        or "codex client" in text
        or "codex desktop" in text
    ):
        return "Codex 客户端"
    if "session-file" in text:
        return "本地 session"
    if "codex-ipc" in text:
        return "实时状态"
    if "plugin" in text or "extension" in text or "插件" in text:
        return "插件"
    if "web" in text or "browser" in text or "chatgpt" in text or "网页" in text:
        return "网页"
    if "vscode" in text or "vs code" in text:
        return "VS Code"
    if (
        "client" in text
        or "desktop" in text
        or "codex" in text
        or "cli" in text
        or "客户端" in text
    ):
        return "客户端"
    return str(raw or fallback or "未知")


def source_confidence(source: str) -> int:
    if source in {"VS Code", "Codex 客户端", "网页", "插件", "客户端"}:
        return 2
    if source in {"实时状态", "Codex IPC", "本地 session"}:
        return 0
    return 1 if source and source != "未知" else 0


def choose_primary_source(previous: object, incoming: str) -> str:
    previous_text = str(previous or "")
    if source_confidence(incoming) >= source_confidence(previous_text):
        return incoming
    return previous_text or incoming


def is_past_timestamp(value: object) -> bool:
    timestamp = comparable_timestamp(value)
    return timestamp > 0 and timestamp <= time.time()


def is_expired_thread(thread: dict) -> bool:
    if thread.get("expired") is True or thread.get("isExpired") is True:
        return True
    expires_at = find_first_value(
        thread,
        (
            "expiresAt",
            "expires_at",
            "expiredAt",
            "expired_at",
            "deadlineAt",
            "deadline_at",
            "endAt",
            "end_at",
        ),
    )
    return is_past_timestamp(expires_at)


def comparable_timestamp(value: object) -> float:
    if value in (None, ""):
        return 0.0
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return 0.0
    if raw > 10_000_000_000:
        raw /= 1000
    return raw


def thread_items(result: object) -> list:
    if not isinstance(result, dict):
        return []
    for key in ("data", "threads", "items", "loadedThreads"):
        items = result.get(key)
        if isinstance(items, list):
            return items
    return []


def clean_thread_text(value: object) -> str:
    text = str(value or "").replace("\n", " ").strip()
    return re.sub(r"\s+", " ", text)


def is_placeholder_thread_text(value: object) -> bool:
    text = clean_thread_text(value)
    if not text or text.lower() in {"none", "null", "unknown", "Chating"}:
        return True
    if re.fullmatch(r"IPC\s+[0-9a-fA-F-]{4,}", text):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{8,}(?:-[0-9a-fA-F]{4,})*", text):
        return True
    return False


def best_thread_title(*threads: dict) -> str:
    for key in ("name", "title", "summary", "preview"):
        for thread in threads:
            if isinstance(thread, dict):
                value = thread.get(key)
                if not is_placeholder_thread_text(value):
                    return clean_thread_text(value)
    return ""


def status_priority(thread: dict) -> int:
    source = str(thread.get("_primarySource") or thread_source_label(thread))
    if thread.get("_ipcMethod") or source in {"实时状态", "VS Code IPC", "Codex IPC"}:
        return 3
    if source == "通知":
        return 2
    if thread.get("_sessionPath") or source == "本地 session":
        return 1
    return 0


def status_looks_idle(status: object) -> bool:
    if isinstance(status, dict):
        status = status.get("type") or status.get("state") or status.get("status")
    text = str(status or "").lower()
    return any(
        marker in text
        for marker in (
            "idle",
            "complete",
            "done",
            "abort",
            "cancel",
            "interrupt",
            "stop",
            "archived",
            "空闲",
            "终止",
            "取消",
            "中断",
            "停止",
        )
    )


def status_looks_waiting_approval(status: object) -> bool:
    if isinstance(status, dict):
        flags = status.get("activeFlags")
        if isinstance(flags, list) and "waitingOnApproval" in flags:
            return True
        status = status.get("type") or status.get("state") or status.get("status")
    text = str(status or "").lower()
    return "approval" in text or "审批" in text or "waitingonapproval" in text


def merge_thread_results(thread_sources: list[dict]) -> dict:
    merged: dict[str, dict] = {}
    errors: list[str] = []

    for source in thread_sources:
        source_label = str(source.get("source") or "未知")
        if source.get("error"):
            errors.append(f"{source_label}: {source.get('error')}")
        for thread in thread_items(source.get("result")):
            if not isinstance(thread, dict):
                continue
            identity = thread_identity(thread)
            source_name = thread_source_label(thread, source_label)
            if identity in merged:
                sources = merged[identity].setdefault("_sources", [])
                if source_name not in sources:
                    sources.append(source_name)
                previous = dict(merged[identity])
                old_thread = merged[identity]
                merged_sources = list(sources)
                newer_idle_status = (
                    "status" in thread
                    and status_looks_idle(thread.get("status"))
                    and comparable_timestamp(thread.get("updatedAt"))
                    >= comparable_timestamp(old_thread.get("updatedAt"))
                )
                waiting_approval_status = (
                    "status" in thread
                    and status_looks_waiting_approval(thread.get("status"))
                )
                if (
                    "status" in thread
                    and (
                        status_priority(thread) >= status_priority(old_thread)
                        or newer_idle_status
                        or waiting_approval_status
                    )
                ):
                    old_thread["status"] = thread["status"]
                    for status_key in (
                        "_ipcMethod",
                        "_sessionPath",
                        "_sessionInferredActive",
                        "_rawSource",
                        "_originator",
                        "activeFlags",
                        "waitingOnApproval",
                        "waitingOnUserInput",
                    ):
                        if status_key in thread:
                            old_thread[status_key] = thread[status_key]
                old_thread["_primarySource"] = choose_primary_source(
                    previous.get("_primarySource"), source_name
                )
                old_thread["_sources"] = merged_sources
                if comparable_timestamp(thread.get("updatedAt")) >= comparable_timestamp(
                    merged[identity].get("updatedAt")
                ):
                    title = best_thread_title(thread, previous)
                    old_thread.update(
                        {
                            key: value
                            for key, value in thread.items()
                            if key != "status"
                        }
                    )
                    if title:
                        merged[identity]["name"] = title
                    merged[identity]["_sources"] = merged_sources
                    merged[identity]["_primarySource"] = old_thread["_primarySource"]
                continue

            item = dict(thread)
            title = best_thread_title(item)
            if title:
                item["name"] = title
            item["_sources"] = [source_name]
            item["_primarySource"] = choose_primary_source(None, source_name)
            merged[identity] = item

    return {
        "data": list(merged.values()),
        "errors": errors,
        "sourceCount": len(thread_sources),
    }


def merge_current_session_threads(
    thread_result: dict | None,
    session_scan: dict | None = None,
) -> dict:
    thread_sources: list[dict] = []
    if isinstance(thread_result, dict):
        thread_sources.append(
            {
                "source": "已有对话",
                "error": thread_result.get("error"),
                "result": thread_result,
            }
        )

    if session_scan is None:
        session_scan = scan_local_sessions()
    if session_scan.get("data") or session_scan.get("errors"):
        thread_sources.append(
            {
                "source": "本地 session",
                "error": "; ".join(session_scan.get("errors") or []),
                "result": {"data": session_scan.get("data") or []},
            }
        )

    result = merge_thread_results(thread_sources)
    result["_scannedAt"] = time.time()
    return result


def thread_status_info(status: object) -> tuple[str, str, list[str]]:
    if isinstance(status, str):
        status_text = status.lower()
        if "approval" in status_text or "审批" in status_text:
            return "等待审批", ACCENT_COLOR, ["waitingOnApproval"]
        if "input" in status_text or "user" in status_text or "输入" in status_text:
            return "等待输入", ACCENT_COLOR, ["waitingOnUserInput"]
        if "thinking" in status_text or "思考" in status_text:
            return "思考中", GOOD_COLOR, []
        if "output" in status_text or "输出" in status_text or "message" in status_text:
            return "输出中", GOOD_COLOR, []
        if "working" in status_text or "busy" in status_text or "工作" in status_text:
            return "执行中", GOOD_COLOR, []
        if "active" in status_text or "running" in status_text or "queued" in status_text:
            return "运行中", DIM_TEXT_COLOR, []
        if any(
            text in status_text
            for text in (
                "idle",
                "done",
                "complete",
                "archived",
                "abort",
                "cancel",
                "interrupt",
                "stop",
                "终止",
                "取消",
                "中断",
                "停止",
            )
        ):
            return "空闲", MUTED_TEXT_COLOR, []
        if "error" in status_text or "failed" in status_text:
            return "系统错误", BAD_COLOR, []
        if "recent" in status_text:
            return "最近有变化", DIM_TEXT_COLOR, []
        return status, DIM_TEXT_COLOR, []

    if not isinstance(status, dict):
        return "状态未知", DIM_TEXT_COLOR, []
    status_type = status.get("type") or status.get("state") or status.get("status")
    if status_type == "active":
        flags = status.get("activeFlags")
        if not isinstance(flags, list):
            flags = []
        if "waitingOnApproval" in flags:
            return "等待审批", ACCENT_COLOR, flags
        if "waitingOnUserInput" in flags:
            return "等待输入", ACCENT_COLOR, flags
        active_state = status.get("activeState") or status.get("phase") or status.get("mode")
        if active_state:
            return thread_status_info(str(active_state))
        return "运行中", DIM_TEXT_COLOR, flags
    if status_type == "idle":
        return "空闲", MUTED_TEXT_COLOR, []
    if status_type == "systemError":
        return "系统错误", BAD_COLOR, []
    if status_type == "notLoaded":
        return "未加载", DIM_TEXT_COLOR, []
    return thread_status_info(str(status_type or "状态未知"))


def thread_activity_info(thread: dict) -> tuple[str, str, list[str]]:
    status = thread.get("status")
    label, color, flags = thread_status_info(status)

    root_flags = thread.get("activeFlags")
    if isinstance(root_flags, list):
        flags = list(dict.fromkeys([*flags, *[str(flag) for flag in root_flags]]))

    if thread.get("waitingOnApproval") is True and "waitingOnApproval" not in flags:
        flags.append("waitingOnApproval")
    if thread.get("waitingOnUserInput") is True and "waitingOnUserInput" not in flags:
        flags.append("waitingOnUserInput")

    if "waitingOnApproval" in flags:
        return "等待审批", ACCENT_COLOR, flags
    if "waitingOnUserInput" in flags:
        return "等待输入", ACCENT_COLOR, flags

    if label == "状态未知":
        for key in ("thinking", "isThinking"):
            if thread.get(key) is True:
                return "思考中", GOOD_COLOR, flags
        for key in ("working", "isWorking", "busy"):
            if thread.get(key) is True:
                return "执行中", GOOD_COLOR, flags
    return label, color, flags


def summarize_threads(thread_result: dict) -> dict:
    if thread_result.get("error"):
        return {
            "error": thread_result.get("error"),
            "errors": [],
            "threads": [],
            "active_threads": [],
            "total": 0,
            "active": 0,
            "manual": [],
            "latest": None,
        }

    threads = thread_result.get("data")
    if not isinstance(threads, list):
        threads = []

    manual = []
    active_threads = []
    active_count = 0
    visible_threads = [
        thread
        for thread in threads
        if isinstance(thread, dict) and not is_expired_thread(thread)
    ]
    for thread in visible_threads:
        label, _color, flags = thread_activity_info(thread)
        if label in ("执行中", "思考中", "输出中", "运行中", "等待审批", "等待输入"):
            active_count += 1
            active_threads.append(thread)
        if "waitingOnApproval" in flags or "waitingOnUserInput" in flags:
            manual.append(thread)

    visible_threads.sort(
        key=lambda thread: comparable_timestamp(thread.get("updatedAt")),
        reverse=True,
    )
    active_threads.sort(
        key=lambda thread: comparable_timestamp(thread.get("updatedAt")),
        reverse=True,
    )
    manual.sort(
        key=lambda thread: comparable_timestamp(thread.get("updatedAt")),
        reverse=True,
    )

    latest = visible_threads[0] if visible_threads else None
    return {
        "error": None,
        "errors": thread_result.get("errors") if isinstance(thread_result.get("errors"), list) else [],
        "threads": visible_threads,
        "active_threads": active_threads,
        "total": len(threads),
        "active": active_count,
        "manual": manual,
        "latest": latest,
    }


def thread_title(thread: dict | None) -> str:
    if not isinstance(thread, dict):
        return "无最近对话"
    text = best_thread_title(thread) or "活动对话"
    if len(text) > 34:
        return text[:31] + "..."
    return text


def active_thread_sort_key(thread: dict) -> tuple[int, int, str, str]:
    source_order = {
        "当前会话": 0,
        "网页": 1,
        "Codex 客户端": 2,
        "客户端": 2,
        "VS Code": 3,
        "插件": 4,
        "实时状态": 5,
        "Codex IPC": 5,
        "VS Code IPC": 5,
        "本地 session": 6,
        "通知": 7,
    }
    source = thread_source_label(thread)
    title = clean_thread_text(best_thread_title(thread)).lower()
    return (
        0 if thread.get("_sessionInferredActive") else 1,
        source_order.get(source, 8),
        title,
        thread_identity(thread),
    )


def activity_breakdown(thread_result: dict) -> dict[str, int]:
    counts = {
        "思考中": 0,
        "输出中": 0,
        "执行中": 0,
        "运行中": 0,
        "等待审批": 0,
        "等待输入": 0,
    }
    for thread in summarize_threads(thread_result)["active_threads"]:
        if not isinstance(thread, dict):
            continue
        label, _color, _flags = thread_activity_info(thread)
        if label in counts:
            counts[label] += 1
    return counts


def activity_overview_text(thread_result: dict) -> tuple[str, str]:
    counts = activity_breakdown(thread_result)
    parts = [f"{label} {count}" for label, count in counts.items() if count > 0]
    if not parts:
        return "空闲", GOOD_COLOR
    if counts["等待审批"] or counts["等待输入"]:
        return " · ".join(parts), ACCENT_COLOR
    if counts["思考中"] or counts["输出中"] or counts["执行中"]:
        return " · ".join(parts), GOOD_COLOR
    return " · ".join(parts), DIM_TEXT_COLOR


class Card(QFrame):
    def __init__(
        self,
        title: str,
        subtitle: str = "",
        badge: str | None = None,
        action_text: str = "",
        action_callback=None,
        action_tooltip: str = "",
    ) -> None:
        super().__init__()
        self.setObjectName("card")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.action_button: QPushButton | None = None

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(0)

        header = QFrame()
        header.setObjectName("cardHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(10, 7, 10, 7)
        header_layout.setSpacing(8)

        self.title_label = QLabel()
        self.title_label.setObjectName("cardTitle")
        self.title_label.setWordWrap(True)
        header_layout.addWidget(self.title_label, 1)

        self.subtitle_label = QLabel()
        self.subtitle_label.setObjectName("muted")
        self.subtitle_label.setWordWrap(False)
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        header_layout.addWidget(self.subtitle_label)

        self.badge_label = QLabel()
        self.badge_label.setObjectName("badge")
        header_layout.addWidget(self.badge_label)

        if action_text:
            self.action_button = QPushButton(action_text)
            self.action_button.setObjectName("headerAction")
            self.action_button.setFixedSize(24, 24)
            if action_tooltip:
                self.action_button.setToolTip(action_tooltip)
            if action_callback is not None:
                self.action_button.clicked.connect(action_callback)
            header_layout.addWidget(self.action_button)

        self.layout.addWidget(header)

        self.body = QFrame()
        self.body.setObjectName("cardBody")
        self.body_layout = QGridLayout(self.body)
        self.body_layout.setContentsMargins(10, 10, 10, 10)
        self.body_layout.setHorizontalSpacing(10)
        self.body_layout.setVerticalSpacing(6)
        self.body_layout.setColumnStretch(1, 1)
        self.layout.addWidget(self.body)
        self.update_header(title, subtitle, badge)

    def update_header(
        self,
        title: str,
        subtitle: str = "",
        badge: str | None = None,
    ) -> None:
        self.title_label.setText(title)
        self.subtitle_label.setText(subtitle)
        self.subtitle_label.setVisible(bool(subtitle))
        self.badge_label.setText(str(badge or ""))
        self.badge_label.setVisible(bool(badge))
        if badge:
            self.badge_label.setStyleSheet(
                f"background:{plan_badge_color(badge)}; color:white; "
                "border-radius:4px; padding:2px 7px; font-weight:600;"
            )


class ConversationCard(Card):
    """Reusable card for one conversation; its child widgets are updated in place."""

    def __init__(self) -> None:
        super().__init__("")
        self.status_value = QLabel()
        self.status_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.preview_value = QLabel()
        self.preview_value.setWordWrap(True)
        self.preview_value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.cwd_label = QLabel("◇ 项目")
        self.cwd_value = QLabel()
        self.cwd_value.setWordWrap(True)
        self.cwd_value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        self.status_label = QLabel("● 状态")
        self.status_label.setStyleSheet(f"color:{MUTED_TEXT_COLOR};")
        self.cwd_label.setStyleSheet(f"color:{MUTED_TEXT_COLOR};")
        self.body_layout.addWidget(self.status_label, 0, 0)
        self.body_layout.addWidget(self.status_value, 0, 1, 1, 2)
        self.body_layout.addWidget(self.preview_value, 1, 0, 1, 3)
        self.body_layout.addWidget(self.cwd_label, 2, 0, Qt.AlignmentFlag.AlignTop)
        self.body_layout.addWidget(self.cwd_value, 2, 1, 1, 2)
        self.preview_value.setVisible(False)
        self.cwd_label.setVisible(False)
        self.cwd_value.setVisible(False)

    def update_conversation(
        self,
        title: str,
        subtitle: str,
        badge: str | None,
        status_text: str,
        status_color: str,
        preview: str,
        cwd: str,
    ) -> None:
        self.update_header(title, subtitle, badge)
        self.status_value.setText(status_text)
        self.status_value.setStyleSheet(f"color:{status_color}; font-family: Consolas;")
        self.preview_value.setText(preview)
        self.preview_value.setVisible(bool(preview))
        self.cwd_value.setText(cwd)
        self.cwd_label.setVisible(bool(cwd))
        self.cwd_value.setVisible(bool(cwd))


class CodexBalanceMonitor(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = load_settings()
        self.client = CodexAppServerClient()
        self.signals = SnapshotSignals()
        self.signals.loaded.connect(self._apply_snapshot)
        self.signals.activity_loaded.connect(self._apply_activity_snapshot)
        self.signals.failed.connect(self._apply_error)

        self.refreshing = False
        self.refreshing_activity = False
        self.snapshot_generation = 0
        self.account_connected = False
        self.warned_keys: set[str] = set()
        self.show_reset_details = bool(self.settings.get("showResetDetails", True))
        self.topmost_state = bool(self.settings.get("topmost", True))
        self.active_view = str(self.settings.get("activeView") or "dashboard")
        if self.active_view == "details":
            self.active_view = "dashboard"
        if self.active_view == "about":
            self.active_view = "settings"
        if self.active_view not in {"dashboard", "sessions", "reset", "settings"}:
            self.active_view = "dashboard"
        self.latest_limits_result: dict | None = None
        self.latest_account_result: dict | None = None
        self.latest_thread_result: dict | None = None
        self.quota_history = load_quota_history()
        self.last_updated_text = ""
        self.conversation_layout: QVBoxLayout | None = None
        self.conversation_cards: dict[str, ConversationCard] = {}
        self.conversation_aux_cards: dict[str, Card] = {}
        self.dashboard_activity_container: QWidget | None = None
        self.dashboard_activity_layout: QVBoxLayout | None = None
        self.dashboard_activity_card: Card | None = None
        self.drag_offset = None
        self.force_quit = False
        self.tray_icon: QSystemTrayIcon | None = None
        self.reset_detail_button: QPushButton | None = None
        self.conversation_resize_timer = QTimer(self)
        self.conversation_resize_timer.setSingleShot(True)
        self.conversation_resize_timer.timeout.connect(self._rerender_conversations_for_width)

        self.setWindowTitle("Codex 额度监控")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        self._apply_saved_geometry()
        self.setWindowOpacity(1.0)

        self._build_ui()
        self._setup_tray_icon()
        self._apply_topmost(self.topmost_state, initial=True)
        self._show_view(self.active_view, save=False)
        self._render_empty_dashboard("准备连接 Codex")
        self.latest_thread_result = {
            "data": [],
            "errors": [],
            "sourceCount": 0,
            "_scannedAt": time.time(),
        }
        self._render_conversation_status(self.latest_thread_result)
        self._render_empty_reset("等待额度数据")

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.refresh_data)
        self.poll_timer.start(POLL_INTERVAL_SECONDS * 1000)

        self.activity_timer = QTimer(self)
        self.activity_timer.timeout.connect(self.refresh_activity)
        self.activity_timer.start(ACTIVITY_POLL_INTERVAL_SECONDS * 1000)
        self._update_window_mask()
        QTimer.singleShot(200, self.refresh_data)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)

        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(1, 1, 1, 1)
        root_layout.setSpacing(0)

        self.titlebar = QFrame()
        self.titlebar.setObjectName("titlebar")
        self.titlebar.setFixedHeight(44)
        title_layout = QHBoxLayout(self.titlebar)
        title_layout.setContentsMargins(12, 0, 8, 0)
        title_layout.setSpacing(8)

        logo = QLabel()
        logo.setPixmap(QPixmap(str(resource_path("resources/codex_monitor_tray.ico"))))
        logo.setObjectName("logo")
        logo.setFixedSize(22, 22)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_layout.addWidget(logo)

        title = QLabel("Codex Monitor")
        title.setObjectName("windowTitle")
        title_layout.addWidget(title)

        self.account_label = QLabel("账户：正在读取")
        self.account_label.setObjectName("muted")
        title_layout.addWidget(self.account_label, 1)

        self.topmost_button = self._title_button(
            "置顶", lambda checked=False: self._apply_topmost(bool(checked)), width=52
        )
        self.topmost_button.setCheckable(True)
        self.topmost_button.setChecked(self.topmost_state)
        title_layout.addWidget(self.topmost_button)
        title_layout.addWidget(self._title_button("X", self.close))
        root_layout.addWidget(self.titlebar)

        self.stack = QStackedWidget()
        root_layout.addWidget(self.stack, 1)

        self.dashboard_page, self.dashboard_layout = self._scroll_page()
        self.sessions_page, self.sessions_layout = self._scroll_page()
        self.reset_page, self.reset_layout = self._scroll_page()
        self.settings_page = self._settings_page()

        self.stack.addWidget(self.dashboard_page)
        self.stack.addWidget(self.sessions_page)
        self.stack.addWidget(self.reset_page)
        self.stack.addWidget(self.settings_page)

        nav = QFrame()
        nav.setObjectName("nav")
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(12, 8, 12, 8)
        nav_layout.setSpacing(4)

        self.nav_buttons: dict[str, QPushButton] = {}
        self.nav_buttons["dashboard"] = self._nav_button("仪表盘", "dashboard")
        self.nav_buttons["sessions"] = self._nav_button("会话", "sessions")
        self.nav_buttons["reset"] = self._nav_button("重置", "reset")
        self.nav_buttons["settings"] = self._nav_button("设置", "settings")
        for button in self.nav_buttons.values():
            nav_layout.addWidget(button)
        self._sync_account_controls(False)

        nav_layout.addStretch(1)
        self.activity_label = QLabel("connecting")
        self.activity_label.setObjectName("muted")
        nav_layout.addWidget(self.activity_label)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.setFixedWidth(52)
        self.refresh_button.clicked.connect(self.refresh_data)
        nav_layout.addWidget(self.refresh_button)
        nav_layout.addWidget(QSizeGrip(self))
        root_layout.addWidget(nav)

        self._apply_style()

    def _tray_icon_image(self) -> QIcon:
        return QIcon(str(resource_path("resources/codex_monitor_tray.ico")))

    def _setup_tray_icon(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray_icon = QSystemTrayIcon(self._tray_icon_image(), self)
        self.tray_icon.setToolTip("Codex 额度监控")

        menu = QMenu(self)
        toggle_action = QAction("显示 / 隐藏", self)
        toggle_action.triggered.connect(self._toggle_window_visible)
        refresh_action = QAction("刷新", self)
        refresh_action.triggered.connect(self.refresh_data)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._quit_from_tray)
        menu.addAction(toggle_action)
        menu.addAction(refresh_action)
        menu.addSeparator()
        menu.addAction(quit_action)

        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(self._handle_tray_activated)
        self.tray_icon.show()

    def _toggle_window_visible(self) -> None:
        if self.isVisible():
            self.hide()
            return
        self.show()
        self.raise_()
        self.activateWindow()

    def _handle_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._toggle_window_visible()

    def _quit_from_tray(self) -> None:
        self.force_quit = True
        self.close()

    def _scroll_page(self) -> tuple[QScrollArea, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setObjectName("scroll")
        content = QWidget()
        content.setObjectName("page")
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        layout.addStretch(1)
        scroll.setWidget(content)
        return scroll, layout

    def _settings_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("page")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        card = Card("设置", "显示偏好")
        row = 0

        self.topmost_checkbox = QCheckBox("窗口置顶")
        self.topmost_checkbox.setChecked(self.topmost_state)
        self.topmost_checkbox.toggled.connect(self._apply_topmost)
        card.body_layout.addWidget(self.topmost_checkbox, row, 0, 1, 3)
        row += 1

        card.body_layout.addWidget(QLabel("透明度"), row, 0)
        self.opacity_slider = QSlider(Qt.Orientation.Horizontal)
        self.opacity_slider.setRange(0, 100)
        self.opacity_slider.setValue(int(coerce_opacity(self.settings.get("opacity", 1.0)) * 100))
        self.opacity_slider.valueChanged.connect(self._apply_opacity)
        card.body_layout.addWidget(self.opacity_slider, row, 1)
        self.opacity_label = QLabel(f"{self.opacity_slider.value()}%")
        self.opacity_label.setObjectName("muted")
        card.body_layout.addWidget(self.opacity_label, row, 2)
        row += 1

        self.startup_checkbox = QCheckBox("开机自启")
        self.startup_checkbox.setChecked(is_startup_enabled())
        self.startup_checkbox.toggled.connect(self._apply_startup)
        card.body_layout.addWidget(self.startup_checkbox, row, 0, 1, 3)
        row += 1

        usage_button = QPushButton("打开官方用量页")
        usage_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(USAGE_PAGE)))
        card.body_layout.addWidget(usage_button, row, 0, 1, 3)

        layout.addWidget(card)
        about_card = Card("关于", "Codex 额度监控")
        text = QLabel(
            "用于查看 Codex 额度窗口、重置时间、附加 credits 和可用额度重置次数。"
            "界面基于 PySide6，适合小工具场景。"
        )
        text.setWordWrap(True)
        text.setObjectName("muted")
        about_card.body_layout.addWidget(text, 0, 0, 1, 3)
        layout.addWidget(about_card)
        layout.addStretch(1)
        return page

    def _title_button(self, text: str, callback, width: int = 34) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("titleButton")
        button.setFixedWidth(width)
        button.clicked.connect(callback)
        return button

    def _nav_button(self, text: str, view_name: str) -> QPushButton:
        button = QPushButton(text)
        button.setCheckable(True)
        button.clicked.connect(lambda: self._show_view(view_name))
        return button

    def _sync_account_controls(
        self,
        connected: bool,
        account: dict | None = None,
        limits_result: dict | None = None,
        error: str = "",
    ) -> None:
        self.account_connected = connected
        if connected and isinstance(account, dict):
            self.account_label.setText(
                f"{account.get('email') or '未知账户'} · {account.get('planType') or '未知套餐'}"
            )
        elif error:
            self.account_label.setText("账户：读取失败")
        else:
            self.account_label.setText("账户：正在读取")

        if hasattr(self, "nav_buttons"):
            has_limits = connected and isinstance(limits_result, dict)
            if "reset" in self.nav_buttons:
                self.nav_buttons["reset"].setEnabled(has_limits)
            if not has_limits and self.active_view == "reset":
                self._show_view("dashboard", save=False)

        if hasattr(self, "refresh_button"):
            self.refresh_button.setEnabled(not self.refreshing)

    def _apply_style(self) -> None:
        opacity = coerce_opacity(
            self.opacity_slider.value()
            if hasattr(self, "opacity_slider")
            else self.settings.get("opacity", 1.0)
        )
        bg = rgba(BG_COLOR, opacity)
        title_bg = rgba(TITLE_BG, opacity)
        card_bg = rgba(CARD_BG, opacity)
        card_header_bg = rgba(CARD_HEADER_BG, opacity)
        button_bg = rgba("#3a3a42", opacity)
        button_hover_bg = rgba("#464650", opacity)
        title_button_hover_bg = rgba("#2a2a2f", opacity)
        track_bg = rgba(TRACK_COLOR, opacity)
        scrollbar_bg = rgba("#474751", opacity)
        self.setStyleSheet(
            f"""
            QWidget#root, QWidget#page, QScrollArea#scroll {{
                background: {bg};
                color: {TEXT_COLOR};
                font-family: Segoe UI, Microsoft YaHei;
                font-size: 10pt;
            }}
            QWidget#root {{
                border-radius: {WINDOW_CORNER_RADIUS}px;
            }}
            QFrame#titlebar {{
                background: {title_bg};
                border-top-left-radius: {WINDOW_CORNER_RADIUS}px;
                border-top-right-radius: {WINDOW_CORNER_RADIUS}px;
            }}
            QLabel, QCheckBox {{
                background: transparent;
            }}
            QLabel {{
                color: {TEXT_COLOR};
            }}
            QWidget#dashboardActivityContainer {{
                background: transparent;
            }}
            QLabel#logo {{
                background: transparent;
                color: white;
                border-radius: 5px;
                font-weight: 700;
            }}
            QLabel#windowTitle, QLabel#cardTitle {{
                color: {TEXT_COLOR};
                font-weight: 700;
            }}
            QLabel#muted {{
                color: {MUTED_TEXT_COLOR};
            }}
            QFrame#card {{
                background: {card_bg};
                border-radius: 8px;
            }}
            QFrame#cardHeader {{
                background: {card_header_bg};
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
            }}
            QFrame#cardBody {{
                background: {card_bg};
                border-bottom-left-radius: 8px;
                border-bottom-right-radius: 8px;
            }}
            QPushButton {{
                background: {button_bg};
                color: {TEXT_COLOR};
                border: 0;
                border-radius: 5px;
                padding: 5px 10px;
            }}
            QPushButton:hover {{
                background: {button_hover_bg};
            }}
            QPushButton:checked {{
                color: {ACCENT_COLOR};
                border-bottom: 2px solid {ACCENT_COLOR};
                border-radius: 0;
                background: transparent;
            }}
            QPushButton#titleButton {{
                background: {title_bg};
                border-radius: 4px;
                padding: 4px 6px;
            }}
            QPushButton#titleButton:hover {{
                background: {title_button_hover_bg};
            }}
            QPushButton#titleButton:checked {{
                background: {ACCENT_COLOR};
                color: #151515;
                border: 0;
                border-radius: 4px;
            }}
            QPushButton#headerAction {{
                background: transparent;
                color: {MUTED_TEXT_COLOR};
                border: 0;
                border-radius: 4px;
                padding: 0;
                font-weight: 700;
            }}
            QPushButton#headerAction:hover {{
                background: {button_hover_bg};
                color: {TEXT_COLOR};
            }}
            QFrame#nav {{
                background: {bg};
                border-top: 1px solid #34343a;
                border-bottom-left-radius: {WINDOW_CORNER_RADIUS}px;
                border-bottom-right-radius: {WINDOW_CORNER_RADIUS}px;
            }}
            QProgressBar {{
                background: {track_bg};
                border: 0;
                border-radius: 4px;
                height: 8px;
                text-align: center;
            }}
            QProgressBar::chunk {{
                border-radius: 4px;
            }}
            QSlider::groove:horizontal {{
                height: 6px;
                background: {track_bg};
                border-radius: 3px;
            }}
            QSlider::handle:horizontal {{
                background: {ACCENT_COLOR};
                width: 14px;
                margin: -4px 0;
                border-radius: 7px;
            }}
            QScrollBar:vertical {{
                background: {bg};
                width: 10px;
            }}
            QScrollBar::handle:vertical {{
                background: {scrollbar_bg};
                border-radius: 5px;
                min-height: 28px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QCheckBox {{
                color: {TEXT_COLOR};
                background: transparent;
            }}
            """
        )

    def _clear_layout(self, layout: QVBoxLayout) -> None:
        if hasattr(self, "dashboard_layout") and layout is self.dashboard_layout:
            self.dashboard_activity_container = None
            self.dashboard_activity_layout = None
            self.dashboard_activity_card = None
        while layout.count() > 0:
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        layout.addStretch(1)

    def _add_card(self, layout: QVBoxLayout, card: Card) -> None:
        layout.insertWidget(max(0, layout.count() - 1), card)

    def _ensure_conversation_container(self) -> QVBoxLayout:
        if self.conversation_layout is not None:
            return self.conversation_layout
        self.conversation_layout = self.sessions_layout
        return self.conversation_layout

    def _ensure_dashboard_activity_container(self) -> QVBoxLayout:
        if (
            self.dashboard_activity_container is not None
            and self.dashboard_activity_layout is not None
        ):
            return self.dashboard_activity_layout

        container = QWidget()
        container.setObjectName("dashboardActivityContainer")
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)
        container_layout.addStretch(1)
        self.dashboard_activity_container = container
        self.dashboard_activity_layout = container_layout
        self.dashboard_layout.insertWidget(
            max(0, self.dashboard_layout.count() - 1),
            container,
        )
        return container_layout

    def _render_conversation_status(self, thread_result: dict) -> None:
        layout = self._ensure_conversation_container()
        self._render_conversation_page(layout, thread_result)

    def _render_dashboard_activity_status(self, thread_result: dict) -> None:
        layout = self._ensure_dashboard_activity_container()
        self._render_activity_overview_card(layout, thread_result)

    def _metric_row(
        self,
        grid: QGridLayout,
        row: int,
        label: str,
        value: str,
        color: str = MUTED_TEXT_COLOR,
    ) -> int:
        label_widget = QLabel(label)
        label_widget.setStyleSheet(f"color:{MUTED_TEXT_COLOR};")
        value_widget = QLabel(value)
        value_widget.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        value_widget.setStyleSheet(f"color:{color}; font-family: Consolas;")
        grid.addWidget(label_widget, row, 0)
        grid.addWidget(value_widget, row, 1, 1, 2)
        return row + 1

    def _fit_text_prefix(
        self,
        text: str,
        width: int,
        metrics: QFontMetrics,
        suffix: str = "",
    ) -> tuple[str, int]:
        if not text:
            return "", 0
        if metrics.horizontalAdvance(text + suffix) <= width:
            return text, len(text)
        low = 0
        high = len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if metrics.horizontalAdvance(text[:mid].rstrip() + suffix) <= width:
                low = mid
            else:
                high = mid - 1
        count = max(1, low)
        return text[:count].rstrip(), count

    def _two_line_elided_text(self, value: str, width: int) -> str:
        text = clean_thread_text(value)
        if not text:
            return ""
        width = max(80, width)
        metrics = QFontMetrics(self.font())
        if metrics.horizontalAdvance(text) <= width:
            return text

        first, count = self._fit_text_prefix(text, width, metrics)
        remaining = text[count:].lstrip()
        if not remaining:
            return first
        if metrics.horizontalAdvance(remaining) <= width:
            return first + "\n" + remaining

        second, _count = self._fit_text_prefix(remaining, width, metrics, "...")
        return first + "\n" + second.rstrip(". ") + "..."

    def _wrapped_row(
        self,
        grid: QGridLayout,
        row: int,
        label: str,
        value: str,
        color: str = MUTED_TEXT_COLOR,
        max_lines: int | None = None,
    ) -> int:
        label_widget = QLabel(label)
        label_widget.setStyleSheet(f"color:{MUTED_TEXT_COLOR};")
        grid.addWidget(label_widget, row, 0, Qt.AlignmentFlag.AlignTop)

        display_value = value
        if max_lines == 2:
            display_value = self._two_line_elided_text(value, self.width() - 154)

        value_widget = QLabel(display_value)
        value_widget.setWordWrap(True)
        value_widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        value_widget.setStyleSheet(f"color:{color};")
        if max_lines == 2:
            value_widget.setMaximumHeight(value_widget.fontMetrics().lineSpacing() * 2 + 6)
            if display_value != value:
                value_widget.setToolTip(value)
        grid.addWidget(value_widget, row, 1, 1, 2)
        return row + 1

    def _conversation_content_row(
        self,
        grid: QGridLayout,
        row: int,
        value: str,
        color: str = TEXT_COLOR,
    ) -> int:
        display_value = self._two_line_elided_text(value, self.width() - 58)
        value_widget = QLabel(display_value)
        value_widget.setWordWrap(True)
        value_widget.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        value_widget.setStyleSheet(f"color:{color};")
        value_widget.setMaximumHeight(value_widget.fontMetrics().lineSpacing() * 2 + 6)
        if display_value != value:
            value_widget.setToolTip(value)
        grid.addWidget(value_widget, row, 0, 1, 3)
        return row + 1

    def _separator(self, grid: QGridLayout, row: int) -> int:
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("color:#393940;")
        grid.addWidget(line, row, 0, 1, 3)
        return row + 1

    def _progress_row(
        self,
        grid: QGridLayout,
        row: int,
        label: str,
        used_percent: float,
        reset_at: object,
    ) -> int:
        used_percent = max(0.0, min(100.0, used_percent))
        label_widget = QLabel(label)
        label_widget.setStyleSheet(f"color:{MUTED_TEXT_COLOR};")
        grid.addWidget(label_widget, row, 0)

        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setValue(int(round(used_percent)))
        progress.setTextVisible(False)
        color = percent_color(used_percent)
        progress.setStyleSheet(
            f"QProgressBar::chunk {{ background:{color}; border-radius:4px; }}"
        )
        grid.addWidget(progress, row, 1)

        right = QLabel(f"{100 - used_percent:.0f}%  {format_until(reset_at)}")
        right.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        right.setStyleSheet(f"color:{color}; font-family: Consolas;")
        grid.addWidget(right, row, 2)
        return row + 1

    def _timeline_row(
        self,
        grid: QGridLayout,
        row: int,
        label: str,
        start_at: object,
        end_at: object,
    ) -> int:
        label_widget = QLabel(label)
        label_widget.setStyleSheet(f"color:{MUTED_TEXT_COLOR};")
        grid.addWidget(label_widget, row, 0)

        if end_at in (None, ""):
            percent = 100.0
            right_text = "不会过期"
            color = GOOD_COLOR
        else:
            try:
                start = float(start_at or time.time())
                end = float(end_at)
                if start > 10_000_000_000:
                    start /= 1000
                if end > 10_000_000_000:
                    end /= 1000
                span = max(1.0, end - start)
                percent = max(0.0, min(100.0, (time.time() - start) * 100 / span))
                right_text = format_until(end)
                color = percent_color(percent)
            except (TypeError, ValueError):
                percent = 0.0
                right_text = "未知"
                color = DIM_TEXT_COLOR

        progress = QProgressBar()
        progress.setRange(0, 100)
        progress.setValue(int(round(percent)))
        progress.setTextVisible(False)
        progress.setStyleSheet(
            f"QProgressBar::chunk {{ background:{color}; border-radius:4px; }}"
        )
        grid.addWidget(progress, row, 1)

        right = QLabel(right_text)
        right.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        right.setStyleSheet(f"color:{color}; font-family: Consolas;")
        grid.addWidget(right, row, 2)
        return row + 1

    def _render_empty_dashboard(self, message: str) -> None:
        self._clear_layout(self.dashboard_layout)
        card = Card("Codex", "状态")
        label = QLabel(message)
        label.setWordWrap(True)
        label.setObjectName("muted")
        card.body_layout.addWidget(label, 0, 0, 1, 3)
        self._add_card(self.dashboard_layout, card)

    def _render_empty_reset(self, message: str) -> None:
        self._clear_layout(self.reset_layout)
        card = Card("额度重置", "状态")
        label = QLabel(message)
        label.setWordWrap(True)
        label.setObjectName("muted")
        card.body_layout.addWidget(label, 0, 0, 1, 3)
        self._add_card(self.reset_layout, card)

    def _render_bucket_card(self, layout: QVBoxLayout, label: str, bucket: dict) -> None:
        subtitle = ""
        if self.last_updated_text:
            subtitle = "↻ " + self.last_updated_text
        card = Card(label, subtitle, bucket.get("planType"))
        row = 0
        shown = 0
        for window in (bucket.get("primary"), bucket.get("secondary")):
            if not isinstance(window, dict):
                continue
            used = float(window.get("usedPercent") or 0)
            name = format_window_name(window.get("windowDurationMins"), shown)
            reset_at = window.get("resetsAt")
            row = self._progress_row(card.body_layout, row, f"▰ {name}", used, reset_at)
            row = self._metric_row(
                card.body_layout,
                row,
                "↻ 重置时间",
                format_time_value(reset_at),
                DIM_TEXT_COLOR,
            )
            shown += 1

        individual_limit = bucket.get("individualLimit")
        if isinstance(individual_limit, dict):
            remaining = individual_limit.get("remainingPercent")
            if remaining is not None:
                used_percent = 100.0 - max(0.0, min(100.0, float(remaining)))
                row = self._progress_row(
                    card.body_layout,
                    row,
                    "◈ Personal",
                    used_percent,
                    individual_limit.get("resetsAt"),
                )
            limit = individual_limit.get("limit")
            used = individual_limit.get("used")
            if used not in (None, "") and limit not in (None, ""):
                row = self._metric_row(
                    card.body_layout,
                    row,
                    "◇ 使用量",
                    f"{used}/{limit}",
                    MUTED_TEXT_COLOR,
                )
            row = self._metric_row(
                card.body_layout,
                row,
                "↻ 个人重置",
                format_time_value(individual_limit.get("resetsAt")),
                DIM_TEXT_COLOR,
            )

        if row == 0:
            row = self._metric_row(card.body_layout, row, "额度窗口", "未返回")

        row = self._separator(card.body_layout, row)
        credits = bucket.get("credits")
        if credits is not None:
            row = self._metric_row(
                card.body_layout,
                row,
                "$ 附加 credits",
                credits_text(credits),
                GOOD_COLOR
                if isinstance(credits, dict) and credits.get("hasCredits")
                else MUTED_TEXT_COLOR,
            )

        status_label, status_color = bucket_status(bucket)
        self._metric_row(card.body_layout, row, "● 限制", status_label, status_color)
        self._add_card(layout, card)

    def _render_account_credits_card(self, layout: QVBoxLayout, credits: object) -> None:
        card = Card("账户 credits", "$ 余额")
        if isinstance(credits, dict):
            self._metric_row(
                card.body_layout,
                0,
                "$ 状态",
                "无限额度" if credits.get("unlimited") else credits_text(credits),
                GOOD_COLOR
                if credits.get("hasCredits") or credits.get("unlimited")
                else MUTED_TEXT_COLOR,
            )
            self._metric_row(
                card.body_layout,
                1,
                "◇ hasCredits",
                "是" if credits.get("hasCredits") else "否",
                MUTED_TEXT_COLOR,
            )
        else:
            self._metric_row(card.body_layout, 0, "$ 状态", "未知", DIM_TEXT_COLOR)
        self._add_card(layout, card)

    def _render_usage_summary_card(self, layout: QVBoxLayout, limits_result: dict) -> None:
        card = Card("额度使用", "日 / 周 / 月")
        usage = quota_summary(limits_result, self.quota_history)
        parts = []
        colors = []
        for key, label in (("day", "日"), ("week", "周"), ("month", "月")):
            item = usage.get(key) or {}
            used = item.get("value")
            if used is None:
                parts.append(f"{label} --")
                colors.append(DIM_TEXT_COLOR)
            else:
                suffix = "" if item.get("source") == "direct" else "*"
                parts.append(f"{label} {used:.0f}%{suffix}")
                colors.append(percent_color(used))
        color = BAD_COLOR if BAD_COLOR in colors else WARN_COLOR if WARN_COLOR in colors else MUTED_TEXT_COLOR
        self._metric_row(card.body_layout, 0, "▰ 使用额度", " · ".join(parts), color)
        self._add_card(layout, card)

    def _render_activity_overview_card(self, layout: QVBoxLayout, thread_result: dict) -> None:
        card = self.dashboard_activity_card
        if card is None:
            card = Card("活动状态", "会话聚合")
            current_label = QLabel()
            current_label.setStyleSheet(f"color:{MUTED_TEXT_COLOR};")
            detail_label = QLabel()
            detail_label.setStyleSheet(f"color:{DIM_TEXT_COLOR};")
            card.body_layout.addWidget(QLabel("● 当前"), 0, 0)
            card.body_layout.addWidget(current_label, 0, 1, 1, 2)
            card.body_layout.addWidget(QLabel("◇ 细分"), 1, 0)
            card.body_layout.addWidget(detail_label, 1, 1, 1, 2)
            card._activity_current_label = current_label
            card._activity_detail_label = detail_label
            self.dashboard_activity_card = card
            self._add_card(layout, card)

        text, color = activity_overview_text(thread_result)
        counts = activity_breakdown(thread_result)
        detail_parts = [
            f"{label} {counts[label]}"
            for label in ("思考中", "输出中", "执行中", "运行中", "等待审批", "等待输入")
            if counts[label] > 0
        ]
        card._activity_current_label.setText(text)
        card._activity_current_label.setStyleSheet(f"color:{color}; font-family: Consolas;")
        card._activity_detail_label.setText(
            " · ".join(detail_parts) if detail_parts else "无活动会话"
        )
        card._activity_detail_label.setStyleSheet(
            f"color:{color if detail_parts else DIM_TEXT_COLOR}; font-family: Consolas;"
        )

    def _render_conversation_card_legacy(self, layout: QVBoxLayout, thread: dict, index: int) -> None:
        status_label, status_color, flags = thread_activity_info(thread)
        manual_label = (
            "需审批"
            if "waitingOnApproval" in flags
            else "需输入"
            if "waitingOnUserInput" in flags
            else "无需操作"
        )
        source_text = thread_source_label(thread)
        updated = format_age(thread.get("updatedAt"))
        title_text = thread_title(thread)
        subtitle = f"{status_label} · {updated}"
        badge = source_text if source_text != "未知" else None
        card = Card(title_text if title_text else f"活动对话 {index}", subtitle, badge)
        row = 0
        row = self._metric_row(
            card.body_layout,
            row,
            "● 状态",
            f"{status_label} · {manual_label}",
            ACCENT_COLOR if manual_label != "无需操作" else status_color,
        )
        preview = clean_thread_text(thread.get("preview"))
        if preview:
            row = self._conversation_content_row(card.body_layout, row, preview)
        cwd = clean_thread_text(thread.get("cwd"))
        if cwd:
            row = self._wrapped_row(card.body_layout, row, "◇ 项目", cwd, DIM_TEXT_COLOR)
        self._add_card(layout, card)

    def _render_conversation_page_legacy(self, layout: QVBoxLayout, thread_result: dict) -> None:
        summary = summarize_threads(thread_result)
        active_threads = [
            thread for thread in summary["active_threads"] if isinstance(thread, dict)
        ]
        active_threads.sort(key=active_thread_sort_key)

        if summary["error"]:
            card = Card("活动对话", "读取失败")
            row = 0
            row = self._metric_row(card.body_layout, row, "● 状态", "读取失败", BAD_COLOR)
            self._metric_row(
                card.body_layout,
                row,
                "◇ 原因",
                str(summary["error"]),
                DIM_TEXT_COLOR,
            )
            self._add_card(layout, card)
            return

        if not active_threads:
            card = Card("活动对话", "空闲")
            self._metric_row(
                card.body_layout,
                0,
                "◇ 对话",
                "没有正在活动的对话",
                DIM_TEXT_COLOR,
            )
            self._add_card(layout, card)
            return

        for index, thread in enumerate(active_threads, start=1):
            self._render_conversation_card(layout, thread, index)

        if summary["errors"]:
            card = Card("会话来源", "部分读取失败")
            self._wrapped_row(
                card.body_layout,
                0,
                "◇ 错误",
                str(summary["errors"][0]),
                DIM_TEXT_COLOR,
            )
            self._add_card(layout, card)

    def _conversation_aux_card(self, key: str) -> Card:
        card = self.conversation_aux_cards.get(key)
        if card is not None:
            return card

        card = Card("")
        rows = []
        for row in range(3):
            label = QLabel()
            label.setStyleSheet(f"color:{MUTED_TEXT_COLOR};")
            value = QLabel()
            value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            card.body_layout.addWidget(label, row, 0, Qt.AlignmentFlag.AlignTop)
            card.body_layout.addWidget(value, row, 1, 1, 2)
            label.setVisible(False)
            value.setVisible(False)
            rows.append((label, value))
        card._conversation_aux_rows = rows
        self.conversation_aux_cards[key] = card
        self._ensure_conversation_container().insertWidget(0, card)
        card.setVisible(False)
        return card

    def _update_conversation_aux_card(
        self,
        key: str,
        title: str,
        subtitle: str,
        rows: list[tuple[str, str, str]],
    ) -> Card:
        card = self._conversation_aux_card(key)
        card.update_header(title, subtitle)
        for index, (label_widget, value_widget) in enumerate(card._conversation_aux_rows):
            if index < len(rows):
                label, value, color = rows[index]
                label_widget.setText(label)
                value_widget.setText(value)
                value_widget.setStyleSheet(f"color:{color};")
                label_widget.setVisible(True)
                value_widget.setVisible(True)
            else:
                label_widget.setVisible(False)
                value_widget.setVisible(False)
        return card

    def _place_conversation_card(self, layout: QVBoxLayout, card: Card, index: int) -> None:
        layout.removeWidget(card)
        layout.insertWidget(index, card)
        card.setVisible(True)

    def _hide_conversation_widgets(self) -> None:
        for card in self.conversation_cards.values():
            card.setVisible(False)
        for card in self.conversation_aux_cards.values():
            card.setVisible(False)

    def _render_conversation_card(self, layout: QVBoxLayout, thread: dict, index: int) -> None:
        status_label, status_color, flags = thread_activity_info(thread)
        manual_label = (
            "需审批"
            if "waitingOnApproval" in flags
            else "需输入"
            if "waitingOnUserInput" in flags
            else "无需操作"
        )
        source_text = thread_source_label(thread)
        updated = format_age(thread.get("updatedAt"))
        title_text = thread_title(thread)
        subtitle = f"{status_label} · {updated}"
        badge = source_text if source_text != "未知" else None
        identity = thread_identity(thread)
        card = self.conversation_cards.get(identity)
        if card is None:
            card = ConversationCard()
            self.conversation_cards[identity] = card

        preview = self._two_line_elided_text(
            clean_thread_text(thread.get("preview")),
            self.width() - 58,
        )
        cwd = clean_thread_text(thread.get("cwd"))
        card.update_conversation(
            title_text if title_text else f"活动对话 {index}",
            subtitle,
            badge,
            f"{status_label} · {manual_label}",
            ACCENT_COLOR if manual_label != "无需操作" else status_color,
            preview,
            cwd,
        )
        self._place_conversation_card(layout, card, index - 1)

    def _render_conversation_page(self, layout: QVBoxLayout, thread_result: dict) -> None:
        summary = summarize_threads(thread_result)
        self._hide_conversation_widgets()
        active_threads = [
            thread for thread in summary["active_threads"] if isinstance(thread, dict)
        ]
        active_threads.sort(key=active_thread_sort_key)

        if summary["error"]:
            card = self._update_conversation_aux_card(
                "error",
                "活动对话",
                "读取失败",
                [
                    ("● 状态", "读取失败", BAD_COLOR),
                    ("◇ 原因", str(summary["error"]), DIM_TEXT_COLOR),
                ],
            )
            self._place_conversation_card(layout, card, 0)
            return

        if not active_threads:
            card = self._update_conversation_aux_card(
                "empty",
                "活动对话",
                "空闲",
                [("◇ 对话", "没有正在活动的对话", DIM_TEXT_COLOR)],
            )
            self._place_conversation_card(layout, card, 0)
            return

        for index, thread in enumerate(active_threads, start=1):
            self._render_conversation_card(layout, thread, index)

        if summary["errors"]:
            card = self._update_conversation_aux_card(
                "source_error",
                "会话来源",
                "部分读取失败",
                [("◇ 错误", str(summary["errors"][0]), DIM_TEXT_COLOR)],
            )
            self._place_conversation_card(layout, card, len(active_threads))

    def _render_dashboard(
        self,
        account_result: dict,
        limits_result: dict,
        thread_result: dict | None = None,
    ) -> None:
        self._clear_layout(self.dashboard_layout)
        account = account_result.get("account") or {}
        plan = account.get("planType") or "未知套餐"
        email = account.get("email") or "未知账户"
        self.account_label.setText(f"{email} · {plan}")

        buckets = iter_buckets(limits_result)
        if not buckets:
            card = Card("Codex", "额度窗口")
            label = QLabel("服务没有返回可显示的额度窗口。")
            label.setWordWrap(True)
            label.setObjectName("muted")
            card.body_layout.addWidget(label, 0, 0, 1, 3)
            self._add_card(self.dashboard_layout, card)
            self._render_usage_summary_card(self.dashboard_layout, limits_result)
            self._render_dashboard_activity_status(thread_result or {})
            global_credits = limits_result.get("credits")
            if global_credits is not None:
                self._render_account_credits_card(self.dashboard_layout, global_credits)
            return

        for bucket_id, bucket in buckets.items():
            if isinstance(bucket, dict):
                label = bucket.get("limitName") or bucket_id or "Codex"
                self._render_bucket_card(self.dashboard_layout, str(label), bucket)

        self._render_usage_summary_card(self.dashboard_layout, limits_result)
        self._render_dashboard_activity_status(thread_result or {})

        global_credits = limits_result.get("credits")
        if global_credits is not None:
            self._render_account_credits_card(self.dashboard_layout, global_credits)

    def _render_reset_view(self, limits_result: dict) -> None:
        self._clear_layout(self.reset_layout)
        reset_credits = limits_result.get("rateLimitResetCredits")
        if not isinstance(reset_credits, dict):
            self._render_empty_reset("后端未返回可用额度重置信息。")
            return

        card = Card(
            "额度重置",
            "可用次数",
            action_text="▴" if self.show_reset_details else "▾",
            action_callback=lambda checked=False: self._toggle_reset_details(),
            action_tooltip="折叠额度重置明细"
            if self.show_reset_details
            else "展开额度重置明细",
        )
        self.reset_detail_button = card.action_button
        row = 0
        row = self._metric_row(
            card.body_layout,
            row,
            "◆ 可用次数",
            str(reset_credits.get("availableCount", 0)),
            GOOD_COLOR,
        )

        available_at = find_first_value(
            reset_credits,
            (
                "availableAt",
                "availableAfter",
                "availableFrom",
                "nextAvailableAt",
                "nextRefillAt",
                "refillsAt",
                "rechargesAt",
                "resetsAt",
                "resetAt",
            ),
        )
        if available_at not in (None, ""):
            row = self._metric_row(
                card.body_layout,
                row,
                "↻ 充值可用",
                format_time_value(available_at),
                MUTED_TEXT_COLOR,
            )

        if not self.show_reset_details:
            self._metric_row(card.body_layout, row, "▸ 明细", "已折叠", DIM_TEXT_COLOR)
            self._add_card(self.reset_layout, card)
            return

        details = available_reset_credits(reset_credits)
        if not details:
            expires_at = find_first_value(
                reset_credits,
                (
                    "expiresAt",
                    "expireAt",
                    "expirationAt",
                    "expirationTime",
                    "validUntil",
                    "validThrough",
                    "usableUntil",
                    "useBefore",
                    "useBy",
                ),
            )
            if expires_at not in (None, ""):
                self._metric_row(
                    card.body_layout,
                    row,
                    "◷ 使用期限",
                    format_time_value(expires_at),
                    MUTED_TEXT_COLOR,
                )
            else:
                self._metric_row(card.body_layout, row, "◷ 明细", "后端未返回", DIM_TEXT_COLOR)
            self._add_card(self.reset_layout, card)
            return

        for index, credit in enumerate(details[:3], start=1):
            if row > 1:
                row = self._separator(card.body_layout, row)
            title = credit.get("title") or credit.get("resetType") or f"重置额度 {index}"
            status = credit.get("status") or "unknown"
            row = self._metric_row(card.body_layout, row, f"◆ {title}", status, GOOD_COLOR)
            row = self._metric_row(
                card.body_layout,
                row,
                "↻ 获得时间",
                format_time_value(credit.get("grantedAt")),
                DIM_TEXT_COLOR,
            )
            row = self._metric_row(
                card.body_layout,
                row,
                "◷ 使用期限",
                "不会过期"
                if credit.get("expiresAt") is None
                else format_time_value(credit.get("expiresAt")),
                MUTED_TEXT_COLOR,
            )
            row = self._timeline_row(
                card.body_layout,
                row,
                "▰ 有效期",
                credit.get("grantedAt"),
                credit.get("expiresAt"),
            )

        remaining_count = len(details) - 3
        if remaining_count > 0:
            self._metric_row(
                card.body_layout,
                row,
                "▸ 更多",
                f"还有 {remaining_count} 条可用重置额度",
                DIM_TEXT_COLOR,
            )
        self._add_card(self.reset_layout, card)

    def _show_view(self, view_name: str, save: bool = True) -> None:
        if view_name == "about":
            view_name = "settings"
        if view_name not in {"dashboard", "sessions", "reset", "settings"}:
            view_name = "dashboard"
        self.active_view = view_name
        index = {"dashboard": 0, "sessions": 1, "reset": 2, "settings": 3}[view_name]
        self.stack.setCurrentIndex(index)
        for name, button in self.nav_buttons.items():
            button.setChecked(name == view_name)
        if save:
            self._schedule_save_settings()

    def _apply_saved_geometry(self) -> None:
        width, height, x, y = parse_geometry(self.settings.get("geometry"))
        self.resize(max(MIN_WINDOW_WIDTH, width), max(MIN_WINDOW_HEIGHT, height))
        if x is not None and y is not None:
            self.move(x, y)

    def _geometry_string(self) -> str:
        return f"{self.width()}x{self.height()}+{self.x()}+{self.y()}"

    def _apply_topmost(self, checked: bool, initial: bool = False) -> None:
        self.topmost_state = checked
        if hasattr(self, "topmost_button") and self.topmost_button.isChecked() != checked:
            self.topmost_button.blockSignals(True)
            self.topmost_button.setChecked(checked)
            self.topmost_button.blockSignals(False)
        if hasattr(self, "topmost_checkbox") and self.topmost_checkbox.isChecked() != checked:
            self.topmost_checkbox.blockSignals(True)
            self.topmost_checkbox.setChecked(checked)
            self.topmost_checkbox.blockSignals(False)

        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        if not initial:
            self.show()
            self._schedule_save_settings()

    def _apply_opacity(self, value: int) -> None:
        self.opacity_label.setText(f"{value}%")
        self.setWindowOpacity(1.0)
        self._apply_style()
        self._schedule_save_settings()

    def _apply_startup(self, checked: bool) -> None:
        try:
            set_startup_enabled(checked)
        except OSError as exc:
            if hasattr(self, "startup_checkbox"):
                self.startup_checkbox.blockSignals(True)
                self.startup_checkbox.setChecked(is_startup_enabled())
                self.startup_checkbox.blockSignals(False)
            QMessageBox.warning(self, "开机自启设置失败", str(exc))

    def _update_reset_toggle_text(self) -> None:
        text = "▴" if self.show_reset_details else "▾"
        if getattr(self, "reset_detail_button", None) is not None:
            self.reset_detail_button.setText(text)
            self.reset_detail_button.setToolTip(
                "折叠额度重置明细" if self.show_reset_details else "展开额度重置明细"
            )

    def _toggle_reset_details(self) -> None:
        self.show_reset_details = not self.show_reset_details
        self._update_reset_toggle_text()
        if self.latest_limits_result is not None:
            self._render_reset_view(self.latest_limits_result)
        self._schedule_save_settings()

    def _schedule_save_settings(self) -> None:
        if hasattr(self, "save_timer"):
            self.save_timer.start(400)
            return
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self._save_settings)
        self.save_timer.start(400)

    def _save_settings(self) -> None:
        save_settings(
            {
                "geometry": self._geometry_string(),
                "topmost": self.topmost_state,
                "opacity": coerce_opacity(
                    self.opacity_slider.value()
                    if hasattr(self, "opacity_slider")
                    else self.settings.get("opacity", 1.0)
                ),
                "showResetDetails": self.show_reset_details,
                "activeView": self.active_view,
            }
        )

    def refresh_data(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        self.snapshot_generation += 1
        snapshot_generation = self.snapshot_generation
        self.refresh_button.setEnabled(False)
        self.activity_label.setText("刷新中")
        self.activity_label.setStyleSheet(f"color:{MUTED_TEXT_COLOR};")
        if self.latest_limits_result is None:
            self._render_empty_dashboard("正在连接 Codex...")
            self._render_conversation_status(self.latest_thread_result)
        QTimer.singleShot(
            SNAPSHOT_SLOW_SECONDS * 1000,
            lambda generation=snapshot_generation: self._mark_snapshot_slow(generation),
        )
        threading.Thread(
            target=self._load_snapshot,
            daemon=True,
        ).start()

    def _mark_snapshot_slow(self, generation: int) -> None:
        if not self.refreshing or generation != self.snapshot_generation:
            return
        self.activity_label.setText("连接中")
        self.activity_label.setStyleSheet(f"color:{WARN_COLOR};")
        if self.latest_limits_result is None:
            self._render_empty_dashboard("正在连接 Codex，app-server 响应较慢...")
            self._render_conversation_status(self.latest_thread_result)

    def _load_snapshot(self) -> None:
        try:
            account, limits, threads = self.client.get_snapshot()
            self.signals.loaded.emit(account, limits, threads)
        except Exception as exc:
            LOGGER.exception("额度快照加载失败")
            self.signals.failed.emit(str(exc))

    def _read_account_for_error_state(self, error: str = "") -> dict | None:
        try:
            self.client.start()
            return self.client.read_account(refresh_token=False)
        except Exception:
            LOGGER.exception("读取账号错误状态失败：%s", error)
            return None

    def refresh_activity(self) -> None:
        if self.refreshing_activity:
            return
        self.refreshing_activity = True
        base_result = self.latest_thread_result
        threading.Thread(
            target=self._load_activity_snapshot,
            args=(base_result,),
            daemon=True,
        ).start()

    def _load_activity_snapshot(self, base_result: dict | None = None) -> None:
        try:
            session_scan = self.client.read_session_scan()
            threads = merge_current_session_threads(
                base_result,
                session_scan=session_scan,
            )
            self.signals.activity_loaded.emit(threads)
        except Exception:
            LOGGER.exception("活动快照加载失败")
            self.signals.activity_loaded.emit(
                {
                    "data": [],
                    "errors": ["本地Chating扫描失败"],
                    "sourceCount": 0,
                    "_scannedAt": time.time(),
                }
            )

    def _apply_snapshot(
        self,
        account_result: dict,
        limits_result: dict,
        thread_result: dict,
    ) -> None:
        try:
            account = validate_account(account_result)
        except Exception as exc:
            self._apply_error(str(exc))
            return

        self.latest_account_result = account_result
        self.latest_limits_result = limits_result
        thread_result = self.latest_thread_result or thread_result
        self.latest_thread_result = thread_result
        self.quota_history = update_quota_history(self.quota_history, limits_result)
        self.last_updated_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._sync_account_controls(True, account, limits_result)
        self._render_dashboard(account_result, limits_result, thread_result)
        self._render_reset_view(limits_result)
        status, color = snapshot_status(limits_result)
        self.activity_label.setText(status)
        self.activity_label.setStyleSheet(f"color:{color};")
        self.refreshing = False
        self._sync_account_controls(True, account, limits_result)

        low_items = collect_low_items(limits_result)
        current_keys = set(low_items)
        new_warnings = current_keys - self.warned_keys
        self.warned_keys = current_keys
        if new_warnings:
            QMessageBox.warning(self, "Codex 额度提醒", "\n".join(sorted(new_warnings)))

    def _apply_activity_snapshot(self, thread_result: dict) -> None:
        try:
            self.latest_thread_result = thread_result
            self._render_conversation_status(thread_result)
            if self.latest_limits_result is not None:
                self._render_dashboard_activity_status(thread_result)
        except Exception:
            LOGGER.exception("活动快照渲染失败")
        finally:
            self.refreshing_activity = False

    def _rerender_conversations_for_width(self) -> None:
        if self.latest_thread_result is None or self.conversation_layout is None:
            return
        self._render_conversation_status(self.latest_thread_result)

    def _apply_error(self, error: str) -> None:
        account_result = (
            self._read_account_for_error_state(error)
            if is_rate_limits_error(error) or is_auth_error(error)
            else None
        )
        account = account_result.get("account") if isinstance(account_result, dict) else None
        account_available = isinstance(account, dict)
        if account_available:
            self.latest_account_result = account_result
            self._sync_account_controls(True, account, None)
            self._render_empty_dashboard("账号已连接，但额度信息读取失败：\n" + error)
        else:
            self._sync_account_controls(False, error=error)
            self._render_empty_dashboard(error)
        self._render_conversation_status(self.latest_thread_result)
        self._render_empty_reset("刷新失败，暂时没有额度重置数据。")
        self.activity_label.setText("额度读取失败" if account_available else "读取失败")
        self.activity_label.setStyleSheet(f"color:{WARN_COLOR if account_available else BAD_COLOR};")
        self.refreshing = False
        if account_available:
            self._sync_account_controls(True, account, None)
        else:
            self._sync_account_controls(False, error=error)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.titlebar.geometry().contains(
            event.position().toPoint()
        ):
            self.drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self.drag_offset = None
        self._schedule_save_settings()
        super().mouseReleaseEvent(event)

    def resizeEvent(self, event) -> None:
        self._update_window_mask()
        self._schedule_save_settings()
        if hasattr(self, "conversation_resize_timer"):
            self.conversation_resize_timer.start(80)
        super().resizeEvent(event)

    def _update_window_mask(self) -> None:
        path = QPainterPath()
        path.addRoundedRect(
            0,
            0,
            self.width(),
            self.height(),
            WINDOW_CORNER_RADIUS,
            WINDOW_CORNER_RADIUS,
        )
        self.setMask(QRegion(path.toFillPolygon().toPolygon()))

    def moveEvent(self, event) -> None:
        self._schedule_save_settings()
        super().moveEvent(event)

    def closeEvent(self, event) -> None:
        self._save_settings()
        if self.tray_icon is not None:
            self.tray_icon.hide()
        self.client.close()
        super().closeEvent(event)
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)


def main() -> int:
    setup_diagnostics()
    LOGGER.info("Codex Monitor starting")
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = CodexBalanceMonitor()
    window.show()
    exit_code = app.exec()
    LOGGER.info("Codex Monitor stopped: exit_code=%s", exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
