from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import handsfree_pc.planner as planner_module
from handsfree_pc.planner import (
    ClaudePlanner,
    CodexPlanner,
    PlannerError,
    _parse_claude_envelope,
    _parse_plan_payload,
    _schema_text,
)


def valid_payload() -> dict:
    return {
        "summary": "switch feedback",
        "risk": "safe",
        "actions": [
            {
                "type": "set_feedback_mode",
                "path": None,
                "app": None,
                "project": None,
                "conversation": None,
                "tab": None,
                "mode": None,
                "text": None,
                "feedback_mode": "overlay",
                "seconds": None,
            }
        ],
    }


def test_claude_schema_keeps_constraints_without_unsupported_draft_hint() -> None:
    schema = json.loads(_schema_text())

    assert "$schema" not in schema
    assert schema["properties"]["actions"]["maxItems"] == 8


def test_local_planner_validation_accepts_valid_payload() -> None:
    plan = _parse_plan_payload(valid_payload(), source="test")

    assert plan.source == "test"
    assert plan.actions[0].feedback_mode.value == "overlay"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.update(summary="x" * 201),
        lambda payload: payload.update(actions=payload["actions"] * 9),
        lambda payload: payload["actions"][0].update(text="x" * 2001),
        lambda payload: payload["actions"][0].update(app=123),
        lambda payload: payload["actions"][0].update(extra="not allowed"),
    ],
)
def test_local_planner_validation_rejects_schema_escape(mutate) -> None:
    payload = valid_payload()
    mutate(payload)

    with pytest.raises(PlannerError, match="local validation"):
        _parse_plan_payload(payload, source="test")


def test_invalid_json_is_reported_as_planner_error() -> None:
    with pytest.raises(PlannerError, match="local validation"):
        _parse_plan_payload("not-json", source="test")


@pytest.mark.parametrize("payload", [[], "text", None])
def test_claude_envelope_requires_an_object(payload) -> None:
    with pytest.raises(PlannerError, match="envelope must be an object"):
        _parse_claude_envelope(json.dumps(payload))


@pytest.mark.parametrize("planner_class", [CodexPlanner, ClaudePlanner])
@pytest.mark.parametrize(
    ("error", "message"),
    [
        (subprocess.TimeoutExpired("planner", 1), "timed out"),
        (OSError("cannot start"), "could not start"),
    ],
)
def test_planner_process_failures_are_wrapped(
    monkeypatch, planner_class, error: Exception, message: str
) -> None:
    settings = SimpleNamespace(
        codex_executable="codex",
        claude_executable="claude",
        timeout_seconds=1,
        model=None,
    )
    monkeypatch.setattr(planner_module.shutil, "which", lambda _name: "planner.exe")

    def fail(*_args, **kwargs):
        assert Path(kwargs["cwd"]).is_dir()
        raise error

    monkeypatch.setattr(planner_module.subprocess, "run", fail)

    with pytest.raises(PlannerError, match=message):
        planner_class(settings).plan("private prompt must not appear in the error")
