from __future__ import annotations

import json
import logging.handlers

import pytest

from handsfree_pc.diagnostics import (
    LOG_BACKUP_COUNT,
    MAX_LOG_BYTES,
    Diagnostics,
    classify_control_failure,
    default_log_path,
    diagnose_last_event,
    sanitize_safe_message,
    tail_events,
)


def test_default_log_path_uses_local_app_data(tmp_path) -> None:
    assert default_log_path({"LOCALAPPDATA": str(tmp_path)}) == (
        tmp_path / "HandsFreePC" / "logs" / "handsfreepc.jsonl"
    )


def test_diagnostics_uses_bounded_rotating_jsonl_schema(tmp_path) -> None:
    path = tmp_path / "handsfreepc.jsonl"
    diagnostics = Diagnostics(path)
    try:
        event = diagnostics.event(
            stage="observe_driver",
            error_code="UIA_READ_FAILED",
            safe_message="The UI Automation observation failed before an action",
            session_id="session-1",
            command_id="command-2",
            sequence=3,
            exception_type=RuntimeError,
            app="Claude",
            generation=4,
        )
        handler = diagnostics._handler
        assert isinstance(handler, logging.handlers.RotatingFileHandler)
        assert handler.maxBytes == MAX_LOG_BYTES
        assert handler.backupCount == LOG_BACKUP_COUNT
    finally:
        diagnostics.close()

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == event
    assert stored["stage"] == "observe_driver"
    assert stored["error_code"] == "UIA_READ_FAILED"
    assert stored["app"] == "claude"
    assert set(stored) == {
        "timestamp",
        "level",
        "stage",
        "error_code",
        "safe_message",
        "session_id",
        "command_id",
        "sequence",
        "exception_type",
        "app",
        "generation",
    }


def test_safe_message_redacts_credentials_opaque_values_and_local_paths() -> None:
    raw = (
        "Bearer abcdefghijklmnopqrstuvwxyz0123456789 and "
        "sk-proj-abcdefghijklmnopqrstuvwx at C:\\Users\\person\\private.txt"
    )

    safe = sanitize_safe_message(raw)

    assert "Bearer" not in safe
    assert "sk-proj" not in safe
    assert "private.txt" not in safe
    assert safe.count("[REDACTED]") == 2
    assert "[LOCAL_PATH]" in safe


def test_diagnostics_rejects_arbitrary_prompt_or_uia_payload_fields(tmp_path) -> None:
    diagnostics = Diagnostics(tmp_path / "events.jsonl")
    try:
        with pytest.raises(TypeError):
            diagnostics.event(
                stage="plan",
                error_code="PLANNER_FAILED",
                safe_message="Planner failed",
                prompt="private prompt",
            )
        with pytest.raises(TypeError):
            diagnostics.event(
                stage="observe_driver",
                error_code="OBSERVE_FAILED",
                safe_message="Observation failed",
                uia_text="private UIA body",
            )
    finally:
        diagnostics.close()


def test_tail_events_drops_unknown_fields_and_resanitizes_manual_lines(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-31T00:00:00.000Z",
                "level": "error",
                "stage": "execute",
                "error_code": "EXECUTE_FAILED",
                "safe_message": "Bearer abcdefghijklmnopqrstuvwxyz0123456789",
                "prompt": "must never be returned",
                "uia_text": "must never be returned",
            }
        )
        + "\nnot-json\n",
        encoding="utf-8",
    )

    events = tail_events(path, limit=10)

    assert len(events) == 1
    assert set(events[0]) == {
        "timestamp",
        "level",
        "stage",
        "error_code",
        "safe_message",
    }
    assert "Bearer" not in events[0]["safe_message"]


def test_diagnose_last_prefers_newest_warning_or_error(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    diagnostics = Diagnostics(path)
    try:
        diagnostics.event(
            stage="runtime",
            error_code="STARTED",
            safe_message="Started",
            level="info",
        )
        diagnostics.event(
            stage="verify_action",
            error_code="POSTCONDITION_FAILED",
            safe_message="The action postcondition was not established",
        )
        diagnostics.event(
            stage="runtime",
            error_code="STOPPED",
            safe_message="Stopped",
            level="info",
        )
    finally:
        diagnostics.close()

    event = diagnose_last_event(path)

    assert event is not None
    assert event["stage"] == "verify_action"
    assert event["error_code"] == "POSTCONDITION_FAILED"


@pytest.mark.parametrize(
    ("message", "stage", "error_code"),
    [
        (
            "FAILURE: 桌面观察失败：WindowsUiaDriverError: private UI text",
            "observe_driver",
            "DESKTOP_OBSERVE_FAILED",
        ),
        (
            "FAILURE: 本地安全策略阻止读取该界面：credential-like private value",
            "observe_safety",
            "OBSERVATION_SAFETY_BLOCKED",
        ),
        (
            "FAILURE: 动作后本地验收失败：private target label",
            "verify_action",
            "ACTION_VERIFICATION_FAILED",
        ),
    ],
)
def test_control_failure_classification_never_copies_legacy_message(
    message, stage, error_code
) -> None:
    status = classify_control_failure(message)

    assert status.stage == stage
    assert status.error_code == error_code
    assert "private" not in status.safe_message


def test_control_failure_prefers_explicit_bounded_status() -> None:
    status = classify_control_failure(
        "legacy private content",
        stage="reobserve",
        error_code="FRESH_OBSERVATION_FAILED",
        safe_message="The target app could not be refreshed after the action",
    )

    assert status.stage == "reobserve"
    assert status.error_code == "FRESH_OBSERVATION_FAILED"
    assert status.safe_message == "The target app could not be refreshed after the action"
