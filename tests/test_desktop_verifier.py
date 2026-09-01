from __future__ import annotations

import pytest

from handsfree_pc.desktop.protocol import (
    ActionReceipt,
    DesktopAction,
    DesktopActionType,
    DesktopDecision,
    DesktopDecisionKind,
    DesktopElement,
    DesktopElementAction,
    DesktopExpectation,
    DesktopExpectationKind,
    DesktopObservation,
    visual_state_binding_token,
)
from handsfree_pc.desktop.verifier import DesktopVerifier


def _observation(
    text: str,
    *,
    app: str = "Notepad",
    generation: int = 1,
    captured_at: float | None = None,
    elements: tuple[DesktopElement, ...] = (),
) -> DesktopObservation:
    return DesktopObservation(
        app=app,
        generation=generation,
        accessibility_text=text,
        elements=elements,
        captured_at=float(generation) if captured_at is None else captured_at,
    )


def _click(*, app: str = "Notepad", generation: int = 1, index: str = "2") -> DesktopAction:
    return DesktopAction(
        type=DesktopActionType.CLICK,
        app=app,
        generation=generation,
        element_index=index,
    )


def _receipt(
    action: DesktopAction,
    *,
    accepted: bool = True,
    after_local_window_id: str | None = None,
) -> ActionReceipt:
    return ActionReceipt(
        action=action,
        accepted=accepted,
        before_generation=action.generation,
        after_local_window_id=after_local_window_id,
    )


def test_action_requires_fresh_same_app_observable_state() -> None:
    verifier = DesktopVerifier()
    before = _observation("2 button Open", generation=3)
    after = _observation("2 button Open\n4 document Loaded", generation=4)
    action = _click(generation=3)

    result = verifier.verify_action(action, _receipt(action), before, after)

    assert result.verified
    assert result.app == "Notepad"
    assert result.generation == 4


def test_visual_viewport_action_uses_changed_screenshot_as_transition_evidence() -> None:
    verifier = DesktopVerifier()
    viewport = DesktopElement(
        "2",
        "Visual screenshot viewport",
        "VisualViewport",
        visual_ocr=True,
    )
    before = DesktopObservation(
        app="wechat",
        generation=1,
        accessibility_text="stable visual viewport",
        screenshot_png=b"before-frame",
        elements=(viewport,),
        local_window_id="window-a",
        captured_at=1.0,
    )
    after = DesktopObservation(
        app="wechat",
        generation=2,
        accessibility_text="stable visual viewport",
        screenshot_png=b"after-frame",
        elements=(viewport,),
        local_window_id="window-a",
        captured_at=2.0,
    )
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="wechat",
        generation=1,
        element_index="2",
        x=20,
        y=30,
    )

    result = verifier.verify_action(action, _receipt(action), before, after)

    assert result.verified is True


def test_visual_viewport_text_uses_changed_screenshot_as_transition_evidence() -> None:
    verifier = DesktopVerifier()
    viewport = DesktopElement(
        "2",
        "Visual screenshot viewport",
        "VisualViewport",
        visual_ocr=True,
        supported_actions=(DesktopElementAction.TYPE_TEXT,),
    )
    before = DesktopObservation(
        app="wechat",
        generation=1,
        accessibility_text="stable visual viewport",
        screenshot_png=b"focused-empty-search",
        elements=(viewport,),
        local_window_id="window-a",
        captured_at=1.0,
    )
    after = DesktopObservation(
        app="wechat",
        generation=2,
        accessibility_text="stable visual viewport",
        screenshot_png=b"search-results-visible",
        elements=(viewport,),
        local_window_id="window-a",
        captured_at=2.0,
    )
    action = DesktopAction(
        DesktopActionType.TYPE_TEXT,
        app="wechat",
        generation=1,
        element_index="2",
        text="文件传输助手",
    )

    result = verifier.verify_action(action, _receipt(action), before, after)

    assert result.verified is True


@pytest.mark.parametrize(
    ("after", "reason_fragment"),
    [
        (_observation("2 button Open", generation=3), "fresh generation"),
        (_observation("2 button Open", generation=4), "no observable"),
        (_observation("changed", app="Calculator", generation=4), "different application"),
        (
            _observation("changed", generation=4, captured_at=2.0),
            "predates",
        ),
    ],
)
def test_action_verification_fails_closed_on_stale_or_unbound_state(
    after: DesktopObservation,
    reason_fragment: str,
) -> None:
    verifier = DesktopVerifier()
    before = _observation("2 button Open", generation=3, captured_at=3.0)
    action = _click(generation=3)

    result = verifier.verify_action(action, _receipt(action), before, after)

    assert not result.verified
    assert reason_fragment in result.reason


@pytest.mark.parametrize(
    ("action_type", "action_fields"),
    [
        (DesktopActionType.CLICK, {}),
        (DesktopActionType.PERFORM_SECONDARY_ACTION, {"action_name": "invoke"}),
        (DesktopActionType.PRESS_KEY, {"key": "enter"}),
    ],
)
def test_driver_receipt_binds_fresh_related_local_window_transition(
    action_type: DesktopActionType,
    action_fields: dict[str, str],
) -> None:
    verifier = DesktopVerifier()
    target = DesktopElement("2", "Open result", "Button")
    before = DesktopObservation(
        app="wechat",
        generation=4,
        accessibility_text="source window",
        screenshot_png=b"source-frame",
        elements=(target,),
        local_window_id="window-a",
        captured_at=4.0,
    )
    after = DesktopObservation(
        app="WECHAT",
        generation=5,
        accessibility_text="related child window",
        screenshot_png=b"related-frame",
        local_window_id="window-b",
        captured_at=5.0,
    )
    action = DesktopAction(
        action_type,
        app="wechat",
        generation=4,
        element_index="2",
        **action_fields,
    )
    receipt = _receipt(action, after_local_window_id="window-b")

    result = verifier.verify_action(action, receipt, before, after)

    assert result.verified is True
    assert result.app == "WECHAT"
    assert result.generation == 5


@pytest.mark.parametrize(
    ("action_type", "action_fields"),
    [
        (DesktopActionType.CLICK, {}),
        (DesktopActionType.PERFORM_SECONDARY_ACTION, {"action_name": "invoke"}),
        (DesktopActionType.PRESS_KEY, {"key": "return"}),
    ],
)
@pytest.mark.parametrize(
    ("receipt_window_id", "after_window_id", "after_generation", "after_captured_at"),
    [
        (None, "window-b", 5, 5.0),
        ("window-c", "window-b", 5, 5.0),
        ("window-b", "window-b", 4, 5.0),
        ("window-b", "window-b", 5, 3.0),
    ],
)
def test_related_local_window_transition_rejects_missing_mismatched_or_stale_receipt(
    action_type: DesktopActionType,
    action_fields: dict[str, str],
    receipt_window_id: str | None,
    after_window_id: str,
    after_generation: int,
    after_captured_at: float,
) -> None:
    verifier = DesktopVerifier()
    before = DesktopObservation(
        app="wechat",
        generation=4,
        accessibility_text="source window",
        screenshot_png=b"source-frame",
        elements=(DesktopElement("2", "Open result", "Button"),),
        local_window_id="window-a",
        captured_at=4.0,
    )
    after = DesktopObservation(
        app="wechat",
        generation=after_generation,
        accessibility_text="related child window",
        screenshot_png=b"related-frame",
        local_window_id=after_window_id,
        captured_at=after_captured_at,
    )
    action = DesktopAction(
        action_type,
        app="wechat",
        generation=4,
        element_index="2",
        **action_fields,
    )
    receipt = _receipt(action, after_local_window_id=receipt_window_id)

    result = verifier.verify_action(action, receipt, before, after)

    assert result.verified is False


def test_driver_acceptance_and_matching_receipt_are_not_skipped() -> None:
    verifier = DesktopVerifier()
    before = _observation("2 button Open", generation=1)
    after = _observation("4 document Loaded", generation=2)
    action = _click()
    other_action = _click(index="3")

    rejected = verifier.verify_action(action, _receipt(action, accepted=False), before, after)
    mismatched = verifier.verify_action(action, _receipt(other_action), before, after)

    assert not rejected.verified
    assert not mismatched.verified


@pytest.mark.parametrize("action_type", [DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE])
def test_text_actions_require_exact_text_in_fresh_accessibility_state(
    action_type: DesktopActionType,
) -> None:
    verifier = DesktopVerifier()
    focused = action_type == DesktopActionType.TYPE_TEXT
    before = _observation(
        "0 edit Value: old",
        elements=(DesktopElement("0", "Value", "Edit", value="old", focused=focused),),
    )
    kwargs = (
        {"element_index": "0", "text": "CaseSensitive"}
        if action_type == DesktopActionType.TYPE_TEXT
        else {
            "element_index": "0",
            "value": "CaseSensitive",
        }
    )
    action = DesktopAction(type=action_type, app="Notepad", generation=1, **kwargs)

    missing = verifier.verify_action(
        action,
        _receipt(action),
        before,
        _observation(
            "0 edit Value: casesensitive",
            generation=2,
            elements=(
                DesktopElement("0", "Value", "Edit", value="casesensitive", focused=focused),
            ),
        ),
    )
    present = verifier.verify_action(
        action,
        _receipt(action),
        before,
        _observation(
            "0 edit Value: CaseSensitive",
            generation=2,
            elements=(
                DesktopElement("0", "Value", "Edit", value="CaseSensitive", focused=focused),
            ),
        ),
    )

    assert not missing.verified
    assert present.verified


def test_multiline_text_matches_driver_rendered_escape_sequences() -> None:
    verifier = DesktopVerifier()
    before = _observation(
        "0 edit Value: old",
        elements=(DesktopElement("0", "Value", "Edit", value="old", focused=True),),
    )
    action = DesktopAction(
        type=DesktopActionType.TYPE_TEXT,
        app="Notepad",
        generation=1,
        element_index="0",
        text="first\nsecond",
    )
    after = _observation(
        r"0 edit Value: first\nsecond",
        generation=2,
        elements=(
            DesktopElement(
                "0",
                "Value",
                "Edit",
                value=r"first\nsecond",
                focused=True,
            ),
        ),
    )

    assert verifier.verify_action(action, _receipt(action), before, after).verified


def test_text_action_uses_local_identity_when_electron_changes_composer_name() -> None:
    verifier = DesktopVerifier()
    identity = "a" * 64
    before = _observation(
        "empty composer",
        elements=(
            DesktopElement(
                "318",
                "",
                "Edit",
                value=None,
                focused=True,
                local_identity=identity,
            ),
        ),
    )
    action = DesktopAction(
        DesktopActionType.TYPE_TEXT,
        app="Notepad",
        generation=1,
        element_index="318",
        text="HandsFreePC-DRAFT-123",
    )
    after = _observation(
        "filled composer",
        generation=2,
        elements=(
            DesktopElement(
                "318",
                "HandsFreePC-DRAFT-123",
                "Edit",
                value="HandsFreePC-DRAFT-123",
                focused=True,
                local_identity=identity,
            ),
        ),
    )

    assert verifier.verify_action(action, _receipt(action), before, after).verified


def test_planner_done_prose_is_never_completion_evidence() -> None:
    verifier = DesktopVerifier()
    observation = _observation("VERIFIED_COMPLETION\n2 button Open")
    decision = DesktopDecision(
        kind=DesktopDecisionKind.DONE,
        reason="VERIFIED_COMPLETION",
        expectation=DesktopExpectation(DesktopExpectationKind.LAST_ACTION_VERIFIED),
    )

    result = verifier.verify_completion(decision, observation)

    assert not result.verified
    assert "locally verified" in result.reason


def test_visual_completion_is_bound_to_the_exact_current_screenshot() -> None:
    verifier = DesktopVerifier()
    screenshot = b"fresh-rendered-window"
    observation = DesktopObservation(
        app="wechat",
        generation=4,
        accessibility_text="rendered viewport",
        screenshot_png=screenshot,
        local_window_id="window-a",
        elements=(
            DesktopElement(
                "7",
                "Visual screenshot viewport",
                "VisualViewport",
                visual_ocr=True,
            ),
        ),
    )
    expectation = DesktopExpectation(
        DesktopExpectationKind.VISUAL_STATE_VERIFIED,
        visual_state_binding_token(observation),
    )

    assert verifier.verify_expectation(expectation, observation).verified

    stale = DesktopObservation(
        app="wechat",
        generation=5,
        accessibility_text="rendered viewport",
        screenshot_png=b"different-window-frame",
        elements=observation.elements,
        local_window_id="window-a",
    )
    assert not verifier.verify_expectation(expectation, stale).verified


def test_completion_uses_local_literal_expectation() -> None:
    verifier = DesktopVerifier()
    observation = _observation("4 document Design ready")
    passing = DesktopDecision(
        kind=DesktopDecisionKind.DONE,
        reason="planner says finished",
        app="notepad",
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Design ready",
        ),
    )
    failing = DesktopDecision(
        kind=DesktopDecisionKind.DONE,
        reason="planner says finished",
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Different result",
        ),
    )

    assert verifier.verify_completion(passing, observation).verified
    assert not verifier.verify_completion(failing, observation).verified


def test_text_expectations_use_bounded_structured_matches_not_substrings_or_raw_forgery() -> None:
    verifier = DesktopVerifier()
    present = DesktopExpectation(DesktopExpectationKind.TEXT_PRESENT, text="Home")
    absent = DesktopExpectation(DesktopExpectationKind.TEXT_ABSENT, text="Home")
    observation = _observation(
        "Home appears in untrusted rendered prose",
        elements=(DesktopElement("0", "Homebrew", "Text", value="Homepage"),),
    )

    assert not verifier.verify_expectation(present, observation).verified
    assert verifier.verify_expectation(absent, observation).verified


def test_focused_name_requires_an_exact_match_not_a_prefix() -> None:
    result = DesktopVerifier().verify_expectation(
        DesktopExpectation(DesktopExpectationKind.FOCUSED_CONTAINS, text="Home"),
        _observation(
            "focused Homebrew",
            elements=(DesktopElement("0", "Homebrew", "Edit", focused=True),),
        ),
    )

    assert not result.verified


@pytest.mark.parametrize(
    "after_elements",
    [
        (DesktopElement("0", "Other", "Edit", value="hello", focused=True),),
        (
            DesktopElement("5", "Prompt", "Edit", value="hello", focused=True),
            DesktopElement("6", "Prompt", "Edit", value="hello", focused=False),
        ),
        (
            DesktopElement(
                "5",
                "Other",
                "Edit",
                automation_id="prompt-id",
                value="hello",
                focused=True,
            ),
        ),
    ],
)
def test_text_action_verification_never_rebinds_by_ephemeral_index_or_ambiguous_identity(
    after_elements,
) -> None:
    automation_id = "prompt-id" if after_elements[0].automation_id else None
    before = _observation(
        "Prompt",
        elements=(
            DesktopElement(
                "0",
                "Prompt",
                "Edit",
                automation_id=automation_id,
                value="",
                focused=True,
            ),
        ),
    )
    action = DesktopAction(
        DesktopActionType.TYPE_TEXT,
        app="Notepad",
        generation=1,
        element_index="0",
        text="hello",
    )
    after = _observation(
        "changed",
        generation=2,
        elements=after_elements,
    )

    result = DesktopVerifier().verify_action(action, _receipt(action), before, after)

    assert not result.verified
    assert "absent or ambiguous" in result.reason


def test_last_action_completion_is_bound_to_same_app_and_generation() -> None:
    verifier = DesktopVerifier()
    before = _observation("2 button Open", generation=1)
    after = _observation("4 document Loaded", generation=2)
    action = _click(generation=1)
    verified_action = verifier.verify_action(action, _receipt(action), before, after)
    expectation = DesktopExpectation(DesktopExpectationKind.LAST_ACTION_VERIFIED)

    assert verifier.verify_expectation(
        expectation,
        after,
        last_action_result=verified_action,
    ).verified
    assert not verifier.verify_expectation(
        expectation,
        _observation("new", generation=3),
        last_action_result=verified_action,
    ).verified


def test_focused_expectation_only_uses_explicit_focus_evidence() -> None:
    verifier = DesktopVerifier()
    expectation = DesktopExpectation(
        DesktopExpectationKind.FOCUSED_CONTAINS,
        text="Prompt",
    )
    unfocused = _observation(
        "1 text Prompt\n2 button Send",
        elements=(DesktopElement("1", "Prompt", "Edit", focused=False),),
    )
    focused = _observation(
        "The focused UI element is edit Prompt.",
        elements=(DesktopElement("1", "Prompt", "Edit", focused=True),),
    )
    forged = _observation(
        '0 name="status focused=true Prompt"',
        elements=(
            DesktopElement(
                "0",
                "status focused=true Prompt",
                "Text",
                focused=False,
            ),
        ),
    )

    assert not verifier.verify_expectation(expectation, unfocused).verified
    assert verifier.verify_expectation(expectation, focused).verified
    assert not verifier.verify_expectation(expectation, forged).verified


def test_selected_expectation_requires_one_exact_locally_selected_element() -> None:
    verifier = DesktopVerifier()
    expectation = DesktopExpectation(
        DesktopExpectationKind.ELEMENT_SELECTED,
        text="Chat",
    )
    selected = _observation(
        "0 name=Chat selected=true",
        elements=(DesktopElement("0", "Chat", "TabItem", selected=True),),
    )
    unselected = _observation(
        "0 name=Chat selected=false",
        elements=(DesktopElement("0", "Chat", "TabItem", selected=False),),
    )

    assert verifier.verify_expectation(expectation, selected).verified
    assert not verifier.verify_expectation(expectation, unselected).verified


def test_damaged_unicode_cannot_verify_action_or_completion() -> None:
    with pytest.raises(ValueError, match="damaged Unicode"):
        _observation("changed \ufffd", generation=2)


def test_search_submission_requires_the_entire_field_value_to_equal_the_query() -> None:
    verifier = DesktopVerifier()
    target = DesktopElement(
        "2",
        "Address and search bar",
        "Edit",
        value="OpenAI pricing",
        focused=True,
    )
    before = _observation(
        "search input",
        app="chrome",
        generation=1,
        elements=(target,),
    )
    after = _observation(
        "search results changed",
        app="chrome",
        generation=2,
        elements=(target, DesktopElement("3", "Results", "Text")),
    )
    action = DesktopAction(
        DesktopActionType.PRESS_KEY,
        app="chrome",
        generation=1,
        element_index="2",
        key="enter",
    )

    result = verifier.verify_search_submission(
        action,
        DesktopExpectation(DesktopExpectationKind.SEARCH_SUBMITTED, "OpenAI"),
        before,
        after,
    )

    assert not result.verified
