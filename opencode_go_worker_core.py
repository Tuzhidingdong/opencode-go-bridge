from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

# Process and Job Object utilities for Windows lifecycle management
def _create_kill_on_close_job_object() -> Any | None:
    """Create a Windows Job Object configured to terminate all child processes on close."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryLimit", ctypes.c_size_t),
                ("PeakJobMemoryLimit", ctypes.c_size_t),
            ]

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE

        res = kernel32.SetInformationJobObject(
            job,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not res:
            kernel32.CloseHandle(job)
            return None
        return job
    except Exception:
        return None


def _assign_process_to_job(job_handle: Any, proc: subprocess.Popen[Any]) -> bool:
    """Assign a subprocess to the Windows Job Object."""
    if os.name != "nt" or not job_handle:
        return False
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        proc_handle = getattr(proc, "_handle", None)
        if proc_handle:
            return bool(kernel32.AssignProcessToJobObject(job_handle, int(proc_handle)))
    except Exception:
        pass
    return False


def _close_job_handle(job_handle: Any) -> None:
    """Safely close a Windows Job Object handle."""
    if os.name == "nt" and job_handle:
        try:
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CloseHandle(job_handle)
        except Exception:
            pass


def _get_process_create_time(pid: int | None) -> float | None:
    """Retrieve process creation timestamp in seconds since epoch."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h_proc:
                return None
            try:
                creation_time = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel_time = wintypes.FILETIME()
                user_time = wintypes.FILETIME()
                success = kernel32.GetProcessTimes(
                    h_proc,
                    ctypes.byref(creation_time),
                    ctypes.byref(exit_time),
                    ctypes.byref(kernel_time),
                    ctypes.byref(user_time),
                )
                if not success:
                    return None
                ft_val = (creation_time.dwHighDateTime << 32) + creation_time.dwLowDateTime
                return (ft_val - 116444736000000000) / 10000000.0
            finally:
                kernel32.CloseHandle(h_proc)
        except Exception:
            pass

    try:
        import psutil
        return psutil.Process(pid).create_time()
    except Exception:
        pass
    return None


def _is_process_alive_and_matching(
    pid: int | None,
    expected_start_time: float | None,
    tolerance: float = 2.0,
) -> bool:
    """Verify that the process with the given PID is currently running and matches expected creation time."""
    if pid is None or pid <= 0:
        return False
    current_time = _get_process_create_time(pid)
    if current_time is None:
        return False
    if expected_start_time is not None:
        if abs(current_time - expected_start_time) > tolerance:
            # PID reused by another process
            return False
    return True


APP_NAME = "OpenCodeGoWorkerManager"
DEFAULT_MAX_CONCURRENT = 3
MAX_CONCURRENT_HARD = 6
API_TIMEOUT_SECONDS = 45

ROLE_POLICIES: dict[str, dict[str, Any]] = {
    "writer": {
        "agent": "build",
        "edit": "allow",
        "shell": "ask",
        "description": "可读取并编辑当前项目；Shell 命令需逐项审批且必须限定在当前项目内。",
    },
    "reviewer": {
        "agent": "explore",
        "edit": "deny",
        "shell": "deny",
        "description": "只读审查；不能修改项目文件，也不能运行 Shell 命令。",
    },
    "reviser": {
        "agent": "build",
        "edit": "allow",
        "shell": "ask",
        "description": "可读取并编辑当前项目；Shell 命令需逐项审批且必须限定在当前项目内。",
    },
    "analyst": {
        "agent": "explore",
        "edit": "deny",
        "shell": "deny",
        "description": "只读分析；不能修改项目文件，也不能运行 Shell 命令。",
    },
    "general": {
        "agent": "build",
        "edit": "ask",
        "shell": "ask",
        "description": "读取默认允许，文件修改与 Shell 命令均需审批。",
    },
}


class ErrorCategory:
    APPLICATION_ERROR = "APPLICATION_ERROR"     # Parameter/schema mismatch, logic validation errors
    SUBMISSION_UNKNOWN = "SUBMISSION_UNKNOWN"   # Job submission reached server but status unconfirmed
    TRANSPORT_ERROR = "TRANSPORT_ERROR"         # HTTP/pipe/network transport drop
    PROCESS_EXITED = "PROCESS_EXITED"           # Monitored child process exited unexpectedly
    TIMEOUT = "TIMEOUT"                         # Request timed out
    RECOVERY_FAILED = "RECOVERY_FAILED"         # Circuit breaker tripped or recovery attempts exhausted


def _compute_file_sha256(path: str) -> str:
    try:
        import hashlib
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return "unknown"


VERSION = "0.3.0"
CORE_VERSION = VERSION
CORE_START_TIME = datetime.now(timezone.utc).isoformat()
CORE_BUILD_HASH = _compute_file_sha256(__file__)[:12] if os.path.exists(__file__) else "dev"


class CircuitBreaker:
    """Three-step circuit breaker for backend communication failures.

    Prevents supervisor from getting stuck in infinite retry loops on transport errors.
    Deterministic application errors (like invalid parameters) do not trip the breaker.
    """
    def __init__(self, failure_threshold: int = 3, reset_timeout: float = 30.0) -> None:
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self.lock = threading.RLock()

    def record_success(self) -> None:
        with self.lock:
            self.failure_count = 0
            self.state = "CLOSED"

    def record_failure(self, category: str | None) -> bool:
        """Record failure and return True if the circuit breaker was tripped into OPEN state."""
        if category not in (ErrorCategory.TRANSPORT_ERROR, ErrorCategory.TIMEOUT):
            return False
        with self.lock:
            now = time.time()
            if self.state == "OPEN" and (now - self.last_failure_time) > self.reset_timeout:
                self.state = "HALF_OPEN"
                self.failure_count = 1
            else:
                self.failure_count += 1
            self.last_failure_time = now
            if self.failure_count >= self.failure_threshold:
                self.state = "OPEN"
                return True
            return False

    def is_tripped(self) -> tuple[bool, str]:
        with self.lock:
            if self.state == "OPEN":
                now = time.time()
                if (now - self.last_failure_time) > self.reset_timeout:
                    self.state = "HALF_OPEN"
                    return False, ""
                rem = int(self.reset_timeout - (now - self.last_failure_time))
                return True, f"通信熔断器已触发（连续失败达到 {self.failure_threshold} 次）。请在 {rem}s 后重试，切勿强杀 MCP 进程。"
            return False, ""



class BridgeError(RuntimeError):
    def __init__(
        self,
        message: str,
        category: str | None = None,
        user_message: str | None = None,
        raw_error: Any = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.user_message = user_message or message
        self.raw_error = raw_error
        self.status_code = status_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": str(self),
            "error_category": self.category or ErrorCategory.APPLICATION_ERROR,
            "user_message": self.user_message,
            "status_code": self.status_code,
            "retryable": self.category in (ErrorCategory.TRANSPORT_ERROR, ErrorCategory.TIMEOUT),
            "circuit_break": self.category == ErrorCategory.RECOVERY_FAILED,
        }


AUTH_KEY_EXPIRED_MESSAGE = (
    "OpenCode Go API Key 已过期，请前往 OpenCode Console 创建或更新 API Key，"
    "然后在 OpenCode 中重新连接。当前任务未能完成。"
)
AUTH_FAILED_MESSAGE = (
    "OpenCode Go 认证失败，可能是 API Key 无效、已撤销、订阅状态异常或 Key 已过期。"
    "请检查 OpenCode Console。"
)
RATE_LIMITED_MESSAGE = "OpenCode Go 达到额度或请求频率限制 (Rate Limited)，请稍后重试或检查 Console 配额。"
SERVER_ERROR_MESSAGE = "OpenCode 服务端故障，请稍后重试。"
NETWORK_ERROR_MESSAGE = "无法连接 OpenCode 服务，网络连接失败。"

ERROR_EXPIRED_PHRASES = (
    "this api key has expired",
    "api key expired",
    "api key has expired",
)


@dataclass
class ErrorInfo:
    category: str
    user_message: str
    raw_detail: str
    status_code: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "message": self.user_message,
            "detail": self.raw_detail,
            "status_code": self.status_code,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


def extract_error_text(payload: Any) -> str:
    """Extract all text from nested error structures (JSON string, dict, list)."""
    if payload is None:
        return ""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        payload_str = payload.strip()
        if (payload_str.startswith("{") and payload_str.endswith("}")) or (
            payload_str.startswith("[") and payload_str.endswith("]")
        ):
            try:
                parsed = json.loads(payload_str)
                return extract_error_text(parsed)
            except Exception:
                return payload_str
        return payload_str

    if isinstance(payload, dict):
        texts = []
        for key in ("error", "message", "msg", "detail", "reason", "description", "title", "code"):
            if key in payload:
                val = payload[key]
                if isinstance(val, (dict, list)):
                    texts.append(extract_error_text(val))
                elif val is not None:
                    texts.append(str(val))
        if texts:
            return " | ".join(t for t in texts if t)
        for val in payload.values():
            if isinstance(val, (dict, list, str)):
                t = extract_error_text(val)
                if t:
                    texts.append(t)
        if texts:
            return " | ".join(texts)
        try:
            return json.dumps(payload, ensure_ascii=False)
        except Exception:
            return str(payload)

    if isinstance(payload, list):
        return " | ".join(extract_error_text(item) for item in payload)

    return str(payload)


def classify_error(
    status_code: int | None = None,
    raw_payload: Any = None,
    exc: Exception | None = None,
) -> ErrorInfo:
    raw_text = extract_error_text(raw_payload)
    exc_text = str(exc) if exc else ""
    combined_text = f"{raw_text} {exc_text}".strip()
    text_lower = combined_text.lower()

    # A. Explicitly expired (highest priority)
    for phrase in ERROR_EXPIRED_PHRASES:
        if phrase in text_lower:
            return ErrorInfo(
                category="AUTH_KEY_EXPIRED",
                user_message=AUTH_KEY_EXPIRED_MESSAGE,
                raw_detail=combined_text or phrase,
                status_code=status_code,
            )

    # B. General Auth Failed (HTTP 401 / 403 without explicit expiration)
    if status_code in (401, 403):
        return ErrorInfo(
            category="AUTH_FAILED",
            user_message=AUTH_FAILED_MESSAGE,
            raw_detail=combined_text,
            status_code=status_code,
        )

    # Check if text clearly indicates 401 / 403 or unauthorized / forbidden / invalid key
    if any(k in text_lower for k in ("unauthorized", "invalid api key", "forbidden", "access denied", "authentication failed", "subscription error")):
        return ErrorInfo(
            category="AUTH_FAILED",
            user_message=AUTH_FAILED_MESSAGE,
            raw_detail=combined_text,
            status_code=status_code or 401,
        )

    # C. Other Failures
    if status_code == 429 or "429" in combined_text or "rate limit" in text_lower or "too many requests" in text_lower:
        return ErrorInfo(
            category="RATE_LIMITED",
            user_message=RATE_LIMITED_MESSAGE,
            raw_detail=combined_text,
            status_code=status_code or 429,
        )

    if (status_code is not None and 500 <= status_code <= 599) or ("http 5" in text_lower):
        return ErrorInfo(
            category="SERVER_ERROR",
            user_message=SERVER_ERROR_MESSAGE,
            raw_detail=combined_text,
            status_code=status_code or 500,
        )

    if (
        isinstance(exc, (urllib.error.URLError, ConnectionError, TimeoutError, socket.error))
        or "无法连接" in combined_text
        or "connection refused" in text_lower
        or "timed out" in text_lower
        or "network" in text_lower
    ):
        return ErrorInfo(
            category="NETWORK_ERROR",
            user_message=NETWORK_ERROR_MESSAGE,
            raw_detail=combined_text,
            status_code=status_code,
        )

    return ErrorInfo(
        category="API_ERROR",
        user_message=f"OpenCode API 错误：{combined_text[:200]}" if combined_text else "OpenCode 未知错误",
        raw_detail=combined_text,
        status_code=status_code,
    )


def state_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        root = Path(appdata)
    else:
        root = Path.home() / ".config"
    target = root / APP_NAME
    target.mkdir(parents=True, exist_ok=True)
    return target


def jobs_db_path() -> Path:
    return state_dir() / "worker_jobs.sqlite3"


def _antigravity_ledger_path() -> Path:
    appdata = os.environ.get("APPDATA")
    root = Path(appdata) if appdata else Path.home()
    return root / "AntigravityTokenMeter" / "task_usage.jsonl"


def _opencode_db_path() -> Path | None:
    candidates: list[Path] = [
        Path.home() / ".local" / "share" / "opencode" / "opencode.db",
    ]
    localappdata = os.environ.get("LOCALAPPDATA")
    if localappdata:
        candidates.append(Path(localappdata) / "opencode" / "opencode.db")
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "opencode" / "opencode.db")
    for cand in candidates:
        if cand.is_file():
            return cand
    return None


def _parse_iso_timestamp(ts_str: Any) -> float | None:
    if not isinstance(ts_str, str) or not ts_str.strip():
        return None
    cleaned = ts_str.strip()
    try:
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        return dt.timestamp()
    except Exception:
        return None


def canonical_project_root(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise BridgeError("project_root 必须是当前 Antigravity Project 的绝对路径。")
    candidate = Path(os.path.expandvars(os.path.expanduser(raw.strip())))
    if not candidate.is_absolute():
        # Windows drive paths are absolute only when running on Windows.
        raise BridgeError("project_root 不是绝对路径。必须传入当前 Antigravity Project 的根目录，不能使用相对路径。")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BridgeError(f"Project 根目录不存在或无法解析：{raw!r}；{exc}") from exc
    if not resolved.is_dir():
        raise BridgeError(f"Project 根路径不是目录：{str(resolved)}")
    return str(resolved)


def _unwrap_data(value: Any) -> Any:
    if isinstance(value, dict) and "data" in value:
        return value.get("data")
    return value


def _server_info(value: Any) -> dict[str, Any]:
    """Reject SPA HTML/fallback pages masquerading as a healthy API response."""
    info = _unwrap_data(value)
    if not isinstance(info, dict) or not info:
        raise BridgeError(
            "OpenCode V2 Server info API 未返回 JSON 对象；拒绝将网页 HTML 或空响应判定为健康。"
        )
    return info


def _safe_model(model: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "id", "providerID", "name", "status", "description", "cost",
        "limit", "capabilities", "variants", "context", "release_date",
    )
    return {k: model[k] for k in keep if k in model}


def _compact_model(model: dict[str, Any]) -> dict[str, Any]:
    keep = ("id", "providerID", "name", "status", "cost", "limit")
    return {k: model[k] for k in keep if k in model}


def _extract_text(parts: Any) -> str:
    """Extract user-visible text from legacy parts or V2 content."""
    if isinstance(parts, str):
        return parts.strip()

    if isinstance(parts, dict):
        parts = [parts]

    if not isinstance(parts, list):
        return ""

    output: list[str] = []

    for part in parts:
        if isinstance(part, str):
            output.append(part)
            continue

        if not isinstance(part, dict):
            continue

        part_type = part.get("type")
        text = part.get("text")

        # Do not expose reasoning or tool payloads as the final answer.
        if part_type == "text" and isinstance(text, str):
            output.append(text)
        elif part_type in (None, "") and isinstance(text, str):
            output.append(text)

    return "\n".join(output).strip()


def _message_info(item: Any) -> dict[str, Any] | None:
    """Normalize legacy {info, parts} and V2 projected root-level messages."""
    if not isinstance(item, dict):
        return None

    nested = item.get("info")
    if isinstance(nested, dict):
        return nested

    return item


def _is_assistant_message(item: Any) -> bool:
    info = _message_info(item)
    if not isinstance(info, dict):
        return False

    return (
        info.get("role") == "assistant"
        or info.get("type") == "assistant"
    )


def _message_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""

    if "parts" in item:
        return _extract_text(item.get("parts"))

    return _extract_text(item.get("content"))


def _token_totals(messages: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {"input": 0, "output": 0, "reasoning": 0, "cache_read": 0, "cache_write": 0, "cost_usd": 0.0, "assistant_messages": 0}
    for item in messages:
        info = _message_info(item)
        if not isinstance(info, dict) or not _is_assistant_message(item):
            continue
        tokens = info.get("tokens") or {}
        if not isinstance(tokens, dict):
            tokens = {}
        cache = tokens.get("cache") or {}
        if not isinstance(cache, dict):
            cache = {}
        totals["input"] += int(tokens.get("input") or 0)
        totals["output"] += int(tokens.get("output") or 0)
        totals["reasoning"] += int(tokens.get("reasoning") or 0)
        totals["cache_read"] += int(cache.get("read") or 0)
        totals["cache_write"] += int(cache.get("write") or 0)
        totals["cost_usd"] += float(info.get("cost") or 0.0)
        totals["assistant_messages"] += 1
    return totals


class JobStore:
    def __init__(self) -> None:
        self.path = jobs_db_path()
        self.lock = threading.RLock()
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a transaction-scoped connection and always close its handle.

        sqlite3.Connection.__exit__ commits/rolls back but does not close the
        connection. Explicit close is required on Windows so TemporaryDirectory
        cleanup and package upgrades are not blocked by open SQLite handles.
        """
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            with con:
                yield con
        finally:
            con.close()

    def _init_db(self) -> None:
        with self.lock, self._connect() as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                """CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    project_root TEXT NOT NULL,
                    role TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    last_error TEXT
                )"""
            )
            # Safe migration: if legacy jobs table has UNIQUE constraint on session_id,
            # migrate to non-unique schema so multiple jobs can reuse the same session.
            try:
                cur = con.cursor()
                cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'")
                row = cur.fetchone()
                if row and row[0] and ("session_id TEXT NOT NULL UNIQUE" in row[0] or "session_id TEXT UNIQUE" in row[0]):
                    con.execute("PRAGMA foreign_keys=OFF")
                    con.execute("""CREATE TABLE IF NOT EXISTS jobs_migration_temp (
                        job_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        project_root TEXT NOT NULL,
                        role TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        state TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        last_error TEXT
                    )""")
                    con.execute("INSERT INTO jobs_migration_temp SELECT * FROM jobs")
                    con.execute("DROP TABLE jobs")
                    con.execute("ALTER TABLE jobs_migration_temp RENAME TO jobs")
                    con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_session_id ON jobs(session_id)")
                    con.execute("PRAGMA foreign_keys=ON")
            except Exception:
                pass
            con.execute(
                """CREATE TABLE IF NOT EXISTS job_process (
                    job_id TEXT PRIMARY KEY,
                    server_pid INTEGER,
                    server_start_time REAL,
                    terminal_result TEXT,
                    permission_scope_json TEXT
                )"""
            )
            job_process_columns = {
                row[1] for row in con.execute("PRAGMA table_info(job_process)").fetchall()
            }
            if "permission_scope_json" not in job_process_columns:
                con.execute("ALTER TABLE job_process ADD COLUMN permission_scope_json TEXT")
            con.execute(
                """CREATE TABLE IF NOT EXISTS worker_slots (
                    project_root TEXT NOT NULL,
                    worker_slot TEXT NOT NULL,
                    current_session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    summary TEXT,
                    summary_updated_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY(project_root, worker_slot)
                )"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS session_routing_decisions (
                    decision_id TEXT PRIMARY KEY,
                    project_root TEXT NOT NULL,
                    worker_slot TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    previous_session_id TEXT,
                    selected_session_id TEXT NOT NULL,
                    selected_strategy TEXT NOT NULL,
                    decision_timestamp REAL NOT NULL,
                    estimated_cost TEXT,
                    actual_usage TEXT,
                    prediction_confidence REAL,
                    decision_reason TEXT,
                    task_outcome TEXT,
                    is_rework INTEGER DEFAULT 0,
                    data_quality TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS session_locks (
                    session_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    owner_instance TEXT NOT NULL,
                    acquired_at REAL NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    status TEXT NOT NULL
                )"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS benchmark_manifests (
                    manifest_id TEXT PRIMARY KEY,
                    job_id TEXT,
                    session_id TEXT,
                    benchmark_name TEXT NOT NULL,
                    benchmark_phase TEXT NOT NULL,
                    prompt_file TEXT,
                    prompt_sha256 TEXT,
                    input_file TEXT,
                    input_sha256 TEXT,
                    model_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    session_strategy TEXT NOT NULL,
                    parameters_json TEXT,
                    created_at REAL NOT NULL
                )"""
            )

    def add(self, job: dict[str, Any]) -> None:
        now = time.time()
        initial_state = job.get("state", "submitted")
        with self.lock, self._connect() as con:
            con.execute(
                "INSERT INTO jobs(job_id,session_id,project_root,role,model_id,title,state,created_at,updated_at,last_error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    job["job_id"],
                    job["session_id"],
                    job["project_root"],
                    job["role"],
                    job["model_id"],
                    job["title"],
                    initial_state,
                    job.get("created_at", now),
                    job.get("updated_at", now),
                    job.get("last_error"),
                ),
            )
            con.execute(
                "INSERT OR REPLACE INTO job_process(job_id, server_pid, server_start_time, terminal_result, permission_scope_json) VALUES(?,?,?,?,?)",
                (
                    job["job_id"],
                    job.get("server_pid"),
                    job.get("server_start_time"),
                    job.get("terminal_result"),
                    job.get("permission_scope_json"),
                ),
            )

    def update_permission_scope(self, job_id: str, permission_scope_json: str) -> None:
        with self.lock, self._connect() as con:
            con.execute(
                """INSERT INTO job_process(job_id, permission_scope_json) VALUES(?,?)
                   ON CONFLICT(job_id) DO UPDATE SET permission_scope_json=excluded.permission_scope_json""",
                (job_id, permission_scope_json),
            )

    def get(self, job_id: str) -> dict[str, Any]:
        with self.lock, self._connect() as con:
            row = con.execute(
                """SELECT j.*, p.server_pid, p.server_start_time, p.terminal_result, p.permission_scope_json
                   FROM jobs j LEFT JOIN job_process p ON j.job_id = p.job_id
                   WHERE j.job_id=?""",
                (job_id,),
            ).fetchone()
        if not row:
            raise BridgeError(f"找不到 job_id：{job_id}")
        return dict(row)

    def list(self, project_root: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        with self.lock, self._connect() as con:
            if project_root:
                rows = con.execute(
                    """SELECT j.*, p.server_pid, p.server_start_time, p.terminal_result
                       FROM jobs j LEFT JOIN job_process p ON j.job_id = p.job_id
                       WHERE j.project_root=? ORDER BY j.created_at DESC LIMIT ?""",
                    (project_root, limit),
                ).fetchall()
            else:
                rows = con.execute(
                    """SELECT j.*, p.server_pid, p.server_start_time, p.terminal_result
                       FROM jobs j LEFT JOIN job_process p ON j.job_id = p.job_id
                       ORDER BY j.created_at DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
        return [dict(x) for x in rows]

    def list_unfinalized(self) -> list[dict[str, Any]]:
        with self.lock, self._connect() as con:
            rows = con.execute(
                """SELECT j.*, p.server_pid, p.server_start_time, p.terminal_result
                   FROM jobs j LEFT JOIN job_process p ON j.job_id = p.job_id
                   WHERE j.state IN ('submitted', 'running', 'queued', 'waiting_for_permission', 'submission_unknown')
                   ORDER BY j.created_at ASC"""
            ).fetchall()
        return [dict(x) for x in rows]

    def update(
        self,
        job_id: str,
        state: str,
        error: str | None = None,
        terminal_result: str | None = None,
    ) -> None:
        now = time.time()
        with self.lock, self._connect() as con:
            con.execute("UPDATE jobs SET state=?,updated_at=?,last_error=? WHERE job_id=?", (state, now, error, job_id))
            if terminal_result is not None:
                con.execute(
                    "INSERT INTO job_process(job_id, terminal_result) VALUES(?,?) ON CONFLICT(job_id) DO UPDATE SET terminal_result=excluded.terminal_result",
                    (job_id, terminal_result),
                )

    def get_slot(self, project_root: str, worker_slot: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as con:
            row = con.execute(
                "SELECT * FROM worker_slots WHERE project_root=? AND worker_slot=?",
                (project_root, worker_slot),
            ).fetchone()
        return dict(row) if row else None

    def set_slot(
        self,
        project_root: str,
        worker_slot: str,
        session_id: str,
        role: str,
        model_id: str,
        summary: str | None = None,
    ) -> None:
        now = time.time()
        with self.lock, self._connect() as con:
            con.execute(
                """INSERT INTO worker_slots(
                    project_root, worker_slot, current_session_id, role, model_id,
                    summary, summary_updated_at, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(project_root, worker_slot) DO UPDATE SET
                    current_session_id=excluded.current_session_id,
                    role=excluded.role,
                    model_id=excluded.model_id,
                    summary=coalesce(excluded.summary, worker_slots.summary),
                    summary_updated_at=case when excluded.summary is not null then excluded.summary_updated_at else worker_slots.summary_updated_at end,
                    updated_at=excluded.updated_at
                """,
                (
                    project_root, worker_slot, session_id, role, model_id,
                    summary, now if summary else None, now, now
                ),
            )

    def update_slot_summary(self, project_root: str, worker_slot: str, summary: str) -> None:
        now = time.time()
        with self.lock, self._connect() as con:
            con.execute(
                "UPDATE worker_slots SET summary=?, summary_updated_at=?, updated_at=? WHERE project_root=? AND worker_slot=?",
                (summary, now, now, project_root, worker_slot),
            )

    def list_slots(self, project_root: str) -> list[dict[str, Any]]:
        with self.lock, self._connect() as con:
            rows = con.execute(
                "SELECT * FROM worker_slots WHERE project_root=? ORDER BY updated_at DESC",
                (project_root,),
            ).fetchall()
        return [dict(x) for x in rows]

    def add_decision(self, dec: dict[str, Any]) -> None:
        now = time.time()
        with self.lock, self._connect() as con:
            con.execute(
                """INSERT INTO session_routing_decisions(
                    decision_id, project_root, worker_slot, job_id,
                    previous_session_id, selected_session_id, selected_strategy,
                    decision_timestamp, estimated_cost, actual_usage,
                    prediction_confidence, decision_reason, task_outcome,
                    is_rework, data_quality, created_at, updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    dec["decision_id"],
                    dec["project_root"],
                    dec["worker_slot"],
                    dec["job_id"],
                    dec.get("previous_session_id"),
                    dec["selected_session_id"],
                    dec["selected_strategy"],
                    dec.get("decision_timestamp", now),
                    json.dumps(dec.get("estimated_cost"), ensure_ascii=False) if isinstance(dec.get("estimated_cost"), (dict, list)) else dec.get("estimated_cost"),
                    json.dumps(dec.get("actual_usage"), ensure_ascii=False) if isinstance(dec.get("actual_usage"), (dict, list)) else dec.get("actual_usage"),
                    dec.get("prediction_confidence"),
                    dec.get("decision_reason"),
                    dec.get("task_outcome", "submitted"),
                    1 if dec.get("is_rework") else 0,
                    dec.get("data_quality"),
                    now,
                    now,
                ),
            )

    def update_decision_outcome(
        self,
        job_id: str,
        task_outcome: str,
        actual_usage: dict[str, Any] | None = None,
        is_rework: bool = False,
    ) -> None:
        now = time.time()
        usage_json = json.dumps(actual_usage, ensure_ascii=False) if isinstance(actual_usage, (dict, list)) else actual_usage
        with self.lock, self._connect() as con:
            con.execute(
                """UPDATE session_routing_decisions
                   SET task_outcome=?, actual_usage=coalesce(?, actual_usage),
                       is_rework=case when ? then 1 else is_rework end,
                       updated_at=?
                   WHERE job_id=?""",
                (task_outcome, usage_json, 1 if is_rework else 0, now, job_id),
            )

    def get_decision_by_job(self, job_id: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as con:
            row = con.execute(
                "SELECT * FROM session_routing_decisions WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_decisions(self, project_root: str, limit: int = 30) -> list[dict[str, Any]]:
        with self.lock, self._connect() as con:
            rows = con.execute(
                "SELECT * FROM session_routing_decisions WHERE project_root=? ORDER BY created_at DESC LIMIT ?",
                (project_root, limit),
            ).fetchall()
        return [dict(x) for x in rows]

    def acquire_session_lock(
        self,
        session_id: str,
        job_id: str,
        owner_instance: str,
        lease_seconds: float = 600.0,
    ) -> dict[str, Any]:
        now = time.time()
        lease_expires = now + lease_seconds
        with self.lock, self._connect() as con:
            cur = con.execute("SELECT * FROM session_locks WHERE session_id=?", (session_id,))
            row = cur.fetchone()
            if row:
                row_dict = dict(row)
                if row_dict["status"] == "active" and row_dict["lease_expires_at"] > now:
                    if row_dict["job_id"] != job_id:
                        raise BridgeError(
                            f"Session {session_id} 当前已被 Job {row_dict['job_id']} (持锁者 {row_dict['owner_instance']}) 独占锁定，"
                            f"租约到期时间: {row_dict['lease_expires_at']:.0f}。同一 Session 严禁并发执行多个 Job！请等待完成后复用或使用 new/fork 策略。",
                            category=ErrorCategory.APPLICATION_ERROR,
                            user_message=f"Session {session_id} 正在被 Job {row_dict['job_id']} 独占锁定，禁止跨进程并发执行！",
                        )
                fencing_token = int(row_dict["fencing_token"]) + 1
            else:
                fencing_token = 1

            con.execute(
                """INSERT INTO session_locks(
                    session_id, job_id, owner_instance, acquired_at, lease_expires_at, fencing_token, status
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                    job_id=excluded.job_id,
                    owner_instance=excluded.owner_instance,
                    acquired_at=excluded.acquired_at,
                    lease_expires_at=excluded.lease_expires_at,
                    fencing_token=excluded.fencing_token,
                    status='active'
                """,
                (session_id, job_id, owner_instance, now, lease_expires, fencing_token, "active"),
            )
            return {
                "session_id": session_id,
                "job_id": job_id,
                "owner_instance": owner_instance,
                "acquired_at": now,
                "lease_expires_at": lease_expires,
                "fencing_token": fencing_token,
                "status": "active",
            }

    def renew_session_lock(self, session_id: str, job_id: str, extension_seconds: float = 600.0) -> bool:
        now = time.time()
        with self.lock, self._connect() as con:
            cur = con.execute(
                "UPDATE session_locks SET lease_expires_at=? WHERE session_id=? AND job_id=? AND status='active'",
                (now + extension_seconds, session_id, job_id),
            )
            return cur.rowcount > 0

    def release_session_lock(self, session_id: str, job_id: str | None = None) -> bool:
        with self.lock, self._connect() as con:
            if job_id:
                cur = con.execute(
                    "UPDATE session_locks SET status='released', lease_expires_at=0 WHERE session_id=? AND job_id=?",
                    (session_id, job_id),
                )
            else:
                cur = con.execute(
                    "UPDATE session_locks SET status='released', lease_expires_at=0 WHERE session_id=?",
                    (session_id,),
                )
            return cur.rowcount > 0

    def get_session_lock(self, session_id: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as con:
            row = con.execute("SELECT * FROM session_locks WHERE session_id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def clean_stale_locks(self, max_age_seconds: float = 3600.0) -> int:
        now = time.time()
        with self.lock, self._connect() as con:
            cur = con.execute(
                "DELETE FROM session_locks WHERE status='released' OR (lease_expires_at < ? AND acquired_at < ?)",
                (now, now - max_age_seconds),
            )
            return cur.rowcount

    def add_manifest(self, manifest: dict[str, Any]) -> None:
        now = time.time()
        with self.lock, self._connect() as con:
            con.execute(
                """INSERT OR REPLACE INTO benchmark_manifests(
                    manifest_id, job_id, session_id, benchmark_name, benchmark_phase,
                    prompt_file, prompt_sha256, input_file, input_sha256,
                    model_id, role, session_strategy, parameters_json, created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    manifest["manifest_id"],
                    manifest.get("job_id"),
                    manifest.get("session_id"),
                    manifest["benchmark_name"],
                    manifest.get("benchmark_phase", "unknown"),
                    manifest.get("prompt_file"),
                    manifest.get("prompt_sha256"),
                    manifest.get("input_file"),
                    manifest.get("input_sha256"),
                    manifest["model_id"],
                    manifest["role"],
                    manifest.get("session_strategy", "new"),
                    json.dumps(manifest.get("parameters", {}), ensure_ascii=False) if isinstance(manifest.get("parameters"), dict) else manifest.get("parameters_json"),
                    manifest.get("created_at", now),
                ),
            )

    def get_manifest(self, identifier: str) -> dict[str, Any] | None:
        with self.lock, self._connect() as con:
            row = con.execute(
                "SELECT * FROM benchmark_manifests WHERE manifest_id=? OR job_id=?",
                (identifier, identifier),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        if d.get("parameters_json"):
            try:
                d["parameters"] = json.loads(d["parameters_json"])
            except Exception:
                d["parameters"] = {}
        return d

    def list_manifests(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.lock, self._connect() as con:
            rows = con.execute(
                "SELECT * FROM benchmark_manifests ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            if d.get("parameters_json"):
                try:
                    d["parameters"] = json.loads(d["parameters_json"])
                except Exception:
                    d["parameters"] = {}
            result.append(d)
        return result

    def list_jobs_by_session(self, session_id: str, limit: int = 20) -> list[dict[str, Any]]:
        with self.lock, self._connect() as con:
            rows = con.execute(
                """SELECT j.*, p.server_pid, p.server_start_time, p.terminal_result
                   FROM jobs j LEFT JOIN job_process p ON j.job_id = p.job_id
                   WHERE j.session_id=? ORDER BY j.created_at DESC LIMIT ?""",
                (session_id, limit),
            ).fetchall()
        return [dict(x) for x in rows]


@dataclass
class ServerHandle:
    process: subprocess.Popen[Any]
    base_url: str
    username: str
    password: str
    log_path: Path
    job_handle: Any | None = None
    server_pid: int | None = None
    server_start_time: float | None = None


class OpenCodeClient:
    def __init__(self) -> None:
        self.server: ServerHandle | None = None
        self.server_lock = threading.RLock()
        self._start_lock = threading.RLock()
        self._session_lock = threading.RLock()
        self._running_sessions: dict[str, str] = {}  # session_id -> active job_id
        self.owner_instance = f"{os.getpid()}_{id(self)}_{uuid.uuid4().hex[:6]}"
        self.circuit_breaker = CircuitBreaker(failure_threshold=3, reset_timeout=30.0)
        self.store = JobStore()
        # Perform restart reconciliation for stale unfinalized jobs
        try:
            self.reconcile_unfinalized_jobs()
        except Exception:
            pass

    def _acquire_session(self, session_id: str, job_id: str) -> None:
        with self._session_lock:
            active_job = self._running_sessions.get(session_id)
            if active_job and active_job != job_id:
                raise BridgeError(
                    f"Session {session_id} 当前正在执行 Job {active_job}。"
                    "同一 Session 严禁并发执行多个 Job！请等待当前完成或使用 'new'/'fork' 策略。",
                    category=ErrorCategory.APPLICATION_ERROR,
                )
            # Check DB for unfinalized or submission_unknown jobs on this session
            try:
                db_jobs = self.store.list_jobs_by_session(session_id, limit=3)
                for j in db_jobs:
                    if j.get("job_id") != job_id:
                        if j.get("state") == "submission_unknown":
                            raise BridgeError(
                                f"Session {session_id} 存在状态未确定的 Job {j['job_id']} (submission_unknown)。"
                                "严禁在该 Session 上提交，避免重复扣费。请先核取原 Job 状态。",
                                category=ErrorCategory.SUBMISSION_UNKNOWN,
                            )
                        if j.get("state") in ("running", "submitted"):
                            raise BridgeError(
                                f"Session {session_id} 在数据库中处于 {j['state']} 状态 (Job {j['job_id']})。"
                                "同一 Session 严禁并发执行多个 Job！请等待当前完成或使用 'new'/'fork' 策略。",
                                category=ErrorCategory.APPLICATION_ERROR,
                            )
            except BridgeError:
                raise
            except Exception:
                pass

            # Acquire persistent cross-process SQLite session lock
            self.store.acquire_session_lock(session_id, job_id, self.owner_instance)
            self._running_sessions[session_id] = job_id

    def _release_session(self, session_id: str, job_id: str | None = None) -> None:
        with self._session_lock:
            self._running_sessions.pop(session_id, None)
            try:
                self.store.release_session_lock(session_id, job_id)
            except Exception:
                pass

    def is_session_busy(self, session_id: str) -> tuple[bool, str | None]:
        with self._session_lock:
            active_job = self._running_sessions.get(session_id)
            if active_job:
                return (True, active_job)
            lock = self.store.get_session_lock(session_id)
            if lock and lock.get("status") == "active" and lock.get("lease_expires_at", 0) > time.time():
                return (True, lock.get("job_id"))
            return (False, None)

    def _fork_session(
        self,
        parent_session_id: str,
        project_root: str,
        role: str,
        model_id: str,
        title: str | None = None,
    ) -> str:
        """Create a forked session from an existing parent session."""
        sid_path = urllib.parse.quote(parent_session_id, safe="")
        safe_title = (title or f"Fork of {parent_session_id}").strip()[:120]
        fork_body = {"title": safe_title}
        try:
            res_raw = self.request("POST", f"/api/session/{sid_path}/fork", fork_body)
            data = _unwrap_data(res_raw)
            if isinstance(data, dict) and data.get("id"):
                return data["id"]
        except Exception:
            pass

        # Fallback fork: create new session within same project root
        model_ref = {"providerID": "opencode-go", "id": model_id}
        create_body = {
            "title": safe_title,
            "agent": ROLE_POLICIES[role]["agent"],
            "model": model_ref,
            "location": {"directory": project_root},
            "permissions": self._permissions_for_role(role),
        }
        created_raw = self.request("POST", "/api/session", create_body)
        created = _unwrap_data(created_raw)
        new_sid = created.get("id") if isinstance(created, dict) else None
        if not new_sid:
            raise BridgeError(f"创建 Fork 会话失败：{created_raw!r}")
        return new_sid

    def _find_opencode(self) -> str:
        candidates: list[str] = []
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(str(Path(appdata) / "npm" / "opencode.cmd"))
            candidates.append(str(Path(appdata) / "npm" / "opencode.exe"))
        for name in ("opencode.exe", "opencode.cmd", "opencode"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
        for item in candidates:
            if Path(item).is_file():
                return item
        raise BridgeError("未找到 OpenCode CLI。预期位置为 %APPDATA%\\npm\\opencode.cmd；请先确认 OpenCode V2.2.5 已安装。")

    def _choose_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def ensure_server(self, raw_project_root: str | None = None) -> ServerHandle:
        with self.server_lock:
            if self.server and self.server.process.poll() is None:
                return self.server
            if not raw_project_root:
                raise BridgeError("启动 OpenCode Server 时必须提供当前 Antigravity Project 根目录；不使用桥接目录或其他固定目录作为工作目录。")
            project_root = canonical_project_root(raw_project_root)

            opencode = self._find_opencode()
            port = self._choose_port()
            password = secrets.token_urlsafe(36)
            username = "opencode"
            env = os.environ.copy()
            env["OPENCODE_SERVER_USERNAME"] = username
            env["OPENCODE_SERVER_PASSWORD"] = password
            env["NO_COLOR"] = "1"
            log_path = state_dir() / "opencode-server.log"
            log_stream = open(log_path, "ab", buffering=0)

            args = [opencode, "serve", "--hostname", "127.0.0.1", "--port", str(port)]
            creationflags = 0
            if os.name == "nt":
                comspec = os.environ.get("COMSPEC", "cmd.exe")
                command_line = subprocess.list2cmdline(args)
                args = [comspec, "/d", "/c", "call " + command_line]
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)

            job_obj = _create_kill_on_close_job_object()
            try:
                proc = subprocess.Popen(
                    args,
                    cwd=project_root,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log_stream,
                    stderr=subprocess.STDOUT,
                    creationflags=creationflags,
                    start_new_session=(os.name != "nt"),
                )
                if job_obj:
                    _assign_process_to_job(job_obj, proc)
            except Exception:
                log_stream.close()
                _close_job_handle(job_obj)
                raise
            finally:
                try:
                    log_stream.close()
                except Exception:
                    pass

            base = f"http://127.0.0.1:{port}"
            server_pid = proc.pid
            server_start_time = _get_process_create_time(server_pid)
            handle = ServerHandle(
                process=proc,
                base_url=base,
                username=username,
                password=password,
                log_path=log_path,
                job_handle=job_obj,
                server_pid=server_pid,
                server_start_time=server_start_time,
            )
            self.server = handle

            deadline = time.monotonic() + 25
            last_error = None
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    info_raw = self._request_raw(handle, "GET", "/api/info", timeout=2)
                    _server_info(info_raw)
                    return handle
                except Exception as exc:  # startup race
                    last_error = str(exc)
                    time.sleep(0.4)
            self.server = None
            self._terminate_server(handle)
            raise BridgeError(
                "无法启动 OpenCode Server。检查 OpenCode CLI 和日志："
                f"{log_path}；最后错误：{last_error or '进程提前退出'}"
            )

    def _terminate_server(self, handle: ServerHandle) -> None:
        proc = handle.process
        pid = handle.server_pid or proc.pid
        start_time = handle.server_start_time

        if proc.poll() is None or _is_process_alive_and_matching(pid, start_time):
            # 1. Graceful termination
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            except Exception:
                pass

            # 2. Force termination if still alive and confirmed matching
            if _is_process_alive_and_matching(pid, start_time):
                try:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/PID", str(pid), "/T", "/F"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=5,
                            check=False,
                        )
                    else:
                        proc.kill()
                        proc.wait(timeout=3)
                except Exception:
                    pass

        _close_job_handle(handle.job_handle)

    def shutdown(self) -> None:
        with self.server_lock:
            if self.server:
                self._terminate_server(self.server)
                self.server = None

    def _request_raw(self, handle: ServerHandle, method: str, path: str, body: Any | None = None, timeout: int = API_TIMEOUT_SECONDS) -> Any:
        url = handle.base_url + path
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {"Accept": "application/json", "Authorization": "Basic " + base64.b64encode(f"{handle.username}:{handle.password}".encode()).decode()}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read()
                if not raw:
                    return None
                content_type = response.headers.get("Content-Type", "")
                if "json" in content_type or raw[:1] in (b"{", b"["):
                    return json.loads(raw.decode("utf-8"))
                return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            message = exc.read().decode("utf-8", errors="replace")[:1200]
            err = classify_error(status_code=exc.code, raw_payload=message, exc=exc)
            raise BridgeError(
                f"[{err.category}] {err.user_message} (HTTP {exc.code} {method} {path}: {message})",
                category=err.category,
                user_message=err.user_message,
                raw_error=err.raw_detail,
                status_code=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            err = classify_error(exc=exc)
            raise BridgeError(
                f"[{err.category}] {err.user_message} ({method} {path}: {exc})",
                category=err.category,
                user_message=err.user_message,
                raw_error=str(exc),
            ) from exc

    def request(self, method: str, path: str, body: Any | None = None, timeout: int = API_TIMEOUT_SECONDS) -> Any:
        if not self.server or self.server.process.poll() is not None:
            raise BridgeError("OpenCode Server 尚未启动。必须先通过当前 Project 根目录执行 inspect 或加载该 Project 的 Worker。")
        return self._request_raw(self.server, method, path, body, timeout)

    def models_and_usage(self, raw_root: str, output_mode: str = "summary") -> dict[str, Any]:
        root = canonical_project_root(raw_root)
        self.ensure_server(root)
        query = urllib.parse.urlencode({"location[directory]": root})
        models_raw = self.request("GET", f"/api/model?{query}")
        model_items = _unwrap_data(models_raw)
        if not isinstance(model_items, list):
            raise BridgeError("OpenCode 模型 API 返回格式异常，预期 data 为数组。", category=ErrorCategory.APPLICATION_ERROR)
        go_models = [m for m in model_items if isinstance(m, dict) and m.get("providerID") == "opencode-go"]
        active_models = [m for m in go_models if m.get("status") in (None, "active")]

        usage_raw = self.request("GET", f"/api/experimental/usage/limits?{query}")
        usage = _unwrap_data(usage_raw)
        server_raw = self.request("GET", "/api/info")
        server_info = _server_info(server_raw)

        agents_raw = self.request("GET", f"/api/agent?{query}")
        agent_items = _unwrap_data(agents_raw)
        if not isinstance(agent_items, list):
            raise BridgeError("OpenCode Agent API 返回格式异常，预期 data 为数组。", category=ErrorCategory.APPLICATION_ERROR)
        agents = [
            {key: item[key] for key in ("id", "name", "description", "mode") if key in item}
            for item in agent_items if isinstance(item, dict)
        ]

        current_hash = _compute_file_sha256(__file__)[:12]
        runtime_version = {
            "version": VERSION,
            "core_build_hash": CORE_BUILD_HASH,
            "current_ondisk_hash": current_hash,
            "start_time": CORE_START_TIME,
            "ondisk_mismatch": CORE_BUILD_HASH != current_hash,
            "supervisor_notice": (
                "MCP Server 运行中版本与磁盘代码不一致，磁盘代码已被修改。请在任务空闲时重启 MCP 服务。"
                if CORE_BUILD_HASH != current_hash
                else "运行中代码与磁盘代码一致。"
            ),
        }

        full_payload = {
            "project_root": root,
            "runtime_version": runtime_version,
            "server": server_info,
            "agents": agents,
            "required_agents_available": {
                agent_id: any(agent.get("id") == agent_id for agent in agents)
                for agent_id in sorted({policy["agent"] for policy in ROLE_POLICIES.values()})
            },
            "provider_filter": "opencode-go",
            "models_total": len(go_models),
            "models_available": len(active_models),
            "models": [_safe_model(m) for m in active_models],
            "inactive_models": [_safe_model(m) for m in go_models if m not in active_models],
            "usage": usage,
            "notes": [
                "只列出 providerID=opencode-go 的模型，其他 Provider 已排除。",
                "usage 为 OpenCode Go workspace allowance / Console budgets，不是每个模型各自的剩余额度。",
                "若 budgets 为空，应如实显示为空，不得推断为模型限额。",
            ],
        }

        if output_mode == "summary":
            full_inspect_path = None
            try:
                full_inspect_path = state_dir() / "last_inspect.json"
                with open(full_inspect_path, "w", encoding="utf-8") as f:
                    json.dump(full_payload, f, ensure_ascii=False, indent=2)
            except Exception:
                pass

            recommended_model_ids = ["opencode-go/qwen3.8-flash", "opencode-go/qwen3.8-coder", "opencode-go/kimi-k3.1-pro", "opencode-go/mimo-v2.6-flash"]
            rec_models = [m for m in active_models if m.get("id") in recommended_model_ids or f"opencode-go/{m.get('id')}" in recommended_model_ids]
            if not rec_models:
                rec_models = active_models[:4]

            summary_payload = {
                "project_root": root,
                "runtime_version": runtime_version,
                "server": server_info,
                "agents": agents,
                "required_agents_available": full_payload["required_agents_available"],
                "provider_filter": "opencode-go",
                "models_total": len(go_models),
                "models_available": len(active_models),
                "models": [_compact_model(m) for m in active_models],
                "recommended_models": [_compact_model(m) for m in rec_models],
                "available_model_ids": [m.get("id") for m in active_models],
                "roles": {
                    role: {"agent": pol["agent"], "edit": pol["edit"], "shell": pol["shell"], "description": pol["description"]}
                    for role, pol in ROLE_POLICIES.items()
                },
                "usage": usage,
                "output_mode": "summary",
                "full_inspection_file": str(full_inspect_path) if full_inspect_path else None,
                "notes": full_payload["notes"],
                "token_optimization_notice": "已返回精简模型列表以节省 Supervisor Token。若需完整 32 个模型的全部详情，可传入 output_mode='full' 或直接查阅 full_inspection_file。",
            }
            return summary_payload

        return full_payload

    def _permissions_for_role(self, role: str) -> list[dict[str, str]]:
        policy = ROLE_POLICIES[role]
        rules = [
            {"action": "external_directory", "resource": "*", "effect": "deny"},
            {"action": "read", "resource": "*.env", "effect": "deny"},
            {"action": "read", "resource": "*.env.*", "effect": "deny"},
            {"action": "read", "resource": "*.pem", "effect": "deny"},
            {"action": "read", "resource": "*.key", "effect": "deny"},
            {"action": "read", "resource": "../*", "effect": "deny"},
            {"action": "read", "resource": "*/../*", "effect": "deny"},
            {"action": "edit", "resource": "*", "effect": policy["edit"]},
            {"action": "edit", "resource": "../*", "effect": "deny"},
            {"action": "edit", "resource": "*/../*", "effect": "deny"},
            {"action": "shell", "resource": "*", "effect": policy["shell"]},
            {"action": "subagent", "resource": "*", "effect": "deny"},
        ]
        return rules

    def _validate_task_authorization(
        self,
        role: str,
        task_authorization: dict[str, Any] | None,
    ) -> list[str]:
        """Validate exact Shell resource strings explicitly approved by the user."""
        if task_authorization is None:
            return []
        if not isinstance(task_authorization, dict):
            raise BridgeError("task_authorization 必须是对象。", category=ErrorCategory.APPLICATION_ERROR)
        allowed_keys = {"approved_by_user", "approved_shell_commands"}
        if set(task_authorization) - allowed_keys:
            raise BridgeError("task_authorization 只允许 approved_by_user 和 approved_shell_commands。")
        if task_authorization.get("approved_by_user") is not True:
            raise BridgeError("任务级 Shell 授权必须明确标记 approved_by_user=true；不得推断用户已批准。")
        if role not in ("writer", "reviser", "general"):
            raise BridgeError(f"role={role!r} 不允许申请任务级 Shell 授权。Reviewer/Analyst 保持只读。")
        commands = task_authorization.get("approved_shell_commands")
        if not isinstance(commands, list) or not 1 <= len(commands) <= 5:
            raise BridgeError("approved_shell_commands 必须包含 1–5 条用户已逐条确认的完整命令资源字符串。")
        normalized: list[str] = []
        for index, command in enumerate(commands, start=1):
            if not isinstance(command, str) or not command or command != command.strip():
                raise BridgeError(f"第 {index} 条已批准 Shell 命令必须是非空且无首尾空格的字符串。")
            if len(command) > 2048 or any(ch in command for ch in ("\x00", "\r", "\n")):
                raise BridgeError(f"第 {index} 条 Shell 命令过长或包含控制字符。")
            if "*" in command or "?" in command:
                raise BridgeError(f"第 {index} 条 Shell 命令不得包含通配符；只接受精确命令匹配。")
            if command in normalized:
                raise BridgeError(f"第 {index} 条 Shell 命令重复。")
            normalized.append(command)
        return normalized

    def _permissions_with_task_authorization(
        self,
        role: str,
        approved_shell_commands: list[str],
    ) -> list[dict[str, str]]:
        rules = self._permissions_for_role(role)
        for command in approved_shell_commands:
            rules.append({"action": "shell", "resource": command, "effect": "allow"})
        return rules

    def _clear_task_permission_scope(self, job: dict[str, Any]) -> dict[str, Any] | None:
        raw_scope = job.get("permission_scope_json")
        if not raw_scope:
            return None
        try:
            scope = json.loads(raw_scope) if isinstance(raw_scope, str) else raw_scope
        except Exception:
            scope = {"state": "cleanup_pending", "error": "Stored task permission scope could not be parsed."}
        if not isinstance(scope, dict) or scope.get("state") == "cleared":
            return None
        if scope.get("state") not in ("active", "cleanup_pending"):
            return None

        sid_path = urllib.parse.quote(job["session_id"], safe="")
        try:
            self.request(
                "PATCH",
                f"/api/session/{sid_path}",
                {"permissions": self._permissions_for_role(job["role"])},
            )
            cleared = {
                **scope,
                "state": "cleared",
                "cleared_at": time.time(),
                "cleanup_error": None,
            }
            self.store.update_permission_scope(job["job_id"], json.dumps(cleared, ensure_ascii=False))
            return {
                "state": "cleared",
                "approved_command_count": len(scope.get("approved_shell_commands") or []),
            }
        except Exception as exc:
            pending = {
                **scope,
                "state": "cleanup_pending",
                "cleanup_error": f"{type(exc).__name__}: {exc}"[:1000],
                "last_cleanup_attempt_at": time.time(),
            }
            self.store.update_permission_scope(job["job_id"], json.dumps(pending, ensure_ascii=False))
            return {
                "state": "cleanup_pending",
                "approved_command_count": len(scope.get("approved_shell_commands") or []),
                "error": pending["cleanup_error"],
                "warning": "Temporary session permission rules may remain until cleanup succeeds. A later reuse start will first restore the role baseline.",
            }

    def session_inspect(
        self,
        raw_root: str,
        worker_slot: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Provide lightweight decision metrics for Anti without reading full message text."""
        root = canonical_project_root(raw_root)
        self.ensure_server(root)

        target_slot = worker_slot
        target_sid = session_id

        if target_slot and not target_sid:
            slot_info = self.store.get_slot(root, target_slot)
            if slot_info:
                target_sid = slot_info.get("current_session_id")

        if not target_sid and not target_slot:
            slots = self.store.list_slots(root)
            return {
                "project_root": root,
                "worker_slots": slots,
                "notes": ["传入 worker_slot 或 session_id 可查询具体 Session 详细度量指标。"],
            }

        slot_info = self.store.get_slot(root, target_slot) if target_slot else None
        if not slot_info and target_sid:
            for s in self.store.list_slots(root):
                if s.get("current_session_id") == target_sid:
                    slot_info = s
                    target_slot = s["worker_slot"]
                    break

        history_jobs = self.store.list_jobs_by_session(target_sid) if target_sid else []
        is_busy, active_job = self.is_session_busy(target_sid) if target_sid else (False, None)

        cumulative_tokens = {
            "input": 0,
            "output": 0,
            "reasoning": 0,
            "cache_read": 0,
            "cache_write": 0,
            "cost_usd": 0.0,
            "completed_jobs": 0,
            "failed_jobs": 0,
        }
        recent_jobs_usage = []
        for j in history_jobs:
            usage = None
            if j.get("terminal_result"):
                try:
                    tr = json.loads(j["terminal_result"])
                    usage = tr.get("usage_total_observed")
                    if isinstance(usage, dict):
                        cumulative_tokens["input"] += int(usage.get("input") or 0)
                        cumulative_tokens["output"] += int(usage.get("output") or 0)
                        cumulative_tokens["reasoning"] += int(usage.get("reasoning") or 0)
                        cumulative_tokens["cache_read"] += int(usage.get("cache_read") or 0)
                        cumulative_tokens["cache_write"] += int(usage.get("cache_write") or 0)
                        cumulative_tokens["cost_usd"] += float(usage.get("cost_usd") or 0.0)
                except Exception:
                    pass
            recent_jobs_usage.append({
                "job_id": j["job_id"],
                "state": j["state"],
                "created_at": j["created_at"],
                "usage": usage or "unavailable",
            })
            if j.get("state") == "completed":
                cumulative_tokens["completed_jobs"] += 1
            elif j.get("state") == "failed":
                cumulative_tokens["failed_jobs"] += 1

        context_limit = "unavailable"
        if slot_info and slot_info.get("model_id"):
            try:
                models_info = self.models_and_usage(root)
                for m in models_info.get("models", []):
                    if m.get("id") == slot_info["model_id"]:
                        lim = (m.get("limit") or {}).get("context")
                        if lim:
                            context_limit = lim
                        break
            except Exception:
                pass

        context_warning = None
        if isinstance(context_limit, int) and cumulative_tokens["input"] > context_limit * 0.7:
            context_warning = f"累计 Input 已达到模型 Context Limit ({context_limit}) 的 70% 以上，建议执行 Fork 或通过 New 切换并迁移摘要。"

        return {
            "project_root": root,
            "worker_slot": target_slot or "unavailable",
            "session_id": target_sid or "unavailable",
            "role": slot_info.get("role") if slot_info else "unavailable",
            "model_id": slot_info.get("model_id") if slot_info else "unavailable",
            "context_limit": context_limit,
            "is_busy": is_busy,
            "active_job_id": active_job,
            "history_jobs_count": len(history_jobs),
            "cumulative_tokens": cumulative_tokens if target_sid else "unavailable",
            "recent_jobs_usage": recent_jobs_usage[:5],
            "slot_summary": slot_info.get("summary") if slot_info else None,
            "slot_summary_updated_at": slot_info.get("summary_updated_at") if slot_info else None,
            "context_warning": context_warning,
            "data_quality": "complete" if target_sid else "missing_session",
        }

    def predict_session_cost(
        self,
        raw_root: str,
        worker_slot: str,
        task: str,
        model_id: str | None = None,
        candidate_strategy: str | None = None,
    ) -> dict[str, Any]:
        """Estimate costs for Reuse, New, and Fork strategies with reliability constraints."""
        root = canonical_project_root(raw_root)
        task_bytes = len(task.encode("utf-8"))
        est_task_input = max(100, task_bytes // 3) + 300
        est_output = 600

        slot_info = self.store.get_slot(root, worker_slot)
        has_existing_session = bool(slot_info and slot_info.get("current_session_id"))
        history_input_tokens = 0
        if has_existing_session:
            inspect_res = self.session_inspect(root, worker_slot=worker_slot)
            cum = inspect_res.get("cumulative_tokens")
            if isinstance(cum, dict):
                history_input_tokens = int(cum.get("input") or 0)

        uncertainty_factors = []
        is_busy, busy_job = (False, None)
        if has_existing_session:
            is_busy, busy_job = self.is_session_busy(slot_info["current_session_id"])

        has_sub_unknown = False
        if has_existing_session:
            recent_jobs = self.store.list_jobs_by_session(slot_info["current_session_id"], limit=3)
            for rj in recent_jobs:
                if rj.get("state") == "submission_unknown":
                    has_sub_unknown = True
                    break

        if not has_existing_session:
            uncertainty_factors.append("当前 Slot 暂无历史 Session，Reuse 与 Fork 不可用。")
        if history_input_tokens == 0 and has_existing_session:
            uncertainty_factors.append("历史 Token 用量为 0 或未观测到基准值。")
        if is_busy:
            uncertainty_factors.append(f"当前 Session 正在被 Job {busy_job} 独占执行中。")
        if has_sub_unknown:
            uncertainty_factors.append("当前 Session 存在状态未确定的 Job (submission_unknown)。")

        # 1. New
        new_input = est_task_input + 500
        new_cache_read = 0
        new_cache_write = new_input
        new_output = est_output
        new_rework_risk = 0.35 if has_existing_session else 0.1
        new_cost_vector = {
            "expected_uncached_input": new_input,
            "expected_cache_read": new_cache_read,
            "expected_cache_write": new_cache_write,
            "expected_output": new_output,
            "rework_risk_factor": new_rework_risk,
        }

        # 2. Reuse
        if has_existing_session and not is_busy and not has_sub_unknown:
            reuse_cache_read = int(history_input_tokens * 0.75)
            reuse_input = est_task_input + int(history_input_tokens * 0.25)
            reuse_cache_write = est_task_input
            reuse_output = est_output
            reuse_rework_risk = 0.1 if history_input_tokens < 60000 else 0.4
            reuse_cost_vector = {
                "expected_uncached_input": reuse_input,
                "expected_cache_read": reuse_cache_read,
                "expected_cache_write": reuse_cache_write,
                "expected_output": reuse_output,
                "rework_risk_factor": reuse_rework_risk,
            }
        elif is_busy:
            reuse_cost_vector = f"unavailable (session is currently busy executing job: {busy_job})"
        elif has_sub_unknown:
            reuse_cost_vector = "unavailable (session has job in submission_unknown state)"
        else:
            reuse_cost_vector = "unavailable (no existing session)"

        # 3. Fork
        if has_existing_session and not is_busy and not has_sub_unknown:
            fork_cache_read = int(history_input_tokens * 0.70)
            fork_input = est_task_input + int(history_input_tokens * 0.30) + 200
            fork_cache_write = est_task_input
            fork_output = est_output
            fork_rework_risk = 0.15
            fork_cost_vector = {
                "expected_uncached_input": fork_input,
                "expected_cache_read": fork_cache_read,
                "expected_cache_write": fork_cache_write,
                "expected_output": fork_output,
                "rework_risk_factor": fork_rework_risk,
            }
        elif is_busy:
            fork_cost_vector = f"unavailable (session is currently busy executing job: {busy_job})"
        elif has_sub_unknown:
            fork_cost_vector = "unavailable (session has job in submission_unknown state)"
        else:
            fork_cost_vector = "unavailable (no existing session)"

        if is_busy:
            recommended = "new"
            rec_reason = f"当前 Session 正在被 Job {busy_job} 独占执行，为保证状态互斥禁止并发提交，强制建议 new 策略。"
            confidence = 0.95
        elif has_sub_unknown:
            recommended = "new"
            rec_reason = "当前 Session 存在状态未确定的 Job (submission_unknown)，必须先核对原 Job 终态，禁止盲目复用。"
            confidence = 0.95
        elif not has_existing_session:
            recommended = "new"
            rec_reason = "当前 Slot 为初始任务，必须新建 Session。"
            confidence = 0.90
        elif history_input_tokens > 70000:
            recommended = "fork"
            rec_reason = f"历史上下文较长 ({history_input_tokens} > 70k tokens)，直接 Reuse 存在严重上下文污染与降级风险，强制建议 fork 分支或 new。"
            confidence = 0.85
        else:
            recommended = "reuse"
            rec_reason = "历史上下文适中，Reuse 可最大化利用 Prompt Cache Read 降低成本。"
            confidence = 0.85

        return {
            "project_root": root,
            "worker_slot": worker_slot,
            "task_estimated_base_tokens": est_task_input,
            "candidate_evaluations": {
                "new": {
                    "available": True,
                    "cost_vector": new_cost_vector,
                    "rationale": "全新独立会话，历史污染为零，适合任务边界重置。",
                },
                "reuse": {
                    "available": has_existing_session and not is_busy and not has_sub_unknown,
                    "cost_vector": reuse_cost_vector,
                    "rationale": "复用现有会话，最大化享受 Prompt Cache 命中，适合连续任务。",
                },
                "fork": {
                    "available": has_existing_session and not is_busy and not has_sub_unknown,
                    "cost_vector": fork_cost_vector,
                    "rationale": "从现有会话分叉出独立分支，继承前置成果同时隔离污染。",
                },
            },
            "recommended_strategy": recommended,
            "recommendation_reason": rec_reason,
            "prediction_confidence": confidence,
            "uncertainty_factors": uncertainty_factors,
            "cost_model_formula": "Expected Cost = Uncached Input + Cache Read + Cache Write + Output + Rework Risk",
        }

    def update_session_summary(self, raw_root: str, worker_slot: str, summary: str) -> dict[str, Any]:
        root = canonical_project_root(raw_root)
        if not isinstance(summary, str) or not summary.strip():
            raise BridgeError("summary 不能为空。")
        self.store.update_slot_summary(root, worker_slot, summary.strip())
        return {
            "project_root": root,
            "worker_slot": worker_slot,
            "summary_length": len(summary),
            "updated_at": time.time(),
            "status": "summary_updated",
        }

    def start_worker(
        self,
        raw_root: str,
        role: str,
        task: str,
        model_id: str,
        title: str | None = None,
        allow_over_limit: bool = False,
        worker_slot: str | None = None,
        session_strategy: str = "new",
        session_id: str | None = None,
        decision_reason: str | None = None,
        estimated_cost: dict[str, Any] | None = None,
        prediction_confidence: float | None = None,
        benchmark_manifest: dict[str, Any] | None = None,
        task_authorization: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        with self._start_lock:
            if benchmark_manifest is not None and not isinstance(benchmark_manifest, dict):
                raise BridgeError("非法参数：benchmark_manifest 必须是字典对象(object/dict)。", category=ErrorCategory.APPLICATION_ERROR)
            root = canonical_project_root(raw_root)
            if role not in ROLE_POLICIES:
                raise BridgeError(f"不支持 role={role!r}。可用角色：{', '.join(ROLE_POLICIES)}")
            if not isinstance(task, str) or not task.strip():
                raise BridgeError("task 不能为空；请传入 Worker 要执行的自然语言任务。")
            if not isinstance(model_id, str) or not model_id.strip():
                raise BridgeError("必须提供 model_id。请先调用 opencode_go_inspect，再使用用户选择的 opencode-go 模型。")
            model_id = model_id.strip()
            if "/" in model_id:
                prefix, short_id = model_id.split("/", 1)
                if prefix != "opencode-go":
                    raise BridgeError("只允许使用 providerID=opencode-go 的模型。")
                model_id = short_id

            slot = (worker_slot or role).strip()
            strategy = (session_strategy or "new").strip().lower()
            if strategy not in ("new", "reuse", "fork"):
                raise BridgeError(f"不支持 session_strategy={session_strategy!r}。可用策略：new, reuse, fork。")

            approved_shell_commands = self._validate_task_authorization(role, task_authorization)
            session_permissions = self._permissions_with_task_authorization(role, approved_shell_commands)

            inspection = self.models_and_usage(root)
            available_agents = {agent.get("id") for agent in inspection.get("agents", [])}
            required_agent = ROLE_POLICIES[role]["agent"]
            if required_agent not in available_agents:
                raise BridgeError(
                    f"当前 OpenCode Project 未提供所需 Agent {required_agent!r}；"
                    "未创建 Session，也未提交模型任务。请先检查 OpenCode Agent 配置。"
                )
            available = {m.get("id"): m for m in inspection["models"]}
            if model_id not in available:
                raise BridgeError(f"模型 {model_id!r} 当前不在此 Project 可用的 opencode-go 模型目录中。请重新 inspect 并让用户选择。")

            active_data = _unwrap_data(self.request("GET", "/api/session/active")) or {}
            active_ids = set(active_data.keys()) if isinstance(active_data, dict) else set()
            existing_jobs = self.store.list(limit=300)
            active_job_count = sum(1 for j in existing_jobs if j["session_id"] in active_ids)
            if active_job_count >= DEFAULT_MAX_CONCURRENT and not allow_over_limit:
                raise BridgeError(
                    f"已达到默认并发上限 {DEFAULT_MAX_CONCURRENT} 个 Worker。"
                    "请等待现有 Worker 完成，或只有在用户明确要求提高并发上限时才设置 allow_over_limit=true。"
                )
            if active_job_count >= MAX_CONCURRENT_HARD:
                raise BridgeError(f"已达到硬性并发上限 {MAX_CONCURRENT_HARD}，不能再启动 Worker。")

            safe_title = (title or f"{slot} ({role}) - {time.strftime('%Y-%m-%d %H:%M:%S')}").strip()[:120]
            slot_info = self.store.get_slot(root, slot)
            previous_sid = slot_info.get("current_session_id") if slot_info else None
            actual_session_id: str | None = None

            job_id = "job_" + uuid.uuid4().hex[:16]

            # Execute Session Strategy
            if strategy == "reuse":
                target_sid = session_id or previous_sid
                if not target_sid:
                    raise BridgeError(f"Worker Slot {slot!r} 在当前项目暂无历史 Session 可复用。请先使用 'new' 策略创建初始会话。")
                self._acquire_session(target_sid, job_id)
                actual_session_id = target_sid

            elif strategy == "fork":
                parent_sid = session_id or previous_sid
                if not parent_sid:
                    raise BridgeError(f"Worker Slot {slot!r} 在当前项目暂无历史 Session 可供 Fork。请先使用 'new' 策略。")
                actual_session_id = self._fork_session(parent_sid, root, role, model_id, safe_title)
                self._acquire_session(actual_session_id, job_id)
                self.store.set_slot(root, slot, actual_session_id, role, model_id)

            else:  # strategy == "new"
                model_ref = {"providerID": "opencode-go", "id": model_id}
                create_body = {
                    "title": safe_title,
                    "agent": ROLE_POLICIES[role]["agent"],
                    "model": model_ref,
                    "location": {"directory": root},
                    "permissions": session_permissions,
                }
                created_raw = self.request("POST", "/api/session", create_body)
                created = _unwrap_data(created_raw)
                new_sid = created.get("id") if isinstance(created, dict) else None
                if not new_sid:
                    raise BridgeError(f"OpenCode session.create 未返回 session id：{created_raw!r}")
                actual_session_id = new_sid
                self._acquire_session(actual_session_id, job_id)
                self.store.set_slot(root, slot, actual_session_id, role, model_id)

            session_id = actual_session_id

            if strategy in ("reuse", "fork"):
                sid_path = urllib.parse.quote(session_id, safe="")
                try:
                    self.request(
                        "PATCH",
                        f"/api/session/{sid_path}",
                        {"permissions": session_permissions},
                    )
                except Exception as exc:
                    self._release_session(session_id)
                    raise BridgeError(
                        "无法安全更新复用/Fork Session 的任务级权限；已停止提交 Worker，未执行用户任务。"
                        f"原因：{type(exc).__name__}: {exc}",
                        category=ErrorCategory.APPLICATION_ERROR,
                    ) from exc

            permission_scope = None
            if approved_shell_commands:
                permission_scope = json.dumps({
                    "state": "active",
                    "approved_shell_commands": approved_shell_commands,
                    "match_mode": "exact",
                }, ensure_ascii=False)

            server_pid = self.server.server_pid if self.server else None
            server_start = self.server.server_start_time if self.server else None

            # 1. Atomically pre-register in database BEFORE prompt
            self.store.add({
                "job_id": job_id,
                "session_id": session_id,
                "project_root": root,
                "role": role,
                "model_id": model_id,
                "title": safe_title,
                "state": "submitted",
                "server_pid": server_pid,
                "server_start_time": server_start,
                "permission_scope_json": permission_scope,
            })

            # 2. Record routing decision
            decision_id = "dec_" + uuid.uuid4().hex[:16]
            self.store.add_decision({
                "decision_id": decision_id,
                "project_root": root,
                "worker_slot": slot,
                "job_id": job_id,
                "previous_session_id": previous_sid,
                "selected_session_id": session_id,
                "selected_strategy": strategy,
                "decision_timestamp": time.time(),
                "estimated_cost": estimated_cost,
                "prediction_confidence": prediction_confidence,
                "decision_reason": decision_reason or f"Anti autonomous routing: strategy={strategy}",
                "task_outcome": "submitted",
            })

            file_drop_hint = ""
            if ROLE_POLICIES.get(role, {}).get("edit") in ("allow", "ask"):
                file_drop_hint = (
                    "DELIVERABLE CONVENTION (FILE-DROP PATTERN): If your deliverable includes extensive reports, "
                    "long analyses, or comprehensive documentation, write it directly to a dedicated file within the project root "
                    "and provide a concise summary and relative file link in your final response.\n\n"
                )

            task_authorization_hint = ""
            if approved_shell_commands:
                task_authorization_hint = (
                    "TASK-SCOPED SHELL AUTHORIZATION: The user explicitly approved only the exact shell "
                    "resource strings listed below. Use only these exact commands; do not add arguments, "
                    "change paths, chain extra commands, or substitute another command. Any non-matching "
                    "shell action must remain subject to the normal approval request. These temporary "
                    "rules will be removed when the job reaches a terminal result.\n"
                    + "\n".join(f"- {command}" for command in approved_shell_commands)
                    + "\n\n"
                )

            prompt = (
                f"[WORKER ROLE: {role.upper()}] [SLOT: {slot}] [STRATEGY: {strategy.upper()}]\n"
                f"You are an independent OpenCode Go worker. Work only within this Antigravity Project root:\n{root}\n\n"
                "STRICT PROJECT BOUNDARY: Do not read, write, create, rename, delete, or execute against files outside this root. "
                "Do not follow symlinks or path references that escape this root. Never use relative paths containing '..'. If a required input is outside the root, stop and report it; do not access it. "
                "Do not create nested agents or delegate to subagents.\n\n"
                f"ROLE POLICY: {ROLE_POLICIES[role]['description']}\n"
                + file_drop_hint
                + task_authorization_hint
                + "Use the selected OpenCode Go model. Preserve source files unless the task explicitly requires changes. "
                "Report exactly which files you read or changed, what you verified, unresolved issues, and any permission requests.\n\n"
                "USER TASK:\n" + task.strip()
            )
            try:
                self.request("POST", f"/api/session/{urllib.parse.quote(session_id, safe='')}/prompt", {"text": prompt, "resume": True})
                self.store.update(job_id, "running")
            except Exception as exc:
                err_text = str(exc).lower()
                is_definite_not_sent = False
                is_explicit_api_error = False

                if "connection refused" in err_text or "10061" in err_text or isinstance(exc, ConnectionRefusedError):
                    is_definite_not_sent = True
                elif isinstance(exc, urllib.error.URLError) and isinstance(getattr(exc, "reason", None), ConnectionRefusedError):
                    is_definite_not_sent = True
                elif isinstance(exc, urllib.error.HTTPError):
                    is_explicit_api_error = True
                elif isinstance(exc, BridgeError) and exc.category in ("AUTH_KEY_EXPIRED", "AUTH_FAILED", "RATE_LIMITED", "SERVER_ERROR"):
                    is_explicit_api_error = True

                if is_definite_not_sent or is_explicit_api_error:
                    # 1. Definite failure: either connection refused before send or server explicitly returned an error response
                    self._release_session(session_id)
                    self.store.update_decision_outcome(job_id, "failed")
                    err_info = (
                        ErrorInfo(category=exc.category, user_message=exc.user_message, raw_detail=str(exc), status_code=exc.status_code)
                        if isinstance(exc, BridgeError) and exc.category
                        else classify_error(exc=exc)
                    )
                    self.store.update(job_id, "failed", err_info.to_json())
                    if isinstance(exc, BridgeError):
                        raise
                    raise BridgeError(
                        f"OpenCode prompt 提交失败：{exc}",
                        category=err_info.category,
                        user_message=err_info.user_message,
                    ) from exc

                # 2. Timeout or disconnected: request may have reached server!
                # DO NOT mark as failed! DO NOT release session lock! DO NOT automatically retry!
                # Attempt immediate status check on session:
                is_running = False
                try:
                    active_data = _unwrap_data(self.request("GET", "/api/session/active", timeout=2)) or {}
                    if isinstance(active_data, dict) and session_id in active_data:
                        is_running = True
                except Exception:
                    pass

                if not is_running:
                    try:
                        msgs = self._session_messages(session_id)
                        if msgs:
                            is_running = True
                    except Exception:
                        pass

                if is_running:
                    # Confirmed: server received prompt and is executing
                    self.store.update(job_id, "running")
                else:
                    # Cannot confirm: transition to submission_unknown!
                    unknown_info = ErrorInfo(
                        category="SUBMISSION_UNKNOWN",
                        user_message=(
                            "Prompt 提交超时或网络中断，当前无法确认服务器是否接收。"
                            "任务已标记为 submission_unknown 待查状态，严禁自动重复提交以避免重复扣费。"
                            "请调用 opencode_go_status 或 opencode_go_wait_result 进行状态核验。"
                        ),
                        raw_detail=str(exc),
                    )
                    self.store.update(job_id, "submission_unknown", unknown_info.to_json())
                    self.store.update_decision_outcome(job_id, "submission_unknown")
                    raise BridgeError(
                        f"Prompt 提交超时或响应丢失，状态未知（已登记为 submission_unknown，严禁重复提交）：{exc}",
                        category="SUBMISSION_UNKNOWN",
                        user_message=unknown_info.user_message,
                    ) from exc

            if benchmark_manifest and isinstance(benchmark_manifest, dict):
                try:
                    self.record_benchmark_manifest(
                        benchmark_name=benchmark_manifest.get("benchmark_name", "benchmark"),
                        benchmark_phase=benchmark_manifest.get("benchmark_phase", "unknown"),
                        model_id=model_id,
                        role=role,
                        session_strategy=strategy,
                        prompt_file=benchmark_manifest.get("prompt_file"),
                        input_file=benchmark_manifest.get("input_file"),
                        parameters=benchmark_manifest.get("parameters"),
                        job_id=job_id,
                        session_id=session_id,
                    )
                except Exception:
                    pass

            return {
                "job_id": job_id,
                "session_id": session_id,
                "worker_slot": slot,
                "session_strategy": strategy,
                "role": role,
                "model": f"opencode-go/{model_id}",
                "project_root": root,
                "state": "submitted",
                "server_pid": server_pid,
                "max_concurrent_default": DEFAULT_MAX_CONCURRENT,
                "active_workers_before_start": active_job_count,
                "permissions": ROLE_POLICIES[role]["description"],
                "task_authorization": {
                    "mode": "exact_shell_commands_only",
                    "approved_shell_commands": approved_shell_commands,
                    "cleanup": "reset_to_role_baseline_on_terminal_result" if approved_shell_commands else None,
                },
                "decision_id": decision_id,
                "important": "任务已异步提交。推荐使用 opencode_go_wait_result 或 opencode_go_status 查询，不要重复启动同一任务。",
            }

    def _load_job(self, job_id: str, raw_root: str) -> dict[str, Any]:
        root = canonical_project_root(raw_root)
        job = self.store.get(job_id)
        stored_root = os.path.normcase(os.path.normpath(job["project_root"]))
        requested_root = os.path.normcase(os.path.normpath(root))
        if stored_root != requested_root:
            raise BridgeError("该 Worker 不属于当前 Antigravity Project 根目录；拒绝跨项目读取或控制。")
        # If the manager's server was restarted, re-open it with this job's
        # recorded Project root rather than using the bridge installation path.
        self.ensure_server(root)
        return job

    def _session_messages(self, session_id: str) -> list[dict[str, Any]]:
        raw = self.request("GET", f"/api/session/{urllib.parse.quote(session_id, safe='')}/message?limit=100&order=asc")
        data = _unwrap_data(raw)
        if isinstance(data, list):
            return data
        if isinstance(raw, list):
            return raw
        return []

    def _status_data(self, job: dict[str, Any], messages: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        sid = job["session_id"]
        sid_path = urllib.parse.quote(sid, safe="")
        active_data = _unwrap_data(self.request("GET", "/api/session/active")) or {}
        active = isinstance(active_data, dict) and sid in active_data

        permissions_raw = self.request("GET", f"/api/session/{sid_path}/permission")
        permissions = _unwrap_data(permissions_raw)
        if not isinstance(permissions, list):
            permissions = []
        pending_permissions = [p for p in permissions if isinstance(p, dict)]

        # Lightweight path: do not read Session metadata or message history
        # while the Worker is active or waiting for permission.
        # Terminal states (failed, interrupted) are never overridden back to running.
        if messages is None and job.get("state") not in ("failed", "interrupted") and (active or pending_permissions):
            state = "waiting_for_permission" if pending_permissions else "running"
            self.store.update(job["job_id"], state, job.get("last_error"))
            output = {
                "job_id": job["job_id"],
                "session_id": sid,
                "role": job["role"],
                "model": f"opencode-go/{job['model_id']}",
                "project_root": job["project_root"],
                "state": state,
                "active": active,
                "pending_permissions": pending_permissions,
                "usage_so_far": None,
                "usage_note": (
                    "Worker is running or awaiting permission. Lightweight status "
                    "does not read message history, so live token totals and the "
                    "latest assistant excerpt are unavailable. Call "
                    "opencode_go_result after termination for final usage."
                ),
                "last_assistant_excerpt": None,
                "session_title": None,
                "session_time": None,
            }
            if job.get("last_error"):
                output["last_error"] = job.get("last_error")
            return output

        session_raw = self.request("GET", f"/api/session/{sid_path}")
        session = _unwrap_data(session_raw) or {}

        if messages is None:
            messages = self._session_messages(sid)

        totals = _token_totals(messages)
        last_assistant = None
        for item in reversed(messages):
            info = _message_info(item)
            if isinstance(info, dict) and _is_assistant_message(item):
                last_assistant = {
                    "info": info,
                    "text": _message_text(item),
                }
                break
        last_finish = last_assistant["info"].get("finish") if last_assistant else None

        error_info: ErrorInfo | None = None
        if last_assistant and last_assistant["info"].get("error"):
            error_info = classify_error(raw_payload=last_assistant["info"].get("error"))
        elif last_assistant and last_finish in ("error", "failed"):
            error_info = classify_error(raw_payload=last_assistant["text"] or last_assistant["info"])
        elif isinstance(session, dict) and session.get("error"):
            error_info = classify_error(raw_payload=session.get("error"))
        elif job.get("last_error"):
            try:
                parsed_last = json.loads(job["last_error"])
                if isinstance(parsed_last, dict) and "category" in parsed_last:
                    error_info = ErrorInfo(
                        category=parsed_last["category"],
                        user_message=parsed_last.get("message", ""),
                        raw_detail=parsed_last.get("detail", ""),
                        status_code=parsed_last.get("status_code"),
                    )
                else:
                    error_info = classify_error(raw_payload=job["last_error"])
            except Exception:
                error_info = classify_error(raw_payload=job["last_error"])

        if job.get("state") == "failed":
            state = "failed"
        elif job.get("state") == "interrupted":
            state = "interrupted"
        elif error_info and error_info.category in ("AUTH_KEY_EXPIRED", "AUTH_FAILED"):
            state = "failed"
        elif pending_permissions:
            state = "waiting_for_permission"
        elif active:
            state = "running"
        elif last_finish in ("stop", "completed", "end_turn"):
            state = "completed"
        elif last_finish in ("error", "failed"):
            state = "failed"
        elif job.get("state") == "submission_unknown":
            server_dead = True
            if self.server and _is_process_alive_and_matching(self.server.server_pid, self.server.server_start_time):
                server_dead = False
            if server_dead and not messages:
                state = "interrupted"
                self._release_session(job["session_id"])
            else:
                state = "submission_unknown"
        else:
            state = "idle_no_response"

        saved_error = error_info.to_json() if error_info else job.get("last_error")
        self.store.update(job["job_id"], state, saved_error)
        output = {
            "job_id": job["job_id"],
            "session_id": sid,
            "role": job["role"],
            "model": f"opencode-go/{job['model_id']}",
            "project_root": job["project_root"],
            "state": state,
            "active": active,
            "pending_permissions": pending_permissions,
            "usage_so_far": totals,
            "last_assistant_excerpt": (
                last_assistant["text"][:1800] if last_assistant else None
            ),
            "session_title": session.get("title") if isinstance(session, dict) else None,
            "session_time": session.get("time") if isinstance(session, dict) else None,
        }
        if error_info:
            output["error_category"] = error_info.category
            output["error_message"] = error_info.user_message
            output["error_detail"] = error_info.raw_detail
        elif job.get("last_error"):
            output["last_error"] = job.get("last_error")
        return output

    def job_status(self, job_id: str, raw_root: str) -> dict[str, Any]:
        return self._status_data(self._load_job(job_id, raw_root))

    def _format_job_result(self, payload: dict[str, Any], output_mode: str) -> dict[str, Any]:
        if output_mode not in ("summary", "full"):
            raise BridgeError("output_mode 仅允许 summary 或 full。", category=ErrorCategory.APPLICATION_ERROR)
        if output_mode == "full":
            return {**payload, "output_mode": "full"}

        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else []
        latest = messages[-1:] if messages else []
        compact_messages: list[dict[str, Any]] = []
        text_truncated = False
        if latest:
            message = dict(latest[0])
            text_value = message.get("text")
            if isinstance(text_value, str) and len(text_value) > 2400:
                message["text"] = text_value[:2400] + "\\n… [truncated; request output_mode='full' for complete text]"
                text_truncated = True
            compact_messages.append(message)

        return {
            **payload,
            "messages": compact_messages,
            "assistant_message_count": len(messages),
            "omitted_assistant_messages": max(0, len(messages) - len(compact_messages)),
            "last_message_text_truncated": text_truncated,
            "output_mode": "summary",
        }

    def job_result(
        self,
        job_id: str,
        raw_root: str,
        output_mode: str = "summary",
    ) -> dict[str, Any]:
        if output_mode not in ("summary", "full"):
            raise BridgeError("output_mode 仅允许 summary 或 full。", category=ErrorCategory.APPLICATION_ERROR)
        job = self._load_job(job_id, raw_root)

        # Keep the complete payload cached; retry permission cleanup if it was pending.
        if job.get("terminal_result"):
            try:
                cached = json.loads(job["terminal_result"])
                if isinstance(cached, dict):
                    cleanup = self._clear_task_permission_scope(job)
                    if cleanup:
                        cached["permission_scope_cleanup"] = cleanup
                        try:
                            self.store.update(
                                job_id,
                                cached.get("state", job.get("state", "completed")),
                                error=job.get("last_error"),
                                terminal_result=json.dumps(cached, ensure_ascii=False),
                            )
                        except Exception:
                            pass
                    return self._format_job_result(cached, output_mode)
            except Exception:
                pass

        messages = self._session_messages(job["session_id"])
        status = self._status_data(job, messages=messages)
        assistant_messages = []
        for item in messages:
            info = _message_info(item)
            if isinstance(info, dict) and _is_assistant_message(item):
                assistant_messages.append({
                    "message_id": info.get("id"),
                    "finish": info.get("finish"),
                    "text": _message_text(item),
                    "tokens": info.get("tokens"),
                    "cost_usd": info.get("cost"),
                    "model": info.get("model"),
                    "time": info.get("time"),
                })

        permission_cleanup = None
        if status.get("state") in ("completed", "failed", "interrupted"):
            permission_cleanup = self._clear_task_permission_scope(job)

        result_payload = {
            **status,
            "messages": assistant_messages,
            "usage_total_observed": status["usage_so_far"],
            "usage_note": "Token 数和费用由 OpenCode API 的 assistant message 用量汇总；它们是本 Session 的消息级观测值，不是模型的额度余额。Reasoning、Cache Read/Write 分项保留，不与普通 Input/Output 混算。",
        }
        if permission_cleanup:
            result_payload["permission_scope_cleanup"] = permission_cleanup

        if status.get("state") in ("completed", "failed", "interrupted"):
            self._release_session(job["session_id"])
            try:
                self.store.update(
                    job_id,
                    status["state"],
                    error=job.get("last_error"),
                    terminal_result=json.dumps(result_payload, ensure_ascii=False),
                )
            except Exception:
                pass
            try:
                self.store.update_decision_outcome(
                    job_id=job_id,
                    task_outcome=status["state"],
                    actual_usage=status.get("usage_so_far"),
                )
            except Exception:
                pass

        return self._format_job_result(result_payload, output_mode)

    def wait_result(
        self,
        job_id: str,
        raw_root: str,
        timeout_seconds: int = 300,
        poll_interval: float = 2.0,
        output_mode: str = "summary",
    ) -> dict[str, Any]:
        """Wait inside Manager and return the compact terminal result by default."""
        if output_mode not in ("summary", "full"):
            raise BridgeError("output_mode 仅允许 summary 或 full。", category=ErrorCategory.APPLICATION_ERROR)
        job = self._load_job(job_id, raw_root)
        timeout = max(1, min(int(timeout_seconds), 300))
        interval = max(0.5, min(float(poll_interval), 10.0))

        if job.get("state") in ("completed", "failed", "interrupted"):
            return self.job_result(job_id, raw_root, output_mode=output_mode)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            st = self.job_status(job_id, raw_root)
            if st.get("pending_permissions"):
                return {
                    **st,
                    "timed_out": False,
                    "wait_status": "waiting_for_permission",
                    "note": "Worker is awaiting user permission. Show the exact action/resource and obtain explicit user approval before replying once or reject.",
                }
            if not st.get("active") or st.get("state") in ("completed", "failed", "interrupted"):
                return self.job_result(job_id, raw_root, output_mode=output_mode)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))

        latest_status = self.job_status(job_id, raw_root)
        return {
            **latest_status,
            "timed_out": True,
            "wait_status": "timeout_expired",
            "wait_seconds": timeout,
            "note": f"Wait timeout ({timeout}s) expired. Worker is still executing. Call opencode_go_wait_result again; do not start a duplicate job.",
        }

    def list_permissions(self, job_id: str, raw_root: str) -> dict[str, Any]:
        job = self._load_job(job_id, raw_root)
        sid = urllib.parse.quote(job["session_id"], safe="")
        raw = self.request("GET", f"/api/session/{sid}/permission")
        data = _unwrap_data(raw)
        return {"job_id": job_id, "session_id": job["session_id"], "pending_permissions": data if isinstance(data, list) else []}

    def reply_permission(self, job_id: str, raw_root: str, request_id: str, decision: str, message: str | None = None) -> dict[str, Any]:
        if decision not in ("once", "reject"):
            raise BridgeError("decision 仅允许 once 或 reject；不开放 always，避免永久扩大权限。")
        job = self._load_job(job_id, raw_root)
        sid = urllib.parse.quote(job["session_id"], safe="")
        rid = urllib.parse.quote(request_id, safe="")
        body: dict[str, Any] = {"decision": decision}
        if message:
            body["message"] = message[:1000]
        self.request("POST", f"/api/session/{sid}/permission/{rid}/reply", body)
        return {"job_id": job_id, "session_id": job["session_id"], "request_id": request_id, "decision": decision}

    def cancel_worker(self, job_id: str, raw_root: str) -> dict[str, Any]:
        job = self._load_job(job_id, raw_root)
        sid = urllib.parse.quote(job["session_id"], safe="")

        # Idempotent cancel: return terminal confirmation if already finalized
        if job.get("state") in ("completed", "failed", "interrupted"):
            return {
                "job_id": job_id,
                "session_id": job["session_id"],
                "state": job["state"],
                "verified_stopped": True,
                "note": f"Worker is already in terminal state ({job['state']}).",
            }

        response = self.request("POST", f"/api/session/{sid}/interrupt")

        # Verify session exits active list within limited deadline
        deadline = time.monotonic() + 5.0
        stopped = False
        while time.monotonic() < deadline:
            try:
                active_data = _unwrap_data(self.request("GET", "/api/session/active", timeout=3)) or {}
                if not (isinstance(active_data, dict) and job["session_id"] in active_data):
                    stopped = True
                    break
            except Exception:
                pass
            time.sleep(0.4)

        if stopped:
            self._release_session(job["session_id"])
            self.store.update(job_id, "interrupted")
            self.store.update_decision_outcome(job_id, "interrupted")
            state = "interrupted"
        else:
            state = "interrupt_requested"

        return {
            "job_id": job_id,
            "session_id": job["session_id"],
            "state": state,
            "verified_stopped": stopped,
            "response": _unwrap_data(response),
        }

    def reconcile_unfinalized_jobs(self) -> dict[str, Any]:
        """Reconcile unfinalized jobs against running processes and session states.

        Called at Manager startup or on demand.
        - Checks recorded server PID and process creation time.
        - If the process is dead or was replaced (PID reuse), converges cleanly to 'interrupted'.
        - If the process is alive, checks active session state and converges if finished.
        - Never automatically re-runs or restarts old jobs.
        """
        unfinalized = self.store.list_unfinalized()
        reconciled = []
        for job in unfinalized:
            job_id = job["job_id"]
            sid = job["session_id"]
            spid = job.get("server_pid")
            sstart = job.get("server_start_time")

            server_alive = False
            if spid is not None and sstart is not None:
                server_alive = _is_process_alive_and_matching(spid, sstart)
            elif self.server and self.server.process.poll() is None:
                server_alive = True

            if not server_alive:
                err_payload = json.dumps({
                    "category": "PROCESS_TERMINATED",
                    "message": "宿主/服务进程已退出，原 Worker 执行已中断。",
                    "detail": f"Recorded server PID {spid} with start_time {sstart} is no longer running.",
                }, ensure_ascii=False)
                self.store.update(job_id, "interrupted", error=err_payload)
                self._release_session(sid)
                reconciled.append({
                    "job_id": job_id,
                    "session_id": sid,
                    "previous_state": job["state"],
                    "new_state": "interrupted",
                    "reason": "server_process_terminated",
                })
            else:
                try:
                    active_data = _unwrap_data(self.request("GET", "/api/session/active", timeout=5)) or {}
                    is_active = isinstance(active_data, dict) and sid in active_data
                    if is_active:
                        if job["state"] == "submission_unknown":
                            self.store.update(job_id, "running")
                            reconciled.append({
                                "job_id": job_id,
                                "session_id": sid,
                                "previous_state": "submission_unknown",
                                "new_state": "running",
                                "reason": "session_confirmed_active",
                            })
                    else:
                        try:
                            final_status = self._status_data(job)
                            new_st = final_status.get("state", "interrupted")
                            if new_st in ("running", "submitted"):
                                new_st = "interrupted"
                            self.store.update(job_id, new_st, error=job.get("last_error"))
                            reconciled.append({
                                "job_id": job_id,
                                "session_id": sid,
                                "previous_state": job["state"],
                                "new_state": new_st,
                                "reason": "session_inactive",
                            })
                        except Exception:
                            self.store.update(job_id, "interrupted", error="Session inactive and status cannot be retrieved")
                            reconciled.append({
                                "job_id": job_id,
                                "session_id": sid,
                                "previous_state": job["state"],
                                "new_state": "interrupted",
                                "reason": "session_inactive_unretrievable",
                            })
                except Exception:
                    pass
        return {"reconciled_count": len(reconciled), "reconciled_jobs": reconciled}

    def record_benchmark_manifest(
        self,
        benchmark_name: str,
        benchmark_phase: str,
        model_id: str,
        role: str,
        session_strategy: str,
        prompt_file: str | None = None,
        input_file: str | None = None,
        parameters: dict[str, Any] | None = None,
        job_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        manifest_id = f"bm_{uuid.uuid4().hex[:12]}"
        prompt_sha256 = _compute_file_sha256(prompt_file) if prompt_file and os.path.exists(prompt_file) else None
        input_sha256 = _compute_file_sha256(input_file) if input_file and os.path.exists(input_file) else None
        manifest_data = {
            "manifest_id": manifest_id,
            "job_id": job_id,
            "session_id": session_id,
            "benchmark_name": benchmark_name,
            "benchmark_phase": benchmark_phase,
            "prompt_file": prompt_file,
            "prompt_sha256": prompt_sha256,
            "input_file": input_file,
            "input_sha256": input_sha256,
            "model_id": model_id,
            "role": role,
            "session_strategy": session_strategy,
            "parameters": parameters or {},
            "created_at": time.time(),
        }
        self.store.add_manifest(manifest_data)
        return manifest_data

    def get_benchmark_manifest(self, identifier: str) -> dict[str, Any]:
        res = self.store.get_manifest(identifier)
        if not res:
            raise BridgeError(f"未找到基准 Manifest: {identifier}", category=ErrorCategory.APPLICATION_ERROR)
        return res

    def list_jobs(self, raw_root: str, limit: int = 30) -> dict[str, Any]:
        root = canonical_project_root(raw_root)
        bounded_limit = max(1, min(int(limit), 100))
        jobs = self.store.list(root, bounded_limit)
        return {"project_root": root, "jobs": jobs, "count": len(jobs), "store_path": str(self.store.path)}

    def usage_report(self, period: str = "all", include_recent: bool = True) -> dict[str, Any]:
        valid_periods = ("all", "30d", "7d")
        if not isinstance(period, str) or period.lower() not in valid_periods:
            raise BridgeError(f"无效的 period={period!r}；支持的值为：{', '.join(valid_periods)}。")
        norm_period = period.lower()

        if isinstance(include_recent, bool):
            norm_recent = include_recent
        elif isinstance(include_recent, str):
            norm_recent = include_recent.lower() in ("true", "1", "yes")
        elif isinstance(include_recent, (int, float)):
            norm_recent = bool(include_recent)
        else:
            raise BridgeError("include_recent 必须是布尔值 (true/false)。")

        now = time.time()
        cutoff: float | None = None
        if norm_period == "7d":
            cutoff = now - 7 * 86400
        elif norm_period == "30d":
            cutoff = now - 30 * 86400

        data_quality = {
            "missing_records": 0,
            "malformed_records": 0,
            "unmatched_sessions": 0,
        }

        # 1. Antigravity Token Meter 统计
        ledger = _antigravity_ledger_path()
        ag_task_count = 0
        ag_planner_responses = 0
        ag_input_uncached = 0
        ag_cache_read = 0
        ag_output = 0
        ag_period_start: str | None = None
        ag_period_end: str | None = None
        ag_by_project: dict[str, dict[str, Any]] = {}

        if not ledger.is_file():
            data_quality["missing_records"] += 1
        else:
            try:
                with ledger.open("r", encoding="utf-8") as f:
                    for line in f:
                        raw = line.strip()
                        if not raw:
                            continue
                        try:
                            row = json.loads(raw)
                        except Exception:
                            data_quality["malformed_records"] += 1
                            continue
                        if not isinstance(row, dict):
                            data_quality["malformed_records"] += 1
                            continue

                        started_str = row.get("started_at")
                        finished_str = row.get("finished_at")
                        ts = _parse_iso_timestamp(finished_str) or _parse_iso_timestamp(started_str)

                        if cutoff is not None:
                            if ts is None:
                                data_quality["malformed_records"] += 1
                                continue
                            if ts < cutoff:
                                continue

                        ag_task_count += 1
                        pr = int(row.get("planner_responses") or 0)
                        uncached = int(row.get("input_uncached_tokens") or 0)
                        cached = int(row.get("input_cache_read_tokens") or 0)
                        out = int(row.get("output_tokens") or 0)

                        ag_planner_responses += pr
                        ag_input_uncached += uncached
                        ag_cache_read += cached
                        ag_output += out

                        if started_str:
                            if ag_period_start is None or started_str < ag_period_start:
                                ag_period_start = started_str
                        if finished_str:
                            if ag_period_end is None or finished_str > ag_period_end:
                                ag_period_end = finished_str

                        ws_list = row.get("workspace_paths") or ["global"]
                        for ws in ws_list:
                            norm_ws = os.path.normpath(ws).replace("\\", "/")
                            if norm_ws not in ag_by_project:
                                ag_by_project[norm_ws] = {
                                    "project": norm_ws,
                                    "task_count": 0,
                                    "planner_responses": 0,
                                    "input_uncached": 0,
                                    "cache_read": 0,
                                    "input_total": 0,
                                    "output": 0,
                                    "total_recorded": 0,
                                }
                            p = ag_by_project[norm_ws]
                            p["task_count"] += 1
                            p["planner_responses"] += pr
                            p["input_uncached"] += uncached
                            p["cache_read"] += cached
                            p["input_total"] += (uncached + cached)
                            p["output"] += out
                            p["total_recorded"] += (uncached + cached + out)
            except Exception:
                data_quality["missing_records"] += 1

        ag_input_total = ag_input_uncached + ag_cache_read
        ag_total_recorded = ag_input_total + ag_output
        antigravity_report = {
            "task_count": ag_task_count,
            "planner_responses": ag_planner_responses,
            "input_uncached": ag_input_uncached,
            "cache_read": ag_cache_read,
            "output": ag_output,
            "input_total": ag_input_total,
            "total_recorded": ag_total_recorded,
            "period_start": ag_period_start,
            "period_end": ag_period_end,
            "by_project": list(ag_by_project.values()),
        }

        # 2. OpenCode Go Worker Manager 统计
        jobs_db = jobs_db_path()
        filtered_jobs: list[dict[str, Any]] = []

        if not jobs_db.is_file():
            data_quality["missing_records"] += 1
        else:
            con = None
            try:
                con = sqlite3.connect(jobs_db, timeout=10)
                con.row_factory = sqlite3.Row
                cursor = con.execute("SELECT job_id, session_id, project_root, role, model_id, title, state, created_at, updated_at, last_error FROM jobs ORDER BY created_at DESC")
                for r in cursor.fetchall():
                    job_dict = dict(r)
                    c_at = float(job_dict.get("created_at") or 0)
                    if cutoff is not None and c_at < cutoff:
                        continue
                    filtered_jobs.append(job_dict)
            except Exception:
                data_quality["missing_records"] += 1
            finally:
                if con:
                    try:
                        con.close()
                    except Exception:
                        pass

        unique_sessions: dict[str, dict[str, Any]] = {}
        for j in filtered_jobs:
            sid = j["session_id"]
            if sid not in unique_sessions:
                unique_sessions[sid] = j

        oc_db_file = _opencode_db_path()
        session_tokens_map: dict[str, dict[str, Any]] = {}

        if oc_db_file and oc_db_file.is_file():
            con = None
            try:
                uri_path = f"file:{oc_db_file.as_posix()}?mode=ro"
                con = sqlite3.connect(uri_path, uri=True, timeout=5)
            except Exception:
                try:
                    con = sqlite3.connect(str(oc_db_file), timeout=5)
                except Exception:
                    con = None

            if con:
                try:
                    con.row_factory = sqlite3.Row
                    for sid in unique_sessions:
                        c = con.cursor()
                        c.execute(
                            "SELECT tokens_input, tokens_output, tokens_reasoning, tokens_cache_read, tokens_cache_write, cost, idle_outcome FROM session_v2 WHERE id = ?",
                            (sid,),
                        )
                        row = c.fetchone()
                        if row:
                            c.execute("SELECT count(*) FROM session_message WHERE session_id = ? AND type = 'assistant'", (sid,))
                            cnt_row = c.fetchone()
                            msg_cnt = int(cnt_row[0] or 0) if cnt_row else 0
                            session_tokens_map[sid] = {
                                "input": int(row["tokens_input"] or 0),
                                "output": int(row["tokens_output"] or 0),
                                "reasoning": int(row["tokens_reasoning"] or 0),
                                "cache_read": int(row["tokens_cache_read"] or 0),
                                "cache_write": int(row["tokens_cache_write"] or 0),
                                "cost_usd": float(row["cost"] or 0.0),
                                "assistant_messages": msg_cnt,
                                "available": True,
                            }
                        else:
                            data_quality["unmatched_sessions"] += 1
                            session_tokens_map[sid] = {"available": False}
                except Exception:
                    pass
                finally:
                    try:
                        con.close()
                    except Exception:
                        pass
            else:
                for sid in unique_sessions:
                    data_quality["unmatched_sessions"] += 1
                    session_tokens_map[sid] = {"available": False}
        else:
            for sid in unique_sessions:
                data_quality["unmatched_sessions"] += 1
                session_tokens_map[sid] = {"available": False}

        oc_job_count = len(filtered_jobs)
        oc_completed_count = sum(1 for j in filtered_jobs if j.get("state") == "completed")
        oc_failed_count = sum(1 for j in filtered_jobs if j.get("state") == "failed")
        oc_running_count = sum(1 for j in filtered_jobs if j.get("state") == "running")
        oc_other_count = oc_job_count - (oc_completed_count + oc_failed_count + oc_running_count)

        oc_input = sum(session_tokens_map[s]["input"] for s in session_tokens_map if session_tokens_map[s].get("available"))
        oc_output = sum(session_tokens_map[s]["output"] for s in session_tokens_map if session_tokens_map[s].get("available"))
        oc_reasoning = sum(session_tokens_map[s]["reasoning"] for s in session_tokens_map if session_tokens_map[s].get("available"))
        oc_cache_read = sum(session_tokens_map[s]["cache_read"] for s in session_tokens_map if session_tokens_map[s].get("available"))
        oc_cache_write = sum(session_tokens_map[s]["cache_write"] for s in session_tokens_map if session_tokens_map[s].get("available"))
        oc_cost = sum(session_tokens_map[s]["cost_usd"] for s in session_tokens_map if session_tokens_map[s].get("available"))
        oc_assistant_msgs = sum(session_tokens_map[s]["assistant_messages"] for s in session_tokens_map if session_tokens_map[s].get("available"))

        oc_period_start: str | None = None
        oc_period_end: str | None = None
        if filtered_jobs:
            min_created = min(float(j.get("created_at") or 0) for j in filtered_jobs)
            max_updated = max(float(j.get("updated_at") or 0) for j in filtered_jobs)
            if min_created > 0:
                oc_period_start = datetime.fromtimestamp(min_created, timezone.utc).isoformat()
            if max_updated > 0:
                oc_period_end = datetime.fromtimestamp(max_updated, timezone.utc).isoformat()

        # 按 Project 分组
        oc_by_project: dict[str, dict[str, Any]] = {}
        for j in filtered_jobs:
            proj = os.path.normpath(j["project_root"]).replace("\\", "/")
            if proj not in oc_by_project:
                oc_by_project[proj] = {
                    "project": proj,
                    "job_count": 0,
                    "completed_count": 0,
                    "failed_count": 0,
                    "running_count": 0,
                    "input": 0,
                    "output": 0,
                    "reasoning": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "cost_usd": 0.0,
                }
            entry = oc_by_project[proj]
            entry["job_count"] += 1
            st = j.get("state")
            if st == "completed":
                entry["completed_count"] += 1
            elif st == "failed":
                entry["failed_count"] += 1
            elif st == "running":
                entry["running_count"] += 1

        for sid, j in unique_sessions.items():
            proj = os.path.normpath(j["project_root"]).replace("\\", "/")
            entry = oc_by_project.get(proj)
            toks = session_tokens_map.get(sid, {})
            if entry and toks.get("available"):
                entry["input"] += toks["input"]
                entry["output"] += toks["output"]
                entry["reasoning"] += toks["reasoning"]
                entry["cache_read"] += toks["cache_read"]
                entry["cache_write"] += toks["cache_write"]
                entry["cost_usd"] += toks["cost_usd"]

        # 按 Model 分组
        oc_by_model: dict[str, dict[str, Any]] = {}
        for sid, j in unique_sessions.items():
            m = str(j.get("model_id") or "unknown")
            if not m.startswith("opencode-go/"):
                m = f"opencode-go/{m}"
            if m not in oc_by_model:
                oc_by_model[m] = {
                    "model": m,
                    "job_count": 0,
                    "input": 0,
                    "output": 0,
                    "reasoning": 0,
                    "cache_read": 0,
                    "cache_write": 0,
                    "cost_usd": 0.0,
                }
            entry = oc_by_model[m]
            toks = session_tokens_map.get(sid, {})
            if toks.get("available"):
                entry["input"] += toks["input"]
                entry["output"] += toks["output"]
                entry["reasoning"] += toks["reasoning"]
                entry["cache_read"] += toks["cache_read"]
                entry["cache_write"] += toks["cache_write"]
                entry["cost_usd"] += toks["cost_usd"]

        for j in filtered_jobs:
            m = str(j.get("model_id") or "unknown")
            if not m.startswith("opencode-go/"):
                m = f"opencode-go/{m}"
            if m in oc_by_model:
                oc_by_model[m]["job_count"] += 1

        recent_jobs_list: list[dict[str, Any]] = []
        if norm_recent:
            for j in filtered_jobs[:10]:
                sid = j["session_id"]
                toks = session_tokens_map.get(sid, {})
                c_ts = float(j.get("created_at") or 0)
                created_iso = datetime.fromtimestamp(c_ts, timezone.utc).isoformat() if c_ts > 0 else None
                rec = {
                    "job_id": j["job_id"],
                    "session_id": sid,
                    "role": j["role"],
                    "model": f"opencode-go/{j['model_id']}" if not str(j['model_id']).startswith("opencode-go/") else j['model_id'],
                    "project": os.path.normpath(j["project_root"]).replace("\\", "/"),
                    "state": j["state"],
                    "created_at": created_iso,
                    "tokens": (
                        {
                            "input": toks["input"],
                            "output": toks["output"],
                            "reasoning": toks["reasoning"],
                            "cache_read": toks["cache_read"],
                            "cache_write": toks["cache_write"],
                            "cost_usd": toks["cost_usd"],
                        }
                        if toks.get("available")
                        else "unavailable"
                    ),
                }
                recent_jobs_list.append(rec)

        opencode_report = {
            "job_count": oc_job_count,
            "completed_count": oc_completed_count,
            "failed_count": oc_failed_count,
            "running_count": oc_running_count,
            "other_count": oc_other_count,
            "assistant_messages": oc_assistant_msgs,
            "input": oc_input,
            "output": oc_output,
            "reasoning": oc_reasoning,
            "cache_read": oc_cache_read,
            "cache_write": oc_cache_write,
            "cost_usd": oc_cost,
            "period_start": oc_period_start,
            "period_end": oc_period_end,
            "by_project": list(oc_by_project.values()),
            "by_model": list(oc_by_model.values()),
            "recent_jobs": recent_jobs_list if norm_recent else [],
        }

        return {
            "period": norm_period,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "notes": [
                "Antigravity Token Meter 统计来自本地 task_usage.jsonl，反映 Supervisor 会话消耗（Gemini 模型），按任务边界在 Stop 钩子完全空闲后记录。",
                "OpenCode Worker 统计来自本地 worker_jobs.sqlite3 与 OpenCode 数据库，反映独立 Worker 消耗（Go 模型）。",
                "两平台 Token 属于不同模型与运行时，未做跨平台任务级绑定，保持独立核算，严禁将 OpenCode Reasoning 计入 Output，严禁将 Cache Read 混入普通 Input。"
            ],
            "antigravity": antigravity_report,
            "opencode_worker": opencode_report,
            "data_quality": data_quality,
        }


CLIENT = OpenCodeClient()


def shutdown() -> None:
    CLIENT.shutdown()
