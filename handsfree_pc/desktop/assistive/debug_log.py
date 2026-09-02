from __future__ import annotations

import json
import logging
import logging.handlers
import re
import threading
import traceback as traceback_module
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from handsfree_pc.diagnostics import default_log_path, sanitize_safe_message

MAX_DEBUG_LOG_BYTES = 1024 * 1024
DEBUG_LOG_BACKUP_COUNT = 3
MAX_EXCEPTION_CHARS = 320
MAX_TRACEBACK_CHARS = 4096
MAX_TRACEBACK_FRAMES = 24

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_SPACE_RE = re.compile(r"\s+")
_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,95}$")
_URL_RE = re.compile(r"(?i)\b(?:https?|file)://[^\s\"'<>]+")
_EMAIL_RE = re.compile(r"(?i)(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Z]{2,}(?![\w.-])")
_PRIVATE_PAYLOAD_RE = re.compile(
    r"(?i)(?:\b(?:audio|prompt|screenshot|transcript|utterance|voice[ _-]?text|"
    r"uia(?:[ _-]?tree)?|accessibility[ _-]?tree|user[ _-]?speech)\b|"
    r"(?:音频|截图|完整界面树|界面树|用户口述|语音原文|转写原文))"
    r"(?:\s+(?:buffer|bytes|data|payload|samples|text|was|is))?\s*[:=]"
)
_UI_TREE_SIGNAL_RE = re.compile(
    r"(?i)(?:\b(?:automation[ _-]?id|control[ _-]?type|bounding[ _-]?rectangle|"
    r"uia[ _-]?tree|accessibility[ _-]?tree)\b|(?:完整界面树|界面树))"
)


def default_debug_log_path(environment: Mapping[str, str] | None = None) -> Path:
    """Return the opt-in local debug log next to ordinary diagnostics."""

    return default_log_path(environment).with_name("debug.log")


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _exception_type(exc: BaseException) -> str:
    candidate = type(exc).__name__
    if _TYPE_RE.fullmatch(candidate):
        return candidate
    return "Exception"


def _private_payload_removed(value: str) -> str:
    """Drop labelled payloads before the general-purpose diagnostic redactor runs.

    Debug logging accepts only exception objects, but provider and UI libraries sometimes
    include their input in an exception string.  Once a sensitive payload label occurs, the
    remainder is deliberately discarded instead of attempting to preserve arbitrary content.
    """

    labelled = _PRIVATE_PAYLOAD_RE.search(value)
    structural = _UI_TREE_SIGNAL_RE.search(value)
    matches = [match for match in (labelled, structural) if match is not None]
    match = min(matches, key=lambda item: item.start()) if matches else None
    if match is None:
        return value
    prefix = value[: match.start()].rstrip(" :=,-")
    replacement = "[PRIVATE_PAYLOAD_OMITTED]"
    return f"{prefix} {replacement}".strip()


def _safe_text(value: object, *, maximum: int) -> str:
    try:
        raw = value if isinstance(value, str) else str(value)
    except Exception:  # pragma: no cover - hostile third-party exceptions are uncommon
        raw = "Exception text could not be rendered"
    raw = _private_payload_removed(raw)
    raw = _URL_RE.sub("[URL]", raw)
    raw = _EMAIL_RE.sub("[EMAIL]", raw)
    return sanitize_safe_message(raw, max_chars=maximum)


def _safe_metadata(value: object, *, fallback: str, maximum: int) -> str:
    text = _safe_text(value, maximum=maximum)
    text = _CONTROL_RE.sub(" ", text)
    text = _SPACE_RE.sub(" ", text).strip()
    return text or fallback


def _safe_filename(filename: str) -> str:
    if filename.startswith("<") and filename.endswith(">"):
        return "[DYNAMIC_CODE]"
    name = Path(filename).name
    safe = _safe_metadata(name, fallback="[SOURCE]", maximum=96)
    return f"[LOCAL_PATH]/{safe}"


def _traceback_frames(exc: BaseException) -> list[traceback_module.FrameSummary]:
    trace = exc.__traceback__
    if trace is None:
        return []
    return list(traceback_module.extract_tb(trace, limit=None))


def _bounded_traceback(exc: BaseException, *, safe_message: str) -> str:
    frames = _traceback_frames(exc)
    omitted = max(0, len(frames) - MAX_TRACEBACK_FRAMES)
    selected = frames[-MAX_TRACEBACK_FRAMES:]
    lines = ["Traceback (most recent call last):"]
    if omitted:
        lines.append(f"  ... {omitted} earlier frame(s) omitted")
    for frame in selected:
        function = _safe_metadata(frame.name, fallback="[FUNCTION]", maximum=96)
        lines.append(
            f'  File "{_safe_filename(frame.filename)}", line {max(0, frame.lineno)}, in {function}'
        )
    lines.append(f"{_exception_type(exc)}: {safe_message}")
    rendered = "\n".join(lines)
    if len(rendered) <= MAX_TRACEBACK_CHARS:
        return rendered
    suffix = "\n...[traceback truncated]"
    return rendered[: MAX_TRACEBACK_CHARS - len(suffix)].rstrip() + suffix


def _derived_code_location(exc: BaseException) -> str:
    frames = _traceback_frames(exc)
    if not frames:
        return "unavailable"
    frame = frames[-1]
    function = _safe_metadata(frame.name, fallback="[FUNCTION]", maximum=96)
    return f"{_safe_filename(frame.filename)}:{max(0, frame.lineno)} in {function}"


class _DebugFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "assistive_debug_event", None)
        if not isinstance(event, dict):
            event = {
                "timestamp": _utc_timestamp(),
                "exception_type": "Exception",
                "exception": "Invalid debug record discarded",
                "traceback": "Traceback unavailable",
                "stage": "runtime",
                "code_location": "unavailable",
            }
        return json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


class LocalDebugLog:
    """Opt-in, local-only, privacy-bounded exception log for the assistive engine."""

    def __init__(
        self,
        enabled: bool,
        local_only: bool,
        path: str | Path | None = None,
    ) -> None:
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        if not isinstance(local_only, bool):
            raise TypeError("local_only must be a boolean")
        if not local_only:
            raise ValueError("Assistive debug logging requires local_only=true")

        self.enabled = enabled
        self.local_only = local_only
        self.path = (
            (Path(path) if path is not None else default_debug_log_path()).expanduser().resolve()
        )
        self._lock = threading.Lock()
        self._closed = False
        self._logger: logging.Logger | None = None
        self._handler: logging.handlers.RotatingFileHandler | None = None

        if not enabled:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        logger = logging.Logger(f"handsfree_pc.assistive.debug.{id(self)}", logging.ERROR)
        logger.propagate = False
        handler = logging.handlers.RotatingFileHandler(
            self.path,
            maxBytes=MAX_DEBUG_LOG_BYTES,
            backupCount=DEBUG_LOG_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(_DebugFormatter())
        logger.addHandler(handler)
        self._logger = logger
        self._handler = handler

    def exception(
        self,
        exc: BaseException,
        stage: str,
        code_location: str | None = None,
    ) -> dict[str, Any] | None:
        """Record one redacted exception. Arbitrary payload fields are not accepted."""

        if not self.enabled:
            return None
        if not isinstance(exc, BaseException):
            raise TypeError("exc must be an exception instance")
        normalized_stage = _safe_metadata(stage, fallback="runtime", maximum=96)
        safe_message = _safe_text(exc, maximum=MAX_EXCEPTION_CHARS)
        if code_location is None:
            safe_location = _derived_code_location(exc)
        elif isinstance(code_location, str):
            safe_location = _safe_metadata(
                code_location,
                fallback="unavailable",
                maximum=240,
            )
        else:
            raise TypeError("code_location must be a string or None")
        event: dict[str, Any] = {
            "timestamp": _utc_timestamp(),
            "exception_type": _exception_type(exc),
            "exception": safe_message,
            "traceback": _bounded_traceback(exc, safe_message=safe_message),
            "stage": normalized_stage,
            "code_location": safe_location,
        }
        with self._lock:
            if self._closed or self._logger is None:
                raise RuntimeError("Assistive debug log is closed")
            self._logger.error(
                "assistive_debug_event",
                extra={"assistive_debug_event": event},
            )
        return dict(event)

    def note(self, stage: str, message: str) -> dict[str, Any] | None:
        """Record one bounded, redacted progress note (planner decisions, outcomes).

        Notes go through the same payload stripping as exception text, so a
        planner reason that quotes UI text is truncated and redacted before it
        reaches disk. The debug log still never accepts arbitrary fields.
        """

        if not self.enabled:
            return None
        if not isinstance(message, str):
            raise TypeError("message must be a string")
        event: dict[str, Any] = {
            "timestamp": _utc_timestamp(),
            "stage": _safe_metadata(stage, fallback="runtime", maximum=96),
            "note": _safe_metadata(message, fallback="[NOTE]", maximum=MAX_EXCEPTION_CHARS),
        }
        with self._lock:
            if self._closed or self._logger is None:
                raise RuntimeError("Assistive debug log is closed")
            self._logger.error(
                "assistive_debug_event",
                extra={"assistive_debug_event": event},
            )
        return dict(event)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._logger is not None and self._handler is not None:
                self._logger.removeHandler(self._handler)
                self._handler.close()

    def __enter__(self) -> LocalDebugLog:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()
