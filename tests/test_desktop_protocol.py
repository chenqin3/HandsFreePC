from __future__ import annotations

import json

import pytest

from handsfree_pc.desktop.protocol import (
    BoundedUiText,
    DesktopAction,
    DesktopActionType,
    DesktopDecision,
    DesktopDecisionKind,
    DesktopElement,
    DesktopExpectationKind,
    DesktopObservation,
)


@pytest.mark.parametrize("maximum", [1, 2, 3, 30, 80])
def test_bounded_ui_text_never_exceeds_the_requested_maximum(maximum: int) -> None:
    bounded = BoundedUiText.from_text("abcdef" * 100, maximum=maximum)

    assert bounded.truncated is True
    assert len(bounded.display) <= maximum
    assert bounded.original_length == 600
    assert len(bounded.sha256) == 64


def _observation(*, app: str = "claude", generation: int = 7) -> DesktopObservation:
    return DesktopObservation(
        app=app,
        generation=generation,
        accessibility_text="0 name=Chat control_type=TabItem",
        elements=(DesktopElement(index="0", name="Chat", control_type="TabItem"),),
    )


def test_action_from_dict_binds_untrusted_payload_to_observation_identity():
    action = DesktopAction.from_dict(
        {
            "type": "click",
            "element_index": "0",
            "click_count": 1,
            "mouse_button": "left",
        },
        app="Claude",
        generation=12,
    )

    assert action == DesktopAction(
        type=DesktopActionType.CLICK,
        app="Claude",
        generation=12,
        element_index="0",
        click_count=1,
        mouse_button="left",
    )
    assert action.tool_arguments() == {
        "app": "Claude",
        "element_index": "0",
        "click_count": 1,
        "mouse_button": "left",
    }


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"type": "click", "element_index": "0", "app": "terminal"}, "unknown"),
        ({"type": "click", "element_index": "zero"}, "decimal"),
        ({"type": "click", "x": -1, "y": 2}, "non-negative"),
        ({"type": "press_key", "element_index": "0", "key": ""}, "non-empty"),
        ({"type": "drag", "from_x": 0, "from_y": 0, "to_x": 1}, "requires"),
        ({"type": "not-a-tool"}, "not a valid"),
    ],
)
def test_action_from_dict_rejects_unknown_or_invalid_fields(payload, message):
    with pytest.raises(ValueError, match=message):
        DesktopAction.from_dict(payload, app="claude", generation=1)


def test_decision_from_dict_rejects_action_for_any_app_except_observed_window():
    observation = _observation(app="claude")
    payload = {
        "kind": "action",
        "reason": "Select Chat",
        "app": "codex",
        "action": {"type": "click", "element_index": "0"},
        "expectation": {"kind": "element_selected", "text": "Chat"},
    }

    with pytest.raises(ValueError, match="observed application"):
        DesktopDecision.from_dict(payload, observation=observation)


def test_decision_from_dict_uses_current_generation_and_requires_one_atomic_shape():
    observation = _observation(generation=19)
    decision = DesktopDecision.from_dict(
        {
            "kind": "action",
            "reason": "Select Chat",
            "app": "CLAUDE",
            "action": {"type": "click", "element_index": "0"},
            "expectation": {"kind": "element_selected", "text": "Chat"},
        },
        observation=observation,
    )

    assert decision.kind == DesktopDecisionKind.ACTION
    assert decision.action is not None
    assert decision.action.app == "claude"
    assert decision.action.generation == 19

    with pytest.raises(ValueError, match="cannot contain action or expectation"):
        DesktopDecision.from_dict(
            {
                "kind": "observe",
                "reason": "Need a snapshot",
                "app": "claude",
                "action": {"type": "click", "element_index": "0"},
                "expectation": None,
            },
            observation=observation,
        )


def test_done_decision_is_only_a_local_expectation_not_an_action_receipt():
    decision = DesktopDecision.from_dict(
        {
            "kind": "done",
            "reason": "The requested tab is visible",
            "app": "claude",
            "action": None,
            "expectation": {"kind": "text_present", "text": "Chat"},
        },
        observation=_observation(),
    )

    assert decision.kind == DesktopDecisionKind.DONE
    assert decision.expectation is not None
    assert decision.expectation.kind == DesktopExpectationKind.TEXT_PRESENT
    assert decision.expectation.text == "Chat"


def test_observation_rejects_duplicate_indexes_and_never_exposes_password_value():
    with pytest.raises(ValueError, match="must never enter"):
        DesktopElement(
            index="0",
            name="Password",
            control_type="Edit",
            value="secret",
            password=True,
        )

    element = DesktopElement(index="0", name="Chat", control_type="TabItem")
    with pytest.raises(ValueError, match="indexes must be unique"):
        DesktopObservation(
            app="claude",
            generation=1,
            accessibility_text="tree",
            elements=(element, element),
        )


def test_planner_context_bounds_combined_accessibility_and_element_payload_size():
    observation = DesktopObservation(
        app="claude",
        generation=1,
        accessibility_text="visible text " * 10000,
        elements=tuple(
            DesktopElement(
                index=str(index),
                name=f"Item {index}",
                control_type="Text",
                value="x" * 1000,
            )
            for index in range(200)
        ),
    )

    context = observation.planner_context(max_chars=4000)
    serialized = json.dumps(context, ensure_ascii=False, sort_keys=True)

    assert len(serialized) <= 4000
    assert context["accessibility_truncated"] is True
    assert context["elements_truncated"] is True


def test_automation_id_stays_local_but_remains_part_of_freshness_fingerprint():
    first = DesktopObservation(
        app="claude",
        generation=1,
        accessibility_text="0 name=Chat",
        elements=(
            DesktopElement(
                index="0",
                name="Chat",
                control_type="Button",
                automation_id="internal-id-one",
            ),
        ),
    )
    second = DesktopObservation(
        app="claude",
        generation=1,
        accessibility_text="0 name=Chat",
        elements=(
            DesktopElement(
                index="0",
                name="Chat",
                control_type="Button",
                automation_id="internal-id-two",
            ),
        ),
    )

    context = first.planner_context(max_chars=4000)

    assert "automation_id" not in context["elements"][0]
    assert "local_identity" not in context["elements"][0]
    assert first.fingerprint != second.fingerprint


def test_local_window_identity_changes_fingerprint_but_never_planner_context():
    first = DesktopObservation(
        app="claude",
        generation=1,
        accessibility_text="2 button Code",
        local_window_id="hwnd:100",
    )
    second = DesktopObservation(
        app="claude",
        generation=1,
        accessibility_text="2 button Code",
        local_window_id="hwnd:200",
    )

    assert first.fingerprint != second.fingerprint
    context = first.planner_context(max_chars=2000)
    assert "local_window_id" not in context
    assert "hwnd:100" not in json.dumps(context, ensure_ascii=False)


@pytest.mark.parametrize("invalid", ["bad\ufffdtext", "bad\ud800text", "bad\x00text"])
def test_observation_and_element_reject_lossy_or_control_unicode(invalid: str):
    with pytest.raises(ValueError, match="Unicode|UTF-8|control"):
        DesktopObservation(app="claude", generation=1, accessibility_text=invalid)
    with pytest.raises(ValueError, match="Unicode|UTF-8|control"):
        DesktopElement(index="0", name=invalid, control_type="Text")


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "click", "element_index": "0", "text": "smuggled"},
        {"type": "click", "element_index": "0", "key": "enter"},
        {"type": "click", "element_index": "0", "value": "smuggled"},
        {
            "type": "press_key",
            "element_index": "0",
            "key": "enter",
            "text": "smuggled",
        },
        {
            "type": "press_key",
            "element_index": "0",
            "key": "enter",
            "action_name": "invoke",
        },
    ],
)
def test_action_rejects_non_null_fields_for_another_action_type(payload):
    with pytest.raises(ValueError, match="inapplicable fields"):
        DesktopAction.from_dict(payload, app="claude", generation=1)
