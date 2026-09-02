from __future__ import annotations

from dataclasses import replace

import pytest

from handsfree_pc.desktop.assistive.models import Goal, GoalKind, TaskSpec
from handsfree_pc.desktop.assistive.policy import (
    AssistivePolicy,
    ConfirmationKind,
    PolicyDisposition,
)
from handsfree_pc.desktop.protocol import (
    DesktopAction,
    DesktopActionType,
    DesktopElement,
    DesktopElementAction,
    DesktopObservation,
    ElementPlane,
)


class Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _task(*, forbid_submit: bool = False) -> TaskSpec:
    return TaskSpec(
        goals=(Goal(GoalKind.FREE_FORM, "发送这条消息"),),
        forbid_submit=forbid_submit,
        side_effect=True,
        raw_text="输入测试内容，不要发送" if forbid_submit else "发送测试内容",
    )


def _send_observation(
    app: str,
    *,
    generation: int = 3,
    window: str = "window-1",
    identity: str = "a" * 64,
    process_name: str | None = None,
) -> DesktopObservation:
    return DesktopObservation(
        app=app,
        generation=generation,
        accessibility_text='0 name="发送" control_type="Button"',
        window_title=app,
        process_name=process_name,
        local_window_id=window,
        elements=(
            DesktopElement(
                "0",
                "发送",
                "Button",
                local_identity=identity,
                plane=ElementPlane.CONTROL,
                supported_actions=(DesktopElementAction.INVOKE,),
            ),
        ),
    )


def _send_action(observation: DesktopObservation) -> DesktopAction:
    return DesktopAction(
        DesktopActionType.CLICK,
        app=observation.app,
        generation=observation.generation,
        element_index="0",
    )


@pytest.mark.parametrize("app", ["claude", "Claude Desktop", "codex", "ChatGPT"])
def test_ai_assistant_send_policy_defaults_to_auto(app: str) -> None:
    observation = _send_observation(app)

    decision = AssistivePolicy().evaluate(_task(), _send_action(observation), observation)

    assert decision.disposition == PolicyDisposition.AUTO
    assert decision.binding is None


@pytest.mark.parametrize("app", ["wechat", "微信", "mail-client", "unknown-app"])
def test_wechat_and_unknown_send_policy_defaults_to_confirmation(app: str) -> None:
    observation = _send_observation(app)

    decision = AssistivePolicy().evaluate(_task(), _send_action(observation), observation)

    assert decision.disposition == PolicyDisposition.CONFIRM
    assert decision.binding is not None
    assert decision.binding.kind == ConfirmationKind.SEND
    assert decision.binding.phrase == "确认发送"


@pytest.mark.parametrize("app", ["claude", "codex", "wechat", "unknown-app"])
def test_forbid_submit_is_stronger_than_every_send_policy(app: str) -> None:
    observation = _send_observation(app)
    policy = AssistivePolicy(send_policy={app: "auto"})

    decision = policy.evaluate(
        _task(forbid_submit=True),
        _send_action(observation),
        observation,
    )

    assert decision.disposition == PolicyDisposition.BLOCK
    assert decision.binding is None
    assert decision.hard_block is False
    assert "forbid_submit" in decision.reason


@pytest.mark.parametrize("action_type", [DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE])
def test_sensitive_words_inside_an_unsent_draft_remain_automatic(
    action_type: DesktopActionType,
) -> None:
    observation = DesktopObservation(
        app="wechat",
        generation=5,
        accessibility_text='0 name="消息输入框" control_type="Edit"',
        window_title="微信",
        local_window_id="wechat-1",
        elements=(
            DesktopElement(
                "0",
                "消息输入框",
                "Edit",
                focused=True,
                plane=ElementPlane.INPUT,
                local_identity="e" * 64,
            ),
        ),
    )
    draft = "请发送支付说明，删除旧稿里的密码二字；这只是未发送草稿"
    action = DesktopAction(
        action_type,
        app="wechat",
        generation=5,
        element_index="0",
        **({"text": draft} if action_type == DesktopActionType.TYPE_TEXT else {"value": draft}),
    )

    decision = AssistivePolicy().evaluate(_task(forbid_submit=True), action, observation)

    assert decision.disposition == PolicyDisposition.AUTO
    assert decision.binding is None


def test_enter_on_bound_chrome_url_omnibox_is_navigation_not_message_send() -> None:
    observation = DesktopObservation(
        app="chrome-dynamic",
        generation=7,
        accessibility_text='0 name="https://google.com/" control_type="Edit"',
        window_title="about:blank - Google Chrome",
        process_name="chrome.exe",
        local_window_id="hwnd:101",
        elements=(
            DesktopElement(
                "0",
                "https://google.com/",
                "Edit",
                automation_id="view_1012",
                value="https://google.com/",
                value_observed=True,
                focused=True,
                editable=True,
                local_identity="f" * 64,
                browser_chrome=True,
            ),
        ),
    )
    action = DesktopAction(
        DesktopActionType.PRESS_KEY,
        app=observation.app,
        generation=observation.generation,
        element_index="0",
        key="enter",
    )

    decision = AssistivePolicy().evaluate(_task(), action, observation)

    assert decision.disposition == PolicyDisposition.AUTO


@pytest.mark.parametrize(
    ("name", "automation_id"),
    [
        ("Website URL", "profile-website"),
        ("Address and search bar", "view_1012"),
        ("Search query", "page-search"),
        ("回调地址", "callback-address"),
    ],
)
def test_enter_on_any_unproven_page_edit_requires_submission_confirmation(
    name: str,
    automation_id: str,
) -> None:
    observation = DesktopObservation(
        app="chrome-dynamic",
        generation=7,
        accessibility_text=f'0 name="{name}" control_type="Edit"',
        window_title="Profile - Google Chrome",
        process_name="chrome.exe",
        local_window_id="hwnd:101",
        elements=(
            DesktopElement(
                "0",
                name,
                "Edit",
                automation_id=automation_id,
                value="https://example.com/",
                value_observed=True,
                focused=True,
                editable=True,
                local_identity="f" * 64,
            ),
        ),
    )
    action = DesktopAction(
        DesktopActionType.PRESS_KEY,
        app=observation.app,
        generation=observation.generation,
        element_index="0",
        key="enter",
    )

    decision = AssistivePolicy().evaluate(_task(), action, observation)

    assert decision.disposition == PolicyDisposition.CONFIRM


def test_confirmation_requires_the_complete_action_specific_phrase() -> None:
    clock = Clock()
    policy = AssistivePolicy(monotonic=clock)
    observation = _send_observation("wechat")
    action = _send_action(observation)
    requested = policy.evaluate(_task(), action, observation)
    assert requested.binding is not None

    partial = policy.confirm(requested.binding, "确认", action, observation)
    exact = policy.confirm(requested.binding, "确认发送", action, observation)
    punctuated = policy.confirm(requested.binding, "确认发送！", action, observation)

    assert partial.disposition == PolicyDisposition.CONFIRM
    assert partial.binding == requested.binding
    assert exact.disposition == PolicyDisposition.AUTO
    assert punctuated.disposition == PolicyDisposition.AUTO


def test_confirmation_expires_after_fifteen_seconds() -> None:
    clock = Clock()
    policy = AssistivePolicy(confirmation_timeout_seconds=15, monotonic=clock)
    observation = _send_observation("wechat")
    action = _send_action(observation)
    requested = policy.evaluate(_task(), action, observation)
    assert requested.binding is not None

    clock.value += 15.001
    expired = policy.confirm(requested.binding, "确认发送", action, observation)

    assert expired.disposition == PolicyDisposition.BLOCK
    assert "15" in expired.reason


@pytest.mark.parametrize(
    "changed_observation",
    [
        _send_observation("wechat", window="window-2"),
        _send_observation("wechat", generation=4),
        _send_observation("wechat", identity="b" * 64),
    ],
)
def test_confirmation_is_bound_to_exact_window_generation_and_control(
    changed_observation: DesktopObservation,
) -> None:
    clock = Clock()
    policy = AssistivePolicy(monotonic=clock)
    original = _send_observation("wechat")
    action = _send_action(original)
    requested = policy.evaluate(_task(), action, original)
    assert requested.binding is not None
    rebound_action = replace(
        action,
        app=changed_observation.app,
        generation=changed_observation.generation,
    )

    decision = policy.confirm(
        requested.binding,
        "确认发送",
        rebound_action,
        changed_observation,
    )

    assert decision.disposition == PolicyDisposition.BLOCK
    assert "失效" in decision.reason


def test_destructive_confirmation_phrase_is_not_a_generic_challenge() -> None:
    observation = DesktopObservation(
        app="explorer",
        generation=2,
        accessibility_text='0 name="删除" control_type="Button"',
        window_title="文件资源管理器",
        local_window_id="explorer-1",
        elements=(DesktopElement("0", "删除", "Button", local_identity="c" * 64),),
    )
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="explorer",
        generation=2,
        element_index="0",
    )

    decision = AssistivePolicy().evaluate(_task(), action, observation)

    assert decision.disposition == PolicyDisposition.CONFIRM
    assert decision.binding is not None
    assert decision.binding.kind == ConfirmationKind.DELETE
    assert decision.binding.phrase == "确认删除"


def test_password_and_terminal_targets_remain_hard_blocked() -> None:
    password = DesktopObservation(
        app="chrome",
        generation=1,
        accessibility_text='0 name="密码" control_type="Edit"',
        window_title="Sign in",
        local_window_id="login-1",
        elements=(
            DesktopElement(
                "0",
                "密码",
                "Edit",
                password=True,
                focused=True,
                local_identity="d" * 64,
            ),
        ),
    )
    type_password = DesktopAction(
        DesktopActionType.TYPE_TEXT,
        app="chrome",
        generation=1,
        element_index="0",
        text="not-a-real-secret",
    )
    terminal = _send_observation("powershell.exe")

    password_result = AssistivePolicy().evaluate(_task(), type_password, password)
    terminal_result = AssistivePolicy().evaluate(_task(), _send_action(terminal), terminal)

    assert password_result.disposition == PolicyDisposition.BLOCK
    assert terminal_result.disposition == PolicyDisposition.BLOCK
    assert password_result.hard_block is True
    assert terminal_result.hard_block is True


def test_opaque_app_with_pwsh_process_identity_is_hard_blocked() -> None:
    observation = _send_observation("window-42", process_name="pwsh.exe")

    decision = AssistivePolicy().evaluate(_task(), _send_action(observation), observation)

    assert decision.disposition == PolicyDisposition.BLOCK
    assert decision.hard_block is True
    assert "Shell" in decision.reason


def test_embedded_terminal_control_and_opaque_powershell_title_are_hard_blocked() -> None:
    embedded = DesktopObservation(
        app="vscode",
        generation=1,
        accessibility_text='0 name="Terminal" control_type="Edit"',
        window_title="Visual Studio Code",
        process_name="code.exe",
        local_window_id="code-1",
        elements=(
            DesktopElement(
                "0",
                "Terminal",
                "Edit",
                automation_id="workbench.panel.terminal",
                focused=True,
                plane=ElementPlane.INPUT,
                local_identity="9" * 64,
            ),
        ),
    )
    embedded_action = DesktopAction(
        DesktopActionType.TYPE_TEXT,
        app="vscode",
        generation=1,
        element_index="0",
        text="harmless diagnostic",
    )
    titled = DesktopObservation(
        app="opaque-window",
        generation=2,
        accessibility_text='0 name="Prompt" control_type="Edit"',
        window_title="Administrator: Windows PowerShell",
        process_name=None,
        local_window_id="terminal-1",
        elements=(DesktopElement("0", "Prompt", "Edit", local_identity="8" * 64),),
    )
    titled_action = DesktopAction(
        DesktopActionType.TYPE_TEXT,
        app=titled.app,
        generation=titled.generation,
        element_index="0",
        text="harmless diagnostic",
    )

    embedded_result = AssistivePolicy().evaluate(_task(), embedded_action, embedded)
    titled_result = AssistivePolicy().evaluate(_task(), titled_action, titled)

    assert embedded_result.disposition == PolicyDisposition.BLOCK
    assert embedded_result.hard_block is True
    assert titled_result.disposition == PolicyDisposition.BLOCK
    assert titled_result.hard_block is True


@pytest.mark.parametrize(
    "title",
    ["PowerShell", "Command Prompt - project", "Ubuntu", "WSL"],
)
def test_opaque_common_terminal_titles_are_hard_blocked(title: str) -> None:
    observation = DesktopObservation(
        app="opaque-window",
        generation=2,
        accessibility_text='0 name="Prompt" control_type="Edit"',
        window_title=title,
        process_name=None,
        local_window_id="terminal-1",
        elements=(DesktopElement("0", "Prompt", "Edit", local_identity="7" * 64),),
    )
    action = DesktopAction(
        DesktopActionType.TYPE_TEXT,
        app=observation.app,
        generation=observation.generation,
        element_index="0",
        text="harmless diagnostic",
    )

    decision = AssistivePolicy().evaluate(_task(), action, observation)

    assert decision.disposition == PolicyDisposition.BLOCK
    assert decision.hard_block is True


def test_stale_action_binding_is_an_ordinary_rejection_not_a_queue_hard_block() -> None:
    observation = _send_observation("wechat")
    stale = replace(_send_action(observation), generation=observation.generation + 1)

    decision = AssistivePolicy().evaluate(_task(), stale, observation)

    assert decision.disposition == PolicyDisposition.BLOCK
    assert decision.hard_block is False


@pytest.mark.parametrize("process_name", ["ChatGPT.exe", "Claude.exe"])
def test_trusted_ai_process_identity_gets_default_auto_send_policy(process_name: str) -> None:
    observation = _send_observation("opaque-window-id", process_name=process_name)

    decision = AssistivePolicy().evaluate(_task(), _send_action(observation), observation)

    assert decision.disposition == PolicyDisposition.AUTO
    assert decision.binding is None


def test_visual_viewport_is_blocked_when_window_contains_high_credentials() -> None:
    observation = DesktopObservation(
        app="opaque-window-id",
        generation=6,
        accessibility_text="visual fallback",
        screenshot_png=b"frame",
        window_title="ordinary document",
        process_name="ordinary.exe",
        local_window_id="window-visual",
        high_credential_count=1,
        low_credential_count=0,
        credential_affected_element_count=1,
        elements=(
            DesktopElement(
                "0",
                "Visual screenshot viewport",
                "VisualViewport",
                visual_ocr=True,
                local_identity="f" * 64,
                supported_actions=(DesktopElementAction.CLICK,),
            ),
        ),
    )
    action = DesktopAction(
        DesktopActionType.CLICK,
        app=observation.app,
        generation=observation.generation,
        element_index="0",
        x=10,
        y=20,
    )

    decision = AssistivePolicy().evaluate(_task(), action, observation)

    assert decision.disposition == PolicyDisposition.BLOCK
    assert "高置信凭据" in decision.reason


def test_enter_on_bound_chrome_omnibox_search_phrase_is_navigation_too() -> None:
    observation = DesktopObservation(
        app="chrome-dynamic",
        generation=7,
        accessibility_text='0 name="Address and search bar" control_type="Edit"',
        window_title="New Tab - Google Chrome",
        process_name="chrome.exe",
        local_window_id="hwnd:101",
        elements=(
            DesktopElement(
                "0",
                "Address and search bar",
                "Edit",
                automation_id="view_1012",
                value="北京 天气",
                value_observed=True,
                focused=True,
                editable=True,
                local_identity="f" * 64,
                browser_chrome=True,
            ),
        ),
    )
    action = DesktopAction(
        DesktopActionType.PRESS_KEY,
        app=observation.app,
        generation=observation.generation,
        element_index="0",
        key="enter",
    )

    decision = AssistivePolicy().evaluate(_task(), action, observation)

    assert decision.disposition == PolicyDisposition.AUTO
    assert decision.binding is None
