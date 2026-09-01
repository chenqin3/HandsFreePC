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
    DesktopElementAction,
    DesktopExpandCollapseState,
    DesktopExpectation,
    DesktopExpectationKind,
    DesktopObservation,
    DesktopScrollAxis,
    visual_state_binding_token,
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


def test_element_capabilities_are_immutable_bounded_and_planner_visible() -> None:
    element = DesktopElement(
        index="4",
        name="Conversation list",
        control_type="Pane",
        supported_actions=("scroll", "collapse"),
        expand_collapse_state="expanded",
        scroll_axes=("vertical",),
    )

    assert element.supported_actions == (
        DesktopElementAction.COLLAPSE,
        DesktopElementAction.SCROLL,
    )
    assert element.expand_collapse_state == DesktopExpandCollapseState.EXPANDED
    assert element.scroll_axes == (DesktopScrollAxis.VERTICAL,)
    assert element.planner_payload()["supported_actions"] == ["collapse", "scroll"]
    assert element.planner_payload()["expand_collapse_state"] == "expanded"
    assert element.planner_payload()["scroll_axes"] == ["vertical"]
    assert element.fingerprint_payload()["supported_actions"] == ["collapse", "scroll"]

    with pytest.raises(ValueError, match="immutable tuple"):
        DesktopElement(
            index="4",
            name="List",
            control_type="Pane",
            supported_actions=["scroll"],  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="unknown action"):
        DesktopElement(
            index="4",
            name="List",
            control_type="Pane",
            supported_actions=("execute_anything",),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="unique"):
        DesktopElement(
            index="4",
            name="List",
            control_type="Pane",
            supported_actions=("scroll", "scroll"),  # type: ignore[arg-type]
            scroll_axes=("vertical",),
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


def test_action_decision_accepts_last_action_verified_for_a_reveal_bridge() -> None:
    observation = _observation(generation=23)

    decision = DesktopDecision.from_dict(
        {
            "kind": "action",
            "reason": "Reveal more of the requested list",
            "app": "claude",
            "action": {
                "type": "scroll",
                "element_index": "0",
                "direction": "down",
                "pages": 1,
            },
            "expectation": {"kind": "last_action_verified", "text": None},
        },
        observation=observation,
    )

    assert decision.action is not None
    assert decision.action.type == DesktopActionType.SCROLL
    assert decision.expectation is not None
    assert decision.expectation.kind == DesktopExpectationKind.LAST_ACTION_VERIFIED


def test_visual_state_expectation_requires_an_exact_lowercase_digest() -> None:
    digest = "a" * 64

    expectation = DesktopExpectation(
        DesktopExpectationKind.VISUAL_STATE_VERIFIED,
        digest,
    )

    assert expectation.text == digest
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        DesktopExpectation(DesktopExpectationKind.VISUAL_STATE_VERIFIED, "not-a-digest")


def test_visual_state_token_binds_app_window_generation_and_frame() -> None:
    base = DesktopObservation(
        app="wechat",
        generation=1,
        accessibility_text="visual",
        screenshot_png=b"frame",
        local_window_id="window-a",
    )

    token = visual_state_binding_token(base)

    assert token != visual_state_binding_token(
        DesktopObservation(
            app="wechat",
            generation=2,
            accessibility_text="visual",
            screenshot_png=b"frame",
            local_window_id="window-a",
        )
    )
    assert token != visual_state_binding_token(
        DesktopObservation(
            app="wechat",
            generation=1,
            accessibility_text="visual",
            screenshot_png=b"frame",
            local_window_id="window-b",
        )
    )
    assert token != visual_state_binding_token(
        DesktopObservation(
            app="wechat",
            generation=1,
            accessibility_text="visual",
            screenshot_png=b"changed",
            local_window_id="window-a",
        )
    )


def test_visual_state_expectation_is_completion_only() -> None:
    observation = _observation(generation=24)

    with pytest.raises(ValueError, match="task-specific UI change"):
        DesktopDecision.from_dict(
            {
                "kind": "action",
                "reason": "invalid self-attested action",
                "app": "claude",
                "action": {
                    "type": "click",
                    "element_index": "0",
                    "click_count": 1,
                    "mouse_button": "left",
                },
                "expectation": {
                    "kind": "visual_state_verified",
                    "text": "a" * 64,
                },
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


def test_planner_context_reserves_the_frame_bound_viewport_after_rich_uia():
    viewport = DesktopElement(
        index="999",
        name="Visual screenshot viewport",
        control_type="VisualViewport",
        enabled=True,
        addressable=True,
        visual_ocr=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )
    observation = DesktopObservation(
        app="claude",
        generation=1,
        accessibility_text="rich UIA " * 4000,
        screenshot_png=b"fixture",
        elements=tuple(
            DesktopElement(
                index=str(index),
                name=f"Long semantic control {index} " + "x" * 800,
                control_type="Button",
            )
            for index in range(300)
        )
        + (viewport,),
    )

    context = observation.planner_context(max_chars=24000)
    serialized = json.dumps(context, ensure_ascii=False, sort_keys=True)

    assert len(serialized) <= 24000
    assert context["elements_truncated"] is True
    assert context["elements"][-1]["index"] == "999"
    assert context["elements"][-1]["control_type"] == "VisualViewport"
    assert sum(
        item["control_type"] == "VisualViewport" for item in context["elements"]
    ) == 1


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


def test_volatile_screenshot_bytes_do_not_change_semantic_freshness_fingerprint():
    first = DesktopObservation(
        app="chrome",
        generation=1,
        accessibility_text='0 name="Search" control_type="Edit" focused=true',
        screenshot_png=b"first-frame",
        window_title="Chrome",
        local_window_id="window-a",
    )
    second = DesktopObservation(
        app="chrome",
        generation=2,
        accessibility_text=first.accessibility_text,
        screenshot_png=b"cursor-blink-frame",
        window_title="Chrome",
        local_window_id="window-a",
    )

    assert first.fingerprint == second.fingerprint


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
