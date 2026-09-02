from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from handsfree_pc.desktop.assistive import controller as controller_module
from handsfree_pc.desktop.assistive.controller import AssistiveController
from handsfree_pc.desktop.assistive.models import (
    ActionOutcome,
    AssistiveDecision,
    AssistiveDecisionKind,
)
from handsfree_pc.desktop.assistive.policy import (
    AssistivePolicy,
    PolicyDecision,
    PolicyDisposition,
)
from handsfree_pc.desktop.assistive.retry import ProgressDetector, WaitResult
from handsfree_pc.desktop.native_skills import NativeRouteStatus, NativeSkillResult
from handsfree_pc.desktop.protocol import (
    ActionReceipt,
    DesktopAction,
    DesktopActionType,
    DesktopElement,
    DesktopElementAction,
    DesktopObservation,
    ElementPlane,
)
from handsfree_pc.desktop.visual_ocr import SensitiveVisualSurfaceError
from handsfree_pc.windows.native import (
    DesktopUnavailableError,
    NativeWindowsError,
    WindowInfo,
)


def _inventory(app: str, *, foreground: bool) -> str:
    return json.dumps(
        [
            {
                "app": app,
                "display_name": app,
                "process_name": f"{app}.exe",
                "window_title": app,
                "foreground": foreground,
            }
        ],
        ensure_ascii=False,
    )


def _composer_observation(
    *,
    generation: int = 1,
    value: str = "",
    focused: bool = True,
    window: str = "window-1",
) -> DesktopObservation:
    return DesktopObservation(
        app="claude",
        generation=generation,
        accessibility_text='0 name="消息输入框" control_type="Edit"',
        window_title="Claude",
        local_window_id=window,
        elements=(
            DesktopElement(
                "0",
                "消息输入框",
                "Edit",
                value=value,
                value_observed=True,
                focused=focused,
                plane=ElementPlane.INPUT,
                local_identity="a" * 64,
            ),
        ),
    )


def _button_observation(*, generation: int, selected: bool) -> DesktopObservation:
    return DesktopObservation(
        app="claude",
        generation=generation,
        accessibility_text=(
            '0 name="左侧第一项" control_type="Button" '
            f'selected={str(selected).lower()}'
        ),
        window_title="Claude",
        local_window_id="window-1",
        elements=(
            DesktopElement(
                "0",
                "左侧第一项",
                "Button",
                selected=selected,
                local_identity="b" * 64,
            ),
        ),
    )


def _send_button_observation() -> DesktopObservation:
    return DesktopObservation(
        app="wechat",
        generation=1,
        accessibility_text='0 name="发送" control_type="Button"',
        window_title="微信",
        process_name="WeChat.exe",
        local_window_id="wechat-1",
        elements=(
            DesktopElement(
                "0",
                "发送",
                "Button",
                local_identity="c" * 64,
            ),
        ),
    )


class Clock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class FakeRouter:
    def __init__(self, result: NativeSkillResult) -> None:
        self.result = result
        self.calls: list[tuple[str, bool]] = []
        self.executor = object()

    def route(
        self,
        instruction: str,
        *,
        explicit_submission: bool = False,
        cancel_event=None,
    ) -> NativeSkillResult:
        self.calls.append((instruction, explicit_submission))
        return self.result


class SequencePlanner:
    def __init__(self, decisions: list[AssistiveDecision]) -> None:
        self.decisions = list(decisions)
        self.calls: list[tuple[str, DesktopObservation | None, tuple[str, ...]]] = []

    def decide(self, task, *, apps, observation, history, cancel_event=None):
        self.calls.append((task, observation, tuple(history)))
        return self.decisions.pop(0)


class SequencePolicy:
    def __init__(self, decisions: list[PolicyDecision]) -> None:
        self.decisions = list(decisions)
        self.calls: list[tuple[DesktopAction, DesktopObservation]] = []

    def evaluate(self, _task, action, observation) -> PolicyDecision:
        self.calls.append((action, observation))
        return self.decisions.pop(0)


class FakeDriver:
    def __init__(
        self,
        inventories: list[str],
        *,
        observations: list[DesktopObservation] | None = None,
    ) -> None:
        self.inventories = list(inventories)
        self.observations = list(observations or [])
        self.calls: list[tuple[object, ...]] = []
        self.actions: list[DesktopAction] = []

    def start(self) -> None:
        self.calls.append(("start",))

    def set_task_context(self, task: str) -> None:
        self.calls.append(("set_task_context", task))

    def list_apps(self, *, cancel_event=None) -> str:
        self.calls.append(("list_apps",))
        if len(self.inventories) > 1:
            return self.inventories.pop(0)
        return self.inventories[0]

    def observe(self, app: str, *, cancel_event=None, capture_screenshot=False):
        self.calls.append(("observe", app, capture_screenshot))
        if len(self.observations) > 1:
            return self.observations.pop(0)
        return self.observations[0]

    def execute(self, action, before, *, cancel_event=None) -> ActionReceipt:
        self.calls.append(("execute", action.type.value, before.generation))
        self.actions.append(action)
        return ActionReceipt(action, True, before.generation, "accepted")

    def cancel(self) -> bool:
        return True

    def close(self) -> None:
        self.calls.append(("close",))


class ActivationNative:
    def __init__(self) -> None:
        self.windows = [
            WindowInfo(900, "Other", 5900, "other.exe", "OtherWindow"),
            WindowInfo(101, "Claude", 5101, "Claude.exe", "ApplicationFrameWindow"),
        ]
        self.foreground_hwnd = 900
        self.activate_calls: list[int] = []
        self.foreground_checks: list[int] = []

    def assert_interactive_desktop(self) -> None:
        return None

    def enumerate_windows(self) -> list[WindowInfo]:
        return list(self.windows)

    def get_foreground_window_info(self) -> WindowInfo | None:
        return next(
            (item for item in self.windows if item.hwnd == self.foreground_hwnd),
            None,
        )

    def activate_window(self, hwnd: int) -> WindowInfo:
        self.activate_calls.append(hwnd)
        selected = next(item for item in self.windows if item.hwnd == hwnd)
        self.foreground_hwnd = hwnd
        return selected

    def assert_foreground(self, hwnd: int) -> None:
        self.foreground_checks.append(hwnd)
        if self.foreground_hwnd != hwnd:
            raise NativeWindowsError("not foreground")


class ActivationDriver(FakeDriver):
    def __init__(self, native: ActivationNative) -> None:
        super().__init__(["unused"], observations=[_composer_observation()])
        self.native = native
        self.bound_windows: list[tuple[str, int]] = []

    def _native_backend(self) -> ActivationNative:
        return self.native

    def list_apps(self, *, cancel_event=None) -> str:
        self.calls.append(("list_apps",))
        entries = []
        for window in self.native.windows:
            app = window.process_name.casefold().removesuffix(".exe")
            entries.append(
                {
                    "app": app,
                    "display_name": app,
                    "process_name": window.process_name,
                    "window_title": window.title,
                    "foreground": window.hwnd == self.native.foreground_hwnd,
                }
            )
        return json.dumps(entries, ensure_ascii=False)

    def observe(self, app: str, *, cancel_event=None, capture_screenshot=False):
        self.calls.append(("observe", app, capture_screenshot))
        if app.casefold() == "claude":
            return _composer_observation()
        return DesktopObservation(
            app=app,
            generation=1,
            accessibility_text="",
            window_title=app,
            local_window_id=f"{app}-window",
        )

    def bind_app_window(self, app: str, hwnd: int) -> None:
        self.bound_windows.append((app, hwnd))


class BrowserNative:
    def __init__(self) -> None:
        self.window = WindowInfo(
            301,
            "Google - Google Chrome",
            5301,
            "chrome.exe",
            "Chrome_WidgetWin_1",
        )
        self.events: list[tuple[str, str | int]] = []

    def assert_interactive_desktop(self) -> None:
        return None

    def get_foreground_window_info(self) -> WindowInfo:
        return self.window

    def assert_foreground(self, hwnd: int) -> None:
        assert hwnd == self.window.hwnd

    def send_hotkey(self, specification: str) -> tuple[int, ...]:
        self.events.append(("hotkey", specification))
        return (1,)

    def send_text(self, text: str) -> int:
        self.events.append(("text", text))
        return len(text)


class BrowserDriver(FakeDriver):
    def __init__(
        self,
        native: BrowserNative,
        observations: list[DesktopObservation],
        *,
        states: list[tuple[str | None, bool | None]] | None = None,
    ) -> None:
        super().__init__([_inventory("chrome", foreground=True)], observations=observations)
        self.native = native
        self.states = list(states or [])
        self.state_reads: list[str] = []

    def _native_backend(self) -> BrowserNative:
        return self.native

    def read_element_state(self, before, element) -> tuple[str | None, bool | None]:
        self.state_reads.append(element.index)
        if len(self.states) > 1:
            return self.states.pop(0)
        if self.states:
            return self.states[0]
        return element.value, element.focused

    @staticmethod
    def assert_browser_address_bound(before, element, *, require_focus=False) -> None:
        assert element in before.elements
        assert element.browser_chrome is True
        if require_focus:
            assert element.focused is True


def _controller(
    *,
    router: FakeRouter,
    driver: FakeDriver,
    planner: SequencePlanner,
    max_steps: int = 20,
) -> AssistiveController:
    return AssistiveController(
        native_router=router,
        driver=driver,
        planner=planner,
        timeout_seconds=30,
        max_steps=max_steps,
    )


def test_goal_already_true_is_a_zero_action_success() -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    planner = SequencePlanner([])
    driver = FakeDriver([_inventory("claude", foreground=True)])
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("切换到 Claude")

    assert result.success
    assert result.error_code == "ASSISTIVE_GOALS_COMPLETED"
    assert router.calls == []
    assert planner.calls == []
    assert driver.actions == []
    assert controller.last_metrics["actions"] == 0


def test_controller_app_match_does_not_trust_a_document_title() -> None:
    entry = {
        "app": "chrome",
        "display_name": "Google Chrome",
        "process_name": "chrome.exe",
        "window_title": "Claude documentation - Google Chrome",
    }

    assert not controller_module._entry_matches(entry, "claude")
    assert controller_module._entry_matches(entry, "chrome")


def test_planner_done_cannot_bypass_the_goal_verifier() -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    done = AssistiveDecision(AssistiveDecisionKind.DONE, "looks done")
    planner = SequencePlanner([done, done])
    driver = FakeDriver([_inventory("claude", foreground=False)])
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("切换到 Claude")

    assert not result.success
    assert result.error_code == "ASSISTIVE_GOALS_NOT_MET"
    assert len(planner.calls) == 2
    assert driver.actions == []


def test_two_consecutive_planner_fail_decisions_terminate_without_an_action() -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    planner = SequencePlanner(
        [
            AssistiveDecision(AssistiveDecisionKind.FAIL, "no useful step"),
            AssistiveDecision(AssistiveDecisionKind.FAIL, "still no useful step"),
        ]
    )
    driver = FakeDriver(
        [_inventory("claude", foreground=True)],
        observations=[_button_observation(generation=1, selected=False)],
    )
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("把左侧第一项展开看看")

    assert not result.success
    assert result.error_code == "ASSISTIVE_NO_PLAN"
    assert len(planner.calls) == 2
    assert driver.actions == []
    assert controller.last_metrics["replans"] == 2


def test_observe_then_screenshot_does_not_spend_action_no_progress_rounds(
    monkeypatch,
) -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    observation = _button_observation(generation=1, selected=False)
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="claude",
        generation=observation.generation,
        element_index="0",
    )
    planner = SequencePlanner(
        [
            AssistiveDecision(
                AssistiveDecisionKind.OBSERVE,
                "inspect semantic controls",
                app="claude",
            ),
            AssistiveDecision(
                AssistiveDecisionKind.SCREENSHOT,
                "inspect pixels once",
                app="claude",
            ),
            AssistiveDecision(
                AssistiveDecisionKind.ACTION,
                "try the requested item",
                app="claude",
                action=action,
            ),
            AssistiveDecision(
                AssistiveDecisionKind.ACTION,
                "replan the requested item",
                app="claude",
                action=action,
            ),
        ]
    )
    driver = FakeDriver(
        [_inventory("claude", foreground=True)],
        observations=[observation],
    )

    def no_effect(_probe, *, before, **_kwargs):
        return WaitResult(ActionOutcome.NO_EFFECT, before, 1, 0.1, False)

    monkeypatch.setattr(controller_module, "wait_for_outcome", no_effect)
    controller = _controller(
        router=router,
        driver=driver,
        planner=planner,
        max_steps=8,
    )

    result = controller.run("把左侧第一项展开看看")

    assert not result.success
    assert result.error_code == "ASSISTIVE_NO_PROGRESS"
    assert len(planner.calls) == 4
    assert driver.actions
    assert controller.last_metrics["actions"] == len(driver.actions)
    assert controller.last_metrics["replans"] == 2
    assert controller.last_metrics["screenshots"] == 1


def test_native_retryable_failure_falls_back_to_assistive_planner() -> None:
    router = FakeRouter(
        NativeSkillResult(NativeRouteStatus.RETRYABLE_FAILURE, "window temporarily unavailable")
    )
    planner = SequencePlanner(
        [AssistiveDecision(AssistiveDecisionKind.OBSERVE, "inspect chrome", app="chrome")]
    )
    driver = FakeDriver(
        [
            _inventory("chrome", foreground=False),
            _inventory("chrome", foreground=False),
            _inventory("chrome", foreground=True),
        ],
        observations=[
            DesktopObservation(
                app="chrome",
                generation=1,
                accessibility_text="",
                window_title="Chrome",
                local_window_id="chrome-1",
            )
        ],
    )
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("切换到 Chrome")

    assert result.success
    assert len(router.calls) == 1
    assert len(planner.calls) == 1
    assert controller.last_metrics["native_fallbacks"] == 1


@pytest.mark.parametrize(
    "native_status",
    [NativeRouteStatus.MISS, NativeRouteStatus.RETRYABLE_FAILURE],
)
def test_native_miss_or_retry_uses_exact_app_activation_for_pure_switch(
    native_status: NativeRouteStatus,
) -> None:
    native = ActivationNative()
    router = FakeRouter(NativeSkillResult(native_status, "native did not finish"))
    driver = ActivationDriver(native)
    planner = SequencePlanner([])
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("切换到 Claude")

    assert result.success
    assert result.error_code == "ASSISTIVE_GOALS_COMPLETED"
    assert router.calls == [("切换到 Claude", False)]
    assert native.activate_calls == [101]
    assert native.foreground_checks == [101]
    assert driver.bound_windows == [("claude", 101)]
    assert planner.calls == []
    assert controller.last_metrics["actions"] == 1


def test_app_activation_window_binding_secure_desktop_is_a_hard_stop() -> None:
    class SecureBindDriver(ActivationDriver):
        def bind_app_window(self, app: str, hwnd: int) -> None:
            raise DesktopUnavailableError("secure desktop")

    native = ActivationNative()
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    driver = SecureBindDriver(native)
    controller = _controller(router=router, driver=driver, planner=SequencePlanner([]))

    result = controller.run("切换到 Claude")

    assert not result.success
    assert result.error_code == "ASSISTIVE_HARD_BLOCK"
    assert result.stage == "app_activation_skill"


def test_app_activation_keeps_fresh_observation_for_remaining_compound_goal() -> None:
    native = ActivationNative()
    router = FakeRouter(
        NativeSkillResult(NativeRouteStatus.RETRYABLE_FAILURE, "native did not finish")
    )
    driver = ActivationDriver(native)
    planner = SequencePlanner(
        [
            AssistiveDecision(AssistiveDecisionKind.DONE, "compound goal looks done"),
            AssistiveDecision(AssistiveDecisionKind.DONE, "compound goal still looks done"),
        ]
    )
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("切换到 Claude 打开 Chat")

    # The remaining "打开 Chat" goal is free-form, so the planner's done stands
    # once it has seen the freshly activated window.
    assert result.success
    assert result.error_code == "ASSISTIVE_GOALS_COMPLETED"
    assert native.activate_calls == [101]
    assert len(planner.calls) == 1
    first_observation = planner.calls[0][1]
    assert first_observation is not None
    assert first_observation.app == "claude"
    assert first_observation.window_title == "Claude"


def test_chrome_url_uses_deterministic_bound_address_skill_without_planner() -> None:
    url = "https://www.google.com/?handsfreepc-scenario=abc123"

    def address_observation(
        generation: int,
        value: str,
        *,
        focused: bool,
    ) -> DesktopObservation:
        return DesktopObservation(
            app="chrome",
            generation=generation,
            accessibility_text='0 name="Address and search bar" control_type="Edit"',
            window_title="Google - Google Chrome",
            process_name="chrome.exe",
            local_window_id="hwnd:301",
            elements=(
                DesktopElement(
                    "0",
                    "Address and search bar",
                    "Edit",
                    automation_id="address-bar",
                    value=value,
                    value_observed=True,
                    focused=focused,
                    plane=ElementPlane.INPUT,
                    local_identity="d" * 64,
                    browser_chrome=True,
                ),
                # A loaded tab exposes its web document with the committed URL.
                DesktopElement(
                    "1",
                    "Page",
                    "Document",
                    automation_id="RootWebArea",
                    value=value if not focused else "https://example.com/",
                    value_observed=True,
                    plane=ElementPlane.CONTENT,
                    local_identity="e" * 64,
                ),
            ),
        )

    native = BrowserNative()
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    router.settings = SimpleNamespace(
        apps={"chrome": SimpleNamespace(process_names=("chrome.exe",))}
    )
    driver = BrowserDriver(
        native,
        [
            address_observation(1, "https://example.com/", focused=False),
            address_observation(2, url, focused=True),
            address_observation(3, url, focused=False),
        ],
        states=[("https://example.com/", True), (url, True)],
    )
    planner = SequencePlanner([])
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run(f"切换到 Chrome 打开 {url}")

    assert result.success
    assert result.error_code == "ASSISTIVE_GOALS_COMPLETED"
    assert planner.calls == []
    assert native.events == [("hotkey", "ctrl+l"), ("text", url), ("hotkey", "enter")]
    # Keystrokes bypass the driver's execute path; the omnibox state is read
    # back live instead of re-walking the window before each step.
    assert driver.actions == []
    assert controller.last_metrics["actions"] == 3


def test_native_hard_block_never_falls_back_to_planner() -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.BLOCKED, "blocked"))
    planner = SequencePlanner([])
    driver = FakeDriver([_inventory("chrome", foreground=False)])
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("切换到 Chrome")

    assert not result.success
    assert result.error_code == "ASSISTIVE_HARD_BLOCK"
    assert len(router.calls) == 1
    assert planner.calls == []


def test_native_confirmation_falls_back_without_pending_or_replay() -> None:
    router = FakeRouter(
        NativeSkillResult(NativeRouteStatus.CONFIRMATION_REQUIRED, "legacy confirmation")
    )
    planner = SequencePlanner(
        [AssistiveDecision(AssistiveDecisionKind.OBSERVE, "inspect chrome", app="chrome")]
    )
    driver = FakeDriver(
        [
            _inventory("chrome", foreground=False),
            _inventory("chrome", foreground=False),
            _inventory("chrome", foreground=True),
        ],
        observations=[
            DesktopObservation(
                app="chrome",
                generation=1,
                accessibility_text="",
                window_title="Chrome",
                local_window_id="chrome-1",
            )
        ],
    )
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("切换到 Chrome")
    fallback_count = controller.last_metrics["native_fallbacks"]
    replay = controller.confirm("legacy-id", phrase="确认发送")

    assert result.success
    assert controller.pending_confirmation_id is None
    assert not replay.success
    assert replay.error_code == "CONFIRMATION_NOT_FOUND"
    assert router.calls == [("切换到 Chrome", False)]
    assert fallback_count == 1


def test_two_consecutive_zero_progress_rounds_fail_after_one_replan(monkeypatch) -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    observation = _composer_observation()
    action = DesktopAction(
        DesktopActionType.TYPE_TEXT,
        app="claude",
        generation=observation.generation,
        element_index="0",
        text="保留的完整原文",
    )
    step = AssistiveDecision(
        AssistiveDecisionKind.ACTION,
        "type the draft",
        app="claude",
        action=action,
    )
    planner = SequencePlanner([step, step])
    driver = FakeDriver(
        [_inventory("claude", foreground=True)],
        observations=[observation],
    )

    def no_effect(probe, *, before, **_kwargs):
        return WaitResult(ActionOutcome.NO_EFFECT, before, 3, 0.25, False)

    monkeypatch.setattr(controller_module, "wait_for_outcome", no_effect)
    controller = _controller(router=router, driver=driver, planner=planner, max_steps=8)

    result = controller.run("输入保留的完整原文，不要发送")

    assert not result.success
    assert result.error_code == "ASSISTIVE_NO_PROGRESS"
    # The composer draft skill owns the first zero-progress round; the planner
    # gets exactly one replan before the second round fails the task.
    assert len(planner.calls) == 1
    # Each round gets one precisely rebound same-target retry before replanning.
    assert len(driver.actions) == 4
    assert controller.last_metrics["replans"] == 2


def test_same_target_retry_propagates_new_hard_policy_block(monkeypatch) -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    before = _composer_observation(generation=1)
    after = _composer_observation(generation=2)
    action = DesktopAction(
        DesktopActionType.TYPE_TEXT,
        app="claude",
        generation=1,
        element_index="0",
        text="完整原文",
    )
    planner = SequencePlanner(
        [
            AssistiveDecision(
                AssistiveDecisionKind.ACTION,
                "type the draft",
                app="claude",
                action=action,
            )
        ]
    )
    driver = FakeDriver(
        [_inventory("claude", foreground=True)],
        observations=[before, after],
    )
    policy = SequencePolicy(
        [
            PolicyDecision(PolicyDisposition.AUTO, "initially safe"),
            PolicyDecision(
                PolicyDisposition.BLOCK,
                "credential target appeared",
                hard_block=True,
            ),
        ]
    )

    def no_effect(probe, *, before, **_kwargs):
        return WaitResult(ActionOutcome.NO_EFFECT, probe(), 1, 0.1, False)

    monkeypatch.setattr(controller_module, "wait_for_outcome", no_effect)
    controller = AssistiveController(
        native_router=router,
        driver=driver,
        planner=planner,
        policy=policy,
        timeout_seconds=30,
    )

    result = controller.run("输入完整原文，不要发送")

    assert not result.success
    assert result.error_code == "ASSISTIVE_HARD_BLOCK"
    assert result.stage == "policy"
    assert len(policy.calls) == 2
    assert len(driver.actions) == 1


def test_alternate_action_propagates_new_hard_policy_block(monkeypatch) -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    before = _button_observation(generation=1, selected=False)
    after_retry = _button_observation(generation=2, selected=False)
    before_alternate = _button_observation(generation=3, selected=False)
    before_alternate = replace(
        before_alternate,
        elements=(
            replace(
                before_alternate.elements[0],
                supported_actions=(DesktopElementAction.INVOKE,),
            ),
        ),
    )
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="claude",
        generation=1,
        element_index="0",
    )
    planner = SequencePlanner(
        [
            AssistiveDecision(
                AssistiveDecisionKind.ACTION,
                "click the requested item",
                app="claude",
                action=action,
            )
        ]
    )
    driver = FakeDriver(
        [_inventory("claude", foreground=True)],
        observations=[before, after_retry, before_alternate],
    )
    policy = SequencePolicy(
        [
            PolicyDecision(PolicyDisposition.AUTO, "initially safe"),
            PolicyDecision(PolicyDisposition.AUTO, "retry remains safe"),
            PolicyDecision(
                PolicyDisposition.BLOCK,
                "terminal target appeared",
                hard_block=True,
            ),
        ]
    )

    def no_effect(probe, *, before, **_kwargs):
        return WaitResult(ActionOutcome.NO_EFFECT, probe(), 1, 0.1, False)

    monkeypatch.setattr(controller_module, "wait_for_outcome", no_effect)
    controller = AssistiveController(
        native_router=router,
        driver=driver,
        planner=planner,
        policy=policy,
        timeout_seconds=30,
    )

    result = controller.run("把左侧第一项展开看看")

    assert not result.success
    assert result.error_code == "ASSISTIVE_HARD_BLOCK"
    assert result.stage == "policy"
    assert len(policy.calls) == 3
    assert len(driver.actions) == 2


def test_execute_exception_does_not_consume_a_full_zero_progress_round(monkeypatch) -> None:
    class RaiseOnceDriver(FakeDriver):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self.execute_attempts = 0

        def execute(self, action, before, *, cancel_event=None) -> ActionReceipt:
            self.execute_attempts += 1
            if self.execute_attempts == 1:
                raise RuntimeError("transient UIA execute failure")
            return super().execute(action, before, cancel_event=cancel_event)

    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    observation = _composer_observation()
    action = DesktopAction(
        DesktopActionType.TYPE_TEXT,
        app="claude",
        generation=observation.generation,
        element_index="0",
        text="完整原文",
    )
    step = AssistiveDecision(
        AssistiveDecisionKind.ACTION,
        "type the draft",
        app="claude",
        action=action,
    )
    planner = SequencePlanner([step, step])
    driver = RaiseOnceDriver(
        [_inventory("claude", foreground=True)],
        observations=[observation],
    )

    def no_effect(probe, *, before, **_kwargs):
        return WaitResult(ActionOutcome.NO_EFFECT, before, 1, 0.1, False)

    monkeypatch.setattr(controller_module, "wait_for_outcome", no_effect)
    controller = _controller(router=router, driver=driver, planner=planner, max_steps=8)

    result = controller.run("输入完整原文，不要发送")

    assert not result.success
    assert result.error_code == "ASSISTIVE_NO_PROGRESS"
    # The transient raise happens inside the composer draft skill's round and
    # still does not consume it; the planner replans once afterwards.
    assert len(planner.calls) == 1
    assert driver.execute_attempts == 4
    assert len(driver.actions) == 3


def test_free_form_done_is_accepted_once_the_planner_has_seen_the_window() -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    done = AssistiveDecision(AssistiveDecisionKind.DONE, "looks complete")
    planner = SequencePlanner([done, done])
    driver = FakeDriver(
        [_inventory("claude", foreground=True)],
        observations=[_button_observation(generation=1, selected=False)],
    )
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("把左侧第一项展开看看")

    # A free-form goal has no local verifier; once the planner has inspected
    # the foreground window, its completion judgement is the final word.
    assert result.success
    assert result.error_code == "ASSISTIVE_GOALS_COMPLETED"
    assert json.loads(planner.calls[0][0])["goals"][0]["kind"] == "free_form"
    assert len(planner.calls) == 1
    assert driver.actions == []


def test_free_form_done_without_any_observation_still_fails() -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    done = AssistiveDecision(AssistiveDecisionKind.DONE, "looks complete")
    planner = SequencePlanner([done, done])
    driver = FakeDriver(
        [_inventory("claude", foreground=False)],
        observations=[_button_observation(generation=1, selected=False)],
    )
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("把左侧第一项展开看看")

    assert not result.success
    assert result.error_code == "ASSISTIVE_GOALS_NOT_MET"
    assert len(planner.calls) == 2
    assert driver.actions == []


def test_unknown_app_name_becomes_one_free_form_goal_for_the_planner() -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    done = AssistiveDecision(AssistiveDecisionKind.DONE, "the window is open")
    planner = SequencePlanner([done])
    driver = FakeDriver(
        [_inventory("claude", foreground=True)],
        observations=[_button_observation(generation=1, selected=False)],
    )
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("切换到周报生成窗口")

    assert result.success
    task = json.loads(planner.calls[0][0])
    assert [goal["kind"] for goal in task["goals"]] == ["free_form"]
    assert task["goals"][0]["target"] == "切换到周报生成窗口"


def test_chat_name_without_a_palette_driver_is_still_planner_judged() -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    done = AssistiveDecision(AssistiveDecisionKind.DONE, "the chat is open")
    planner = SequencePlanner([done])
    driver = FakeDriver(
        [_inventory("claude", foreground=True)],
        observations=[_button_observation(generation=1, selected=False)],
    )
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("切换到周报生成聊天框")

    assert result.success
    task = json.loads(planner.calls[0][0])
    assert [goal["kind"] for goal in task["goals"]] == ["conversation_selected"]
    assert task["goals"][0]["target"] == "周报生成"


def test_unknown_app_name_matching_a_window_title_stays_an_app_goal() -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    inventory = json.dumps(
        [
            {
                "app": "claude-1",
                "display_name": "claude",
                "process_name": "claude.exe",
                "window_title": "Design",
                "foreground": True,
            }
        ],
        ensure_ascii=False,
    )
    driver = FakeDriver(
        [inventory],
        observations=[_button_observation(generation=1, selected=False)],
    )
    controller = _controller(router=router, driver=driver, planner=SequencePlanner([]))

    result = controller.run("切换到 Design")

    assert result.success
    assert result.error_code == "ASSISTIVE_GOALS_COMPLETED"
    assert driver.actions == []


def test_missing_absolute_path_fails_fast_without_planner() -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    driver = FakeDriver([_inventory("claude", foreground=True)])
    planner = SequencePlanner([])
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("打开路径 C:\\__handsfreepc_missing_folder_for_tests__\\子目录")

    assert not result.success
    assert result.error_code == "ASSISTIVE_PATH_NOT_FOUND"
    assert planner.calls == []
    assert router.calls == []


def test_native_success_completes_free_form_goals_without_planner() -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.SUCCEEDED, "opened"))
    driver = FakeDriver(
        [_inventory("claude", foreground=True)],
        observations=[_button_observation(generation=1, selected=False)],
    )
    planner = SequencePlanner([])
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("打开调查数据库")

    assert result.success
    assert result.error_code == "ASSISTIVE_GOALS_COMPLETED"
    assert planner.calls == []
    assert router.calls[0][0] == "打开调查数据库"


def test_observe_decision_activates_a_background_window_first() -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    inventory = json.dumps(
        [
            {
                "app": "chrome",
                "display_name": "chrome",
                "process_name": "chrome.exe",
                "window_title": "Chrome",
                "foreground": True,
            },
            {
                "app": "claude",
                "display_name": "claude",
                "process_name": "claude.exe",
                "window_title": "Claude",
                "foreground": False,
            },
        ],
        ensure_ascii=False,
    )
    activated = json.dumps(
        [
            {
                "app": "chrome",
                "display_name": "chrome",
                "process_name": "chrome.exe",
                "window_title": "Chrome",
                "foreground": False,
            },
            {
                "app": "claude",
                "display_name": "claude",
                "process_name": "claude.exe",
                "window_title": "Claude",
                "foreground": True,
            },
        ],
        ensure_ascii=False,
    )

    class ActivatingDriver(FakeDriver):
        def __init__(self) -> None:
            super().__init__(
                [inventory, inventory, activated],
                observations=[
                    replace(_button_observation(generation=1, selected=False), app="chrome"),
                    _button_observation(generation=2, selected=True),
                ],
            )
            self.activations: list[str] = []

        def activate_app(self, app: str, *, cancel_event=None) -> int:
            self.activations.append(app)
            return 101

    driver = ActivatingDriver()
    planner = SequencePlanner(
        [
            AssistiveDecision(AssistiveDecisionKind.OBSERVE, "look at Claude", app="claude"),
            AssistiveDecision(AssistiveDecisionKind.DONE, "the requested item is visible"),
        ]
    )
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("看看 Claude 里左侧第一项")

    assert result.success
    assert driver.activations == ["claude"]
    assert ("observe", "claude", False) in driver.calls
    assert controller.last_metrics["actions"] == 1


def test_free_form_arbitrary_bound_action_progress_plus_done_completes(monkeypatch) -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    before = _button_observation(generation=1, selected=False)
    after = _button_observation(generation=2, selected=True)
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="claude",
        generation=1,
        element_index="0",
    )
    planner = SequencePlanner(
        [
            AssistiveDecision(
                AssistiveDecisionKind.ACTION,
                "expand the requested item",
                app="claude",
                action=action,
            ),
            AssistiveDecision(AssistiveDecisionKind.DONE, "requested item is expanded"),
            AssistiveDecision(AssistiveDecisionKind.DONE, "requested item still looks expanded"),
        ]
    )
    driver = FakeDriver(
        [_inventory("claude", foreground=True)],
        observations=[before, after],
    )

    def locally_observed_progress(probe, *, before, **_kwargs):
        after_probe = probe()
        assert ProgressDetector.changed(
            ProgressDetector.signature(before),
            ProgressDetector.signature(after_probe),
        )
        return WaitResult(ActionOutcome.PROGRESS, after_probe, 1, 0.1, True)

    monkeypatch.setattr(controller_module, "wait_for_outcome", locally_observed_progress)
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("把左侧第一项展开看看")

    assert result.success
    assert result.error_code == "ASSISTIVE_GOALS_COMPLETED"
    assert json.loads(planner.calls[0][0])["goals"][0]["kind"] == "free_form"
    assert len(planner.calls) == 2
    assert len(driver.actions) == 1
    assert controller.last_metrics["actions"] == 1


def test_arm_confirmation_restarts_fifteen_seconds_and_punctuation_is_accepted(
    monkeypatch,
) -> None:
    clock = Clock()
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    observation = _send_button_observation()
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="wechat",
        generation=1,
        element_index="0",
    )
    planner = SequencePlanner(
        [
            AssistiveDecision(
                AssistiveDecisionKind.ACTION,
                "send the current draft",
                app="wechat",
                action=action,
            ),
            AssistiveDecision(AssistiveDecisionKind.DONE, "send appears complete"),
            AssistiveDecision(AssistiveDecisionKind.DONE, "send still appears complete"),
        ]
    )
    driver = FakeDriver(
        [_inventory("wechat", foreground=True)],
        observations=[observation],
    )

    def progress(probe, *, before, **_kwargs):
        return WaitResult(ActionOutcome.PROGRESS, before, 1, 0.1, True)

    monkeypatch.setattr(controller_module, "wait_for_outcome", progress)
    controller = AssistiveController(
        native_router=router,
        driver=driver,
        planner=planner,
        policy=AssistivePolicy(monotonic=clock),
        timeout_seconds=30,
    )

    pending = controller.run("把当前消息发送出去")
    assert pending.needs_confirmation
    assert pending.confirmation_id is not None

    clock.value += 14
    assert controller.arm_confirmation(pending.confirmation_id)
    clock.value += 14
    confirmed = controller.confirm(pending.confirmation_id, phrase="确认发送！")

    assert not confirmed.needs_confirmation
    # The confirmed send ran and the planner judged the free-form request done.
    assert confirmed.success
    assert confirmed.error_code == "ASSISTIVE_GOALS_COMPLETED"
    assert len(driver.actions) == 1


def test_armed_confirmation_wait_expires_and_cannot_remain_active() -> None:
    clock = Clock()
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    observation = _send_button_observation()
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="wechat",
        generation=1,
        element_index="0",
    )
    planner = SequencePlanner(
        [
            AssistiveDecision(
                AssistiveDecisionKind.ACTION,
                "send the current draft",
                app="wechat",
                action=action,
            )
        ]
    )
    driver = FakeDriver(
        [_inventory("wechat", foreground=True)],
        observations=[observation],
    )
    controller = AssistiveController(
        native_router=router,
        driver=driver,
        planner=planner,
        policy=AssistivePolicy(monotonic=clock),
        timeout_seconds=30,
    )

    pending = controller.run("把当前消息发送出去")
    assert pending.needs_confirmation
    assert pending.confirmation_id is not None
    assert controller.arm_confirmation(pending.confirmation_id)

    clock.value += 15.001

    assert controller.pending_confirmation_id is None
    expired = controller.confirm(pending.confirmation_id, phrase="确认发送")
    assert not expired.success
    assert expired.error_code == "CONFIRMATION_NOT_FOUND"
    assert driver.actions == []


def test_text_retry_requires_same_focused_target_and_absent_text() -> None:
    original = _composer_observation(generation=1, value="", focused=True)
    action = DesktopAction(
        DesktopActionType.TYPE_TEXT,
        app="claude",
        generation=1,
        element_index="0",
        text="完整原文",
    )
    eligible = _composer_observation(generation=2, value="", focused=True)
    already_present = _composer_observation(generation=2, value="完整原文", focused=True)
    focus_lost = _composer_observation(generation=2, value="", focused=False)
    unreadable = replace(
        eligible,
        elements=(replace(eligible.elements[0], value=None, value_observed=False),),
    )

    retry = AssistiveController._same_target_retry(action, original, eligible)

    assert retry is not None
    assert retry.generation == 2
    assert AssistiveController._same_target_retry(action, original, already_present) is None
    assert AssistiveController._same_target_retry(action, original, focus_lost) is None
    assert AssistiveController._same_target_retry(action, original, unreadable) is None
    assert (
        AssistiveController._same_target_retry(
            action,
            original,
            replace(eligible, local_window_id="window-2"),
        )
        is None
    )


def test_sensitive_visual_surface_is_a_non_fallback_hard_boundary() -> None:
    assert controller_module._is_hard_desktop_boundary(
        SensitiveVisualSurfaceError("credential surface")
    )


def test_composer_draft_skill_focuses_then_types_without_the_planner() -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    driver = FakeDriver(
        [_inventory("claude", foreground=True)],
        observations=[
            # The skill re-observes the foreground app, so an unfocused frame is
            # served until the focus click lands.
            _composer_observation(generation=1, value="", focused=False),
            _composer_observation(generation=2, value="", focused=False),
            _composer_observation(generation=3, value="", focused=True),
            _composer_observation(generation=4, value="这是测试", focused=True),
        ],
    )
    planner = SequencePlanner([])
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("在 Claude 里输入 这是测试 不要发送")

    assert result.success
    assert result.error_code == "ASSISTIVE_GOALS_COMPLETED"
    assert [action.type for action in driver.actions] == [
        DesktopActionType.CLICK,
        DesktopActionType.TYPE_TEXT,
    ]
    assert driver.actions[1].text == "这是测试"
    assert planner.calls == []


def test_composer_draft_skill_types_directly_into_a_focused_composer() -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    driver = FakeDriver(
        [_inventory("claude", foreground=True)],
        observations=[
            _composer_observation(generation=1, value="", focused=True),
            _composer_observation(generation=2, value="你好", focused=True),
        ],
    )
    planner = SequencePlanner([])
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("输入你好，不要发送")

    assert result.success
    assert [action.type for action in driver.actions] == [DesktopActionType.TYPE_TEXT]
    assert planner.calls == []


def test_composer_draft_skill_steps_aside_when_no_single_composer_exists() -> None:
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    driver = FakeDriver(
        [_inventory("claude", foreground=True)],
        observations=[_button_observation(generation=1, selected=False)],
    )
    planner = SequencePlanner([AssistiveDecision(AssistiveDecisionKind.FAIL, "no input here")] * 2)
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("输入你好，不要发送")

    assert not result.success
    assert driver.actions == []
    assert len(planner.calls) == 2


def test_goal_scoped_app_is_activated_before_the_draft_is_typed() -> None:
    native = ActivationNative()
    router = FakeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))

    class DraftDriver(ActivationDriver):
        def __init__(self, native: ActivationNative) -> None:
            super().__init__(native)
            self.claude_observations = [
                _composer_observation(generation=1, value="", focused=True),
                _composer_observation(generation=2, value="这是测试", focused=True),
            ]

        def observe(self, app: str, *, cancel_event=None, capture_screenshot=False):
            self.calls.append(("observe", app, capture_screenshot))
            if app.casefold() == "claude":
                if len(self.claude_observations) > 1:
                    return self.claude_observations.pop(0)
                return self.claude_observations[0]
            return super().observe(app, cancel_event=cancel_event)

    driver = DraftDriver(native)
    planner = SequencePlanner([])
    controller = _controller(router=router, driver=driver, planner=planner)

    result = controller.run("在 Claude 里输入 这是测试 不要发送")

    assert result.success
    assert native.activate_calls == [101]
    assert [action.type for action in driver.actions] == [DesktopActionType.TYPE_TEXT]
    assert planner.calls == []
