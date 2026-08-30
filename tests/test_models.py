from __future__ import annotations

import pytest

from handsfree_pc.models import Action, ActionType, FeedbackMode, Plan


def test_action_round_trip() -> None:
    action = Action(ActionType.SET_FEEDBACK_MODE, feedback_mode=FeedbackMode.BOTH)
    assert Action.from_dict(action.to_dict()) == action


def test_action_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError, match="Unknown action fields"):
        Action.from_dict({"type": "open_path", "path": "D:\\\\a", "shell": "oops"})


def test_action_requires_target() -> None:
    with pytest.raises(ValueError, match="requires path"):
        Action.from_dict({"type": "open_path"})


def test_plan_rejects_unknown_action_type() -> None:
    with pytest.raises(ValueError):
        Plan.from_dict({"summary": "bad", "risk": "safe", "actions": [{"type": "shell"}]})


@pytest.mark.parametrize("text", ["send\r", "line\nnext", "tab\tvalue", "escape\x1b"])
def test_type_text_rejects_control_characters(text: str) -> None:
    with pytest.raises(ValueError, match="control characters"):
        Action(ActionType.TYPE_TEXT, text=text).validate()


def test_type_text_accepts_printable_multilingual_text() -> None:
    Action(ActionType.TYPE_TEXT, text="请继续设计 — hello 👶").validate()


@pytest.mark.parametrize("field", ["path", "app", "project", "conversation", "tab", "mode"])
def test_all_action_text_fields_reject_control_characters(field: str) -> None:
    values = {
        "type": "open_conversation",
        "app": "codex",
        "conversation": "demo",
        field: "unsafe\rvalue",
    }
    with pytest.raises(ValueError, match="control characters"):
        Action.from_dict(values)


def test_plan_summary_rejects_control_characters() -> None:
    with pytest.raises(ValueError, match="summary cannot contain control"):
        Plan.from_dict({"summary": "safe\nexecute", "risk": "safe", "actions": []})


@pytest.mark.parametrize("missing", ["summary", "risk", "actions"])
def test_plan_requires_all_schema_fields(missing: str) -> None:
    payload = {"summary": "blocked", "risk": "blocked", "actions": []}
    payload.pop(missing)
    with pytest.raises(ValueError, match="Missing required plan fields"):
        Plan.from_dict(payload)


def test_empty_actions_are_only_allowed_for_blocked_plans() -> None:
    with pytest.raises(ValueError, match="must contain at least one action"):
        Plan.from_dict({"summary": "nothing", "risk": "safe", "actions": []})
    blocked = Plan.from_dict({"summary": "blocked", "risk": "blocked", "actions": []})
    assert blocked.risk.value == "blocked"


def test_direct_plan_validation_rejects_control_summary() -> None:
    plan = Plan("unsafe\rsummary", [Action(ActionType.PAUSE)])
    with pytest.raises(ValueError, match="summary cannot contain control"):
        plan.validate()
