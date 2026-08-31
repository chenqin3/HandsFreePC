from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import threading
from collections import deque
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_TRANSCRIPT_BYTES = 5 * 1024 * 1024
TRANSCRIPT_BACKUP_COUNT = 5
_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def default_transcript_path(environment: Mapping[str, str] | None = None) -> Path:
    """Return the per-user ASR transcript path without using project configuration."""

    values = os.environ if environment is None else environment
    local_app_data = values.get("LOCALAPPDATA")
    root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return root / "HandsFreePC" / "transcripts" / "asr-transcripts.jsonl"


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class _TranscriptFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = getattr(record, "transcript_event", None)
        if not isinstance(event, dict):
            raise ValueError("Transcript journal received an invalid record")
        return json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=False)


class TranscriptJournal:
    """Write opted-in session-layer ASR text to an independent rotating JSONL file."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_transcript_path()
        self.path = self.path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._closed = False
        self._logger = logging.Logger(f"handsfree_pc.transcripts.{id(self)}", logging.INFO)
        self._logger.propagate = False
        handler = logging.handlers.RotatingFileHandler(
            self.path,
            maxBytes=MAX_TRANSCRIPT_BYTES,
            backupCount=TRANSCRIPT_BACKUP_COUNT,
            encoding="utf-8",
            delay=True,
        )
        handler.setFormatter(_TranscriptFormatter())
        self._logger.addHandler(handler)
        self._handler = handler

    def record(
        self,
        *,
        source: str,
        text: str,
        session_id: str | None = None,
        segment_index: int | None = None,
        segment_count: int | None = None,
        transcribed: bool | None = None,
        skip_reason: str | None = None,
    ) -> dict[str, Any]:
        """Persist model-adapter text before any prompt normalization."""

        if not isinstance(text, str):
            raise TypeError("Transcript text must be a string")
        if not isinstance(source, str) or _SOURCE_RE.fullmatch(source) is None:
            raise ValueError("Transcript source must be a lowercase identifier")
        event: dict[str, Any] = {
            "timestamp": _utc_timestamp(),
            "source": source,
            "text": text,
        }
        if session_id is not None:
            event["session_id"] = str(session_id)
        if segment_index is not None:
            if (
                isinstance(segment_index, bool)
                or not isinstance(segment_index, int)
                or segment_index < 0
            ):
                raise ValueError("segment_index must be a non-negative integer")
            event["segment_index"] = segment_index
        if segment_count is not None:
            if (
                isinstance(segment_count, bool)
                or not isinstance(segment_count, int)
                or segment_count < 1
            ):
                raise ValueError("segment_count must be a positive integer")
            event["segment_count"] = segment_count
        if transcribed is not None:
            if not isinstance(transcribed, bool):
                raise ValueError("transcribed must be a boolean")
            event["transcribed"] = transcribed
        if skip_reason is not None:
            if not isinstance(skip_reason, str) or not skip_reason.strip():
                raise ValueError("skip_reason must be a non-empty string")
            if transcribed is not False:
                raise ValueError("skip_reason requires transcribed=false")
            event["skip_reason"] = skip_reason
        with self._lock:
            if self._closed:
                raise RuntimeError("Transcript journal is closed")
            self._logger.info(
                "transcript_event",
                extra={"transcript_event": event},
            )
        return dict(event)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._logger.removeHandler(self._handler)
            self._handler.close()


def _journal_files(path: Path) -> Iterator[Path]:
    # RotatingFileHandler uses .1 for the newest backup. Read oldest -> newest.
    for index in range(TRANSCRIPT_BACKUP_COUNT, 0, -1):
        candidate = Path(f"{path}.{index}")
        if candidate.is_file():
            yield candidate
    if path.is_file():
        yield path


def _validated_transcript(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    timestamp = value.get("timestamp")
    source = value.get("source")
    text = value.get("text")
    if (
        not isinstance(timestamp, str)
        or not isinstance(source, str)
        or _SOURCE_RE.fullmatch(source) is None
        or not isinstance(text, str)
    ):
        return None
    event: dict[str, Any] = {
        "timestamp": timestamp,
        "source": source,
        "text": text,
    }
    session_id = value.get("session_id")
    if isinstance(session_id, str):
        event["session_id"] = session_id
    for key, minimum in (("segment_index", 0), ("segment_count", 1)):
        candidate = value.get(key)
        if (
            isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and candidate >= minimum
        ):
            event[key] = candidate
    transcribed = value.get("transcribed")
    if isinstance(transcribed, bool):
        event["transcribed"] = transcribed
        skip_reason = value.get("skip_reason")
        if transcribed is False and isinstance(skip_reason, str) and skip_reason.strip():
            event["skip_reason"] = skip_reason
    return event


def tail_transcripts(path: str | Path | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("Transcript tail limit must be a positive integer")
    resolved = Path(path) if path is not None else default_transcript_path()
    resolved = resolved.expanduser().resolve()
    newest: deque[dict[str, Any]] = deque(maxlen=limit)
    for candidate in _journal_files(resolved):
        try:
            with candidate.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if (event := _validated_transcript(value)) is not None:
                        newest.append(event)
        except OSError:
            continue
    return list(newest)
