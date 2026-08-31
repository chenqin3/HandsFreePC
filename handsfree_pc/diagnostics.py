from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import re
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_LOG_BYTES = 2 * 1024 * 1024
LOG_BACKUP_COUNT = 5
DEFAULT_TAIL_COUNT = 50
MAX_TAIL_COUNT = 500

DIAGNOSTIC_STAGES = frozenset(
    {
        "native_route",
        "list_apps",
        "plan",
        "observe_driver",
        "observe_safety",
        "action_safety",
        "execute",
        "reobserve",
        "verify_action",
        "verify_completion",
        "runtime",
    }
)

_ALLOWED_LEVELS = frozenset({"debug", "info", "warning", "error"})
_SAFE_EVENT_FIELDS = (
    "timestamp",
    "level",
    "session_id",
    "command_id",
    "sequence",
    "stage",
    "error_code",
    "exception_type",
    "app",
    "generation",
    "safe_message",
)
_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_.-]{0,63}$")
_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,95}$")
_APP_RE = re.compile(r"^[\w .+-]{1,64}$", re.UNICODE)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,96}$")
_WINDOWS_PATH_RE = re.compile(
    r"(?i)(?<![\w])(?:[a-z]:\\|\\\\)[^\s\"'<>|]{2,}",
)
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]{1,48}PRIVATE KEY-----", re.IGNORECASE)
_OPENAI_KEY_RE = re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{12,}\b", re.IGNORECASE)
_GITHUB_TOKEN_RE = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b",
    re.IGNORECASE,
)
_AWS_KEY_RE = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\."
    r"[A-Za-z0-9_-]{6,}(?![A-Za-z0-9_-])"
)
_OPAQUE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z0-9_+/=.-]{40,512}(?![A-Za-z0-9_])"
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class DiagnosticStatus:
    stage: str
    error_code: str
    safe_message: str


_CONTROL_FAILURE_RULES: tuple[tuple[tuple[str, ...], DiagnosticStatus], ...] = (
    (
        ("native_", "确定性本机", "本机动作"),
        DiagnosticStatus(
            "native_route",
            "NATIVE_ROUTE_FAILED",
            "确定性本机操作没有完成",
        ),
    ),
    (
        ("桌面驱动不可用", "应用清单", "app inventory", "list_apps"),
        DiagnosticStatus(
            "list_apps",
            "APP_INVENTORY_FAILED",
            "无法建立当前可见应用清单",
        ),
    ),
    (
        ("规划器", "planner", "plan failed"),
        DiagnosticStatus(
            "plan",
            "DESKTOP_PLAN_FAILED",
            "单步规划器没有返回可执行的安全步骤",
        ),
    ),
    (
        ("安全策略阻止读取", "安全策略阻止观察", "界面不能发送给规划器"),
        DiagnosticStatus(
            "observe_safety",
            "OBSERVATION_SAFETY_BLOCKED",
            "本地界面安全检查未通过",
        ),
    ),
    (
        ("桌面观察失败", "observe failed", "observation failed"),
        DiagnosticStatus(
            "observe_driver",
            "DESKTOP_OBSERVE_FAILED",
            "桌面驱动未能建立可用的界面观察",
        ),
    ),
    (
        ("安全策略阻止动作", "安全分类", "action safety"),
        DiagnosticStatus(
            "action_safety",
            "ACTION_SAFETY_BLOCKED",
            "本地动作安全检查未通过",
        ),
    ),
    (
        ("动作后本地验收", "verify_action", "action verification"),
        DiagnosticStatus(
            "verify_action",
            "ACTION_VERIFICATION_FAILED",
            "动作后的本地界面变化未通过验收",
        ),
    ),
    (
        (
            "动作后任务条件",
            "本地完成条件",
            "全部桌面步骤",
            "达到最大单步数",
            "verify_completion",
            "completion verification",
        ),
        DiagnosticStatus(
            "verify_completion",
            "COMPLETION_VERIFICATION_FAILED",
            "用户要求的最终界面条件尚未得到本地证明",
        ),
    ),
    (
        ("桌面动作", "执行失败", "execute failed", "刷新失败"),
        DiagnosticStatus(
            "execute",
            "DESKTOP_EXECUTION_FAILED",
            "桌面动作或动作后的刷新没有完成",
        ),
    ),
)

_STAGE_DISPLAY_NAMES = {
    "native_route": "本机指令路由",
    "list_apps": "可见应用检查",
    "plan": "单步规划",
    "observe_driver": "桌面观察",
    "observe_safety": "界面安全检查",
    "action_safety": "动作安全检查",
    "execute": "桌面执行",
    "reobserve": "动作后刷新",
    "verify_action": "动作验收",
    "verify_completion": "完成条件验收",
    "runtime": "运行时",
}


def classify_control_failure(
    message: object,
    *,
    error_type: object = None,
    stage: object = None,
    error_code: object = None,
    safe_message: object = None,
) -> DiagnosticStatus:
    """Convert a controller failure to a fixed, non-content-bearing status.

    Structured controller fields win when valid. The legacy message is used only for literal
    category matching and is never copied into the returned status or diagnostics log.
    """

    if isinstance(stage, str) and stage in DIAGNOSTIC_STAGES:
        code = _safe_code(error_code, fallback="CONTROL_COMMAND_FAILED")
        summary = sanitize_safe_message(safe_message) if isinstance(safe_message, str) else ""
        if not summary or summary == "Diagnostic detail unavailable":
            summary = "电脑控制指令在本地处理期间失败"
        return DiagnosticStatus(stage, code, summary)

    normalized_error = _safe_exception_type(error_type)
    if normalized_error == "ConfirmationChallengeUnavailable":
        return DiagnosticStatus(
            "runtime",
            "CONFIRMATION_CHALLENGE_UNAVAILABLE",
            "无法签发唯一的语音确认口令，操作已取消",
        )
    normalized = message.casefold() if isinstance(message, str) else ""
    for needles, status in _CONTROL_FAILURE_RULES:
        if any(needle.casefold() in normalized for needle in needles):
            return status
    return DiagnosticStatus(
        "runtime",
        "CONTROL_COMMAND_FAILED",
        "电脑控制指令在本地处理期间失败",
    )


def stage_display_name(stage: str) -> str:
    return _STAGE_DISPLAY_NAMES.get(stage, "运行时")


def default_log_path(environment: Mapping[str, str] | None = None) -> Path:
    """Return the per-user diagnostics path without consulting project configuration."""

    values = os.environ if environment is None else environment
    local_app_data = values.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "HandsFreePC" / "logs" / "handsfreepc.jsonl"


def sanitize_safe_message(value: object, *, max_chars: int = 240) -> str:
    """Bound and redact a caller-authored diagnostic summary.

    This is a last line of defence, not permission to pass prompts, UIA text, screenshots,
    exception strings, or credential matches. Callers must construct a short categorical message.
    """

    text = value if isinstance(value, str) else "Diagnostic detail unavailable"
    text = _CONTROL_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    for pattern in (
        _PRIVATE_KEY_RE,
        _OPENAI_KEY_RE,
        _GITHUB_TOKEN_RE,
        _AWS_KEY_RE,
        _BEARER_RE,
        _JWT_RE,
        _OPAQUE_TOKEN_RE,
    ):
        text = pattern.sub("[REDACTED]", text)
    text = _WINDOWS_PATH_RE.sub("[LOCAL_PATH]", text)
    if not text:
        text = "Diagnostic detail unavailable"
    if len(text) > max_chars:
        text = f"{text[: max_chars - 14].rstrip()}...[truncated]"
    return text


def _safe_identifier(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if _IDENTIFIER_RE.fullmatch(value):
        return value
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"sha256:{digest}"


def _safe_code(value: object, *, fallback: str) -> str:
    if isinstance(value, str):
        normalized = value.strip().upper().replace(" ", "_")
        if _CODE_RE.fullmatch(normalized):
            return normalized
    return fallback


def _safe_exception_type(value: object) -> str | None:
    if isinstance(value, type) and issubclass(value, BaseException):
        value = value.__name__
    if isinstance(value, BaseException):
        value = type(value).__name__
    if isinstance(value, str) and _TYPE_RE.fullmatch(value):
        return value
    return None


def _safe_app(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = _SPACE_RE.sub(" ", value).strip().casefold()
    return normalized if _APP_RE.fullmatch(normalized) else None


def _safe_nonnegative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "diagnostic_event", None)
        if not isinstance(event, dict):
            event = {
                "timestamp": _utc_timestamp(),
                "level": "error",
                "stage": "runtime",
                "error_code": "INVALID_DIAGNOSTIC_RECORD",
                "safe_message": "An invalid diagnostic record was discarded",
            }
        return json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


class Diagnostics:
    """Write privacy-bounded JSONL events to one rotating per-user log."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_log_path()
        self.path = self.path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._logger = logging.Logger(f"handsfree_pc.diagnostics.{id(self)}", logging.DEBUG)
        self._logger.propagate = False
        handler = logging.handlers.RotatingFileHandler(
            self.path,
            maxBytes=MAX_LOG_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(_JsonLineFormatter())
        self._logger.addHandler(handler)
        self._handler = handler

    def event(
        self,
        *,
        stage: str,
        error_code: str,
        safe_message: str,
        level: str = "error",
        session_id: str | None = None,
        command_id: str | None = None,
        sequence: int | None = None,
        exception_type: str | type[BaseException] | BaseException | None = None,
        app: str | None = None,
        generation: int | None = None,
    ) -> dict[str, Any]:
        """Record only the fixed safe schema; arbitrary payload fields are not accepted."""

        normalized_stage = stage if stage in DIAGNOSTIC_STAGES else "runtime"
        normalized_level = level.casefold() if isinstance(level, str) else "error"
        if normalized_level not in _ALLOWED_LEVELS:
            normalized_level = "error"
        event: dict[str, Any] = {
            "timestamp": _utc_timestamp(),
            "level": normalized_level,
            "stage": normalized_stage,
            "error_code": _safe_code(error_code, fallback="UNCLASSIFIED_ERROR"),
            "safe_message": sanitize_safe_message(safe_message),
        }
        optional = {
            "session_id": _safe_identifier(session_id),
            "command_id": _safe_identifier(command_id),
            "sequence": _safe_nonnegative_int(sequence),
            "exception_type": _safe_exception_type(exception_type),
            "app": _safe_app(app),
            "generation": _safe_nonnegative_int(generation),
        }
        for key in _SAFE_EVENT_FIELDS:
            if key in optional and optional[key] is not None:
                event[key] = optional[key]
        with self._lock:
            self._logger.log(
                getattr(logging, normalized_level.upper(), logging.ERROR),
                "diagnostic_event",
                extra={"diagnostic_event": event},
            )
        return dict(event)

    def close(self) -> None:
        with self._lock:
            self._logger.removeHandler(self._handler)
            self._handler.close()


_default_lock = threading.Lock()
_default_diagnostics: Diagnostics | None = None


def configure_diagnostics(path: str | Path | None = None) -> Diagnostics:
    """Create or replace the process-wide diagnostics sink."""

    global _default_diagnostics
    with _default_lock:
        previous = _default_diagnostics
        _default_diagnostics = Diagnostics(path)
        if previous is not None:
            previous.close()
        return _default_diagnostics


def get_diagnostics() -> Diagnostics:
    global _default_diagnostics
    with _default_lock:
        if _default_diagnostics is None:
            _default_diagnostics = Diagnostics()
        return _default_diagnostics


def _log_files(path: Path) -> Iterator[Path]:
    # RotatingFileHandler uses .1 for the newest backup. Read oldest -> newest.
    for index in range(LOG_BACKUP_COUNT, 0, -1):
        candidate = Path(f"{path}.{index}")
        if candidate.is_file():
            yield candidate
    if path.is_file():
        yield path


def _validated_event(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    required = ("timestamp", "level", "stage", "error_code", "safe_message")
    if not all(isinstance(value.get(key), str) for key in required):
        return None
    # Re-sanitize on read so old or manually modified files are never echoed verbatim.
    stage = value["stage"] if value["stage"] in DIAGNOSTIC_STAGES else "runtime"
    level = value["level"].casefold()
    if level not in _ALLOWED_LEVELS:
        level = "error"
    event: dict[str, Any] = {
        "timestamp": sanitize_safe_message(value["timestamp"], max_chars=40),
        "level": level,
        "stage": stage,
        "error_code": _safe_code(value["error_code"], fallback="UNCLASSIFIED_ERROR"),
        "safe_message": sanitize_safe_message(value["safe_message"]),
    }
    for key, sanitizer in (
        ("session_id", _safe_identifier),
        ("command_id", _safe_identifier),
        ("sequence", _safe_nonnegative_int),
        ("exception_type", _safe_exception_type),
        ("app", _safe_app),
        ("generation", _safe_nonnegative_int),
    ):
        sanitized = sanitizer(value.get(key))
        if sanitized is not None:
            event[key] = sanitized
    return event


def _iter_valid_events(path: Path) -> Iterator[dict[str, Any]]:
    for candidate in _log_files(path):
        try:
            with candidate.open("r", encoding="utf-8", errors="replace") as handle:
                for raw_line in handle:
                    try:
                        value = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    event = _validated_event(value)
                    if event is not None:
                        yield event
        except OSError:
            continue


def tail_events(
    path: str | Path | None = None,
    *,
    limit: int = DEFAULT_TAIL_COUNT,
) -> list[dict[str, Any]]:
    """Read the newest valid records without ever returning unknown/raw JSON fields."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_TAIL_COUNT:
        raise ValueError(f"limit must be between 1 and {MAX_TAIL_COUNT}")
    resolved = (
        Path(path).expanduser().resolve() if path is not None else default_log_path().resolve()
    )
    events = list(_iter_valid_events(resolved))
    return events[-limit:]


def diagnose_last_event(path: str | Path | None = None) -> dict[str, Any] | None:
    resolved = (
        Path(path).expanduser().resolve() if path is not None else default_log_path().resolve()
    )
    last_event: dict[str, Any] | None = None
    last_failure: dict[str, Any] | None = None
    for event in _iter_valid_events(resolved):
        last_event = event
        if event.get("level") in {"warning", "error"}:
            last_failure = event
    return last_failure or last_event
