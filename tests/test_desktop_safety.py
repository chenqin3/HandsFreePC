from __future__ import annotations

import pytest

from handsfree_pc.desktop.protocol import (
    DesktopAction,
    DesktopActionType,
    DesktopElement,
    DesktopExpectation,
    DesktopExpectationKind,
    DesktopObservation,
)
from handsfree_pc.desktop.safety import (
    DesktopSafetyDisposition,
    DesktopSafetyPolicy,
)


def _observation(
    text: str = "0 window Main\n  2 button Open\n\nThe focused UI element is edit Prompt.",
    *,
    app: str = "Claude",
    generation: int = 7,
    title: str = "Claude",
    elements: tuple[DesktopElement, ...] = (),
) -> DesktopObservation:
    return DesktopObservation(
        app=app,
        generation=generation,
        accessibility_text=f'App={app}\nWindow: "{title}", App: {app}.\n{text}',
        window_title=title,
        elements=elements,
    )


def _action(
    action_type: DesktopActionType = DesktopActionType.CLICK,
    *,
    app: str = "Claude",
    generation: int = 7,
    index: str | None = "2",
    **kwargs: object,
) -> DesktopAction:
    values = dict(kwargs)
    if index is not None:
        values["element_index"] = index
    return DesktopAction(type=action_type, app=app, generation=generation, **values)


def test_semantic_low_risk_action_is_allowed() -> None:
    observation = _observation(
        elements=(DesktopElement("2", "Open", "Button"),),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(),
        observation,
        user_text="click Open in Claude",
    )

    assert result.allowed
    assert result.confirmation is None


def test_duplicate_semantic_labels_are_neither_exposed_nor_actionable() -> None:
    observation = _observation(
        elements=(
            DesktopElement("2", "Open", "Button"),
            DesktopElement("3", "Open", "Button"),
        ),
    )
    policy = DesktopSafetyPolicy()
    task = "In Claude, click Open to show Home"

    planner_view = policy.planner_observation(observation, user_text=task)
    result = policy.evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Home",
        ),
    )

    assert planner_view.elements == ()
    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    "action",
    [
        _action(app="Codex"),
        _action(generation=6),
        _action(index="99"),
    ],
)
def test_action_must_be_bound_to_current_observation_and_element(
    action: DesktopAction,
) -> None:
    result = DesktopSafetyPolicy().evaluate(action, _observation())

    assert result.disposition == DesktopSafetyDisposition.BLOCK


def test_coordinate_click_and_drag_are_blocked_without_semantic_target() -> None:
    observation = _observation()
    click = _action(index=None, x=10.0, y=20.0)
    drag = _action(
        DesktopActionType.DRAG,
        index=None,
        from_x=1.0,
        from_y=2.0,
        to_x=3.0,
        to_y=4.0,
    )

    assert DesktopSafetyPolicy().evaluate(click, observation).disposition == "block"
    assert DesktopSafetyPolicy().evaluate(drag, observation).disposition == "block"


@pytest.mark.parametrize(
    ("app", "title"),
    [
        ("Windows Terminal", "PowerShell"),
        ("cmd.exe", "Command Prompt"),
        ("PowerShell", "Administrator"),
        ("终端", "终端"),
    ],
)
def test_terminal_and_shell_apps_are_blocked(app: str, title: str) -> None:
    observation = _observation("2 button New tab", app=app, title=title)
    action = _action(app=app)

    result = DesktopSafetyPolicy().evaluate(action, observation)

    assert result.disposition == DesktopSafetyDisposition.BLOCK
    assert "terminal" in result.reason


def test_embedded_terminal_target_is_blocked() -> None:
    observation = _observation("2 tab Terminal\n3 tab Editor")

    result = DesktopSafetyPolicy().evaluate(_action(), observation)

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    ("text", "user_text", "reason_fragment"),
    [
        ("2 button User Account Control", "", "security"),
        ("2 edit Password", "", "password"),
        ("2 button Pay now", "", "payments"),
        ("2 edit Prompt", "输入银行卡号", "payments"),
        ("2 edit Prompt", "type my password", "password"),
    ],
)
def test_security_secret_and_payment_targets_are_blocked(
    text: str,
    user_text: str,
    reason_fragment: str,
) -> None:
    observation = _observation(text)

    result = DesktopSafetyPolicy().evaluate(
        _action(),
        observation,
        user_text=user_text,
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK
    assert reason_fragment in result.reason


def test_false_password_metadata_does_not_make_every_uia_element_secret() -> None:
    observation = _observation(
        '2 name="Prompt" focused=true password=false enabled=true',
        elements=(DesktopElement("2", "Prompt", "Edit", focused=True),),
    )
    action = _action(
        DesktopActionType.TYPE_TEXT,
        index="2",
        text="hello",
    )

    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="type hello into Prompt",
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            text="hello",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.CONFIRM
    assert "secret" not in result.reason


def test_static_label_and_its_single_edit_field_are_not_false_ambiguity() -> None:
    observation = _observation(
        '2 name="Prompt" control_type="Text" focused=false\n'
        '3 name="Prompt" control_type="Edit" focused=true',
        elements=(
            DesktopElement("2", "Prompt", "Text"),
            DesktopElement("3", "Prompt", "Edit", focused=True),
        ),
    )
    action = _action(DesktopActionType.TYPE_TEXT, index="3", text="hello")

    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="type hello into Prompt",
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            text="hello",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.CONFIRM


def test_two_same_named_edit_fields_remain_ambiguous() -> None:
    observation = _observation(
        elements=(
            DesktopElement("2", "Prompt", "Edit", focused=True),
            DesktopElement("3", "Prompt", "Edit", focused=False),
        ),
    )
    action = _action(DesktopActionType.TYPE_TEXT, index="2", text="hello")

    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="type hello into Prompt",
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            text="hello",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


def test_text_input_cannot_target_a_non_editable_control() -> None:
    observation = _observation(
        elements=(DesktopElement("2", "Prompt", "Button", focused=True),),
    )
    action = _action(DesktopActionType.TYPE_TEXT, index="2", text="hello")

    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="type hello into Prompt",
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            text="hello",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    "target",
    [
        "2 button Send",
        "2 button Submit",
        "2 button Delete",
        "2 button Install",
        "2 button Upload",
        "2 button 发送",
        "2 button 删除",
        "2 button 安装",
        "2 button 上传",
    ],
)
def test_external_or_destructive_click_requires_action_bound_confirmation(
    target: str,
) -> None:
    policy = DesktopSafetyPolicy()
    observation = _observation(target)
    action = _action()

    element_name = target.split(maxsplit=2)[-1]
    observation = _observation(
        target,
        elements=(DesktopElement("2", element_name, "Button"),),
    )
    first = policy.evaluate(action, observation, user_text=f"click {element_name} in Claude")
    second = policy.evaluate(action, observation, user_text=f"click {element_name} in Claude")

    assert first.disposition == DesktopSafetyDisposition.CONFIRM
    assert first.needs_confirmation
    assert first.confirmation is not None
    assert first.confirmation.confirmation_id.startswith("desktop-")
    assert f'user-spoken-target="{element_name}"' in first.confirmation.summary
    assert first.confirmation == second.confirmation


def test_confirmation_is_bound_to_exact_action_and_observation() -> None:
    policy = DesktopSafetyPolicy()
    observation = _observation(
        "2 button Send\n3 button Delete",
        elements=(
            DesktopElement("2", "Send", "Button"),
            DesktopElement("3", "Delete", "Button"),
        ),
    )
    send = policy.evaluate(_action(index="2"), observation, user_text="click Send")
    delete = policy.evaluate(_action(index="3"), observation, user_text="click Delete")
    newer_observation = _observation(
        "2 button Send",
        generation=8,
        elements=(DesktopElement("2", "Send", "Button"),),
    )
    newer = policy.evaluate(
        _action(generation=8),
        newer_observation,
        user_text="click Send",
    )

    assert send.confirmation is not None
    assert delete.confirmation is not None
    assert newer.confirmation is not None
    assert send.confirmation.confirmation_id != delete.confirmation.confirmation_id
    assert send.confirmation.confirmation_id != newer.confirmation.confirmation_id


def test_typing_a_draft_is_not_mistaken_for_sending_it() -> None:
    observation = _observation(
        "2 button Send\n3 edit Prompt focused=true\n\nThe focused UI element is edit Prompt.",
        elements=(DesktopElement("3", "Prompt", "Edit", focused=True),),
    )
    action = _action(
        DesktopActionType.TYPE_TEXT,
        index="3",
        text="draft only",
    )

    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="type draft only into Prompt",
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            text="draft only",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.CONFIRM
    assert result.reason == "enter exact user-spoken text"


def test_enter_on_send_surface_requires_confirmation() -> None:
    observation = _observation(
        "2 button Send\n3 edit Prompt focused=true\n\nThe focused UI element is edit Prompt.",
        elements=(DesktopElement("3", "Prompt", "Edit", focused=True),),
    )
    action = _action(DesktopActionType.PRESS_KEY, index="3", key="Return")

    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="press Return on Prompt to Send",
    )

    assert result.disposition == DesktopSafetyDisposition.CONFIRM


@pytest.mark.parametrize(
    ("action", "task"),
    [
        (_action(index="2"), "click OK to permanently delete this file"),
        (
            _action(DesktopActionType.PRESS_KEY, index="2", key="space"),
            "press Space on OK to permanently delete this file",
        ),
    ],
)
def test_generic_confirmation_control_uses_whole_dialog_risk_context(action, task) -> None:
    observation = _observation(
        "0 text Permanently delete this file?\n2 button OK focused=true",
        elements=(DesktopElement("2", "OK", "Button", focused=True),),
    )

    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text=task,
    )

    assert result.disposition == DesktopSafetyDisposition.CONFIRM


@pytest.mark.parametrize(
    "key",
    ["Ctrl+Enter", "Alt+S", "Shift+Delete", "Alt+F4", "Win+R", "F5", "Delete"],
)
def test_side_effect_and_system_keyboard_shortcuts_are_rejected_by_protocol(key: str) -> None:
    with pytest.raises(ValueError, match="navigation-only"):
        _action(DesktopActionType.PRESS_KEY, index="2", key=key)


def test_unbound_secondary_action_name_is_blocked() -> None:
    action = _action(
        DesktopActionType.PERFORM_SECONDARY_ACTION,
        action_name="Delete",
    )

    observation = _observation(
        elements=(DesktopElement("2", "Open", "Button"),),
    )
    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="Delete Open in Claude",
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


def test_clipboard_paste_is_blocked_because_payload_is_unknown() -> None:
    with pytest.raises(ValueError, match="navigation-only"):
        _action(DesktopActionType.PRESS_KEY, index="2", key="Ctrl+V")


def test_send_is_matched_as_a_word_not_inside_sender() -> None:
    observation = _observation(
        "2 button Sender settings",
        elements=(DesktopElement("2", "Sender settings", "Button"),),
    )

    result = DesktopSafetyPolicy().evaluate(
        _action(),
        observation,
        user_text="click Sender settings in Claude",
    )

    assert result.disposition == DesktopSafetyDisposition.ALLOW


def test_text_input_requires_explicit_focused_element_evidence() -> None:
    observation = _observation(
        "2 edit Prompt",
        elements=(DesktopElement("2", "Prompt", "Edit", focused=False),),
    )
    action = _action(DesktopActionType.TYPE_TEXT, index="2", text="hello")

    result = DesktopSafetyPolicy().evaluate(action, observation)

    assert result.disposition == DesktopSafetyDisposition.BLOCK


def test_damaged_unicode_state_is_blocked() -> None:
    with pytest.raises(ValueError, match="damaged Unicode"):
        _observation("2 button Open \ufffd")


@pytest.mark.parametrize(
    "identifier",
    [
        "API_KEY",
        "api-key",
        "apiKey",
        "APIKey",
        "APIKEY",
        "access_token",
        "secret-key",
        "one_time_code",
        "PasswordField",
        "PowerShell",
        "Power-Shell",
        "power_shell",
    ],
)
def test_identifier_styles_cannot_bypass_sensitive_surface_filter(identifier: str) -> None:
    result = DesktopSafetyPolicy().inspect_observation(_observation(f"2 edit {identifier}"))

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    "dummy_secret",
    [
        "sk-proj-" + "DUMMYTOKEN1234567890abcdef",
        "ghp_" + "DUMMYTOKEN1234567890abcdef123456",
        "AKIA" + "DUMMYTOKEN123456",
        "eyJDUMMYHEADER123.DUMMYPAYLOAD123.DUMMYSIGNATURE123",
        "Bearer DUMMYmixedToken1234567890_abcdef-XYZ",
        "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789-_TOKEN",
        "-----BEGIN PRIVATE KEY-----",
    ],
)
def test_unlabelled_credential_like_values_are_blocked_before_planning(
    dummy_secret: str,
) -> None:
    result = DesktopSafetyPolicy().inspect_observation(_observation(f"2 text {dummy_secret}"))

    assert result.disposition == DesktopSafetyDisposition.BLOCK


def test_task_authorized_planner_view_omits_but_local_policy_blocks_sensitive_content() -> None:
    dummy_secret = "sk-proj-" + "DUMMYTOKEN1234567890abcdef"
    observation = _observation(
        f'2 name="Code" control_type="Button"\n9 value="{dummy_secret}"',
        elements=(
            DesktopElement("2", "Code", "Button"),
            DesktopElement("9", "Unrelated conversation", "Document", value=dummy_secret),
        ),
    )
    policy = DesktopSafetyPolicy()

    raw = policy.inspect_observation(observation)
    scoped = policy.inspect_observation(
        observation,
        user_text="In Claude, click Code",
    )
    planner_view = policy.planner_observation(
        observation,
        user_text="In Claude, click Code",
    )

    assert raw.disposition == DesktopSafetyDisposition.BLOCK
    assert scoped.disposition == DesktopSafetyDisposition.BLOCK
    assert [element.name for element in planner_view.elements] == ["Code"]
    assert dummy_secret not in planner_view.accessibility_text
    assert planner_view.screenshot_png is None


def test_quoted_command_text_and_standalone_not_do_not_authorize_clicks() -> None:
    observation = _observation(
        '2 name="Green button" control_type="Button"\n3 name="Dashboard" control_type="Text"',
        elements=(
            DesktopElement("2", "Green button", "Button"),
            DesktopElement("3", "Dashboard", "Text"),
        ),
    )
    action = _action(DesktopActionType.CLICK, index="2")
    expectation = DesktopExpectation(DesktopExpectationKind.TEXT_PRESENT, text="Dashboard")
    policy = DesktopSafetyPolicy()

    quoted = policy.evaluate(
        action,
        observation,
        user_text='Type the phrase "click Green button and show Dashboard" into Prompt',
        expectation=expectation,
    )
    negated = policy.evaluate(
        action,
        observation,
        user_text="not click Green button and show Dashboard",
        expectation=expectation,
    )
    planner_view = policy.planner_observation(
        observation,
        user_text='Type the phrase "click Green button and show Dashboard" into Prompt',
    )

    assert quoted.disposition == DesktopSafetyDisposition.BLOCK
    assert negated.disposition == DesktopSafetyDisposition.BLOCK
    assert planner_view.elements == ()


@pytest.mark.parametrize(
    "task",
    [
        "type click Green button into Prompt and show Dashboard",
        "type the words click Green button into Prompt and show Dashboard",
        "cannot click Green button and show Dashboard",
        "can't click Green button and show Dashboard",
        "shouldn't click Green button and show Dashboard",
        "mustn't click Green button and show Dashboard",
        "avoid click Green button and show Dashboard",
        "不能点击 Green button 然后显示 Dashboard",
        "请勿点击 Green button 然后显示 Dashboard",
        "避免点击 Green button 然后显示 Dashboard",
    ],
)
def test_unquoted_dictation_and_extended_negations_do_not_authorize_click(
    task: str,
) -> None:
    observation = _observation(
        '2 name="Green button" control_type="Button"\n3 name="Dashboard" control_type="Text"',
        elements=(
            DesktopElement("2", "Green button", "Button"),
            DesktopElement("3", "Dashboard", "Text"),
        ),
    )
    policy = DesktopSafetyPolicy()
    result = policy.evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Dashboard",
        ),
    )
    planner_view = policy.planner_observation(observation, user_text=task)

    assert result.disposition == DesktopSafetyDisposition.BLOCK
    assert all(element.name != "Green button" for element in planner_view.elements)


@pytest.mark.parametrize(
    "task",
    [
        "type go to click Open into Prompt",
        "type words go to click Open into Prompt",
        "输入文字进度点击 Open 到 Prompt",
        "输入文字到底点击 Open 到 Prompt",
        "输入文字至少点击 Open 到 Prompt",
    ],
)
def test_action_words_after_an_earlier_payload_separator_remain_dictated_data(
    task: str,
) -> None:
    observation = _observation(
        elements=(
            DesktopElement("2", "Prompt", "Edit", value="", focused=True),
            DesktopElement("3", "Open", "Button"),
        ),
    )
    policy = DesktopSafetyPolicy()

    planner_view = policy.planner_observation(observation, user_text=task)
    result = policy.evaluate(
        _action(DesktopActionType.CLICK, index="3"),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            text="Open",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK
    assert "Open" not in {element.name for element in planner_view.elements}


def test_action_words_inside_dictated_payload_do_not_hide_the_exact_text_target() -> None:
    observation = _observation(
        elements=(DesktopElement("2", "Prompt", "Edit", value="", focused=True),),
    )
    action = _action(
        DesktopActionType.TYPE_TEXT,
        index="2",
        text="click Green button",
    )

    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="type click Green button into Prompt",
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            text="click Green button",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.CONFIRM


@pytest.mark.parametrize(
    "task",
    [
        "In Claude, click Safe to show Done next to Enable Sync",
        "在 Claude 中点击 安全 以显示 完成 旁边的 启用同步",
    ],
)
def test_spatial_reference_label_is_not_authorized_as_an_action_target(task: str) -> None:
    target_name = "Enable Sync" if "Enable" in task else "启用同步"
    safe_name = "Safe" if "Safe" in task else "安全"
    done_name = "Done" if "Done" in task else "完成"
    observation = _observation(
        elements=(
            DesktopElement("2", safe_name, "Button"),
            DesktopElement("3", done_name, "Text"),
            DesktopElement("4", target_name, "Button"),
        ),
    )
    policy = DesktopSafetyPolicy()
    planner_view = policy.planner_observation(observation, user_text=task)
    result = policy.evaluate(
        _action(DesktopActionType.CLICK, index="4"),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text=done_name,
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK
    assert target_name not in {element.name for element in planner_view.elements}
    assert {safe_name, done_name}.issubset({element.name for element in planner_view.elements})


@pytest.mark.parametrize(
    "task",
    [
        "In Claude, click the button following Done to show Home",
        "In Claude, click the button preceding Done to show Home",
        "In Claude, click the button opposite Done to show Home",
        "In Claude, click the button across from Done to show Home",
        "In Claude, click in the group named Done to show Home",
        "In Claude, click the button associated with Done to show Home",
        "In Claude, click a different button from Done to show Home",
        "In Claude, click the parent or child of Done to show Home",
        "In Claude, click the icon inside Done button to show Home",
        "In Claude, click the control in Done button to show Home",
        "In Claude, click the child within Done button to show Home",
    ],
)
def test_unknown_relationship_grammar_never_turns_reference_into_click_authority(
    task: str,
) -> None:
    observation = _observation(
        elements=(
            DesktopElement("2", "Done", "Button"),
            DesktopElement("3", "Home", "Text"),
        ),
    )
    policy = DesktopSafetyPolicy()

    planner_view = policy.planner_observation(observation, user_text=task)
    result = policy.evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Home",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK
    assert "Done" not in {element.name for element in planner_view.elements}


@pytest.mark.parametrize(
    "task",
    [
        "In Claude, click Done and verify Home is absent",
        "In Claude, click Done and verify Home remains hidden",
    ],
)
def test_positive_presence_postcondition_cannot_reverse_user_requested_polarity(
    task: str,
) -> None:
    observation = _observation(
        elements=(
            DesktopElement("2", "Done", "Button"),
            DesktopElement("3", "Home", "Text"),
        ),
    )

    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Home",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    "task",
    [
        "In Claude, click Safe but do not show Secret",
        "In Claude, click Safe but don't display Secret",
        "In Claude, click Safe but never reveal Secret",
        "In Claude, click Safe but do not open Secret",
    ],
)
def test_negated_destination_cannot_be_used_as_positive_success_evidence(task: str) -> None:
    observation = _observation(
        elements=(
            DesktopElement("2", "Safe", "Button"),
            DesktopElement("3", "Secret", "Text"),
        ),
    )

    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Secret",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    "task",
    [
        "In Claude, click Done, but do not select Done",
        "In Claude, click Done, but never select Done",
        "In Claude, click Done, but avoid selecting Done",
    ],
)
def test_explicitly_negated_selection_vetoes_an_earlier_click_reference(task: str) -> None:
    observation = _observation(elements=(DesktopElement("2", "Done", "Button"),))

    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            text="Done",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    "task",
    [
        "In Claude, click Open to show Home, but do not click Open",
        "In Claude, do not click Open, but click Open to show Home",
        "In Claude, never open Open, but show Home",
        "In Claude, avoid clicking Open, but show Home",
    ],
)
def test_any_explicit_negation_of_the_same_target_vetoes_click_authority(task: str) -> None:
    observation = _observation(
        elements=(
            DesktopElement("2", "Open", "Button"),
            DesktopElement("3", "Home", "Text"),
        ),
    )
    policy = DesktopSafetyPolicy()

    assert (
        policy.evaluate(
            _action(DesktopActionType.CLICK, index="2"),
            observation,
            user_text=task,
            expectation=DesktopExpectation(
                DesktopExpectationKind.TEXT_PRESENT,
                text="Home",
            ),
        ).disposition
        == DesktopSafetyDisposition.BLOCK
    )


@pytest.mark.parametrize(
    "task",
    [
        "In Claude, click Safe to show Done, then view Home",
        "在 Claude 中点击 安全 以显示 完成，然后查看 首页",
    ],
)
def test_destination_state_verb_does_not_authorize_clicking_the_destination(task: str) -> None:
    target_name = "Done" if "Done" in task else "完成"
    safe_name = "Safe" if "Safe" in task else "安全"
    home_name = "Home" if "Home" in task else "首页"
    observation = _observation(
        elements=(
            DesktopElement("2", safe_name, "Button"),
            DesktopElement("3", target_name, "Button"),
            DesktopElement("4", home_name, "Text"),
        ),
    )
    policy = DesktopSafetyPolicy()
    planner_view = policy.planner_observation(observation, user_text=task)
    result = policy.evaluate(
        _action(DesktopActionType.CLICK, index="3"),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text=home_name,
        ),
    )

    # Destination labels remain available for postconditions; show/view does
    # not become local authority to activate that destination as a control.
    assert target_name in {element.name for element in planner_view.elements}
    assert result.disposition == DesktopSafetyDisposition.BLOCK


def test_full_local_payment_surface_blocks_harmless_looking_next_action() -> None:
    observation = _observation(
        '2 name="Next" control_type="Button"\n3 name="Credit card" control_type="Edit"\n'
        '4 name="Pay now" control_type="Button"',
        elements=(
            DesktopElement("2", "Next", "Button"),
            DesktopElement("3", "Credit card", "Edit"),
            DesktopElement("4", "Pay now", "Button"),
        ),
    )
    action = _action(DesktopActionType.CLICK, index="2")
    task = "click Next and show Review"
    policy = DesktopSafetyPolicy()

    assert policy.inspect_observation(observation, user_text=task).disposition == (
        DesktopSafetyDisposition.BLOCK
    )
    assert policy.evaluate(action, observation, user_text=task).disposition == (
        DesktopSafetyDisposition.BLOCK
    )


@pytest.mark.parametrize(
    "labels",
    [
        ("Telemetry", "Diagnostic data"),
        ("Account settings", "Profile"),
        ("Public link", "Link sharing"),
        ("Activity controls", "Ordinary setting"),
        ("History settings", "Ordinary setting"),
        ("Temporary Chat", "Ordinary setting"),
        ("Incognito mode", "Ordinary setting"),
    ],
)
def test_full_local_privacy_surface_blocks_harmless_looking_continue_action(labels) -> None:
    first, second = labels
    observation = _observation(
        f'2 name="Continue" control_type="Button"\n'
        f'3 name="{first}" control_type="Text"\n'
        f'4 name="{second}" control_type="Text"',
        elements=(
            DesktopElement("2", "Continue", "Button"),
            DesktopElement("3", first, "Text"),
            DesktopElement("4", second, "Text"),
        ),
    )
    action = _action(DesktopActionType.CLICK, index="2")
    task = "In Claude, click Continue to open Home"
    policy = DesktopSafetyPolicy()

    assert policy.inspect_observation(observation, user_text=task).disposition == (
        DesktopSafetyDisposition.BLOCK
    )
    assert (
        policy.evaluate(
            action,
            observation,
            user_text=task,
            expectation=DesktopExpectation(
                DesktopExpectationKind.TEXT_PRESENT,
                text="Home",
            ),
        ).disposition
        == DesktopSafetyDisposition.BLOCK
    )


@pytest.mark.parametrize(
    "public_visibility_label",
    [
        "Anyone with the link can view",
        "Make public",
        "Public access",
        "General access",
        "Public URL",
        "Public on the web",
        "Anyone on the internet",
        "Shareable link",
        "Link settings",
        "Link access",
        "拥有链接的任何人",
        "任何知道链接的人",
    ],
)
def test_common_public_link_visibility_labels_block_the_whole_surface(
    public_visibility_label,
) -> None:
    observation = _observation(
        f'2 name="Continue" control_type="Button"\n'
        f'3 name="{public_visibility_label}" control_type="Text"',
        elements=(
            DesktopElement("2", "Continue", "Button"),
            DesktopElement("3", public_visibility_label, "Text"),
        ),
    )
    policy = DesktopSafetyPolicy()
    task = "In Claude, click Continue to open Home"

    assert policy.inspect_observation(observation, user_text=task).disposition == (
        DesktopSafetyDisposition.BLOCK
    )
    assert (
        policy.evaluate(
            _action(DesktopActionType.CLICK, index="2"),
            observation,
            user_text=task,
            expectation=DesktopExpectation(
                DesktopExpectationKind.TEXT_PRESENT,
                text="Home",
            ),
        ).disposition
        == DesktopSafetyDisposition.BLOCK
    )


@pytest.mark.parametrize(
    "privacy_label",
    [
        "Data controls",
        "Improve the model for everyone",
        "Model training",
        "Training data",
        "Usage data",
        "帮助改进模型",
        "训练数据",
    ],
)
def test_model_training_and_usage_data_privacy_labels_block_the_whole_surface(
    privacy_label,
) -> None:
    observation = _observation(
        f'2 name="Continue" control_type="Button"\n3 name="{privacy_label}" control_type="Text"',
        elements=(
            DesktopElement("2", "Continue", "Button"),
            DesktopElement("3", privacy_label, "Text"),
        ),
    )
    policy = DesktopSafetyPolicy()

    assert (
        policy.inspect_observation(
            observation,
            user_text="In Claude, click Continue to show Home",
        ).disposition
        == DesktopSafetyDisposition.BLOCK
    )


@pytest.mark.parametrize(
    ("action", "task"),
    [
        (_action(DesktopActionType.CLICK, index="2"), "click Share and show Done"),
        (
            _action(
                DesktopActionType.PERFORM_SECONDARY_ACTION,
                index="2",
                action_name="invoke",
            ),
            "click Share and show Done",
        ),
        (
            _action(DesktopActionType.PRESS_KEY, index="2", key="enter"),
            "press Enter on Share and show Done",
        ),
        (
            _action(DesktopActionType.PRESS_KEY, index="2", key="space"),
            "press Space on Share and show Done",
        ),
    ],
)
def test_share_activation_always_requires_confirmation(action, task) -> None:
    observation = _observation(
        '2 name="Share" control_type="Button" focused=true',
        elements=(DesktopElement("2", "Share", "Button", focused=True),),
    )

    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Done",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.CONFIRM


@pytest.mark.parametrize(
    "label",
    [
        "Attach",
        "Attachment",
        "Add attachment",
        "Add file",
        "Choose files",
        "附件",
    ],
)
@pytest.mark.parametrize(
    "action_type",
    [
        DesktopActionType.CLICK,
        DesktopActionType.PERFORM_SECONDARY_ACTION,
        DesktopActionType.PRESS_KEY,
    ],
)
def test_attachment_activation_always_requires_confirmation(label, action_type) -> None:
    observation = _observation(
        f'2 name="{label}" control_type="Button" focused=true',
        elements=(DesktopElement("2", label, "Button", focused=True),),
    )
    kwargs = (
        {"action_name": "invoke"}
        if action_type == DesktopActionType.PERFORM_SECONDARY_ACTION
        else {"key": "enter"}
        if action_type == DesktopActionType.PRESS_KEY
        else {}
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(action_type, index="2", **kwargs),
        observation,
        user_text=(
            f"press Enter on {label} and show Done"
            if action_type == DesktopActionType.PRESS_KEY
            else f"click {label} and show Done"
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Done",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.CONFIRM


@pytest.mark.parametrize(
    "label",
    [
        "Comment",
        "Clear",
        "Clear all",
        "Update",
        "Trash",
        "Move to bin",
        "Move to Recycle Bin",
        "Invite people",
        "Grant access",
        "End task",
        "Terminate",
        "Unpublish",
        "Repost",
    ],
)
@pytest.mark.parametrize(
    "action_type",
    [
        DesktopActionType.CLICK,
        DesktopActionType.PERFORM_SECONDARY_ACTION,
        DesktopActionType.PRESS_KEY,
    ],
)
def test_common_side_effect_labels_require_confirmation(label, action_type) -> None:
    observation = _observation(
        f'2 name="{label}" control_type="Button" focused=true',
        elements=(DesktopElement("2", label, "Button", focused=True),),
    )
    kwargs = (
        {"action_name": "invoke"}
        if action_type == DesktopActionType.PERFORM_SECONDARY_ACTION
        else {"key": "enter"}
        if action_type == DesktopActionType.PRESS_KEY
        else {}
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(action_type, index="2", **kwargs),
        observation,
        user_text=(
            f"press Enter on {label} and show Done"
            if action_type == DesktopActionType.PRESS_KEY
            else f"click {label} and show Done"
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Done",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.CONFIRM


@pytest.mark.parametrize(
    "sibling_label",
    [
        "Permanently delete all data",
        "Upload and share file",
        "Install software",
        "Close without saving",
        "Send message externally",
    ],
)
def test_full_surface_side_effect_requires_confirmation_for_innocuous_next_button(
    sibling_label,
) -> None:
    observation = _observation(
        f'2 name="Next" control_type="Button"\n3 name="{sibling_label}" control_type="Text"',
        elements=(
            DesktopElement("2", "Next", "Button"),
            DesktopElement("3", sibling_label, "Text"),
        ),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text="In Claude, click Next to open Home",
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Home",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.CONFIRM
    assert result.confirmation is not None
    assert 'user-spoken-target="Next"' in result.confirmation.summary
    assert sibling_label not in result.confirmation.summary


@pytest.mark.parametrize("automation_id", ["PasswordField", "APIKey", "Terminal"])
def test_local_automation_id_is_classified_but_never_sent_to_planner(
    automation_id: str,
) -> None:
    observation = _observation(
        '2 name="Code" control_type="Edit"',
        elements=(
            DesktopElement(
                "2",
                "Code",
                "Edit",
                automation_id=automation_id,
                value="ordinary",
            ),
        ),
    )
    policy = DesktopSafetyPolicy()
    planner_view = policy.planner_observation(
        observation,
        user_text="click Code in Claude",
    )

    assert (
        policy.inspect_observation(
            observation,
            user_text="click Code in Claude",
        ).disposition
        == DesktopSafetyDisposition.BLOCK
    )
    assert automation_id not in planner_view.accessibility_text
    assert all(element.automation_id is None for element in planner_view.elements)


@pytest.mark.parametrize("label", ["Sign in with Google", "Account verification", "Login"])
def test_embedded_authentication_controls_are_blocked(label: str) -> None:
    observation = _observation(
        f'2 name="{label}" control_type="Button"',
        elements=(DesktopElement("2", label, "Button"),),
    )
    policy = DesktopSafetyPolicy()
    task = f"click {label} in Claude"

    assert policy.inspect_observation(observation, user_text=task).disposition == (
        DesktopSafetyDisposition.BLOCK
    )
    assert (
        policy.evaluate(
            _action(DesktopActionType.CLICK, index="2"),
            observation,
            user_text=task,
        ).disposition
        == DesktopSafetyDisposition.BLOCK
    )


@pytest.mark.parametrize(
    "label",
    [
        "Continue with Google",
        "Continue with Apple",
        "Use another account",
        "Sign up",
        "Create account",
        "Register",
    ],
)
def test_oauth_registration_and_account_selection_controls_are_blocked(label: str) -> None:
    observation = _observation(
        f'2 name="{label}" control_type="Button"',
        elements=(DesktopElement("2", label, "Button"),),
    )
    policy = DesktopSafetyPolicy()
    task = f"click {label} in Claude and show Home"

    assert policy.inspect_observation(observation, user_text=task).disposition == (
        DesktopSafetyDisposition.BLOCK
    )
    assert (
        policy.evaluate(
            _action(DesktopActionType.CLICK, index="2"),
            observation,
            user_text=task,
            expectation=DesktopExpectation(
                DesktopExpectationKind.TEXT_PRESENT,
                text="Home",
            ),
        ).disposition
        == DesktopSafetyDisposition.BLOCK
    )


@pytest.mark.parametrize(
    "task",
    [
        "click Continue to create a public link and show Done",
        "click Continue to enable link sharing and show Done",
        "click Continue to open Privacy and show Done",
    ],
)
def test_explicit_privacy_or_public_link_intent_is_blocked_even_on_plain_surface(task) -> None:
    observation = _observation(
        '2 name="Continue" control_type="Button"',
        elements=(DesktopElement("2", "Continue", "Button"),),
    )

    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Done",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    "sensitive_sibling",
    [
        "Pay",
        "Payment",
        "Make payment",
        "Complete payment",
        "Confirm payment",
        "Apple Pay",
        "Google Pay",
        "Card number",
        "Card details",
        "CVC",
        "CVV",
        "Expiry date",
        "Withdraw",
        "Authorization required",
        "Authorize",
        "Connect account",
        "Verify account",
        "Confirm your email",
        "Grant permission",
        "Reference chat history",
        "Saved memories",
        "Conversation history",
        "Opt out of training",
        "Do not train on my data",
        "Export data",
        "Download your data",
        "Protected View",
        "Enable Editing",
        "Enable Content",
        "Macros have been disabled",
        "Your connection is not private",
        "Proceed anyway",
        "This file may harm your computer",
        "Keep anyway",
        "Dangerous file",
        "Virus & threat protection",
        "Allow on device",
        "Certificate warning",
    ],
)
def test_strong_sensitive_surface_labels_block_an_innocuous_continue_action(
    sensitive_sibling: str,
) -> None:
    observation = _observation(
        elements=(
            DesktopElement("2", "Continue", "Button"),
            DesktopElement("3", sensitive_sibling, "Text"),
        ),
    )
    policy = DesktopSafetyPolicy()
    task = "In Claude, click Continue to show Home"

    assert policy.inspect_observation(observation, user_text=task).disposition == (
        DesktopSafetyDisposition.BLOCK
    )
    assert (
        policy.evaluate(
            _action(DesktopActionType.CLICK, index="2"),
            observation,
            user_text=task,
            expectation=DesktopExpectation(
                DesktopExpectationKind.TEXT_PRESENT,
                text="Home",
            ),
        ).disposition
        == DesktopSafetyDisposition.BLOCK
    )


def test_click_focus_is_not_a_navigation_postcondition() -> None:
    observation = _observation(
        '2 name="Code" control_type="Button" focused=false',
        elements=(DesktopElement("2", "Code", "Button", focused=False),),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text="click Code in Claude",
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            text="Code",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


def test_close_click_requires_confirmation_before_side_effect() -> None:
    observation = _observation(
        '2 name="Close" control_type="Button"',
        elements=(DesktopElement("2", "Close", "Button"),),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text="In Claude, click Close and show Dashboard",
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Dashboard",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.CONFIRM


def test_escape_requires_named_target_and_confirmation() -> None:
    observation = _observation(
        '2 name="Unsaved draft" control_type="Document" focused=true',
        elements=(DesktopElement("2", "Unsaved draft", "Document", focused=True),),
    )
    action = _action(DesktopActionType.PRESS_KEY, index="2", key="escape")
    expectation = DesktopExpectation(DesktopExpectationKind.TEXT_PRESENT, text="Dashboard")
    policy = DesktopSafetyPolicy()

    unnamed = policy.evaluate(
        action,
        observation,
        user_text="In Claude, show Dashboard",
        expectation=expectation,
    )
    named = policy.evaluate(
        action,
        observation,
        user_text="press Escape on Unsaved draft and show Dashboard",
        expectation=expectation,
    )

    assert unnamed.disposition == DesktopSafetyDisposition.BLOCK
    assert named.disposition == DesktopSafetyDisposition.CONFIRM


def test_enter_on_editable_target_always_confirms_and_space_is_blocked() -> None:
    observation = _observation(
        '2 name="Prompt" control_type="Edit" focused=true',
        elements=(DesktopElement("2", "Prompt", "Edit", focused=True),),
    )
    expectation = DesktopExpectation(DesktopExpectationKind.TEXT_PRESENT, text="Response")
    policy = DesktopSafetyPolicy()

    enter = policy.evaluate(
        _action(DesktopActionType.PRESS_KEY, index="2", key="enter"),
        observation,
        user_text="In Claude, press enter on Prompt and show Response",
        expectation=expectation,
    )
    space = policy.evaluate(
        _action(DesktopActionType.PRESS_KEY, index="2", key="space"),
        observation,
        user_text="In Claude, press space on Prompt and show Response",
        expectation=expectation,
    )

    assert enter.disposition == DesktopSafetyDisposition.CONFIRM
    assert space.disposition == DesktopSafetyDisposition.BLOCK


def test_model_invented_and_negated_text_are_blocked_but_positive_exact_text_confirms() -> None:
    observation = _observation(
        '2 name="Prompt" focused=true password=false enabled=true',
        elements=(DesktopElement("2", "Prompt", "Edit", focused=True),),
    )
    action = _action(DesktopActionType.TYPE_TEXT, index="2", text="PRIVATE_SAMPLE")
    expectation = DesktopExpectation(
        DesktopExpectationKind.FOCUSED_CONTAINS,
        text="PRIVATE_SAMPLE",
    )

    invented = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="打开 Claude",
        expectation=expectation,
    )
    negated = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="do not type PRIVATE_SAMPLE into Prompt",
        expectation=expectation,
    )
    positive = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="type PRIVATE_SAMPLE into Prompt",
        expectation=expectation,
    )

    assert invented.disposition == DesktopSafetyDisposition.BLOCK
    assert negated.disposition == DesktopSafetyDisposition.BLOCK
    assert positive.disposition == DesktopSafetyDisposition.CONFIRM
    assert positive.confirmation is not None
    assert "PRIVATE_SAMPLE" in positive.confirmation.summary
    assert 'user-spoken-target="Prompt"' in positive.confirmation.summary


def test_prompt_injected_click_target_is_not_authorized_by_unrelated_task() -> None:
    observation = _observation(
        '7 name="Enable telemetry" control_type="Button"',
        elements=(DesktopElement("7", "Enable telemetry", "Button"),),
    )
    action = _action(DesktopActionType.CLICK, index="7")

    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="Open Claude Chat",
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


def test_enter_on_unquoted_focused_target_is_blocked() -> None:
    observation = _observation(
        '7 name="Enable sync" control_type="Button" focused=true',
        elements=(DesktopElement("7", "Enable sync", "Button", focused=True),),
    )
    action = _action(DesktopActionType.PRESS_KEY, index="7", key="enter")

    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="Open Claude Chat",
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


def test_negated_click_target_is_not_authorized() -> None:
    observation = _observation(
        '7 name="Enable sync" control_type="Button"',
        elements=(DesktopElement("7", "Enable sync", "Button"),),
    )
    action = _action(DesktopActionType.CLICK, index="7")

    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="In Claude, do not click Enable sync; open Chat",
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_ABSENT,
            text="Enable sync",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


def test_click_target_disappearance_cannot_prove_navigation_success() -> None:
    observation = _observation(
        '7 name="Chat" control_type="Button"',
        elements=(DesktopElement("7", "Chat", "Button"),),
    )
    action = _action(DesktopActionType.CLICK, index="7")

    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="click Chat in Claude",
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_ABSENT,
            text="Chat",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


def test_arrow_key_on_named_control_requires_confirmation() -> None:
    observation = _observation(
        '7 name="Visibility" control_type="ComboBox" focused=true',
        elements=(DesktopElement("7", "Visibility", "ComboBox", focused=True),),
    )
    action = _action(DesktopActionType.PRESS_KEY, index="7", key="down")

    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="In Claude, press down on Visibility to select Private",
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            text="Private",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.CONFIRM


def test_short_target_label_needs_a_real_user_token_not_a_substring() -> None:
    observation = _observation(
        '7 name="a" control_type="Button"',
        elements=(DesktopElement("7", "a", "Button"),),
    )
    action = _action(DesktopActionType.CLICK, index="7")

    unrelated = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="Open Claude Chat",
    )
    explicit = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text="click button a in Claude",
    )

    assert unrelated.disposition == DesktopSafetyDisposition.BLOCK
    assert explicit.disposition == DesktopSafetyDisposition.ALLOW


@pytest.mark.parametrize(
    "task",
    [
        "if Home is visible, click Open in Claude",
        "when Home is visible, click Open in Claude",
        "unless Home is visible, click Open in Claude",
        "once Home is visible, click Open in Claude",
        "as long as Home is visible, click Open in Claude",
        "in case Home is visible, click Open in Claude",
        "如果 Home 可见，点击 Open",
        "当 Home 可见，点击 Open",
        "除非 Home 可见，点击 Open",
        "一旦 Home 可见，点击 Open",
    ],
)
def test_unsupported_conditions_never_become_unconditional_click_authority(task) -> None:
    observation = _observation(
        elements=(DesktopElement("2", "Open", "Button"),),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            text="Open",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    ("task", "blocked_label", "allowed_label"),
    [
        ("click Home next to Open", "Home", None),
        ("点击 Home 旁边的 Open", "Home", None),
        ("select Project's Settings", "Project", "Settings"),
        ("选择 Project 的 Settings", "Project", "Settings"),
    ],
)
def test_reference_anchors_are_not_click_targets(task, blocked_label, allowed_label) -> None:
    labels = ("Home", "Open") if "Home" in task else ("Project", "Settings")
    observation = _observation(
        "\n".join(
            f'{index} name="{label}" control_type="Button"'
            for index, label in enumerate(labels, start=2)
        ),
        elements=tuple(
            DesktopElement(str(index), label, "Button")
            for index, label in enumerate(labels, start=2)
        ),
    )
    policy = DesktopSafetyPolicy()
    blocked = policy.evaluate(
        _action(DesktopActionType.CLICK, index=str(labels.index(blocked_label) + 2)),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            text=blocked_label,
        ),
    )

    assert blocked.disposition == DesktopSafetyDisposition.BLOCK
    if allowed_label is not None:
        allowed = policy.evaluate(
            _action(DesktopActionType.CLICK, index=str(labels.index(allowed_label) + 2)),
            observation,
            user_text=task,
            expectation=DesktopExpectation(
                DesktopExpectationKind.ELEMENT_SELECTED,
                text=allowed_label,
            ),
        )
        assert allowed.disposition == DesktopSafetyDisposition.ALLOW


@pytest.mark.parametrize(
    ("task", "long_label", "short_label"),
    [
        ('click "Ask Claude"', "Ask Claude", "Claude"),
        ('click "Open settings"', "Open settings", "Settings"),
        ("click Open settings", "Open settings", "Open"),
    ],
)
def test_exact_longest_ui_label_wins_over_visible_substrings(task, long_label, short_label) -> None:
    observation = _observation(
        elements=(
            DesktopElement("2", long_label, "Button"),
            DesktopElement("3", short_label, "Button"),
        ),
    )
    policy = DesktopSafetyPolicy()
    planner_view = policy.planner_observation(observation, user_text=task)
    short = policy.evaluate(
        _action(DesktopActionType.CLICK, index="3"),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            text=short_label,
        ),
    )

    assert [element.name for element in planner_view.elements] == [long_label]
    assert short.disposition == DesktopSafetyDisposition.BLOCK


def test_typed_state_words_do_not_authorize_an_unrelated_postcondition() -> None:
    observation = _observation(
        elements=(
            DesktopElement("2", "Safe", "Button"),
            DesktopElement("3", "Prompt", "Edit", focused=True),
        ),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text="click Safe; type show Secret into Prompt",
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Secret",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    ("task", "short_label"),
    [
        ("click Open settings", "Open"),
        ("click Project Alpha", "Project"),
        ("click Ask Claude", "Ask"),
        ("click the Open settings button", "Open"),
    ],
)
def test_user_authored_long_target_cannot_collapse_to_sole_short_element(
    task,
    short_label,
) -> None:
    observation = _observation(
        f'2 name="{short_label}" control_type="Button"',
        elements=(DesktopElement("2", short_label, "Button"),),
    )
    policy = DesktopSafetyPolicy()

    planner_view = policy.planner_observation(observation, user_text=task)
    result = policy.evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text=task,
    )

    assert planner_view.elements == ()
    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    ("task", "label"),
    [
        ("open File in Claude", "File"),
        ("click Send in Claude", "Send"),
        ("delete Item in Claude", "Item"),
    ],
)
def test_same_target_selection_cannot_fake_activation_or_side_effect_success(
    task,
    label,
) -> None:
    observation = _observation(
        f'2 name="{label}" control_type="Button"',
        elements=(DesktopElement("2", label, "Button"),),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            text=label,
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


def test_explicit_selection_may_use_same_target_selection_evidence() -> None:
    observation = _observation(
        '2 name="File" control_type="TabItem"',
        elements=(DesktopElement("2", "File", "TabItem"),),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text="In Claude, select File",
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            text="File",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.ALLOW


@pytest.mark.parametrize(
    "task",
    [
        "scroll Panel up to make Down visible",
        "scroll Panel up and ensure Down is visible",
        "scroll Panel up with the result that Down is visible",
        "scroll Panel up as a result Down is visible",
        "scroll Panel up thereby making Down visible",
    ],
)
def test_scroll_outcome_words_cannot_supply_an_opposite_direction(task) -> None:
    observation = _observation(
        '2 name="Panel" control_type="Pane"\n3 name="Down" control_type="Text"',
        elements=(
            DesktopElement("2", "Panel", "Pane"),
            DesktopElement("3", "Down", "Text"),
        ),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.SCROLL, index="2", direction="down"),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Down",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    "task",
    [
        "should Home appear, click Open in Claude",
        "in the event that Home appears, click Open in Claude",
        "on condition that Home appears, click Open in Claude",
        "depending on whether Home appears, click Open in Claude",
        "given that Home is visible, click Open in Claude",
        "where Home is visible, click Open in Claude",
        "click Open in Claude on the proviso that Home is visible",
        "click Open in Claude subject to Home being visible",
        "click Open in Claude contingent on Home being visible",
        "suppose Home is visible, click Open in Claude",
        "assume Home is visible, click Open in Claude",
        "insofar as Home is visible, click Open in Claude",
        "click Open in Claude depending upon Home being visible",
        "so long as Home is visible, click Open in Claude",
        "lest Home disappear, click Open in Claude",
        "presuming Home is visible, click Open in Claude",
        "at such time as Home is visible, click Open in Claude",
        "要是 Home 出现，点击 Open",
        "假设 Home 出现，点击 Open",
        "待 Home 出现，点击 Open",
        "Home 可见的话，点击 Open",
        "在 Home 可见的情况下，点击 Open",
        "在 Home 可见的前提下，点击 Open",
        "以 Home 可见为条件，点击 Open",
        "以 Home 可见为前提，点击 Open",
        "视 Home 是否可见，点击 Open",
        "假定 Home 可见，点击 Open",
    ],
)
def test_additional_condition_forms_fail_closed(task) -> None:
    observation = _observation(
        '2 name="Open" control_type="Button"',
        elements=(DesktopElement("2", "Open", "Button"),),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text=task,
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    ("task", "action"),
    [
        ("press Enter on Option and show Done", _action(index="2")),
        (
            "press Enter on Option and show Done",
            _action(DesktopActionType.PRESS_KEY, index="2", key="space"),
        ),
        (
            "click Option and show Done",
            _action(
                DesktopActionType.PERFORM_SECONDARY_ACTION,
                index="2",
                action_name="select",
            ),
        ),
    ],
)
def test_action_kind_key_and_secondary_name_are_exactly_bound(task, action) -> None:
    observation = _observation(
        '2 name="Option" control_type="Button" focused=true',
        elements=(DesktopElement("2", "Option", "Button", focused=True),),
    )
    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Done",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    ("task", "expected"),
    [
        ("click Go to show Home settings", "Home"),
        ("press Tab on Current to focus Next button", "Next"),
    ],
)
def test_postcondition_cannot_shrink_a_user_authored_result_label(task, expected) -> None:
    target = "Go" if task.startswith("click") else "Current"
    action = (
        _action(DesktopActionType.CLICK, index="2")
        if task.startswith("click")
        else _action(DesktopActionType.PRESS_KEY, index="2", key="tab")
    )
    expectation_kind = (
        DesktopExpectationKind.TEXT_PRESENT
        if task.startswith("click")
        else DesktopExpectationKind.FOCUSED_CONTAINS
    )
    observation = _observation(
        f'2 name="{target}" control_type="Button" focused=true',
        elements=(DesktopElement("2", target, "Button", focused=True),),
    )
    result = DesktopSafetyPolicy().evaluate(
        action,
        observation,
        user_text=task,
        expectation=DesktopExpectation(expectation_kind, text=expected),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    "task",
    [
        "click Go to show Home in Settings in Claude",
        "click Go to show Home inside Settings",
        "click Go to show Home next to Settings",
        "click Go to show Home below Settings",
        "click Go to show Home on the right",
        "click Go to show Home selected",
        "click Go to show Home visible in sidebar",
        "click Go to show Home and Settings",
    ],
)
def test_one_text_expectation_cannot_drop_result_qualifiers_or_conjuncts(task) -> None:
    observation = _observation(
        '2 name="Go" control_type="Button"',
        elements=(DesktopElement("2", "Go", "Button"),),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.CLICK, index="2"),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Home",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


def test_focus_expectation_cannot_drop_a_result_qualifier() -> None:
    observation = _observation(
        '2 name="Current" control_type="Button" focused=true',
        elements=(DesktopElement("2", "Current", "Button", focused=True),),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.PRESS_KEY, index="2", key="tab"),
        observation,
        user_text="press Tab on Current to focus Next inside Settings",
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            text="Next",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize("action_type", [DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE])
@pytest.mark.parametrize("partial_payload", ["hello", "world", "ello wor"])
def test_text_action_requires_the_complete_spoken_payload(action_type, partial_payload) -> None:
    observation = _observation(
        '2 name="Prompt" control_type="Edit" focused=true',
        elements=(DesktopElement("2", "Prompt", "Edit", focused=True),),
    )
    kwargs = (
        {"text": partial_payload}
        if action_type == DesktopActionType.TYPE_TEXT
        else {"value": partial_payload}
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(action_type, index="2", **kwargs),
        observation,
        user_text="type hello world into Prompt",
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            text=partial_payload,
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    ("action_type", "task", "payload_field"),
    [
        (DesktopActionType.SET_VALUE, "type hello into Prompt", {"value": "hello"}),
        (DesktopActionType.TYPE_TEXT, "fill hello into Prompt", {"text": "hello"}),
    ],
)
def test_text_action_kind_must_match_the_user_authored_verb(
    action_type, task, payload_field
) -> None:
    observation = _observation(
        '2 name="Prompt" control_type="Edit" focused=true',
        elements=(DesktopElement("2", "Prompt", "Edit", focused=True),),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(action_type, index="2", **payload_field),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            text="hello",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    ("action_type", "task", "payload_field"),
    [
        (DesktopActionType.TYPE_TEXT, "type hello into Prompt", {"text": "hello"}),
        (DesktopActionType.SET_VALUE, "fill hello into Prompt", {"value": "hello"}),
    ],
)
def test_text_action_kind_accepts_its_exact_user_authored_verb(
    action_type, task, payload_field
) -> None:
    observation = _observation(
        '2 name="Prompt" control_type="Edit" focused=true',
        elements=(DesktopElement("2", "Prompt", "Edit", focused=True),),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(action_type, index="2", **payload_field),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            text="hello",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.CONFIRM


def test_text_payload_cannot_replace_a_separate_user_authored_outcome() -> None:
    observation = _observation(
        '2 name="Prompt" control_type="Edit" focused=true',
        elements=(DesktopElement("2", "Prompt", "Edit", focused=True),),
    )
    action = _action(DesktopActionType.TYPE_TEXT, index="2", text="hello")
    policy = DesktopSafetyPolicy()

    substituted = policy.evaluate(
        action,
        observation,
        user_text="type hello into Prompt so Draft saved appears",
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="hello",
        ),
    )
    authored = policy.evaluate(
        action,
        observation,
        user_text="type hello into Prompt so Draft saved appears",
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text="Draft saved",
        ),
    )

    assert substituted.disposition == DesktopSafetyDisposition.BLOCK
    assert authored.disposition == DesktopSafetyDisposition.CONFIRM


def test_first_text_step_cannot_borrow_a_later_text_steps_payload() -> None:
    observation = _observation(
        '2 name="Prompt" control_type="Edit" focused=true\n'
        '3 name="Other" control_type="Edit" focused=false',
        elements=(
            DesktopElement("2", "Prompt", "Edit", focused=True),
            DesktopElement("3", "Other", "Edit", focused=False),
        ),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.TYPE_TEXT, index="2", text="beta"),
        observation,
        user_text="In Prompt type alpha plus in Other type beta",
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            text="beta",
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    ("task", "payload"),
    [
        ("type hello into Prompt; expect Draft saved", "hello"),
        ("type hello into Prompt. outcome Draft saved", "hello"),
        ("type hello into Prompt, result Draft saved", "hello"),
        ("type hello into Prompt; I expect Draft saved", "hello"),
        ("type hello into Prompt; we expect Draft saved", "hello"),
        ("type hello into Prompt; the expected result is Draft saved", "hello"),
        ("type hello into Prompt; desired result is Draft saved", "hello"),
        ("type hello into Prompt; success means Draft saved", "hello"),
        ("type hello into Prompt; postcondition is Draft saved", "hello"),
        ("type hello into Prompt; afterwards Draft saved appears", "hello"),
        ("type hello into Prompt; Draft saved is expected", "hello"),
        ("在 Prompt 输入你好；预期为草稿完成", "你好"),
        ("在 Prompt 输入你好；结果是草稿完成", "你好"),
        ("在 Prompt 输入你好；希望看到草稿完成", "你好"),
        ("在 Prompt 输入你好；成功标志是草稿完成", "你好"),
        ("type hello into Prompt; do not send or expect Draft saved", "hello"),
        ("type hello into Prompt; do not fail or expect Draft saved", "hello"),
        ("type hello into Prompt; do not send / expected result Draft saved", "hello"),
        ("type hello into Prompt; without sending expect Draft saved", "hello"),
        ("在 Prompt 输入你好；不要发送或预期草稿完成", "你好"),
        ("在 Prompt 输入你好；不要失败或结果应为草稿完成", "你好"),
    ],
)
def test_declared_result_clause_cannot_be_replaced_by_payload_presence(task, payload) -> None:
    observation = _observation(
        '2 name="Prompt" control_type="Edit" focused=true',
        elements=(DesktopElement("2", "Prompt", "Edit", focused=True),),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.TYPE_TEXT, index="2", text=payload),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            text=payload,
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.BLOCK


@pytest.mark.parametrize(
    ("task", "payload"),
    [
        ("type hello into Prompt; do not send", "hello"),
        ("type hello into Prompt; do not click Send", "hello"),
        ("type hello into Prompt; without sending", "hello"),
        ("在 Prompt 输入你好；不要发送", "你好"),
        ("在 Prompt 输入你好；不要点击发送", "你好"),
        ("type hello into Prompt; please", "hello"),
        ("在 Prompt 输入你好；请", "你好"),
    ],
)
def test_pure_negative_or_polite_text_tail_keeps_payload_verification(task, payload) -> None:
    observation = _observation(
        '2 name="Prompt" control_type="Edit" focused=true',
        elements=(DesktopElement("2", "Prompt", "Edit", focused=True),),
    )
    result = DesktopSafetyPolicy().evaluate(
        _action(DesktopActionType.TYPE_TEXT, index="2", text=payload),
        observation,
        user_text=task,
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            text=payload,
        ),
    )

    assert result.disposition == DesktopSafetyDisposition.CONFIRM
