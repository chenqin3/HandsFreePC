from __future__ import annotations

import json
import logging.handlers
from pathlib import Path

import pytest

from handsfree_pc.desktop.assistive.debug_log import (
    DEBUG_LOG_BACKUP_COUNT,
    MAX_DEBUG_LOG_BYTES,
    MAX_EXCEPTION_CHARS,
    MAX_TRACEBACK_CHARS,
    LocalDebugLog,
    default_debug_log_path,
)


def _captured_exception(message: str) -> RuntimeError:
    try:
        raise RuntimeError(message)
    except RuntimeError as exc:
        return exc


def test_default_debug_log_is_next_to_diagnostics(tmp_path: Path) -> None:
    path = default_debug_log_path({"LOCALAPPDATA": str(tmp_path)})

    assert path == tmp_path / "HandsFreePC" / "logs" / "debug.log"


def test_disabled_log_does_not_create_a_file_or_parent(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "debug.log"
    sink = LocalDebugLog(False, True, path)

    assert sink.exception(RuntimeError("ignored"), "plan") is None
    sink.close()

    assert not path.exists()
    assert not path.parent.exists()


def test_non_local_debug_log_is_rejected_without_creating_anything(tmp_path: Path) -> None:
    path = tmp_path / "debug.log"

    with pytest.raises(ValueError, match="local_only=true"):
        LocalDebugLog(True, False, path)

    assert not path.exists()


def test_exception_log_is_rotating_bounded_and_privacy_redacted(tmp_path: Path) -> None:
    path = tmp_path / "debug.log"
    private_speech = "请打开调查数据库然后把家里的资料发出去"
    secret = "sk-proj-abcdefghijklmnopqrstuvwx"
    exc = _captured_exception(
        f"planner failed at C:\\Users\\person\\private.py; "
        f"prompt={private_speech}; token={secret}"
    )
    sink = LocalDebugLog(True, True, path)
    try:
        event = sink.exception(exc, "plan", "C:\\repo\\controller.py:42")
        assert isinstance(sink._handler, logging.handlers.RotatingFileHandler)
        assert sink._handler.maxBytes == MAX_DEBUG_LOG_BYTES
        assert sink._handler.backupCount == DEBUG_LOG_BACKUP_COUNT
    finally:
        sink.close()

    assert event is not None
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == event
    assert set(stored) == {
        "timestamp",
        "exception_type",
        "exception",
        "traceback",
        "stage",
        "code_location",
    }
    assert stored["exception_type"] == "RuntimeError"
    assert stored["stage"] == "plan"
    assert private_speech not in path.read_text(encoding="utf-8")
    assert secret not in path.read_text(encoding="utf-8")
    assert "C:\\Users" not in path.read_text(encoding="utf-8")
    assert "[PRIVATE_PAYLOAD_OMITTED]" in stored["exception"]
    assert len(stored["exception"]) <= MAX_EXCEPTION_CHARS
    assert len(stored["traceback"]) <= MAX_TRACEBACK_CHARS
    assert "Traceback (most recent call last):" in stored["traceback"]
    assert "_captured_exception" in stored["traceback"]


def test_traceback_omits_source_line_and_derives_a_local_code_location(tmp_path: Path) -> None:
    path = tmp_path / "debug.log"
    raw_message = "uia_tree=" + ("private visible label " * 1000)
    exc = _captured_exception(raw_message)

    with LocalDebugLog(True, True, path) as sink:
        event = sink.exception(exc, "observe")

    assert event is not None
    assert raw_message not in event["traceback"]
    assert "private visible label" not in event["traceback"]
    assert event["code_location"].startswith("[LOCAL_PATH]/")
    assert "_captured_exception" in event["code_location"]


@pytest.mark.parametrize(
    "message",
    [
        "audio bytes: private waveform samples",
        "screenshot payload=private pixels",
        "provider error contained control_type Button and private visible label",
    ],
)
def test_debug_log_omits_labelled_media_and_structured_uia_payloads(
    tmp_path: Path,
    message: str,
) -> None:
    path = tmp_path / "debug.log"

    with LocalDebugLog(True, True, path) as sink:
        event = sink.exception(RuntimeError(message), "observe")

    assert event is not None
    assert "private" not in event["exception"]
    assert "private" not in event["traceback"]


def test_enabled_log_rejects_use_after_context_close(tmp_path: Path) -> None:
    path = tmp_path / "debug.log"
    with LocalDebugLog(True, True, path) as sink:
        sink.exception(RuntimeError("first"), "runtime")

    with pytest.raises(RuntimeError, match="closed"):
        sink.exception(RuntimeError("second"), "runtime")


def test_note_records_a_bounded_progress_line_only_when_enabled(tmp_path: Path) -> None:
    path = tmp_path / "debug.log"
    sink = LocalDebugLog(True, True, path)
    try:
        event = sink.note("plan", "decision=screenshot app=weixin-1 reason=" + "x" * 600)
    finally:
        sink.close()

    assert event is not None
    assert event["stage"] == "plan"
    assert len(event["note"]) <= 320
    assert "decision=screenshot app=weixin-1" in path.read_text(encoding="utf-8")
    assert LocalDebugLog(False, True, tmp_path / "off.log").note("plan", "ignored") is None
