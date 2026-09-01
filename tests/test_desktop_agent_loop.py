from __future__ import annotations

import io
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from handsfree_pc.desktop.agent_loop import (
    _MAX_IDENTICAL_VISUAL_POINT_REGIONS,
    _MAX_VISUAL_POINT_CLICKS,
    DesktopAgentLoopController,
    _explicit_step_window_scope,
    _explicitly_named_apps,
    _TaskState,
    _unsupported_explicit_app_scopes,
    _visual_point_click_signature,
)
from handsfree_pc.desktop.native_skills import (
    NativeRouteStatus,
    NativeSkillResult,
    NativeSkillRouter,
)
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
    ElementPlane,
    visual_state_binding_token,
)
from handsfree_pc.desktop.safety import DesktopSafetyPolicy, user_action_step_count
from handsfree_pc.models import Action, ActionType, ExecutionResult, Plan, RiskLevel


class FakeNativeExecutor:
    def __init__(self, results=None):
        self.plans = []
        self.results = results

    def prepare_plan(self, plan):
        return plan

    def execute_plan(self, plan):
        self.plans.append(plan)
        if self.results is not None:
            return list(self.results)
        return [
            ExecutionResult(True, "verified", action=action, evidence={"verified": True})
            for action in plan.actions
        ]


class FakeNativeRouter:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.path_context_clears = 0
        self.executor = FakeNativeExecutor()
        self.safety = type(
            "FakeNativeSafety",
            (),
            {"evaluate": staticmethod(lambda plan, **_kwargs: plan)},
        )()

    def route(self, text):
        self.calls.append(text)
        return self.result

    def clear_path_context(self):
        self.path_context_clears += 1


class FakeDriver:
    def __init__(self, observations):
        self.observations = list(observations)
        self.calls = []
        self.closed = False

    def start(self):
        self.calls.append(("start",))

    def list_apps(self, *, cancel_event=None):
        self.calls.append(("list_apps",))
        return '[{"app":"claude","visible_window_count":1}]'

    def observe(self, app, *, cancel_event=None):
        self.calls.append(("observe", app))
        return self.observations.pop(0)

    def execute(self, action, before, *, cancel_event=None):
        self.calls.append(("execute", action.type.value, before.generation))
        return ActionReceipt(action, True, before.generation, "accepted")

    def cancel(self):
        self.calls.append(("cancel",))
        return True

    def close(self):
        self.closed = True


class InventoryDriver(FakeDriver):
    def __init__(self, observations, inventory):
        super().__init__(observations)
        self.inventory = inventory

    def list_apps(self, *, cancel_event=None):
        self.calls.append(("list_apps",))
        return json.dumps(self.inventory, ensure_ascii=False)


class AppObservationDriver(InventoryDriver):
    def __init__(self, inventory, observations_by_app):
        super().__init__([], inventory)
        self.observations_by_app = {
            app: list(observations) for app, observations in observations_by_app.items()
        }

    def observe(self, app, *, cancel_event=None):
        self.calls.append(("observe", app))
        value = self.observations_by_app[app].pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class ContextRecordingDriver(FakeDriver):
    def __init__(self, observations):
        super().__init__(observations)
        self.task_contexts = []
        self.profiles = {"claude": object(), "codex": object()}

    def set_task_context(self, task):
        self.task_contexts.append(task)


class NativeHwndBindingDriver(FakeDriver):
    def bind_app_window(self, app, hwnd):
        self.calls.append(("bind_app_window", app, hwnd))


class SequencePlanner:
    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.calls = []

    def decide(self, task, *, apps, observation, history, cancel_event=None):
        self.calls.append(
            (
                task,
                None if observation is None else observation.generation,
                tuple(history),
            )
        )
        return self.decisions.pop(0)


class AppsRecordingPlanner(SequencePlanner):
    def __init__(self, decisions):
        super().__init__(decisions)
        self.app_payloads = []

    def decide(self, task, *, apps, observation, history, cancel_event=None):
        self.app_payloads.append(apps)
        return super().decide(
            task,
            apps=apps,
            observation=observation,
            history=history,
            cancel_event=cancel_event,
        )


class TextRecordingDriver(FakeDriver):
    def __init__(self, observations):
        super().__init__(observations)
        self.text_payloads = []

    def execute(self, action, before, *, cancel_event=None):
        if action.type == DesktopActionType.TYPE_TEXT:
            self.text_payloads.append(action.text)
        return super().execute(action, before, cancel_event=cancel_event)


def _miss_router():
    return FakeNativeRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))


def _observation(
    generation,
    text,
    *,
    title="Claude",
    elements=(),
    local_window_id=None,
):
    return DesktopObservation(
        app="claude",
        generation=generation,
        accessibility_text=text,
        window_title=title,
        elements=tuple(elements),
        local_window_id=local_window_id,
    )


_COMPOSER_IDENTITY = "a" * 64


def _app_observation(
    app,
    generation,
    *,
    elements,
    local_window_id,
    title=None,
):
    text = "\n".join(
        f'{element.index} name="{element.name}" '
        f'control_type="{element.control_type}" '
        f'focused={str(bool(element.focused)).lower()} '
        f'value="{element.value or ""}"'
        for element in elements
    )
    return DesktopObservation(
        app=app,
        generation=generation,
        accessibility_text=text,
        window_title=title or app,
        elements=tuple(elements),
        local_window_id=local_window_id,
    )


def _composer_observation(
    generation,
    value="",
    *,
    app="claude",
    focused=True,
    composer=True,
    local_window_id="window-a",
    local_identity=_COMPOSER_IDENTITY,
):
    return _app_observation(
        app,
        generation,
        elements=(
            DesktopElement(
                "0",
                "Message",
                "Edit",
                value=value,
                value_observed=True,
                focused=focused,
                enabled=True,
                addressable=True,
                composer=composer,
                editable=True,
                local_identity=local_identity,
            ),
        ),
        local_window_id=local_window_id,
        title="Claude" if app == "claude" else "Google Chrome",
    )


def _native_dictation_plan(app="claude"):
    return Plan(
        "enter local dictation",
        [
            Action(ActionType.ACTIVATE_APP, app=app),
            Action(ActionType.ENTER_DICTATION, app=app),
        ],
    )


def _native_bound_controller(observations, *, control_prefixes=("电脑操作",)):
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.SUCCEEDED,
            "verified",
            plan=_native_dictation_plan(),
        )
    )
    driver = TextRecordingDriver(observations)
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=driver,
        planner=SequencePlanner([]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
        control_prefixes=control_prefixes,
    )
    result = controller.run("打开 Claude 并开始语音输入")
    assert result.success
    assert controller._dictation_context() is not None
    router.result = NativeSkillResult(NativeRouteStatus.MISS, "miss")
    return controller, driver, router


def _observe_decision():
    return DesktopDecision(
        DesktopDecisionKind.OBSERVE,
        "observe target",
        app="claude",
    )


def _done_decision(kind=DesktopExpectationKind.APP_VISIBLE, text=None):
    return DesktopDecision(
        DesktopDecisionKind.DONE,
        "locally check",
        app="claude",
        expectation=DesktopExpectation(kind, text=text),
    )


def _selection_observation(
    generation,
    *,
    selected=(),
    local_window_id="window-a",
    marker=None,
):
    selected_names = frozenset(selected)
    elements = [
        DesktopElement(index, name, "Button", selected=name in selected_names)
        for index, name in (("0", "Alpha"), ("1", "Beta"))
    ]
    if marker is not None:
        elements.append(DesktopElement("2", marker, "Text"))
    text = "\n".join(
        f'{element.index} name="{element.name}" control_type="Button" '
        f"selected={str(bool(element.selected)).lower()}"
        if element.control_type == "Button"
        else f'{element.index} name="{element.name}" control_type="Text"'
        for element in elements
    )
    return _observation(
        generation,
        text,
        elements=tuple(elements),
        local_window_id=local_window_id,
    )


def _selection_action(name, generation):
    index = "0" if name == "Alpha" else "1"
    expectation = DesktopExpectation(
        DesktopExpectationKind.ELEMENT_SELECTED,
        text=name,
    )
    return DesktopDecision(
        DesktopDecisionKind.ACTION,
        f"select {name}",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=generation,
            element_index=index,
        ),
        expectation=expectation,
    )


def _dynamic_window_entry(app, *, foreground=False):
    return {
        "app": app,
        "display_name": "Claude",
        "foreground": foreground,
        "process_name": "claude.exe",
        "visible_window_count": 1,
        "window_title": "Claude",
    }


def _dynamic_selection_observation(app, generation, *, selected=()):
    selected_names = frozenset(selected)
    elements = tuple(
        DesktopElement(index, name, "Button", selected=name in selected_names)
        for index, name in (("0", "Alpha"), ("1", "Beta"))
    )
    return _app_observation(
        app,
        generation,
        elements=elements,
        local_window_id=f"window-{app}",
    )


def _dynamic_selection_action(app, name, generation):
    index = "0" if name == "Alpha" else "1"
    return DesktopDecision(
        DesktopDecisionKind.ACTION,
        f"select {name}",
        app=app,
        action=DesktopAction(
            DesktopActionType.CLICK,
            app=app,
            generation=generation,
            element_index=index,
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            text=name,
        ),
    )


def _reveal_observation(
    generation,
    marker,
    *,
    target_visible=False,
    target_selected=False,
):
    elements = [DesktopElement("0", "Conversation list", "ScrollBar")]
    if target_visible:
        elements.append(
            DesktopElement("1", "Target", "Button", selected=target_selected)
        )
    elements.append(DesktopElement("9", marker, "Text", addressable=False))
    return _observation(
        generation,
        "\n".join(
            f'{element.index} name="{element.name}" '
            f'control_type="{element.control_type}"'
            for element in elements
        ),
        elements=tuple(elements),
        local_window_id="window-a",
    )


def _scroll_reveal_decision(generation, *, direction="down"):
    return DesktopDecision(
        DesktopDecisionKind.ACTION,
        "scroll once to reveal more of the current window",
        app="claude",
        action=DesktopAction(
            DesktopActionType.SCROLL,
            app="claude",
            generation=generation,
            element_index="0",
            direction=direction,
            pages=1.0,
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.LAST_ACTION_VERIFIED,
        ),
    )


def _target_click_decision(generation):
    return DesktopDecision(
        DesktopDecisionKind.ACTION,
        "click the now-visible target",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=generation,
            element_index="1",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            text="Target",
        ),
    )


def _run_verified_alpha_session(*, profile, later_observations=(), later_decisions=()):
    planner = SequencePlanner(
        [
            _observe_decision(),
            _selection_action("Alpha", 1),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "Alpha"),
            *later_decisions,
        ]
    )
    driver = FakeDriver(
        [
            _selection_observation(1),
            _selection_observation(2),
            _selection_observation(3, selected={"Alpha"}),
            *later_observations,
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy(profile=profile),
    )
    first = controller.run("In Claude, select Alpha")
    assert first.success
    return controller, driver, planner


def test_continuous_dictation_types_two_over_segments_exactly_into_same_composer():
    controller, driver, router = _native_bound_controller(
        [
            _composer_observation(1, ""),
            _composer_observation(2, ""),
            _composer_observation(3, " 第一段原文 "),
            _composer_observation(4, " 第一段原文 "),
            _composer_observation(5, " 第一段原文 ，第二段也保留。"),
        ]
    )

    first = controller.run(" 第一段原文 ")
    second = controller.run("，第二段也保留。")

    assert first.success
    assert second.success
    assert driver.text_payloads == [" 第一段原文 ", "，第二段也保留。"]
    assert router.calls == ["打开 Claude 并开始语音输入"]
    assert [call[1] for call in driver.calls if call[0] == "execute"] == [
        "type_text",
        "type_text",
    ]
    assert controller._dictation_context() is not None


def test_control_prefix_is_stripped_and_never_typed_as_dictation_payload():
    controller, driver, router = _native_bound_controller(
        [_composer_observation(1, "")]
    )
    router.result = NativeSkillResult(
        NativeRouteStatus.SUCCEEDED,
        "verified",
        plan=Plan("local control", []),
    )

    result = controller.run("电脑操作，切换反馈模式")

    assert result.success
    assert router.calls[-1] == "切换反馈模式"
    assert driver.text_payloads == []
    assert controller._dictation_context() is None


def test_direct_dictation_exit_clears_binding_without_typing_or_planning():
    controller, driver, router = _native_bound_controller(
        [_composer_observation(1, "")]
    )
    calls_before = tuple(driver.calls)

    exited = controller.run("结束听写")

    assert exited.success
    assert controller._dictation_context() is None
    assert tuple(driver.calls) == calls_before
    assert router.calls == ["打开 Claude 并开始语音输入"]


@pytest.mark.parametrize(
    "changed_observation",
    [
        _composer_observation(2, "", focused=False),
        _composer_observation(2, "", local_window_id="window-b"),
        _composer_observation(2, "", local_identity="b" * 64),
    ],
    ids=("focus", "window", "identity"),
)
def test_dictation_focus_window_or_identity_change_fails_closed(changed_observation):
    controller, driver, _router = _native_bound_controller(
        [_composer_observation(1, ""), changed_observation]
    )

    result = controller.run("不得误输的文字")

    assert not result.success
    assert result.error_code == "DICTATION_BINDING_CHANGED"
    assert driver.text_payloads == []
    assert controller._dictation_context() is None


def test_cancel_and_close_each_clear_continuous_dictation_binding():
    controller, _driver, _router = _native_bound_controller(
        [_composer_observation(1, "")]
    )

    assert controller.cancel()
    assert controller._dictation_context() is None

    second, driver, _router = _native_bound_controller(
        [_composer_observation(1, "")]
    )
    second.close()
    assert second._dictation_context() is None
    assert driver.closed is True


def test_focused_chrome_search_input_does_not_create_dictation_binding():
    search_observation = _composer_observation(
        1,
        "OpenAI",
        app="chrome",
        composer=False,
        local_window_id="chrome-window",
    )
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.SUCCEEDED,
            "verified",
            plan=Plan(
                "activate Chrome",
                [Action(ActionType.ACTIVATE_APP, app="chrome")],
            ),
        )
    )
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=TextRecordingDriver([search_observation]),
        planner=SequencePlanner([]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("打开 Chrome 搜索 OpenAI")

    assert result.success
    assert controller._dictation_context() is None


def test_standalone_start_dictation_binds_composer_from_previous_verified_window():
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.SUCCEEDED,
            "verified",
            plan=Plan(
                "activate Claude",
                [Action(ActionType.ACTIVATE_APP, app="claude")],
            ),
        )
    )
    driver = TextRecordingDriver(
        [
            _composer_observation(1, ""),
            _composer_observation(2, ""),
            _composer_observation(3, ""),
            _composer_observation(4, "后续原文"),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=driver,
        planner=SequencePlanner([]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )
    assert controller.run("打开 Claude").success
    router.result = NativeSkillResult(NativeRouteStatus.MISS, "miss")

    started = controller.run("开始听写")
    dictated = controller.run("后续原文")

    assert started.success
    assert dictated.success
    assert driver.text_payloads == ["后续原文"]
    assert router.calls == ["打开 Claude"]


def test_local_unrestricted_next_control_gets_previous_fresh_window_and_full_inventory():
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.SUCCEEDED,
            "verified",
            plan=Plan(
                "activate Claude",
                [Action(ActionType.ACTIVATE_APP, app="claude")],
            ),
        )
    )
    inventory = [
        {"app": "claude", "visible_window_count": 1},
        {"app": "chrome", "visible_window_count": 1},
    ]
    driver = InventoryDriver(
        [
            _selection_observation(1),
            _selection_observation(2),
            _selection_observation(3),
            _selection_observation(4, selected={"Beta"}),
            _selection_observation(5, selected={"Beta"}),
        ],
        inventory,
    )
    planner = AppsRecordingPlanner(
        [
            _selection_action("Beta", 2),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "Beta"),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )
    assert controller.run("open Claude").success
    router.result = NativeSkillResult(NativeRouteStatus.MISS, "miss")

    second = controller.run("select Beta")

    assert second.success
    assert planner.calls[0][1] == 2
    assert any(
        "fresh initial planner context" in item for item in planner.calls[0][2]
    )
    assert json.loads(planner.app_payloads[0]) == inventory


def test_native_success_rebinds_the_canonical_app_to_its_verified_exact_hwnd():
    action = Action(ActionType.ACTIVATE_APP, app="claude")
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.SUCCEEDED,
            "verified",
            plan=Plan("activate Claude", [action]),
            execution_results=(
                ExecutionResult(
                    True,
                    "verified",
                    action=action,
                    evidence={"hwnd": 202, "postcondition_verified": True},
                ),
            ),
        )
    )
    driver = NativeHwndBindingDriver(
        [_selection_observation(1, local_window_id="hwnd:202")]
    )
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=driver,
        planner=SequencePlanner([]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("打开 Claude")

    assert result.success
    assert driver.calls[:3] == [
        ("start",),
        ("bind_app_window", "claude", 202),
        ("observe", "claude"),
    ]
    assert controller._trusted_context() == ("claude", "hwnd:202")


def test_failed_native_app_workflow_falls_back_to_generic_uia_and_succeeds():
    failed_plan = Plan(
        "open Claude mode",
        [
            Action(ActionType.ACTIVATE_APP, app="claude"),
            Action(ActionType.OPEN_MODE, app="claude", mode="chat"),
        ],
    )
    router = FakeNativeRouter(
        NativeSkillResult(NativeRouteStatus.FAILED, "ambiguous", plan=failed_plan)
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            _selection_action("Alpha", 1),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "Alpha"),
        ]
    )
    driver = FakeDriver(
        [
            _selection_observation(1),
            _selection_observation(2),
            _selection_observation(3, selected={"Alpha"}),
            _selection_observation(4, selected={"Alpha"}),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, select Alpha")

    assert result.success
    assert [call[1] for call in driver.calls if call[0] == "execute"] == ["click"]


def test_failed_native_path_never_loses_its_obligation_to_generic_ui():
    failed_plan = Plan(
        "open path",
        [Action(ActionType.OPEN_PATH, path=r"G:\deep\file.txt")],
    )
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.FAILED,
            "NATIVE_PREPARE_FAILED: bounded failure",
            plan=failed_plan,
        )
    )
    planner = SequencePlanner([])
    driver = FakeDriver([])
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(r"打开 G:\deep\file.txt")

    assert not result.success
    assert result.error_code == "NATIVE_PREPARE_FAILED"
    assert driver.calls == []
    assert planner.calls == []
    assert router.path_context_clears == 0


def test_dispatched_native_path_failure_never_repeats_through_generic_ui():
    failed_plan = Plan(
        "open path",
        [Action(ActionType.OPEN_PATH, path=r"G:\deep\file.txt")],
    )
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.FAILED,
            "NATIVE_EXECUTION_FAILED: postcondition missing",
            plan=failed_plan,
        )
    )
    driver = FakeDriver([])
    planner = SequencePlanner([])
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(r"打开 G:\deep\file.txt")

    assert not result.success
    assert result.error_code == "NATIVE_EXECUTION_FAILED"
    assert driver.calls == []
    assert planner.calls == []
    assert router.path_context_clears == 0


def test_native_failure_preserves_content_free_failure_code():
    plan = Plan("open path", [Action(ActionType.OPEN_PATH, path=r"G:\deep\file.txt")])
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.FAILED,
            "NATIVE_PATH_BINDING_FAILED: private path detail",
            plan=plan,
        )
    )
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=None,
        planner=None,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(r"打开 G:\deep\file.txt")

    assert not result.success
    assert result.error_code == "NATIVE_PATH_BINDING_FAILED"
    assert "private path detail" not in result.safe_message


def test_unrestricted_observe_quarantines_one_bad_dynamic_window_then_uses_another():
    bad_app = "claude-1111111111111111"
    good_app = "claude-2222222222222222"
    inventory = [
        _dynamic_window_entry(bad_app, foreground=True),
        _dynamic_window_entry(good_app),
    ]
    planner = AppsRecordingPlanner(
        [
            DesktopDecision(DesktopDecisionKind.OBSERVE, "first", app=bad_app),
            DesktopDecision(DesktopDecisionKind.OBSERVE, "second", app=good_app),
            _dynamic_selection_action(good_app, "Alpha", 1),
            DesktopDecision(
                DesktopDecisionKind.DONE,
                "verified",
                app=good_app,
                expectation=DesktopExpectation(
                    DesktopExpectationKind.ELEMENT_SELECTED,
                    "Alpha",
                ),
            ),
        ]
    )
    driver = AppObservationDriver(
        inventory,
        {
            bad_app: [RuntimeError("private attachment detail")],
            good_app: [
                _dynamic_selection_observation(good_app, 1),
                _dynamic_selection_observation(good_app, 2),
                _dynamic_selection_observation(good_app, 3, selected={"Alpha"}),
                _dynamic_selection_observation(good_app, 4, selected={"Alpha"}),
            ],
        },
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("在 Claude 里选择 Alpha")

    assert result.success
    assert [call for call in driver.calls if call == ("observe", bad_app)] == [
        ("observe", bad_app)
    ]
    assert bad_app in {item["app"] for item in json.loads(planner.app_payloads[0])}
    assert bad_app not in {
        item["app"] for item in json.loads(planner.app_payloads[1])
    }
    assert good_app in {
        item["app"] for item in json.loads(planner.app_payloads[1])
    }
    second_history = " ".join(planner.calls[1][2])
    assert "window candidate was unavailable" in second_history
    assert bad_app not in second_history
    assert "private attachment detail" not in second_history


def test_unrestricted_observe_never_retries_a_quarantined_dynamic_window():
    bad_app = "claude-3333333333333333"
    other_app = "claude-4444444444444444"
    planner = AppsRecordingPlanner(
        [
            DesktopDecision(DesktopDecisionKind.OBSERVE, "first", app=bad_app),
            DesktopDecision(DesktopDecisionKind.OBSERVE, "repeat", app=bad_app),
        ]
    )
    driver = AppObservationDriver(
        [_dynamic_window_entry(bad_app), _dynamic_window_entry(other_app)],
        {
            bad_app: [RuntimeError("private first failure")],
            other_app: [_dynamic_selection_observation(other_app, 1)],
        },
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("在 Claude 里选择 Alpha")

    assert not result.success
    assert result.stage == "observe_driver"
    assert result.error_code == "OBSERVE_DRIVER_FAILED"
    assert [call for call in driver.calls if call == ("observe", bad_app)] == [
        ("observe", bad_app)
    ]
    assert ("observe", other_app) not in driver.calls
    assert bad_app not in " ".join(planner.calls[1][2])


def test_unrestricted_observe_stops_after_three_distinct_bad_dynamic_windows():
    apps = [f"claude-{digit * 16}" for digit in ("5", "6", "7")]
    planner = SequencePlanner(
        [
            DesktopDecision(DesktopDecisionKind.OBSERVE, "candidate", app=app)
            for app in apps
        ]
    )
    driver = AppObservationDriver(
        [_dynamic_window_entry(app) for app in apps],
        {app: [RuntimeError(f"private failure {index}")] for index, app in enumerate(apps)},
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("在 Claude 里选择 Alpha")

    assert not result.success
    assert result.stage == "observe_driver"
    assert result.error_code == "OBSERVE_DRIVER_FAILED"
    assert len(planner.calls) == 3
    assert [call for call in driver.calls if call[0] == "observe"] == [
        ("observe", app) for app in apps
    ]


def test_unrestricted_observe_failure_after_action_never_switches_windows():
    good_app = "claude-8888888888888888"
    bad_app = "claude-9999999999999999"
    planner = SequencePlanner(
        [
            DesktopDecision(DesktopDecisionKind.OBSERVE, "good", app=good_app),
            _dynamic_selection_action(good_app, "Alpha", 1),
            DesktopDecision(DesktopDecisionKind.OBSERVE, "bad", app=bad_app),
            DesktopDecision(DesktopDecisionKind.OBSERVE, "switch", app=good_app),
        ]
    )
    driver = AppObservationDriver(
        [_dynamic_window_entry(good_app), _dynamic_window_entry(bad_app)],
        {
            good_app: [
                _dynamic_selection_observation(good_app, 1),
                _dynamic_selection_observation(good_app, 2),
                _dynamic_selection_observation(good_app, 3, selected={"Alpha"}),
            ],
            bad_app: [RuntimeError("private post-action attachment detail")],
        },
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("在 Claude 里选择 Alpha")

    assert not result.success
    assert result.stage == "plan"
    assert result.error_code == "OBSERVE_AFTER_USER_STEPS_COMPLETE"
    assert len(planner.calls) == 3
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "click", 2)
    ]
    assert not any(call == ("observe", bad_app) for call in driver.calls)


@pytest.mark.parametrize("spoken", ["ChatGPT", "chat gpt", "聊天GPT"])
def test_chatgpt_spoken_alias_resolves_to_existing_codex_profile(spoken):
    task = f"打开 {spoken}，然后点击 Projects"

    assert _explicitly_named_apps(task, ("codex", "claude")) == frozenset(
        {"codex"}
    )


@pytest.mark.parametrize("spoken", ["ChatGPT", "chat gpt", "聊天GPT"])
def test_dedicated_chatgpt_profile_wins_over_codex_compatibility_alias(spoken):
    task = f"打开 {spoken}，然后点击 Projects"

    assert _explicitly_named_apps(task, ("codex", "chatgpt", "claude")) == frozenset(
        {"chatgpt"}
    )


def test_local_unrestricted_never_requires_an_explicit_app_scope_for_visible_window():
    planner = SequencePlanner([_observe_decision(), _done_decision()])
    driver = FakeDriver([_selection_observation(1), _selection_observation(2)])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("切换到 Claude")

    assert result.success is True
    assert result.error_code is None
    assert ("observe", "claude") in driver.calls


def test_local_unrestricted_cannot_claim_an_arbitrary_task_done_after_only_observing():
    planner = SequencePlanner([_observe_decision(), _done_decision()])
    driver = FakeDriver([_selection_observation(1), _selection_observation(2)])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("继续处理当前界面")

    assert result.success is False
    assert result.error_code == "NO_POSITIVE_USER_ACTION"
    assert not any(call[0] == "observe" for call in driver.calls)


def test_local_unrestricted_accepts_verified_intermediate_navigation_not_named_by_user():
    events = []

    class Diagnostics:
        def event(self, **kwargs):
            events.append(kwargs)

    destination = DesktopExpectation(DesktopExpectationKind.TEXT_PRESENT, "最终页面")

    def terminal(generation: int, *, selected: bool) -> DesktopObservation:
        target = DesktopElement("2", "最终页面", "Button", selected=selected)
        return _observation(
            generation,
            (
                '2 name="最终页面" control_type="Button" '
                f"selected={str(selected).lower()}"
            ),
            elements=(target,),
            local_window_id="window-a",
        )
    planner = SequencePlanner(
        [
            _observe_decision(),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "open one necessary intermediate control",
                app="claude",
                action=DesktopAction(
                    DesktopActionType.CLICK,
                    app="claude",
                    generation=1,
                    element_index="0",
                ),
                expectation=destination,
            ),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "activate the exact revealed user target",
                app="claude",
                action=DesktopAction(
                    DesktopActionType.CLICK,
                    app="claude",
                    generation=3,
                    element_index="2",
                ),
                expectation=DesktopExpectation(
                    DesktopExpectationKind.ELEMENT_SELECTED,
                    "最终页面",
                ),
            ),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "最终页面"),
        ]
    )
    driver = FakeDriver(
        [
            _selection_observation(1),
            _selection_observation(2),
            terminal(3, selected=False),
            terminal(4, selected=False),
            terminal(5, selected=True),
            terminal(6, selected=True),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
        diagnostics=Diagnostics(),
    )

    result = controller.run("打开最终页面")

    assert result.success is True
    assert any(call[0] == "execute" for call in driver.calls)
    codes = [event["error_code"] for event in events]
    assert "APP_SCOPE_RESOLVED" in codes
    assert "PLANNER_CALL_STARTED" in codes
    assert "PLANNER_CALL_FINISHED" in codes
    assert "PLANNER_DECISION_OBSERVE" in codes
    assert "PLANNER_DECISION_ACTION" in codes
    assert "VISUAL_VIEWPORT_UNAVAILABLE" in codes
    assert "ACTION_DISPATCHED" in codes
    assert "ACTION_VERIFIED" in codes


def test_local_unrestricted_reveals_target_with_two_scrolls_then_clicks_it():
    planner = SequencePlanner(
        [
            _observe_decision(),
            _scroll_reveal_decision(1),
            _scroll_reveal_decision(3),
            _target_click_decision(5),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "Target"),
        ]
    )
    driver = FakeDriver(
        [
            _reveal_observation(1, "page-zero"),
            _reveal_observation(2, "page-zero"),
            _reveal_observation(3, "page-one"),
            _reveal_observation(4, "page-one"),
            _reveal_observation(5, "page-two", target_visible=True),
            _reveal_observation(6, "page-two", target_visible=True),
            _reveal_observation(
                7,
                "target-selected",
                target_visible=True,
                target_selected=True,
            ),
            _reveal_observation(
                8,
                "target-selected",
                target_visible=True,
                target_selected=True,
            ),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, click Target")

    assert result.success is True
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "scroll", 2),
        ("execute", "scroll", 4),
        ("execute", "click", 6),
    ]
    assert any(
        "did not complete a user step" in item for item in planner.calls[2][2]
    )
    assert any(
        "did not complete a user step" in item for item in planner.calls[3][2]
    )


def test_local_unrestricted_stops_a_reveal_scroll_that_makes_no_progress():
    planner = SequencePlanner([_observe_decision(), _scroll_reveal_decision(1)])
    driver = FakeDriver(
        [
            _reveal_observation(1, "same-page"),
            _reveal_observation(2, "same-page"),
            _reveal_observation(3, "same-page"),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, click Target")

    assert result.success is False
    assert result.error_code == "INSTRUMENTAL_REVEAL_NO_PROGRESS"
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "scroll", 2)
    ]


def test_local_unrestricted_stops_a_reveal_fingerprint_toggle_loop():
    planner = SequencePlanner(
        [
            _observe_decision(),
            _scroll_reveal_decision(1),
            _scroll_reveal_decision(3),
        ]
    )
    driver = FakeDriver(
        [
            _reveal_observation(1, "page-a"),
            _reveal_observation(2, "page-a"),
            _reveal_observation(3, "page-b"),
            _reveal_observation(4, "page-b"),
            _reveal_observation(5, "page-a"),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, click Target")

    assert result.success is False
    assert result.error_code == "INSTRUMENTAL_REVEAL_LOOP_DETECTED"
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "scroll", 2),
        ("execute", "scroll", 4),
    ]


def test_local_unrestricted_bounds_repeating_the_same_reveal_action():
    planner = SequencePlanner(
        [
            _observe_decision(),
            *[
                _scroll_reveal_decision(1 + index * 2)
                for index in range(5)
            ],
        ]
    )
    observations = [_reveal_observation(1, "page-0")]
    for index in range(4):
        observations.extend(
            [
                _reveal_observation(2 + index * 2, f"page-{index}"),
                _reveal_observation(3 + index * 2, f"page-{index + 1}"),
            ]
        )
    observations.append(_reveal_observation(10, "page-4"))
    driver = FakeDriver(observations)
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, click Target")

    assert result.success is False
    assert result.error_code == "INSTRUMENTAL_REVEAL_REPEAT_LIMIT_REACHED"
    assert len([call for call in driver.calls if call[0] == "execute"]) == 4


def test_local_unrestricted_bounds_total_reveal_actions_across_signatures():
    directions = ("down", "up", "left", "right") * 3
    planner = SequencePlanner(
        [
            _observe_decision(),
            *[
                _scroll_reveal_decision(
                    1 + index * 2,
                    direction=directions[index],
                )
                for index in range(9)
            ],
        ]
    )
    observations = [_reveal_observation(1, "page-0")]
    for index in range(8):
        observations.extend(
            [
                _reveal_observation(2 + index * 2, f"page-{index}"),
                _reveal_observation(3 + index * 2, f"page-{index + 1}"),
            ]
        )
    observations.append(_reveal_observation(18, "page-8"))
    driver = FakeDriver(observations)
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, click Target")

    assert result.success is False
    assert result.error_code == "INSTRUMENTAL_REVEAL_LIMIT_REACHED"
    assert len([call for call in driver.calls if call[0] == "execute"]) == 8


def test_last_action_verified_cannot_be_used_as_done_condition():
    planner = SequencePlanner(
        [
            _observe_decision(),
            _done_decision(DesktopExpectationKind.LAST_ACTION_VERIFIED),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=FakeDriver([_reveal_observation(1, "page-zero")]),
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, click Target")

    assert result.success is False
    assert result.error_code == "INSTRUMENTAL_REVEAL_CANNOT_COMPLETE"
    assert not any(call[0] == "execute" for call in controller.driver.calls)


def _loop_visual_observation(
    generation,
    marker,
    *,
    app="claude",
    local_window_id="window-a",
):
    viewport = DesktopElement(
        "2",
        "Visual screenshot viewport",
        "VisualViewport",
        plane=ElementPlane.CONTROL,
        visual_ocr=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )
    return DesktopObservation(
        app=app,
        generation=generation,
        accessibility_text=(
            '2 name="Visual screenshot viewport" '
            f'control_type="VisualViewport" visual-frame={marker}'
        ),
        screenshot_png=b"visual-loop:" + marker.encode(),
        window_title=app,
        elements=(viewport,),
        local_window_id=local_window_id,
    )


def _loop_visual_click_decision(generation, x, y, *, app="claude"):
    return DesktopDecision(
        DesktopDecisionKind.ACTION,
        "click one frame-bound rendered point",
        app=app,
        action=DesktopAction(
            DesktopActionType.CLICK,
            app=app,
            generation=generation,
            element_index="2",
            x=x,
            y=y,
            click_count=1,
            mouse_button="left",
        ),
        expectation=DesktopExpectation(DesktopExpectationKind.LAST_ACTION_VERIFIED),
    )


def _run_visual_click_limit(coordinates):
    successful_clicks = len(coordinates) - 1
    decisions = [_observe_decision()]
    decisions.extend(
        _loop_visual_click_decision(1 + index * 2, x, y)
        for index, (x, y) in enumerate(coordinates)
    )
    observations = [_loop_visual_observation(1, "frame-0")]
    for index in range(successful_clicks):
        observations.extend(
            (
                _loop_visual_observation(2 + index * 2, f"frame-{index}"),
                _loop_visual_observation(3 + index * 2, f"frame-{index + 1}"),
            )
        )
    observations.append(
        _loop_visual_observation(
            2 + successful_clicks * 2,
            f"frame-{successful_clicks}",
        )
    )
    driver = FakeDriver(observations)
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner(decisions),
        safety=DesktopSafetyPolicy("local_unrestricted"),
        max_steps=len(decisions) + 1,
    )
    result = controller.run("In Claude, open the rendered Target conversation")
    return result, driver


def test_visual_point_click_stops_after_repeating_one_64px_region_limit():
    coordinates = [
        (65 + offset, 65 + offset)
        for offset in range(_MAX_IDENTICAL_VISUAL_POINT_REGIONS + 1)
    ]

    result, driver = _run_visual_click_limit(coordinates)

    assert result.success is False
    assert result.error_code == "VISUAL_POINT_REGION_REPEAT_LIMIT_REACHED"
    assert len([call for call in driver.calls if call[0] == "execute"]) == (
        _MAX_IDENTICAL_VISUAL_POINT_REGIONS
    )
    assert not driver.observations


def test_visual_point_click_stops_at_total_limit_across_distinct_regions():
    coordinates = [
        (32 + 64 * index, 32)
        for index in range(_MAX_VISUAL_POINT_CLICKS + 1)
    ]

    result, driver = _run_visual_click_limit(coordinates)

    assert result.success is False
    assert result.error_code == "VISUAL_POINT_CLICK_LIMIT_REACHED"
    assert len([call for call in driver.calls if call[0] == "execute"]) == (
        _MAX_VISUAL_POINT_CLICKS
    )
    assert not driver.observations


def _visual_click_task_state(observation):
    return _TaskState(
        task="In Claude, open the rendered Target conversation",
        apps='[{"app":"claude","visible_window_count":1}]',
        allowed_apps=frozenset({"claude"}),
        observation=observation,
        history=[],
        last_verification=None,
        last_action_expectation=None,
        last_action=None,
        last_action_target=None,
        steps=1,
        verified_action_count=0,
        verified_user_step_count=0,
        remaining_seconds=30.0,
    )


def test_visual_point_click_counts_only_after_successful_local_verification():
    initial = _loop_visual_observation(1, "before")
    decision = _loop_visual_click_decision(1, 20, 30)

    failed_state = _visual_click_task_state(initial)
    failed_driver = FakeDriver(
        [
            _loop_visual_observation(2, "before"),
            _loop_visual_observation(3, "before"),
        ]
    )
    failed_controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=failed_driver,
        planner=SequencePlanner([]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    failed = failed_controller._perform_action(
        failed_state,
        decision.action,
        expectation=decision.expectation,
        counts_as_user_step=False,
        cancel_event=None,
    )

    assert failed is not None
    assert failed.error_code == "ACTION_NOT_VERIFIED"
    assert failed_state.visual_point_click_count == 0
    assert failed_state.visual_point_region_counts == {}

    successful_state = _visual_click_task_state(initial)
    successful_driver = FakeDriver(
        [
            _loop_visual_observation(2, "before"),
            _loop_visual_observation(3, "after"),
        ]
    )
    successful_controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=successful_driver,
        planner=SequencePlanner([]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    succeeded = successful_controller._perform_action(
        successful_state,
        decision.action,
        expectation=decision.expectation,
        counts_as_user_step=False,
        cancel_event=None,
    )

    assert succeeded is None
    assert successful_state.visual_point_click_count == 1
    assert sum(successful_state.visual_point_region_counts.values()) == 1


def test_visual_point_region_signature_separates_app_and_exact_window():
    base_observation = _loop_visual_observation(1, "base")
    same_region_action = _loop_visual_click_decision(1, 127, 127).action
    base_action = _loop_visual_click_decision(1, 64, 64).action
    same_signature = _visual_point_click_signature(base_action, base_observation)

    assert same_signature == _visual_point_click_signature(
        same_region_action,
        base_observation,
    )
    assert same_signature != _visual_point_click_signature(
        base_action,
        _loop_visual_observation(
            1,
            "base",
            local_window_id="window-b",
        ),
    )
    assert same_signature != _visual_point_click_signature(
        _loop_visual_click_decision(1, 64, 64, app="wechat").action,
        _loop_visual_observation(
            1,
            "base",
            app="wechat",
            local_window_id="window-a",
        ),
    )


def _visual_png(*, target: tuple[int, int, int], distant: tuple[int, int, int]) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (256, 160), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((92, 60, 132, 100), fill=target)
    draw.rectangle((205, 15, 225, 35), fill=distant)
    payload = io.BytesIO()
    image.save(payload, format="PNG")
    return payload.getvalue()


def _visual_patch_observation(
    generation: int,
    screenshot_png: bytes,
    *,
    text: str = "stable visual viewport",
) -> DesktopObservation:
    viewport = DesktopElement(
        "2",
        "Visual screenshot viewport",
        "VisualViewport",
        plane=ElementPlane.CONTROL,
        visual_ocr=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )
    return DesktopObservation(
        app="claude",
        generation=generation,
        accessibility_text=(
            '2 name="Visual screenshot viewport" control_type="VisualViewport" '
            + text
        ),
        screenshot_png=screenshot_png,
        window_title="Claude",
        elements=(viewport,),
        local_window_id="window-a",
    )


def _visual_bound_input_observation(
    generation: int,
    screenshot_png: bytes,
    capability: DesktopElementAction,
) -> DesktopObservation:
    viewport = DesktopElement(
        "2",
        "Visual screenshot viewport",
        "VisualViewport",
        plane=ElementPlane.CONTROL,
        visual_ocr=True,
        supported_actions=(DesktopElementAction.CLICK, capability),
    )
    return DesktopObservation(
        app="claude",
        generation=generation,
        accessibility_text=(
            '2 name="Visual screenshot viewport" control_type="VisualViewport" '
            "stable-armed-input"
        ),
        screenshot_png=screenshot_png,
        window_title="Claude",
        elements=(viewport,),
        local_window_id="window-a",
    )


def test_visual_point_click_replans_when_the_planned_local_patch_changed() -> None:
    planned = _visual_patch_observation(
        1,
        _visual_png(target=(20, 80, 180), distant=(10, 120, 10)),
    )
    fresh = _visual_patch_observation(
        2,
        _visual_png(target=(210, 30, 30), distant=(10, 120, 10)),
    )
    assert planned.fingerprint == fresh.fingerprint
    state = _visual_click_task_state(planned)
    driver = FakeDriver([fresh])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner([]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )
    decision = _loop_visual_click_decision(1, 112, 80)

    result = controller._perform_action(
        state,
        decision.action,
        expectation=decision.expectation,
        counts_as_user_step=False,
        cancel_event=None,
    )

    assert result is None
    assert state.observation is fresh
    assert state.stale_replans == 1
    assert state.verified_action_count == 0
    assert not any(call[0] == "execute" for call in driver.calls)


def test_visual_text_replans_if_the_armed_field_changes_before_dispatch() -> None:
    planned = _visual_bound_input_observation(
        1,
        _visual_png(target=(20, 80, 180), distant=(10, 120, 10)),
        DesktopElementAction.TYPE_TEXT,
    )
    fresh = _visual_bound_input_observation(
        2,
        _visual_png(target=(210, 30, 30), distant=(10, 120, 10)),
        DesktopElementAction.TYPE_TEXT,
    )
    assert planned.fingerprint == fresh.fingerprint
    state = _visual_click_task_state(planned)
    state.task = 'In Claude, type "hello" into Message'
    driver = FakeDriver([fresh])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner([]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )
    action = DesktopAction(
        DesktopActionType.TYPE_TEXT,
        app="claude",
        generation=1,
        element_index="2",
        text="hello",
    )

    result = controller._perform_action(
        state,
        action,
        expectation=DesktopExpectation(DesktopExpectationKind.LAST_ACTION_VERIFIED),
        counts_as_user_step=True,
        cancel_event=None,
    )

    assert result is None
    assert state.observation is fresh
    assert state.stale_replans == 1
    assert not any(call[0] == "execute" for call in driver.calls)


def test_visual_enter_replans_when_a_result_appears_before_dispatch() -> None:
    planned = _visual_bound_input_observation(
        1,
        _visual_png(target=(20, 80, 180), distant=(10, 120, 10)),
        DesktopElementAction.PRESS_KEY,
    )
    fresh = _visual_bound_input_observation(
        2,
        _visual_png(target=(20, 80, 180), distant=(220, 80, 10)),
        DesktopElementAction.PRESS_KEY,
    )
    assert planned.fingerprint == fresh.fingerprint
    state = _visual_click_task_state(planned)
    state.task = "In Claude, search for Alpha"
    driver = FakeDriver([fresh])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner([]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )
    action = DesktopAction(
        DesktopActionType.PRESS_KEY,
        app="claude",
        generation=1,
        element_index="2",
        key="enter",
    )

    result = controller._perform_action(
        state,
        action,
        expectation=DesktopExpectation(DesktopExpectationKind.LAST_ACTION_VERIFIED),
        counts_as_user_step=True,
        cancel_event=None,
    )

    assert result is None
    assert state.observation is fresh
    assert state.stale_replans == 1
    assert not any(call[0] == "execute" for call in driver.calls)


def test_visual_point_click_ignores_a_distant_animation_and_executes_once() -> None:
    planned = _visual_patch_observation(
        1,
        _visual_png(target=(20, 80, 180), distant=(10, 120, 10)),
    )
    fresh = _visual_patch_observation(
        2,
        _visual_png(target=(20, 80, 180), distant=(220, 80, 10)),
    )
    after = _visual_patch_observation(
        3,
        _visual_png(target=(20, 80, 180), distant=(20, 20, 220)),
        text="post-action visual viewport",
    )
    assert planned.fingerprint == fresh.fingerprint
    state = _visual_click_task_state(planned)
    driver = FakeDriver([fresh, after])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner([]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )
    decision = _loop_visual_click_decision(1, 112, 80)

    result = controller._perform_action(
        state,
        decision.action,
        expectation=decision.expectation,
        counts_as_user_step=False,
        cancel_event=None,
    )

    assert result is None
    assert state.verified_action_count == 1
    assert state.stale_replans == 0
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "click", 2)
    ]


def test_frame_bound_visual_click_replans_from_fresh_screenshot_and_can_complete():
    def visual_observation(generation, marker):
        viewport = DesktopElement(
            "2",
            "Visual screenshot viewport",
            "VisualViewport",
            plane=ElementPlane.CONTROL,
            visual_ocr=True,
            supported_actions=(DesktopElementAction.CLICK, DesktopElementAction.SCROLL),
            scroll_axes=("vertical",),
        )
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text=(
                '2 name="Visual screenshot viewport" '
                f'control_type="VisualViewport" visual-frame={marker}'
            ),
            screenshot_png=b"\x89PNG\r\n\x1a\n" + marker.encode(),
            window_title="Claude",
            elements=(viewport,),
            local_window_id="window-a",
        )

    action = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "click the rendered target in the exact frame",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="2",
            x=20,
            y=30,
            click_count=1,
            mouse_button="left",
        ),
        expectation=DesktopExpectation(DesktopExpectationKind.LAST_ACTION_VERIFIED),
    )
    initial = visual_observation(1, "before")
    pre_action = visual_observation(2, "before")
    after_action = visual_observation(3, "after")
    confirmation = visual_observation(4, "after-confirmation")
    planner = SequencePlanner(
        [
            _observe_decision(),
            action,
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(after_action),
            ),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(confirmation),
            ),
        ]
    )
    driver = FakeDriver([initial, pre_action, after_action, confirmation])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, open the rendered Target conversation")

    assert result.success is True
    assert len([call for call in driver.calls if call[0] == "execute"]) == 1
    assert not driver.observations


def _visual_input_observation(
    generation: int,
    marker: str,
    supported_actions: tuple[DesktopElementAction, ...],
) -> DesktopObservation:
    viewport = DesktopElement(
        "2",
        "Visual screenshot viewport",
        "VisualViewport",
        plane=ElementPlane.CONTROL,
        visual_ocr=True,
        supported_actions=supported_actions,
    )
    return DesktopObservation(
        app="claude",
        generation=generation,
        accessibility_text=(
            '2 name="Visual screenshot viewport" control_type="VisualViewport" '
            f"state={marker}"
        ),
        screenshot_png=f"visual-input:{marker}".encode(),
        window_title="Claude",
        elements=(viewport,),
        local_window_id="window-a",
    )


def _visual_type_decision(generation: int, payload: str) -> DesktopDecision:
    return DesktopDecision(
        DesktopDecisionKind.ACTION,
        "type the exact user payload into the armed visual field",
        app="claude",
        action=DesktopAction(
            DesktopActionType.TYPE_TEXT,
            app="claude",
            generation=generation,
            element_index="2",
            text=payload,
        ),
        expectation=DesktopExpectation(DesktopExpectationKind.LAST_ACTION_VERIFIED),
    )


def _visual_enter_decision(generation: int) -> DesktopDecision:
    return DesktopDecision(
        DesktopDecisionKind.ACTION,
        "submit the exact armed visual search",
        app="claude",
        action=DesktopAction(
            DesktopActionType.PRESS_KEY,
            app="claude",
            generation=generation,
            element_index="2",
            key="enter",
        ),
        expectation=DesktopExpectation(DesktopExpectationKind.LAST_ACTION_VERIFIED),
    )


def test_visual_unsent_draft_counts_as_the_current_user_step_without_enter() -> None:
    armed = (DesktopElementAction.CLICK, DesktopElementAction.TYPE_TEXT)
    unarmed = (DesktopElementAction.CLICK,)
    initial = _visual_input_observation(1, "draft-focus", armed)
    pre_type = _visual_input_observation(2, "draft-focus", armed)
    after_type = _visual_input_observation(3, "draft-typed", unarmed)
    confirmation = _visual_input_observation(4, "draft-confirmed", unarmed)
    planner = SequencePlanner(
        [
            _observe_decision(),
            _visual_type_decision(1, "hello"),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(after_type),
            ),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(confirmation),
            ),
        ]
    )
    driver = FakeDriver([initial, pre_type, after_type, confirmation])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run('In Claude, type "hello" into Message but do not send it')

    assert result.success is True
    assert [call[1] for call in driver.calls if call[0] == "execute"] == [
        "type_text"
    ]


def test_one_visual_draft_cannot_receive_credit_for_a_second_text_step() -> None:
    armed = (DesktopElementAction.CLICK, DesktopElementAction.TYPE_TEXT)
    unarmed = (DesktopElementAction.CLICK,)
    initial = _visual_input_observation(1, "first-focus", armed)
    pre_type = _visual_input_observation(2, "first-focus", armed)
    after_type = _visual_input_observation(3, "hello-typed", unarmed)
    confirmation = _visual_input_observation(4, "world-still-missing", unarmed)
    planner = SequencePlanner(
        [
            _observe_decision(),
            _visual_type_decision(1, "hello"),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(after_type),
            ),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(confirmation),
            ),
        ]
    )
    driver = FakeDriver([initial, pre_type, after_type, confirmation])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(
        'In Claude, type "hello" into Message, then type "world" into Prompt'
    )

    assert result.success is False
    assert result.error_code == "USER_STEPS_INCOMPLETE"
    assert [call[1] for call in driver.calls if call[0] == "execute"] == [
        "type_text"
    ]


def test_visual_click_cannot_replace_the_only_requested_text_step() -> None:
    click_only = (DesktopElementAction.CLICK,)
    initial = _visual_input_observation(1, "message-visible", click_only)
    pre_click = _visual_input_observation(2, "message-visible", click_only)
    after_click = _visual_input_observation(3, "message-focused", click_only)
    confirmation = _visual_input_observation(4, "message-still-empty", click_only)
    planner = SequencePlanner(
        [
            _observe_decision(),
            _loop_visual_click_decision(1, 20, 20),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(after_click),
            ),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(confirmation),
            ),
        ]
    )
    driver = FakeDriver([initial, pre_click, after_click, confirmation])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run('In Claude, type "hello" into Message')

    assert result.success is False
    assert result.error_code == "USER_STEPS_INCOMPLETE"
    assert [call[1] for call in driver.calls if call[0] == "execute"] == ["click"]


@pytest.mark.parametrize(
    "task",
    [
        "In Claude, click Message to show Settings",
        "In Claude, click Message which should show Settings",
        "In Claude, click Message should show Settings",
        "在 Claude 点击消息这样就能看到设置",
        "在 Claude 点击消息以便显示设置",
    ],
)
def test_visual_click_cannot_replace_a_distinct_authored_outcome(task: str) -> None:
    click_only = (DesktopElementAction.CLICK,)
    initial = _visual_input_observation(1, "message-visible", click_only)
    pre_click = _visual_input_observation(2, "message-visible", click_only)
    after_click = _visual_input_observation(3, "message-only", click_only)
    confirmation = _visual_input_observation(4, "settings-missing", click_only)
    planner = SequencePlanner(
        [
            _observe_decision(),
            _loop_visual_click_decision(1, 20, 20),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(after_click),
            ),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(confirmation),
            ),
        ]
    )
    driver = FakeDriver([initial, pre_click, after_click, confirmation])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert result.success is False
    assert result.error_code == "USER_STEPS_INCOMPLETE"
    assert [call[1] for call in driver.calls if call[0] == "execute"] == ["click"]


def test_visual_search_and_focus_click_cannot_replace_the_later_text_step() -> None:
    text_armed = (DesktopElementAction.CLICK, DesktopElementAction.TYPE_TEXT)
    enter_armed = (DesktopElementAction.CLICK, DesktopElementAction.PRESS_KEY)
    click_only = (DesktopElementAction.CLICK,)
    initial = _visual_input_observation(1, "search-focus", text_armed)
    pre_type = _visual_input_observation(2, "search-focus", text_armed)
    after_type = _visual_input_observation(3, "search-typed", enter_armed)
    pre_enter = _visual_input_observation(4, "search-typed", enter_armed)
    after_enter = _visual_input_observation(5, "search-results", click_only)
    pre_click = _visual_input_observation(6, "search-results", click_only)
    after_click = _visual_input_observation(7, "message-focused", click_only)
    confirmation = _visual_input_observation(8, "message-still-empty", click_only)
    planner = SequencePlanner(
        [
            _observe_decision(),
            _visual_type_decision(1, "Alpha"),
            _visual_enter_decision(3),
            _loop_visual_click_decision(5, 20, 20),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(after_click),
            ),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(confirmation),
            ),
        ]
    )
    driver = FakeDriver(
        [
            initial,
            pre_type,
            after_type,
            pre_enter,
            after_enter,
            pre_click,
            after_click,
            confirmation,
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(
        'In Claude, search for Alpha, then type "hello" into Message'
    )

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert [call[1] for call in driver.calls if call[0] == "execute"] == [
        "type_text",
    ]


def test_visual_natural_search_rejects_viewport_enter_after_exact_filter() -> None:
    text_armed = (DesktopElementAction.CLICK, DesktopElementAction.TYPE_TEXT)
    enter_armed = (DesktopElementAction.CLICK, DesktopElementAction.PRESS_KEY)
    unarmed = (DesktopElementAction.CLICK,)
    initial = _visual_input_observation(1, "search-focus", text_armed)
    pre_type = _visual_input_observation(2, "search-focus", text_armed)
    after_type = _visual_input_observation(3, "search-typed", enter_armed)
    pre_enter = _visual_input_observation(4, "search-typed", enter_armed)
    after_enter = _visual_input_observation(5, "search-result", unarmed)
    confirmation = _visual_input_observation(6, "search-confirmed", unarmed)
    planner = SequencePlanner(
        [
            _observe_decision(),
            _visual_type_decision(1, "Alpha"),
            _visual_enter_decision(3),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(after_enter),
            ),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(confirmation),
            ),
        ]
    )
    driver = FakeDriver(
        [initial, pre_type, after_type, pre_enter, after_enter, confirmation]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, search for Alpha")

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert [call[1] for call in driver.calls if call[0] == "execute"] == [
        "type_text",
    ]


def test_visual_navigation_search_enter_cannot_complete_open_without_the_result() -> None:
    text_armed = (DesktopElementAction.CLICK, DesktopElementAction.TYPE_TEXT)
    enter_armed = (DesktopElementAction.CLICK, DesktopElementAction.PRESS_KEY)
    results_only = (DesktopElementAction.CLICK,)
    initial = _visual_input_observation(1, "navigation-focus", text_armed)
    pre_type = _visual_input_observation(2, "navigation-focus", text_armed)
    after_type = _visual_input_observation(3, "navigation-query", enter_armed)
    pre_enter = _visual_input_observation(4, "navigation-query", enter_armed)
    after_enter = _visual_input_observation(5, "search-results-only", results_only)
    confirmation = _visual_input_observation(
        6,
        "search-results-only-confirmed",
        results_only,
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            _visual_type_decision(1, "Alpha"),
            _visual_enter_decision(3),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(after_enter),
            ),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(confirmation),
            ),
        ]
    )
    driver = FakeDriver(
        [initial, pre_type, after_type, pre_enter, after_enter, confirmation]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, open Alpha")

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert [call[1] for call in driver.calls if call[0] == "execute"] == [
        "type_text",
    ]


def test_semantic_search_query_can_bridge_to_the_exact_current_open_target() -> None:
    def observation(
        generation: int,
        *,
        query: str,
        include_result: bool,
        selected: bool = False,
    ) -> DesktopObservation:
        search = DesktopElement(
            "0",
            "Search",
            "Edit",
            value=query,
            focused=True,
            editable=True,
            enabled=True,
            addressable=True,
            automation_id="SearchBox",
            supported_actions=(DesktopElementAction.TYPE_TEXT,),
        )
        elements = [search]
        if include_result:
            elements.append(
                DesktopElement(
                    "1",
                    "Alpha",
                    "Button",
                    selected=selected,
                    enabled=True,
                    addressable=True,
                    supported_actions=(DesktopElementAction.CLICK,),
                )
            )
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text=(
                f'0 name="Search" control_type="Edit" focused=true value="{query}"'
                + (
                    f'\n1 name="Alpha" control_type="Button" selected={str(selected).lower()}'
                    if include_result
                    else ""
                )
            ),
            window_title="Claude",
            elements=tuple(elements),
            local_window_id="window-a",
        )

    type_query = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "filter the local result list with the exact current target",
        app="claude",
        action=DesktopAction(
            DesktopActionType.TYPE_TEXT,
            app="claude",
            generation=1,
            element_index="0",
            text="Alpha",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            "Alpha",
        ),
    )
    click_result = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "open the exact semantic result",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=3,
            element_index="1",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            "Alpha",
        ),
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            type_query,
            click_result,
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "Alpha"),
        ]
    )
    driver = FakeDriver(
        [
            observation(1, query="", include_result=False),
            observation(2, query="", include_result=False),
            observation(3, query="Alpha", include_result=True),
            observation(4, query="Alpha", include_result=True),
            observation(5, query="Alpha", include_result=True, selected=True),
            observation(6, query="Alpha", include_result=True, selected=True),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, open Alpha")

    assert result.success is True
    assert [call[1] for call in driver.calls if call[0] == "execute"] == [
        "type_text",
        "click",
    ]


@pytest.mark.parametrize(
    "task",
    [
        "In Claude, open Alpha, but do not type Alpha",
        "In Claude, search Alpha, but do not type Alpha",
        "在 Claude 打开 Alpha，但不要输入 Alpha",
    ],
)
def test_semantic_search_bridge_never_types_an_explicitly_negated_payload(
    task: str,
) -> None:
    search = DesktopElement(
        "0",
        "Search",
        "Edit",
        value="",
        focused=True,
        editable=True,
        enabled=True,
        addressable=True,
        automation_id="SearchBox",
        supported_actions=(DesktopElementAction.TYPE_TEXT,),
    )
    initial = DesktopObservation(
        app="claude",
        generation=1,
        accessibility_text=(
            '0 name="Search" control_type="Edit" focused=true value=""'
        ),
        window_title="Claude",
        elements=(search,),
        local_window_id="window-a",
    )
    type_query = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "a negated payload must not become an inferred search bridge",
        app="claude",
        action=DesktopAction(
            DesktopActionType.TYPE_TEXT,
            app="claude",
            generation=1,
            element_index="0",
            text="Alpha",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            "Alpha",
        ),
    )
    driver = FakeDriver([initial])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner([_observe_decision(), type_query]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert not any(call[0] == "execute" for call in driver.calls)


def test_mixed_search_then_draft_blocks_the_first_viewport_enter_before_dispatch() -> None:
    click_only = (DesktopElementAction.CLICK,)
    text_armed = (DesktopElementAction.CLICK, DesktopElementAction.TYPE_TEXT)
    enter_armed = (DesktopElementAction.CLICK, DesktopElementAction.PRESS_KEY)
    observations = [
        _visual_input_observation(1, "initial", click_only),
        _visual_input_observation(2, "initial", click_only),
        _visual_input_observation(3, "search-focus", text_armed),
        _visual_input_observation(4, "search-focus", text_armed),
        _visual_input_observation(5, "search-typed", enter_armed),
        _visual_input_observation(6, "search-typed", enter_armed),
        _visual_input_observation(7, "search-result", click_only),
        _visual_input_observation(8, "search-result", click_only),
        _visual_input_observation(9, "draft-focus", text_armed),
        _visual_input_observation(10, "draft-focus", text_armed),
        # Even if a malicious or stale driver advertises Enter later, the
        # controller must reject the first viewport Enter before reaching it.
        _visual_input_observation(11, "draft-typed", enter_armed),
    ]
    decisions = [
        _observe_decision(),
        _loop_visual_click_decision(1, 20, 20),
        _visual_type_decision(3, "Alpha"),
        _visual_enter_decision(5),
        _loop_visual_click_decision(7, 20, 20),
        _visual_type_decision(9, "hello"),
        _visual_enter_decision(11),
    ]
    driver = FakeDriver(observations)
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner(decisions),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run('In Claude, search for Alpha, then type "hello" into Message')

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert [call[1] for call in driver.calls if call[0] == "execute"] == [
        "click",
        "type_text",
    ]


def test_task_specific_semantic_click_can_finish_with_visual_review():
    button = DesktopElement(
        "0",
        "Open",
        "Button",
        enabled=True,
        addressable=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )
    viewport = DesktopElement(
        "2",
        "Visual screenshot viewport",
        "VisualViewport",
        plane=ElementPlane.CONTROL,
        visual_ocr=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )

    def observation(
        generation: int,
        screenshot: bytes,
        *,
        terminal: bool,
    ) -> DesktopObservation:
        elements = (
            (viewport, DesktopElement("3", "Target", "Text", addressable=False))
            if terminal
            else (button, viewport)
        )
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text=(
                '2 name="Visual screenshot viewport" control_type="VisualViewport"\n'
                + (
                    '3 name="Target" control_type="Text"'
                    if terminal
                    else '0 name="Open" control_type="Button"'
                )
            ),
            screenshot_png=screenshot,
            window_title="Claude",
            elements=elements,
            local_window_id="window-a",
        )

    initial = observation(1, b"before", terminal=False)
    pre_action = observation(2, b"before", terminal=False)
    after_action = observation(3, b"after", terminal=True)
    confirmation = observation(4, b"after-confirmation", terminal=True)
    action = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "click the exact semantic control",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="0",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            "Target",
        ),
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            action,
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(after_action),
            ),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(confirmation),
            ),
        ]
    )
    driver = FakeDriver([initial, pre_action, after_action, confirmation])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, click Open to show Target")

    assert result.success is True
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "click", 2)
    ]


def test_visual_click_can_upgrade_transition_proof_to_fresh_structured_terminal_state():
    viewport = DesktopElement(
        "2",
        "Visual screenshot viewport",
        "VisualViewport",
        plane=ElementPlane.CONTROL,
        visual_ocr=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )

    def before_observation(generation: int) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text="2 visual viewport",
            screenshot_png=b"before-rendered-target",
            window_title="Claude",
            elements=(viewport,),
            local_window_id="window-a",
        )

    def terminal_observation(generation: int, marker: bytes) -> DesktopObservation:
        selected_target = DesktopElement("0", "Target", "Button", selected=True)
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text=(
                '0 name="Target" control_type="Button" selected=true\n'
                '2 name="Visual screenshot viewport" control_type="VisualViewport"'
            ),
            screenshot_png=marker,
            window_title="Target",
            elements=(selected_target, viewport),
            local_window_id="window-a",
        )

    action = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "click the rendered target in the exact frame",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="2",
            x=20,
            y=30,
        ),
        expectation=DesktopExpectation(DesktopExpectationKind.LAST_ACTION_VERIFIED),
    )
    after = terminal_observation(3, b"after-rendered-target")
    confirmation = terminal_observation(4, b"fresh-terminal-target")
    planner = SequencePlanner(
        [
            _observe_decision(),
            action,
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(after),
            ),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(confirmation),
            ),
        ]
    )
    driver = FakeDriver(
        [
            before_observation(1),
            before_observation(2),
            after,
            confirmation,
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("打开 Target")

    assert result.success is True
    assert len([call for call in driver.calls if call[0] == "execute"]) == 1


@pytest.mark.parametrize(
    ("task", "revealed_label"),
    [
        ("In Claude, open Target", "Target"),
        ("In Claude, click Message to show Settings", "Settings"),
    ],
)
def test_revealing_a_target_label_does_not_prove_that_the_target_was_opened(
    task: str,
    revealed_label: str,
) -> None:
    menu = DesktopElement(
        "0",
        "Menu",
        "Button",
        enabled=True,
        addressable=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )

    def source(generation: int) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text='0 name="Menu" control_type="Button"',
            window_title="Claude",
            elements=(menu,),
            local_window_id="window-a",
        )

    def revealed(generation: int) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text=(
                f'0 name="{revealed_label}" control_type="Text"'
            ),
            window_title="Claude",
            elements=(
                DesktopElement("0", revealed_label, "Text", addressable=False),
            ),
            local_window_id="window-a",
        )

    action = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "reveal the target label without opening it",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="0",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            revealed_label,
        ),
    )
    driver = FakeDriver([source(1), source(2), revealed(3), revealed(4)])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner(
            [
                _observe_decision(),
                action,
                _done_decision(
                    DesktopExpectationKind.TEXT_PRESENT,
                    revealed_label,
                ),
            ]
        ),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert result.success is False
    assert result.error_code == "USER_STEPS_INCOMPLETE"
    assert [call[1] for call in driver.calls if call[0] == "execute"] == ["click"]


def test_related_semantic_window_transition_replans_rendered_destination_before_done():
    button = DesktopElement(
        "0",
        "前往",
        "Button",
        enabled=True,
        addressable=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )
    viewport = DesktopElement(
        "2",
        "Visual screenshot viewport",
        "VisualViewport",
        plane=ElementPlane.CONTROL,
        visual_ocr=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )

    def source(generation: int) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text='0 name="前往" control_type="Button"',
            screenshot_png=b"source-window",
            window_title="Search",
            elements=(button,),
            local_window_id="window-a",
        )

    def destination(generation: int, marker: bytes) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text="2 visual viewport",
            screenshot_png=marker,
            window_title="Claude",
            elements=(viewport,),
            local_window_id="window-b",
        )

    after = destination(3, b"rendered-destination")
    confirmation = destination(4, b"fresh-rendered-destination")
    action = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "open the exact semantic result",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="0",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            "文件传输助手",
        ),
    )

    class RelatedWindowDriver(FakeDriver):
        def execute(self, action, before, *, cancel_event=None):
            self.calls.append(("execute", action.type.value, before.generation))
            return ActionReceipt(
                action,
                True,
                before.generation,
                "accepted related transition",
                after_local_window_id="window-b",
            )

    planner = SequencePlanner(
        [
            _observe_decision(),
            action,
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(after),
            ),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(confirmation),
            ),
        ]
    )
    driver = RelatedWindowDriver([source(1), source(2), after, confirmation])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("打开文件传输助手")

    assert result.success is True
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "click", 2)
    ]


def test_related_result_disappearance_is_a_bridge_to_fresh_child_terminal_state():
    label = "文件传输助手 在手机和电脑之间传输各类文件 前往"
    result_button = DesktopElement(
        "7",
        label,
        "Button",
        enabled=True,
        addressable=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )

    def source(generation: int) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text=f'7 name="{label}" control_type="Button"',
            window_title="Search",
            elements=(result_button,),
            local_window_id="window-a",
        )

    def destination(generation: int) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text='0 name="文件传输助手" control_type="Text"',
            window_title="文件传输助手",
            elements=(DesktopElement("0", "文件传输助手", "Text", addressable=False),),
            local_window_id="window-b",
        )

    class RelatedWindowDriver(FakeDriver):
        def execute(self, action, before, *, cancel_event=None):
            self.calls.append(("execute", action.type.value, before.generation))
            return ActionReceipt(
                action,
                True,
                before.generation,
                "accepted related transition",
                after_local_window_id="window-b",
            )

    action = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "open the unique exact result",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="7",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_ABSENT,
            label,
        ),
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            action,
            _done_decision(
                DesktopExpectationKind.TEXT_PRESENT,
                "文件传输助手",
            ),
        ]
    )
    driver = RelatedWindowDriver(
        [source(1), source(2), destination(3), destination(4)]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("打开文件传输助手")

    assert result.success is True
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "click", 2)
    ]
    assert not driver.observations


def test_same_window_navigation_bridge_cannot_replace_a_requested_text_step() -> None:
    open_button = DesktopElement(
        "0",
        "Open",
        "Button",
        enabled=True,
        addressable=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )
    message_text = DesktopElement("0", "Message", "Text", addressable=False)

    def source(generation: int) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text='0 name="Open" control_type="Button"',
            window_title="Claude",
            elements=(open_button,),
            local_window_id="window-a",
        )

    def destination(generation: int) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text='0 name="Message" control_type="Text"',
            window_title="Claude",
            elements=(message_text,),
            local_window_id="window-a",
        )

    action = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "open a semantic affordance before the requested text field",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="0",
        ),
        expectation=DesktopExpectation(DesktopExpectationKind.TEXT_PRESENT, "Message"),
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            action,
            _done_decision(DesktopExpectationKind.TEXT_PRESENT, "Message"),
        ]
    )
    driver = FakeDriver([source(1), source(2), destination(3), destination(4)])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run('In Claude, type "hello" into Message')

    assert result.success is False
    assert result.error_code == "USER_STEPS_INCOMPLETE"
    assert [call[1] for call in driver.calls if call[0] == "execute"] == ["click"]


def test_related_window_navigation_bridge_cannot_replace_a_requested_text_step() -> None:
    label = "Message 前往"
    result_button = DesktopElement(
        "7",
        label,
        "Button",
        enabled=True,
        addressable=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )
    message_text = DesktopElement("0", "Message", "Text", addressable=False)

    def source(generation: int) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text=f'7 name="{label}" control_type="Button"',
            window_title="Search",
            elements=(result_button,),
            local_window_id="window-a",
        )

    def destination(generation: int) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text='0 name="Message" control_type="Text"',
            window_title="Claude",
            elements=(message_text,),
            local_window_id="window-b",
        )

    class RelatedWindowDriver(FakeDriver):
        def execute(self, action, before, *, cancel_event=None):
            self.calls.append(("execute", action.type.value, before.generation))
            return ActionReceipt(
                action,
                True,
                before.generation,
                "accepted related transition",
                after_local_window_id="window-b",
            )

    action = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "open the semantic bridge, but do not treat it as text entry",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="7",
        ),
        expectation=DesktopExpectation(DesktopExpectationKind.TEXT_ABSENT, label),
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            action,
            _done_decision(DesktopExpectationKind.TEXT_PRESENT, "Message"),
        ]
    )
    driver = RelatedWindowDriver(
        [source(1), source(2), destination(3), destination(4)]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run('In Claude, type "hello" into Message')

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert not any(call[0] == "execute" for call in driver.calls)


def test_related_window_destination_cannot_replace_an_authored_outcome() -> None:
    label = "Message 前往"
    result_button = DesktopElement(
        "7",
        label,
        "Button",
        enabled=True,
        addressable=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )
    message_text = DesktopElement("0", "Message", "Text", addressable=False)

    def source(generation: int) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text=f'7 name="{label}" control_type="Button"',
            window_title="Search",
            elements=(result_button,),
            local_window_id="window-a",
        )

    def wrong_destination(generation: int) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text='0 name="Message" control_type="Text"',
            window_title="Claude",
            elements=(message_text,),
            local_window_id="window-b",
        )

    class RelatedWindowDriver(FakeDriver):
        def execute(self, action, before, *, cancel_event=None):
            self.calls.append(("execute", action.type.value, before.generation))
            return ActionReceipt(
                action,
                True,
                before.generation,
                "accepted related transition",
                after_local_window_id="window-b",
            )

    action = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "open the exact result",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="7",
        ),
        expectation=DesktopExpectation(DesktopExpectationKind.TEXT_ABSENT, label),
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            action,
            _done_decision(DesktopExpectationKind.TEXT_PRESENT, "Message"),
        ]
    )
    driver = RelatedWindowDriver(
        [source(1), source(2), wrong_destination(3), wrong_destination(4)]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, click Message to show Settings")

    assert result.success is False
    assert result.error_code == "USER_STEPS_INCOMPLETE"
    assert [call[1] for call in driver.calls if call[0] == "execute"] == ["click"]


def test_failed_related_outcome_cannot_advance_before_a_later_text_step() -> None:
    open_button = DesktopElement(
        "0",
        "Open",
        "Button",
        enabled=True,
        addressable=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )

    def source(generation: int) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text='0 name="Open" control_type="Button"',
            window_title="Search",
            elements=(open_button,),
            local_window_id="window-a",
        )

    def composer(generation: int, value: str) -> DesktopObservation:
        editor = DesktopElement(
            "0",
            "Message",
            "Edit",
            value=value,
            focused=True,
            editable=True,
            supported_actions=(DesktopElementAction.TYPE_TEXT,),
        )
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text=(
                f'0 name="Message" control_type="Edit" focused=true value="{value}"'
            ),
            window_title="Claude",
            elements=(editor,),
            local_window_id="window-b",
        )

    class RelatedWindowDriver(FakeDriver):
        def execute(self, action, before, *, cancel_event=None):
            self.calls.append(("execute", action.type.value, before.generation))
            return ActionReceipt(
                action,
                True,
                before.generation,
                "accepted",
                after_local_window_id=(
                    "window-b" if action.type == DesktopActionType.CLICK else None
                ),
            )

    click = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "click the requested source target",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="0",
        ),
        expectation=DesktopExpectation(DesktopExpectationKind.TEXT_PRESENT, "Target"),
    )
    type_text = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "type the later exact payload",
        app="claude",
        action=DesktopAction(
            DesktopActionType.TYPE_TEXT,
            app="claude",
            generation=3,
            element_index="0",
            text="hello",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            "hello",
        ),
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            click,
            type_text,
            _done_decision(DesktopExpectationKind.FOCUSED_CONTAINS, "hello"),
        ]
    )
    driver = RelatedWindowDriver(
        [
            source(1),
            source(2),
            composer(3, ""),
            composer(4, ""),
            composer(5, "hello"),
            composer(6, "hello"),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(
        'In Claude, click Open to show Target, then type "hello" into Message'
    )

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert [call[1] for call in driver.calls if call[0] == "execute"] == ["click"]


def test_future_enter_step_cannot_dispatch_before_the_current_click_step() -> None:
    open_button = DesktopElement(
        "0",
        "Open",
        "Button",
        enabled=True,
        addressable=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )
    confirm_button = DesktopElement(
        "1",
        "Confirm",
        "Button",
        enabled=True,
        addressable=True,
        focused=True,
        supported_actions=(DesktopElementAction.PRESS_KEY,),
    )
    initial = DesktopObservation(
        app="claude",
        generation=1,
        accessibility_text=(
            '0 name="Open" control_type="Button"\n'
            '1 name="Confirm" control_type="Button" focused=true'
        ),
        window_title="Claude",
        elements=(open_button, confirm_button),
        local_window_id="window-a",
    )
    future_enter = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "incorrectly skip to the later key step",
        app="claude",
        action=DesktopAction(
            DesktopActionType.PRESS_KEY,
            app="claude",
            generation=1,
            element_index="1",
            key="enter",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            "Confirm",
        ),
    )
    driver = FakeDriver([initial])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner([_observe_decision(), future_enter]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, click Open, then press Enter on Confirm")

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert not any(call[0] == "execute" for call in driver.calls)


def test_future_click_step_cannot_dispatch_before_the_current_click_step() -> None:
    future_click = _selection_action("Beta", 1)
    driver = FakeDriver([_selection_observation(1)])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner([_observe_decision(), future_click]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, click Alpha, then click Beta")

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert not any(call[0] == "execute" for call in driver.calls)


def test_decorated_future_click_label_cannot_bypass_step_order() -> None:
    menu = DesktopElement(
        "0",
        "Menu button",
        "Button",
        enabled=True,
        addressable=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )
    initial = DesktopObservation(
        app="claude",
        generation=1,
        accessibility_text='0 name="Menu button" control_type="Button"',
        window_title="Claude",
        elements=(menu,),
        local_window_id="window-a",
    )
    click_menu = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "incorrectly skip to the decorated later target",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="0",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            "Menu button",
        ),
    )
    driver = FakeDriver([initial])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner([_observe_decision(), click_menu]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, click Open, then click Menu")

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert not any(call[0] == "execute" for call in driver.calls)


@pytest.mark.parametrize(
    ("task", "target_label", "expected_error"),
    [
        ("In Claude, do not click Menu", "Menu", "NO_POSITIVE_USER_ACTION"),
        (
            "In Claude, click Open, but do not click Menu",
            "Menu",
            "ACTION_NOT_BOUND_TO_TASK",
        ),
        (
            "In Claude, click Open, but do not click Menu",
            "Menu button",
            "ACTION_NOT_BOUND_TO_TASK",
        ),
    ],
)
def test_generic_navigation_bridge_never_uses_a_negated_target(
    task: str,
    target_label: str,
    expected_error: str,
) -> None:
    menu = DesktopElement(
        "0",
        target_label,
        "Button",
        enabled=True,
        addressable=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )
    initial = DesktopObservation(
        app="claude",
        generation=1,
        accessibility_text=f'0 name="{target_label}" control_type="Button"',
        window_title="Claude",
        elements=(menu,),
        local_window_id="window-a",
    )
    click_menu = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "an inferred bridge must not come from a negated clause",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="0",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            target_label,
        ),
    )
    driver = FakeDriver([initial])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner([_observe_decision(), click_menu]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert result.success is False
    assert result.error_code == expected_error
    assert not any(call[0] == "execute" for call in driver.calls)


def test_direct_click_never_overrides_a_same_target_click_negation() -> None:
    alpha = DesktopElement(
        "0",
        "Alpha",
        "Button",
        enabled=True,
        addressable=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )
    initial = DesktopObservation(
        app="claude",
        generation=1,
        accessibility_text='0 name="Alpha" control_type="Button"',
        window_title="Claude",
        elements=(alpha,),
        local_window_id="window-a",
    )
    click_alpha = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "the positive open target cannot override a later click prohibition",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="0",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            "Alpha",
        ),
    )
    driver = FakeDriver([initial])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner([_observe_decision(), click_alpha]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(
        "In Claude, open Alpha, but do not click Alpha"
    )

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert not any(call[0] == "execute" for call in driver.calls)


def test_direct_text_step_never_overrides_a_same_payload_text_negation() -> None:
    message = DesktopElement(
        "0",
        "Message",
        "Edit",
        value="",
        focused=True,
        editable=True,
        enabled=True,
        addressable=True,
        supported_actions=(DesktopElementAction.TYPE_TEXT,),
    )
    initial = DesktopObservation(
        app="claude",
        generation=1,
        accessibility_text=(
            '0 name="Message" control_type="Edit" focused=true value=""'
        ),
        window_title="Claude",
        elements=(message,),
        local_window_id="window-a",
    )
    type_alpha = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "the positive input cannot override a later payload prohibition",
        app="claude",
        action=DesktopAction(
            DesktopActionType.TYPE_TEXT,
            app="claude",
            generation=1,
            element_index="0",
            text="Alpha",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            "Alpha",
        ),
    )
    driver = FakeDriver([initial])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner([_observe_decision(), type_alpha]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(
        "In Claude, type Alpha into Message, but do not type Alpha"
    )

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert not any(call[0] == "execute" for call in driver.calls)


def test_visual_reviews_cannot_complete_multiple_steps_without_an_action():
    viewport = DesktopElement(
        "2",
        "Visual screenshot viewport",
        "VisualViewport",
        plane=ElementPlane.CONTROL,
        visual_ocr=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )
    def visual_observation(generation: int, screenshot: bytes) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text="2 visual viewport",
            screenshot_png=screenshot,
            window_title="Claude",
            elements=(viewport,),
            local_window_id="window-a",
        )

    initial = visual_observation(1, b"already-at-rendered-target")
    confirmation = visual_observation(2, b"fresh-confirmation-of-rendered-target")
    planner = SequencePlanner(
        [
            _observe_decision(),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(initial),
            ),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(confirmation),
            ),
        ]
    )
    driver = FakeDriver([initial, confirmation])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, click Alpha, then click Beta")

    assert result.success is False
    assert result.error_code == "NO_VERIFIED_ACTIONS"
    assert not any(call[0] == "execute" for call in driver.calls)
    assert not driver.observations


def test_one_visual_transition_cannot_complete_a_second_spoken_step():
    viewport = DesktopElement(
        "2",
        "Visual screenshot viewport",
        "VisualViewport",
        plane=ElementPlane.CONTROL,
        visual_ocr=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )

    def observation(
        generation: int,
        marker: str,
        *,
        with_viewport: bool,
    ) -> DesktopObservation:
        elements = (
            (viewport,)
            if with_viewport
            else (DesktopElement("0", "Intermediate", "Text", addressable=False),)
        )
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text=(
                '2 name="Visual screenshot viewport" control_type="VisualViewport" '
                if with_viewport
                else '0 name="Intermediate" control_type="Text" '
            )
            + marker,
            screenshot_png=marker.encode(),
            window_title="Claude",
            elements=elements,
            local_window_id="window-a",
        )

    action = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "click one rendered point",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="2",
            x=20,
            y=30,
        ),
        expectation=DesktopExpectation(DesktopExpectationKind.LAST_ACTION_VERIFIED),
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            action,
            _done_decision(DesktopExpectationKind.LAST_ACTION_VERIFIED),
        ]
    )
    driver = FakeDriver(
        [
            observation(1, "before", with_viewport=True),
            observation(2, "before", with_viewport=True),
            observation(3, "changed", with_viewport=False),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, click Alpha, then click Beta")

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert not any(call[0] == "execute" for call in driver.calls)


def test_private_visual_binding_alone_is_not_completion_evidence():
    viewport = DesktopElement(
        "2",
        "Visual screenshot viewport",
        "VisualViewport",
        plane=ElementPlane.CONTROL,
        visual_ocr=True,
        supported_actions=(DesktopElementAction.CLICK,),
    )

    def visual_observation(generation: int, screenshot: bytes) -> DesktopObservation:
        return DesktopObservation(
            app="claude",
            generation=generation,
            accessibility_text="2 visual viewport",
            screenshot_png=screenshot,
            window_title="Claude",
            elements=(viewport,),
            local_window_id="private-window-a",
        )

    initial = visual_observation(1, b"rendered-target")
    confirmation = visual_observation(2, b"fresh-rendered-target")
    planner = SequencePlanner(
        [
            _observe_decision(),
            _done_decision(DesktopExpectationKind.TEXT_PRESENT, "Target"),
            _done_decision(DesktopExpectationKind.TEXT_PRESENT, "Target"),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=FakeDriver([initial, confirmation]),
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("打开 Target")

    assert result.success is False
    assert result.error_code == "NO_VERIFIED_ACTIONS"
    assert planner.calls[1][1] == 1
    assert planner.calls[2][1] == 2


@pytest.mark.parametrize("profile", ["strict", "personal_trusted"])
def test_visual_zero_action_completion_is_local_unrestricted_only(profile):
    viewport = DesktopElement(
        "2",
        "Visual screenshot viewport",
        "VisualViewport",
        plane=ElementPlane.CONTROL,
        visual_ocr=True,
    )
    observation = DesktopObservation(
        app="claude",
        generation=1,
        accessibility_text="2 visual viewport",
        screenshot_png=b"rendered-target",
        elements=(viewport,),
        local_window_id="window-a",
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            _done_decision(
                DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                visual_state_binding_token(observation),
            ),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=FakeDriver([observation]),
        planner=planner,
        safety=DesktopSafetyPolicy(profile),
    )

    result = controller.run("In Claude, open the rendered Target conversation")

    assert result.success is False
    assert result.error_code == "NO_VERIFIED_ACTIONS"


def test_local_unrestricted_refreshes_inventory_for_a_window_opened_mid_task():
    def window_observation(app: str, generation: int, *, destination_visible: bool = False):
        elements = [DesktopElement("0", "Launch", "Button", selected=False)]
        text = '0 name="Launch" control_type="Button" selected=false'
        if destination_visible:
            elements.append(
                DesktopElement(
                    "1",
                    "目标窗口",
                    "Text",
                    addressable=False,
                )
            )
            text += '\n1 name="目标窗口" control_type="Text"'
        return DesktopObservation(
            app=app,
            generation=generation,
            accessibility_text=text,
            window_title="Source" if app == "source" else "目标窗口",
            elements=tuple(elements),
            local_window_id=f"window-{app}",
        )

    class ExpandingInventoryDriver(FakeDriver):
        def __init__(self):
            super().__init__(
                [
                    window_observation("source", 1),
                    window_observation("source", 2),
                    window_observation("source", 3, destination_visible=True),
                    window_observation("target", 4, destination_visible=True),
                    window_observation("target", 5, destination_visible=True),
                ]
            )
            self.inventory_calls = 0

        def list_apps(self, *, cancel_event=None):
            self.inventory_calls += 1
            self.calls.append(("list_apps",))
            items = [
                {
                    "app": "source",
                    "display_name": "Source",
                    "visible_window_count": 1,
                }
            ]
            if self.inventory_calls >= 3:
                items.append(
                    {
                        "app": "target",
                        "display_name": "目标窗口",
                        "visible_window_count": 1,
                    }
                )
            return json.dumps(items, ensure_ascii=False)

    destination = DesktopExpectation(DesktopExpectationKind.TEXT_PRESENT, "目标窗口")
    planner = SequencePlanner(
        [
            DesktopDecision(DesktopDecisionKind.OBSERVE, "observe source", app="source"),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "launch destination",
                app="source",
                action=DesktopAction(
                    DesktopActionType.CLICK,
                    app="source",
                    generation=1,
                    element_index="0",
                ),
                expectation=destination,
            ),
            DesktopDecision(DesktopDecisionKind.OBSERVE, "observe new window", app="target"),
            DesktopDecision(
                DesktopDecisionKind.DONE,
                "destination is visible",
                app="target",
                expectation=destination,
            ),
        ]
    )
    driver = ExpandingInventoryDriver()
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("打开目标窗口")

    assert result.success is True
    assert driver.inventory_calls >= 4
    assert ("observe", "target") in driver.calls


def test_local_unrestricted_cannot_finish_after_only_the_first_spoken_action():
    planner = SequencePlanner(
        [
            _observe_decision(),
            _selection_action("Alpha", 1),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "Alpha"),
        ]
    )
    driver = FakeDriver(
        [
            _selection_observation(1),
            _selection_observation(2),
            _selection_observation(3, selected={"Alpha"}),
            _selection_observation(4, selected={"Alpha"}),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("open Alpha then open Beta")

    assert result.success is False
    assert result.error_code == "USER_STEPS_INCOMPLETE"


def test_local_unrestricted_done_uses_a_fresh_completion_observation():
    destination = DesktopExpectation(DesktopExpectationKind.TEXT_PRESENT, "最终页面")

    def terminal(generation: int, *, selected: bool) -> DesktopObservation:
        target = DesktopElement("2", "最终页面", "Button", selected=selected)
        return _observation(
            generation,
            (
                '2 name="最终页面" control_type="Button" '
                f"selected={str(selected).lower()}"
            ),
            elements=(target,),
            local_window_id="window-a",
        )
    planner = SequencePlanner(
        [
            _observe_decision(),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "open one necessary intermediate control",
                app="claude",
                action=DesktopAction(
                    DesktopActionType.CLICK,
                    app="claude",
                    generation=1,
                    element_index="0",
                ),
                expectation=destination,
            ),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "activate the exact revealed user target",
                app="claude",
                action=DesktopAction(
                    DesktopActionType.CLICK,
                    app="claude",
                    generation=3,
                    element_index="2",
                ),
                expectation=DesktopExpectation(
                    DesktopExpectationKind.ELEMENT_SELECTED,
                    "最终页面",
                ),
            ),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "最终页面"),
        ]
    )
    driver = FakeDriver(
        [
            _selection_observation(1),
            _selection_observation(2),
            terminal(3, selected=False),
            terminal(4, selected=False),
            terminal(5, selected=True),
            terminal(6, selected=False),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("打开最终页面")

    assert result.success is False
    assert result.error_code == "COMPLETION_NOT_VERIFIED"


def test_app_scope_requires_one_affirmative_control_clause_and_rejects_denials():
    visible = ("codex", "claude")

    assert _explicitly_named_apps("在 Claude 输入你好但不要发送", visible) == {"claude"}
    assert _explicitly_named_apps("click Code in Claude", visible) == {"claude"}
    assert _explicitly_named_apps("In Claude, click Code", visible) == {"claude"}
    assert _explicitly_named_apps("不要操作 Claude，只操作 Codex", visible) == {"codex"}
    assert not _explicitly_named_apps(
        "不要在任何情况下通过任何方式去查看或者读取这个现在已经打开的 Claude",
        visible,
    )
    assert not _explicitly_named_apps(
        "Never under any circumstances use, inspect, observe, read, touch, or interact with Claude",
        visible,
    )
    for non_command in (
        "Explain Claude to me",
        "Because Claude is visible",
        "This note is in Claude",
        "问题在 Claude",
        "Claude typewriter notes",
    ):
        assert not _explicitly_named_apps(non_command, visible)


@pytest.mark.parametrize(
    "task",
    [
        "切换到cloud，打开chat and cowork。",
        "在cloloud的输入框输入，这是语音控制测试，不要发送。",
        "切换道克劳德，打开聊天和协作。",
    ],
)
def test_sensevoice_claude_aliases_still_require_affirmative_app_scope(task: str) -> None:
    assert _explicitly_named_apps(task, ("codex", "claude")) == {"claude"}


@pytest.mark.parametrize(
    "task",
    [
        "cloud computing 很重要",
        "输入 cloud 到输入框",
        "不要操作 cloud，只操作 Codex",
    ],
)
def test_sensevoice_claude_aliases_do_not_bypass_app_scope_gate(task: str) -> None:
    assert "claude" not in _explicitly_named_apps(task, ("codex", "claude"))


def test_longer_cloud_product_name_remains_an_unsupported_explicit_scope() -> None:
    task = "切换到 Cloud Storage，然后打开设置"

    assert _unsupported_explicit_app_scopes(task, ("codex", "claude")) == ("cloud storage",)


def test_app_scope_reuses_data_and_negation_boundaries() -> None:
    visible = ("codex", "claude")
    for task in (
        "type the words open Claude into Prompt",
        'type "open Claude" into Prompt',
        "cannot open Claude",
        "can't open Claude",
        "不能打开 Claude",
        "请勿使用 Claude",
        "cannot open Claude, click Code",
    ):
        assert not _explicitly_named_apps(task, visible)

    assert _explicitly_named_apps("In Claude, click Code", visible) == {"claude"}
    assert _explicitly_named_apps(
        "In Claude, type the words open Codex into Prompt",
        visible,
    ) == {"claude"}


def test_unknown_explicit_app_scope_is_detected_without_matching_quotes_or_negation():
    known = ("claude", "codex")

    for task in (
        "In Notepad, click Code",
        "In Notepad click Code",
        "click Code in Notepad",
        "在记事本里点击 Code",
        "点击 Code 于记事本",
        "在记事本点击 Code",
        "点击 Code，在记事本里",
        "点击记事本里的 Code",
        "Use Notepad to click Code",
        "Click Code using Notepad",
        "用记事本点击 Code",
        "使用记事本点击 Code",
        "到记事本点击 Code",
        "Notepad: click Code",
        "Go to Notepad and click Code",
        "Switch to Notepad and click Code",
        "Open Notepad and click Code",
        "Launch Notepad then click Code",
        "Select Notepad, then click Code",
        "Navigate to Notepad and click Code",
        "打开记事本然后点击 Code",
        "切换到记事本然后点击 Code",
        "进入记事本并点击 Code",
        "启动记事本再点击 Code",
        "选择记事本，然后点击 Code",
        "导航到记事本然后点击 Code",
    ):
        assert _unsupported_explicit_app_scopes(task, known)

    assert not _unsupported_explicit_app_scopes(
        'type "In Notepad, click Code" into Prompt',
        known,
    )
    assert not _unsupported_explicit_app_scopes(
        "Do not click Code in Notepad, click Code in Claude",
        known,
    )
    assert not _unsupported_explicit_app_scopes(
        'type "Use Notepad to click Code" into Prompt',
        known,
    )
    assert not _unsupported_explicit_app_scopes(
        "Do not use Notepad to click Code; click Code in Claude",
        known,
    )
    assert not _unsupported_explicit_app_scopes(
        'type "Go to Notepad and click Code" into Prompt',
        known,
    )
    assert not _unsupported_explicit_app_scopes(
        "Do not go to Notepad and click Code; click Code in Claude",
        known,
    )
    assert not _unsupported_explicit_app_scopes(
        "输入“打开记事本然后点击 Code”到 Prompt",
        known,
    )
    assert not _unsupported_explicit_app_scopes(
        "不要打开记事本然后点击 Code；在 Claude 点击 Code",
        known,
    )
    for task in (
        "在 Claude 的输入框输入 测试",
        "在 Claude 应用的输入框输入 测试",
        "切换到 Codex，打开其中的项目",
    ):
        assert not _unsupported_explicit_app_scopes(task, known)


def test_chinese_click_tab_role_suffix_counts_one_step_and_completes():
    task = "在 Claude 中点击 Code 选项卡"
    before = _observation(
        1,
        '25 name="Code" control_type="TabItem" selected=false',
        elements=(DesktopElement("25", "Code", "TabItem", selected=False),),
    )
    refresh = _observation(
        2,
        '25 name="Code" control_type="TabItem" selected=false',
        elements=(DesktopElement("25", "Code", "TabItem", selected=False),),
    )
    after = _observation(
        3,
        '25 name="Code" control_type="TabItem" selected=true',
        elements=(DesktopElement("25", "Code", "TabItem", selected=True),),
    )
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="claude",
        generation=1,
        element_index="25",
    )
    expectation = DesktopExpectation(
        DesktopExpectationKind.ELEMENT_SELECTED,
        text="Code",
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "click exact tab",
                app="claude",
                action=action,
                expectation=expectation,
            ),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "Code"),
        ]
    )
    driver = FakeDriver([before, refresh, after])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("personal_trusted"),
    )

    result = controller.run(task)

    assert result.success
    assert result.message.startswith("LOCAL_VERIFIED_COMPLETION:")
    assert [call for call in driver.calls if call[0] == "execute"] == [("execute", "click", 2)]


@pytest.mark.parametrize(
    "task",
    [
        "Go to Notepad and click Code",
        "Launch Notepad then click Code",
        "切换到记事本然后点击 Code",
        "启动记事本再点击 Code",
    ],
)
def test_action_before_unknown_app_clears_personal_context(task):
    controller, driver, planner = _run_verified_alpha_session(
        profile="personal_trusted",
    )
    calls_before = len(planner.calls)

    unsupported = controller.run(task)
    inherited = controller.run("select Beta")

    assert unsupported.error_code == "APP_SCOPE_UNSUPPORTED"
    assert inherited.error_code == "APP_SCOPE_REQUIRED"
    assert len(planner.calls) == calls_before
    assert [call for call in driver.calls if call[0] == "execute"] == [("execute", "click", 2)]


def test_personal_trusted_reuses_same_verified_app_window_for_next_over_command():
    controller, driver, planner = _run_verified_alpha_session(
        profile="personal_trusted",
        later_observations=(
            _selection_observation(4, selected={"Alpha"}),
            _selection_observation(5, selected={"Alpha"}),
            _selection_observation(6, selected={"Alpha", "Beta"}),
        ),
        later_decisions=(
            _selection_action("Beta", 4),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "Beta"),
        ),
    )

    second = controller.run("select Beta")

    assert second.success
    assert planner.calls[3][0] == "select Beta"
    assert planner.calls[3][1] == 4
    assert "resumed the same locally verified app window" in planner.calls[3][2]
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "click", 2),
        ("execute", "click", 5),
    ]


def test_unknown_explicit_app_clears_personal_context_instead_of_using_claude():
    controller, driver, planner = _run_verified_alpha_session(
        profile="personal_trusted",
    )
    calls_before = len(planner.calls)

    unsupported = controller.run("In Notepad click Code")
    inherited = controller.run("select Beta")

    assert unsupported.error_code == "APP_SCOPE_UNSUPPORTED"
    assert inherited.error_code == "APP_SCOPE_REQUIRED"
    assert len(planner.calls) == calls_before
    assert [call for call in driver.calls if call[0] == "execute"] == [("execute", "click", 2)]


def test_named_but_invisible_app_clears_personal_context():
    controller, _driver, planner = _run_verified_alpha_session(
        profile="personal_trusted",
    )
    calls_before = len(planner.calls)

    invisible = controller.run("In Codex, click Projects")
    inherited = controller.run("select Beta")

    assert invisible.error_code == "APP_SCOPE_NOT_VISIBLE"
    assert inherited.error_code == "APP_SCOPE_REQUIRED"
    assert len(planner.calls) == calls_before


def test_strict_profile_does_not_reuse_previous_verified_app_window():
    controller, driver, planner = _run_verified_alpha_session(profile="strict")
    calls_before = len(planner.calls)

    second = controller.run("select Beta")

    assert not second.success
    assert second.error_code == "APP_SCOPE_REQUIRED"
    assert len(planner.calls) == calls_before
    assert [call for call in driver.calls if call[0] == "observe"] == [
        ("observe", "claude"),
        ("observe", "claude"),
        ("observe", "claude"),
    ]


def test_new_personal_controller_has_no_inherited_app_window():
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=FakeDriver([]),
        planner=SequencePlanner([]),
        safety=DesktopSafetyPolicy(profile="personal_trusted"),
    )

    result = controller.run("select Beta")

    assert not result.success
    assert result.error_code == "APP_SCOPE_REQUIRED"
    assert not any(call[0] == "observe" for call in controller.driver.calls)


def test_personal_context_fails_closed_when_local_window_changes():
    controller, driver, planner = _run_verified_alpha_session(
        profile="personal_trusted",
        later_observations=(
            _selection_observation(
                4,
                selected={"Alpha"},
                local_window_id="window-b",
            ),
        ),
    )
    calls_before = len(planner.calls)

    result = controller.run("select Beta")

    assert not result.success
    assert result.error_code == "SESSION_WINDOW_CHANGED"
    assert len(planner.calls) == calls_before
    assert not any(call == ("execute", "click", 4) for call in driver.calls)


def test_native_route_runs_before_driver_or_planner():
    native = FakeNativeRouter(
        NativeSkillResult(NativeRouteStatus.SUCCEEDED, "done", plan=Plan("done", []))
    )
    driver = FakeDriver([])
    planner = SequencePlanner([])
    controller = DesktopAgentLoopController(
        native_router=native,
        driver=driver,
        planner=planner,
    )

    result = controller.run("打开文档")

    assert result.success
    assert native.calls == ["打开文档"]
    assert driver.calls == []
    assert planner.calls == []


def test_native_router_exception_returns_structured_content_free_failure():
    class ExplodingRouter(FakeNativeRouter):
        def route(self, text):
            self.calls.append(text)
            raise RuntimeError("private-window-title-and-secret")

    router = ExplodingRouter(NativeSkillResult(NativeRouteStatus.MISS, "miss"))
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=None,
        planner=None,
    )

    result = controller.run("open a local folder")

    assert result.success is False
    assert result.stage == "native_route"
    assert result.error_code == "NATIVE_ROUTE_INTERNAL_ERROR"
    assert result.exception_type == "RuntimeError"
    assert result.safe_message
    assert "private-window-title-and-secret" not in result.message
    assert "private-window-title-and-secret" not in result.safe_message


def test_native_app_success_establishes_fresh_context_for_next_utterance():
    plan = Plan(
        "activate Claude",
        [Action(ActionType.ACTIVATE_APP, app="claude")],
    )
    router = FakeNativeRouter(NativeSkillResult(NativeRouteStatus.SUCCEEDED, "done", plan=plan))
    driver = ContextRecordingDriver(
        [
            _selection_observation(1, selected={"Alpha"}),
            _selection_observation(2, selected={"Alpha"}),
            _selection_observation(3, selected={"Alpha"}),
            _selection_observation(4, selected={"Alpha", "Beta"}),
        ]
    )
    planner = SequencePlanner(
        [
            _selection_action("Beta", 2),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "Beta"),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("personal_trusted"),
    )

    first = controller.run("open Claude")
    router.result = NativeSkillResult(NativeRouteStatus.MISS, "miss")
    second = controller.run("select Beta")

    assert first.success
    assert second.success
    assert planner.calls[0][1] == 2
    assert "resumed the same locally verified app window" in planner.calls[0][2]
    assert driver.task_contexts[-1] is None
    assert driver.task_contexts.count("open Claude") >= 1
    assert driver.task_contexts.count("select Beta") == 1


def test_cancelled_native_app_switch_clears_previous_personal_context():
    controller, _driver, _planner = _run_verified_alpha_session(
        profile="personal_trusted",
    )
    controller.native_router.result = NativeSkillResult(
        NativeRouteStatus.SUCCEEDED,
        "done",
        plan=Plan(
            "activate Codex",
            [Action(ActionType.ACTIVATE_APP, app="codex")],
        ),
    )
    cancelled = threading.Event()
    cancelled.set()

    switched = controller.run("open Codex", cancel_event=cancelled)
    controller.native_router.result = NativeSkillResult(NativeRouteStatus.MISS, "miss")
    inherited = controller.run("select Beta")

    assert switched.cancelled is True
    assert switched.stage == "native_route"
    assert switched.error_code == "CANCELLED"
    assert inherited.error_code == "APP_SCOPE_REQUIRED"


def test_driver_task_context_is_cleared_on_early_scope_failure():
    driver = ContextRecordingDriver([])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner([]),
        safety=DesktopSafetyPolicy("personal_trusted"),
    )

    result = controller.run("In Notepad click Code")

    assert result.error_code == "APP_SCOPE_UNSUPPORTED"
    assert driver.task_contexts == ["In Notepad click Code", None]


def test_generic_loop_observes_one_action_reobserves_and_verifies_done():
    before = _observation(
        1,
        '0 name="Message" focused=true value=""',
        elements=(DesktopElement("0", "Message", "Edit", value="", focused=True),),
    )
    refresh = _observation(
        2,
        '0 name="Message" focused=true value=""',
        elements=(DesktopElement("0", "Message", "Edit", value="", focused=True),),
    )
    after = _observation(
        3,
        '0 name="Message" focused=true value="你好"',
        elements=(DesktopElement("0", "Message", "Edit", value="你好", focused=True),),
    )
    action = DesktopAction(
        DesktopActionType.TYPE_TEXT,
        app="claude",
        generation=1,
        element_index="0",
        text="你好",
    )
    expectation = DesktopExpectation(
        DesktopExpectationKind.FOCUSED_CONTAINS,
        text="你好",
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "type once",
                app="claude",
                action=action,
                expectation=expectation,
            ),
            _done_decision(DesktopExpectationKind.FOCUSED_CONTAINS, "你好"),
        ]
    )
    driver = FakeDriver([before, refresh, after])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
    )

    waiting = controller.run("在 Claude 里输入你好到 Message，但不要发送")
    result = controller.confirm(waiting.confirmation_id)

    assert waiting.needs_confirmation
    assert result.success
    assert driver.calls == [
        ("start",),
        ("list_apps",),
        ("observe", "claude"),
        ("observe", "claude"),
        ("execute", "type_text", 2),
        ("observe", "claude"),
    ]
    assert len(planner.calls) == 3


def test_post_action_reobserve_retries_tree_rebuild_without_repeating_action():
    class TransientReobserveDriver(FakeDriver):
        def observe(self, app, *, cancel_event=None):
            self.calls.append(("observe", app))
            value = self.observations.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

    planner = SequencePlanner(
        [
            _observe_decision(),
            _selection_action("Alpha", 1),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "Alpha"),
        ]
    )
    driver = TransientReobserveDriver(
        [
            _selection_observation(1),
            _selection_observation(2),
            RuntimeError("transient Electron tree rebuild"),
            _selection_observation(3, selected={"Alpha"}),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        sleeper=lambda _seconds: None,
    )

    result = controller.run("In Claude, select Alpha")

    assert result.success
    assert [call for call in driver.calls if call[0] == "execute"] == [("execute", "click", 2)]
    assert [call for call in driver.calls if call[0] == "observe"] == [
        ("observe", "claude"),
        ("observe", "claude"),
        ("observe", "claude"),
        ("observe", "claude"),
    ]


def test_changed_same_window_is_refreshed_and_replanned_without_executing_stale_action():
    planner = SequencePlanner(
        [
            _observe_decision(),
            _selection_action("Alpha", 1),
            _selection_action("Alpha", 2),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "Alpha"),
        ]
    )
    driver = FakeDriver(
        [
            _selection_observation(1, marker="state-zero"),
            _selection_observation(2, marker="state-one"),
            _selection_observation(3, marker="state-one"),
            _selection_observation(4, selected={"Alpha"}, marker="state-one"),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
    )

    result = controller.run("In Claude, select Alpha")

    assert result.success
    assert planner.calls[2][1] == 2
    assert (
        "refreshed changed UI in the same local window; previous action was not executed"
        in planner.calls[2][2]
    )
    assert [call for call in driver.calls if call[0] == "execute"] == [("execute", "click", 3)]


def test_local_unrestricted_executes_stable_semantic_target_across_unrelated_animation():
    identity = "a" * 64

    def animated_observation(generation: int, marker: str, *, selected: bool = False):
        elements = (
            DesktopElement(
                "0",
                "Alpha",
                "Button",
                selected=selected,
                enabled=True,
                addressable=True,
                local_identity=identity,
                supported_actions=(DesktopElementAction.CLICK,),
            ),
            DesktopElement("1", marker, "Text", addressable=False),
        )
        return _observation(
            generation,
            f'0 name="Alpha" control_type="Button" selected={str(selected).lower()}\n'
            f'1 name="{marker}" control_type="Text"',
            elements=elements,
            local_window_id="window-a",
        )

    planner = SequencePlanner(
        [
            _observe_decision(),
            _selection_action("Alpha", 1),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "Alpha"),
        ]
    )
    driver = FakeDriver(
        [
            animated_observation(1, "animation-a"),
            animated_observation(2, "animation-b"),
            animated_observation(3, "animation-c", selected=True),
            animated_observation(4, "animation-d", selected=True),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, select Alpha")

    assert result.success is True
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "click", 2)
    ]


def test_semantic_target_label_drift_replans_before_outcome_click_dispatch():
    identity = "b" * 64

    def observation(
        generation: int,
        target_name: str,
        *,
        settings_visible: bool = False,
    ) -> DesktopObservation:
        elements = [
            DesktopElement(
                "0",
                target_name,
                "Button",
                enabled=True,
                addressable=True,
                local_identity=identity,
                supported_actions=(DesktopElementAction.CLICK,),
            )
        ]
        if settings_visible:
            elements.append(DesktopElement("1", "Settings", "Text", addressable=False))
        return _observation(
            generation,
            "\n".join(
                f'{element.index} name="{element.name}" control_type="{element.control_type}"'
                for element in elements
            ),
            elements=tuple(elements),
            local_window_id="window-a",
        )

    outcome_action = DesktopDecision(
        DesktopDecisionKind.ACTION,
        "click Alpha to reveal the authored outcome",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="0",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.TEXT_PRESENT,
            "Settings",
        ),
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            outcome_action,
            _done_decision(DesktopExpectationKind.TEXT_PRESENT, "Settings"),
        ]
    )
    driver = FakeDriver(
        [
            observation(1, "Alpha"),
            # A virtualized UIA node reused the same local identity for Beta
            # between planner frame A and dispatch frame B.
            observation(2, "Beta"),
            observation(3, "Beta", settings_visible=True),
            observation(4, "Beta", settings_visible=True),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, click Alpha to show Settings")

    assert result.success is False
    assert result.error_code == "NO_VERIFIED_ACTIONS"
    assert planner.calls[2][1] == 2
    assert (
        "refreshed changed UI in the same local window; previous action was not executed"
        in planner.calls[2][2]
    )
    assert [call for call in driver.calls if call[0] == "execute"] == []


def test_repeated_pre_action_changes_fail_as_unstable_without_executing():
    planner = SequencePlanner(
        [
            _observe_decision(),
            _selection_action("Alpha", 1),
            _selection_action("Alpha", 2),
            _selection_action("Alpha", 3),
        ]
    )
    driver = FakeDriver(
        [
            _selection_observation(1, marker="state-zero"),
            _selection_observation(2, marker="state-one"),
            _selection_observation(3, marker="state-two"),
            _selection_observation(4, marker="state-three"),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
    )

    result = controller.run("In Claude, select Alpha")

    assert not result.success
    assert result.stage == "observe_driver"
    assert result.error_code == "UI_STATE_UNSTABLE"
    assert not any(call[0] == "execute" for call in driver.calls)


def test_generic_loop_cannot_substitute_typed_payload_for_an_authored_outcome():
    before = _observation(
        1,
        '0 name="Prompt" focused=true value=""',
        elements=(DesktopElement("0", "Prompt", "Edit", value="", focused=True),),
    )
    refresh = _observation(
        2,
        '0 name="Prompt" focused=true value=""',
        elements=(DesktopElement("0", "Prompt", "Edit", value="", focused=True),),
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "substitute payload for requested result",
                app="claude",
                action=DesktopAction(
                    DesktopActionType.TYPE_TEXT,
                    app="claude",
                    generation=1,
                    element_index="0",
                    text="hello",
                ),
                expectation=DesktopExpectation(
                    DesktopExpectationKind.TEXT_PRESENT,
                    text="hello",
                ),
            ),
        ]
    )
    driver = FakeDriver([before, refresh])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
    )

    result = controller.run("In Claude, type hello into Prompt so Draft saved appears")

    assert not result.success
    assert not result.needs_confirmation
    assert not any(call[0] == "execute" for call in driver.calls)


def test_action_specific_expectation_is_checked_after_fresh_observation():
    before = _observation(
        1,
        '0 name="Code" focused=true',
        elements=(DesktopElement("0", "Code", "Button", focused=True),),
    )
    refresh = _observation(
        2,
        '0 name="Code" focused=true',
        elements=(DesktopElement("0", "Code", "Button", focused=True),),
    )
    after = _observation(
        3,
        '0 name="Code" focused=false',
        elements=(DesktopElement("0", "Code", "Button", focused=False),),
    )
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="claude",
        generation=1,
        element_index="0",
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "click once",
                app="claude",
                action=action,
                expectation=DesktopExpectation(
                    DesktopExpectationKind.TEXT_PRESENT,
                    text="Result",
                ),
            ),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=FakeDriver([before, refresh, after]),
        planner=planner,
    )

    result = controller.run("click Code in Claude to show Result")

    assert not result.success
    assert "动作后任务条件未成立" in result.message


def test_planner_done_is_rejected_when_local_expectation_is_false():
    planner = SequencePlanner(
        [
            _observe_decision(),
            _done_decision(DesktopExpectationKind.TEXT_PRESENT, "not visible"),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=FakeDriver([_observation(1, "ordinary UI")]),
        planner=planner,
    )

    result = controller.run("检查 Claude 的一个不存在状态")

    assert not result.success
    assert "尚无任何经过本地验收的动作" in result.message


def test_action_task_cannot_finish_on_a_preexisting_visible_label():
    planner = SequencePlanner(
        [
            _observe_decision(),
            _done_decision(DesktopExpectationKind.TEXT_PRESENT, "Code"),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=FakeDriver([_observation(1, "0 button Code")]),
        planner=planner,
    )

    result = controller.run("click the Code button in Claude")

    assert not result.success
    assert "尚无任何经过本地验收的动作" in result.message


def test_sensitive_observation_is_blocked_before_second_cloud_planner_call():
    planner = SequencePlanner([_observe_decision()])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=FakeDriver([_observation(1, "password entry", title="Sign in")]),
        planner=planner,
    )

    result = controller.run("查看 Claude 登录界面")

    assert not result.success
    assert "安全策略阻止读取" in result.message
    assert len(planner.calls) == 1


def test_confirmation_is_bound_one_time_then_continues_loop():
    before_text = '0 name="Send" focused=true enabled=true'
    before = _observation(
        1,
        before_text,
        elements=(DesktopElement("0", "Send", "Button", focused=True),),
    )
    confirm_refresh = _observation(
        2,
        before_text,
        elements=(DesktopElement("0", "Send", "Button", focused=True),),
    )
    after = _observation(
        3,
        '0 name="Sent" focused=false enabled=true',
        elements=(DesktopElement("0", "Sent", "Button", focused=False),),
    )
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="claude",
        generation=1,
        element_index="0",
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "send",
                app="claude",
                action=action,
                expectation=DesktopExpectation(
                    DesktopExpectationKind.TEXT_PRESENT,
                    text="Sent",
                ),
            ),
            _done_decision(DesktopExpectationKind.TEXT_PRESENT, "Sent"),
        ]
    )
    driver = FakeDriver([before, confirm_refresh, after])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
    )

    waiting = controller.run("在 Claude 点击 Send 发送并看到 Sent")
    assert waiting.needs_confirmation
    assert waiting.confirmation_id
    assert not any(call[0] == "execute" for call in driver.calls)

    completed = controller.confirm(waiting.confirmation_id)
    replay = controller.confirm(waiting.confirmation_id)

    assert completed.success
    assert not replay.success
    assert "不匹配或已经使用" in replay.message
    assert ("execute", "click", 2) in driver.calls


def test_confirmation_fails_closed_when_interface_changes():
    before = _observation(
        1,
        '0 name="Send" focused=true',
        elements=(DesktopElement("0", "Send", "Button", focused=True),),
    )
    changed = _observation(
        2,
        '0 name="Delete" focused=true',
        elements=(DesktopElement("0", "Delete", "Button", focused=True),),
    )
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="claude",
        generation=1,
        element_index="0",
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "send",
                app="claude",
                action=action,
                expectation=DesktopExpectation(
                    DesktopExpectationKind.TEXT_PRESENT,
                    text="Sent",
                ),
            ),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=FakeDriver([before, changed]),
        planner=planner,
    )

    waiting = controller.run("在 Claude 点击 Send 发送并看到 Sent")
    result = controller.confirm(waiting.confirmation_id)

    assert not result.success
    assert "界面已经变化" in result.message
    assert result.error_code == "STALE_WINDOW_CHANGED"


def test_native_confirmation_executes_exact_saved_plan_once():
    action = Action(ActionType.START_NATIVE_VOICE, app="claude")
    plan = Plan("voice", [action], risk=RiskLevel.CONFIRM)
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.CONFIRMATION_REQUIRED,
            "需要启动应用内语音",
            plan=plan,
        )
    )
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=None,
        planner=None,
    )

    waiting = controller.run("打开 Claude 应用内语音")
    completed = controller.confirm(waiting.confirmation_id)
    replay = controller.confirm(waiting.confirmation_id)

    assert waiting.needs_confirmation
    assert completed.success
    assert router.executor.plans == [plan]
    assert not replay.success


def test_native_confirmation_executes_a_detached_plan_snapshot():
    entered = threading.Event()
    release = threading.Event()
    plan = Plan(
        "voice",
        [Action(ActionType.START_NATIVE_VOICE, app="claude")],
        risk=RiskLevel.CONFIRM,
    )
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.CONFIRMATION_REQUIRED,
            "需要启动应用内语音",
            plan=plan,
        )
    )

    def barrier_execute(execution_plan):
        entered.set()
        assert release.wait(timeout=2)
        router.executor.plans.append(execution_plan)
        return [
            ExecutionResult(True, "verified", action=action, evidence={"verified": True})
            for action in execution_plan.actions
        ]

    router.executor.execute_plan = barrier_execute
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=None,
        planner=None,
    )
    waiting = controller.run("打开 Claude 应用内语音")

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(controller.confirm, waiting.confirmation_id)
        assert entered.wait(timeout=2)
        plan.actions[0].app = "codex"
        release.set()
        completed = future.result(timeout=2)

    assert completed.success
    assert [executed.actions[0].app for executed in router.executor.plans] == ["claude"]


def test_native_confirmation_rejects_recreated_directory_with_restored_mtime(tmp_path):
    target = tmp_path / "approved-folder"
    target.mkdir()
    before = target.stat()
    plan = Plan(
        "open approved folder",
        [Action(ActionType.OPEN_PATH, path=str(target))],
        risk=RiskLevel.CONFIRM,
    )
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.CONFIRMATION_REQUIRED,
            "confirmation required",
            plan=plan,
        )
    )
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=None,
        planner=None,
    )

    waiting = controller.run("open the approved folder")
    target.rmdir()
    target.mkdir()
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    completed = controller.confirm(waiting.confirmation_id)

    assert waiting.needs_confirmation
    assert not completed.success
    assert "目标身份或内容已经变化" in completed.message
    assert router.executor.plans == []


def test_native_binding_race_returns_bounded_failure_instead_of_escaping(monkeypatch):
    plan = Plan(
        "open unstable target",
        [Action(ActionType.OPEN_PATH, path="unstable")],
        risk=RiskLevel.CONFIRM,
    )
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.CONFIRMATION_REQUIRED,
            "confirmation required",
            plan=plan,
        )
    )
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=None,
        planner=None,
    )

    def unstable_binding(_plan):
        raise RuntimeError("target changed while binding")

    monkeypatch.setattr(controller, "_native_binding", unstable_binding)

    result = controller.run("open the unstable target")

    assert not result.success
    assert "稳定身份" in result.message
    assert router.executor.plans == []


def test_cancelled_confirmation_id_cannot_authorize_a_new_identical_pending_action():
    action = Action(ActionType.START_NATIVE_VOICE, app="claude")
    plan = Plan("voice", [action], risk=RiskLevel.CONFIRM)
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.CONFIRMATION_REQUIRED,
            "需要启动应用内语音",
            plan=plan,
        )
    )
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=None,
        planner=None,
    )

    first = controller.run("打开 Claude 应用内语音")
    assert controller.cancel()
    second = controller.run("打开 Claude 应用内语音")
    stale = controller.confirm(first.confirmation_id)

    assert first.confirmation_id != second.confirmation_id
    assert not stale.success
    assert router.executor.plans == []


@dataclass
class AdvancingClock:
    value: float = 0.0

    def __call__(self):
        return self.value


def test_confirmation_expires_inside_controller():
    clock = AdvancingClock()
    before = _observation(
        1,
        '0 name="Send" focused=true',
        elements=(DesktopElement("0", "Send", "Button", focused=True),),
    )
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="claude",
        generation=1,
        element_index="0",
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "send",
                app="claude",
                action=action,
                expectation=DesktopExpectation(
                    DesktopExpectationKind.TEXT_PRESENT,
                    text="Sent",
                ),
            ),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=FakeDriver([before]),
        planner=planner,
        confirmation_timeout_seconds=5,
        monotonic=clock,
    )

    waiting = controller.run("在 Claude 点击 Send 发送并看到 Sent")
    clock.value = 6
    result = controller.confirm(waiting.confirmation_id)

    assert not result.success
    assert "确认已经超时" in result.message


def test_cancel_during_native_route_cannot_resurrect_a_confirmation():
    entered = threading.Event()
    release = threading.Event()
    plan = Plan(
        "voice",
        [Action(ActionType.START_NATIVE_VOICE, app="claude")],
        risk=RiskLevel.CONFIRM,
    )

    class BarrierRouter(FakeNativeRouter):
        def route(self, text):
            entered.set()
            assert release.wait(timeout=2)
            return super().route(text)

    router = BarrierRouter(
        NativeSkillResult(
            NativeRouteStatus.CONFIRMATION_REQUIRED,
            "confirm",
            plan=plan,
        )
    )
    controller = DesktopAgentLoopController(native_router=router, driver=None, planner=None)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(controller.run, "打开 Claude 应用内语音")
        assert entered.wait(timeout=2)
        assert controller.cancel()
        release.set()
        result = future.result(timeout=2)

    assert result.cancelled
    assert controller.pending_confirmation_id is None
    assert router.executor.plans == []


def test_cancel_during_native_binding_cannot_publish_pending_confirmation():
    entered = threading.Event()
    release = threading.Event()
    plan = Plan(
        "voice",
        [Action(ActionType.START_NATIVE_VOICE, app="claude")],
        risk=RiskLevel.CONFIRM,
    )
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.CONFIRMATION_REQUIRED,
            "confirm",
            plan=plan,
        )
    )
    controller = DesktopAgentLoopController(native_router=router, driver=None, planner=None)
    original_binding = controller._native_binding

    def blocking_binding(bound_plan):
        entered.set()
        assert release.wait(timeout=2)
        return original_binding(bound_plan)

    controller._native_binding = blocking_binding

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(controller.run, "打开 Claude 应用内语音")
        assert entered.wait(timeout=2)
        assert controller.cancel()
        release.set()
        result = future.result(timeout=2)

    assert result.cancelled
    assert controller.pending_confirmation_id is None
    assert router.executor.plans == []


def test_cancel_is_not_starved_by_blocking_native_confirmation_execution():
    entered = threading.Event()
    release = threading.Event()
    plan = Plan(
        "voice",
        [Action(ActionType.START_NATIVE_VOICE, app="claude")],
        risk=RiskLevel.CONFIRM,
    )
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.CONFIRMATION_REQUIRED,
            "confirm",
            plan=plan,
        )
    )

    def blocking_execute(execution_plan):
        entered.set()
        assert release.wait(timeout=2)
        return [
            ExecutionResult(True, "verified", action=action, evidence={"verified": True})
            for action in execution_plan.actions
        ]

    router.executor.execute_plan = blocking_execute
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=None,
        planner=None,
    )
    waiting = controller.run("打开 Claude 应用内语音")

    with ThreadPoolExecutor(max_workers=2) as pool:
        confirm_future = pool.submit(controller.confirm, waiting.confirmation_id)
        assert entered.wait(timeout=2)
        cancel_future = pool.submit(controller.cancel)
        try:
            assert cancel_future.result(timeout=0.5) is True
        finally:
            release.set()
        result = confirm_future.result(timeout=2)

    assert result.cancelled is True
    assert result.error_code == "CANCELLED"


def test_cancel_during_native_confirmation_verification_cannot_publish_success(
    monkeypatch,
):
    entered = threading.Event()
    release = threading.Event()
    plan = Plan(
        "voice",
        [Action(ActionType.START_NATIVE_VOICE, app="claude")],
        risk=RiskLevel.CONFIRM,
    )
    router = FakeNativeRouter(
        NativeSkillResult(
            NativeRouteStatus.CONFIRMATION_REQUIRED,
            "confirm",
            plan=plan,
        )
    )

    def blocking_verifier(_plan, _results):
        entered.set()
        assert release.wait(timeout=2)
        return True

    monkeypatch.setattr(
        NativeSkillRouter,
        "execution_is_locally_verified",
        staticmethod(blocking_verifier),
    )
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=None,
        planner=None,
    )
    waiting = controller.run("打开 Claude 应用内语音")

    with ThreadPoolExecutor(max_workers=2) as pool:
        confirm_future = pool.submit(controller.confirm, waiting.confirmation_id)
        assert entered.wait(timeout=2)
        assert controller.cancel() is True
        release.set()
        result = confirm_future.result(timeout=2)

    assert result.cancelled is True
    assert result.error_code == "CANCELLED"
    assert controller._trusted_context() is None


def test_cancel_during_native_context_refresh_cannot_publish_success():
    entered = threading.Event()
    release = threading.Event()
    plan = Plan(
        "activate Claude",
        [Action(ActionType.ACTIVATE_APP, app="claude")],
    )
    router = FakeNativeRouter(NativeSkillResult(NativeRouteStatus.SUCCEEDED, "done", plan=plan))

    class BlockingContextDriver(ContextRecordingDriver):
        def observe(self, app, *, cancel_event=None):
            entered.set()
            assert release.wait(timeout=2)
            return super().observe(app, cancel_event=cancel_event)

    driver = BlockingContextDriver([_selection_observation(1)])
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=driver,
        planner=SequencePlanner([]),
        safety=DesktopSafetyPolicy("personal_trusted"),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        run_future = pool.submit(controller.run, "open Claude")
        assert entered.wait(timeout=2)
        assert controller.cancel() is True
        release.set()
        result = run_future.result(timeout=2)

    assert result.cancelled is True
    assert result.error_code == "CANCELLED"
    assert controller._trusted_context() is None


def test_cancel_is_not_starved_by_blocking_desktop_execution():
    entered = threading.Event()
    release = threading.Event()
    before = _selection_observation(1)
    refreshed = _selection_observation(2)
    planner = SequencePlanner([_observe_decision(), _selection_action("Alpha", 1)])

    class BlockingDriver(FakeDriver):
        def execute(self, action, observation, *, cancel_event=None):
            self.calls.append(("execute", action.type.value, observation.generation))
            entered.set()
            assert release.wait(timeout=2)
            return ActionReceipt(action, True, observation.generation, "accepted")

    driver = BlockingDriver([before, refreshed])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("personal_trusted"),
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        run_future = pool.submit(controller.run, "In Claude, select Alpha")
        assert entered.wait(timeout=2)
        cancel_future = pool.submit(controller.cancel)
        try:
            assert cancel_future.result(timeout=0.5) is True
        finally:
            release.set()
        result = run_future.result(timeout=2)

    assert result.cancelled is True
    assert result.stage == "execute"
    assert result.error_code == "CANCELLED"
    assert controller._trusted_context() is None


def test_generic_loop_cannot_finish_after_only_the_first_of_two_user_steps():
    before = _observation(
        1,
        '0 name="Alpha" control_type="Button"\n1 name="Beta" control_type="Button"',
        elements=(
            DesktopElement("0", "Alpha", "Button"),
            DesktopElement("1", "Beta", "Button"),
        ),
    )
    refresh = _observation(
        2,
        '0 name="Alpha" control_type="Button"\n1 name="Beta" control_type="Button"',
        elements=(
            DesktopElement("0", "Alpha", "Button"),
            DesktopElement("1", "Beta", "Button"),
        ),
    )
    after = _observation(
        3,
        (
            '0 name="Alpha" control_type="Button" selected=true\n'
            '1 name="Beta" control_type="Button"\n'
            '2 name="X" control_type="Text"'
        ),
        elements=(
            DesktopElement("0", "Alpha", "Button", selected=True),
            DesktopElement("1", "Beta", "Button"),
            DesktopElement("2", "X", "Text"),
        ),
    )
    expectation = DesktopExpectation(
        DesktopExpectationKind.ELEMENT_SELECTED,
        text="Alpha",
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "first only",
                app="claude",
                action=DesktopAction(
                    DesktopActionType.CLICK,
                    app="claude",
                    generation=1,
                    element_index="0",
                ),
                expectation=expectation,
            ),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "Alpha"),
        ]
    )
    driver = FakeDriver([before, refresh, after])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
    )

    result = controller.run("In Claude, select Alpha and select Beta")

    assert not result.success
    assert "全部桌面步骤" in result.message
    assert [call for call in driver.calls if call[0] == "execute"] == [("execute", "click", 2)]


def test_generic_loop_rejects_skipping_directly_to_a_later_user_step():
    before = _observation(
        1,
        "Alpha Beta",
        elements=(
            DesktopElement("0", "Alpha", "Button"),
            DesktopElement("1", "Beta", "Button"),
        ),
    )
    planner = SequencePlanner(
        [
            _observe_decision(),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "skip first",
                app="claude",
                action=DesktopAction(
                    DesktopActionType.CLICK,
                    app="claude",
                    generation=1,
                    element_index="1",
                ),
                expectation=DesktopExpectation(
                    DesktopExpectationKind.TEXT_PRESENT,
                    text="Y",
                ),
            ),
        ]
    )
    driver = FakeDriver([before])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
    )

    result = controller.run("In Claude, click Alpha to show X, then click Beta to show Y")

    assert not result.success
    assert "未对应用户要求" in result.message
    assert not any(call[0] == "execute" for call in driver.calls)


def test_generic_loop_cannot_ignore_a_trailing_unsupported_action():
    before_text = (
        '0 name="Open" control_type="Button" selected=false\n1 name="Slider" control_type="Slider"'
    )
    after_text = (
        '0 name="Open" control_type="Button" selected=true\n1 name="Slider" control_type="Slider"'
    )
    before = _observation(
        1,
        before_text,
        elements=(
            DesktopElement("0", "Open", "Button", selected=False),
            DesktopElement("1", "Slider", "Slider"),
        ),
    )
    refresh = _observation(
        2,
        before_text,
        elements=(
            DesktopElement("0", "Open", "Button", selected=False),
            DesktopElement("1", "Slider", "Slider"),
        ),
    )
    after = _observation(
        3,
        after_text,
        elements=(
            DesktopElement("0", "Open", "Button", selected=True),
            DesktopElement("1", "Slider", "Slider"),
        ),
    )
    selected = DesktopExpectation(DesktopExpectationKind.ELEMENT_SELECTED, text="Open")
    planner = SequencePlanner(
        [
            _observe_decision(),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "select first",
                app="claude",
                action=DesktopAction(
                    DesktopActionType.CLICK,
                    app="claude",
                    generation=1,
                    element_index="0",
                ),
                expectation=selected,
            ),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "Open"),
        ]
    )
    driver = FakeDriver([before, refresh, after])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
    )

    result = controller.run("In Claude, select Open and drag Slider")

    assert not result.success
    assert "全部桌面步骤" in result.message
    assert [call for call in driver.calls if call[0] == "execute"] == [("execute", "click", 2)]


def _dynamic_observation(
    app: str,
    generation: int,
    title: str,
    elements: tuple[DesktopElement, ...],
    *,
    local_window_id: str | None = None,
    screenshot_png: bytes | None = None,
) -> DesktopObservation:
    text = "\n".join(
        f'{element.index} name="{element.name}" control_type="{element.control_type}" '
        f'focused={str(bool(element.focused)).lower()} '
        f'selected={str(bool(element.selected)).lower()} value="{element.value or ""}"'
        for element in elements
    )
    return DesktopObservation(
        app=app,
        generation=generation,
        accessibility_text=text,
        screenshot_png=screenshot_png,
        window_title=title,
        elements=elements,
        local_window_id=local_window_id,
    )


def test_explicit_scope_prefers_trusted_app_identity_over_spoofed_window_title():
    inventory = json.dumps(
        [
            {
                "app": "claude-id",
                "display_name": "Claude",
                "process_name": "claude.exe",
                "window_title": "Claude",
            },
            {
                "app": "chrome-id",
                "display_name": "Google Chrome",
                "process_name": "chrome.exe",
                "window_title": "Claude",
            },
        ]
    )

    assert _explicit_step_window_scope(
        "在 Claude 打开 Chat",
        inventory=inventory,
        completed_steps=0,
    ) == (True, frozenset({"claude-id"}))


def test_scope_scan_treats_backslashes_in_unrelated_window_title_as_literal_text():
    inventory = json.dumps(
        [
            {
                "app": "notes-id",
                "display_name": "Notepad",
                "process_name": "notepad.exe",
                "window_title": r"C:\maps\notes.txt - Notepad",
            },
            {
                "app": "chrome-id",
                "display_name": "Google Chrome",
                "process_name": "chrome.exe",
                "window_title": "Google Chrome",
            },
        ]
    )

    assert _explicit_step_window_scope(
        "切换到 Chrome，打开 Google",
        inventory=inventory,
        completed_steps=0,
    ) == (True, frozenset({"chrome-id"}))


def test_known_app_name_cannot_be_satisfied_only_by_an_untrusted_window_title():
    inventory = json.dumps(
        [
            {
                "app": "chrome-id",
                "display_name": "Google Chrome",
                "process_name": "chrome.exe",
                "window_title": "Claude",
            }
        ]
    )

    assert _explicit_step_window_scope(
        "在 Claude 输入你好",
        inventory=inventory,
        completed_steps=0,
    ) == (True, frozenset())


def test_explicit_app_scope_blocks_text_entry_in_the_wrong_visible_app():
    inventory = [
        {"app": "claude-id", "display_name": "Claude", "process_name": "claude.exe"},
        {
            "app": "chrome-id",
            "display_name": "Google Chrome",
            "process_name": "chrome.exe",
        },
    ]
    observation = _dynamic_observation(
        "chrome-id",
        1,
        "Browser",
        (DesktopElement("0", "Address and search bar", "Edit", value="", focused=True),),
    )
    planner = SequencePlanner(
        [
            DesktopDecision(DesktopDecisionKind.OBSERVE, "observe", app="chrome-id"),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "wrong app",
                app="chrome-id",
                action=DesktopAction(
                    DesktopActionType.TYPE_TEXT,
                    app="chrome-id",
                    generation=1,
                    element_index="0",
                    text="hello",
                ),
                expectation=DesktopExpectation(
                    DesktopExpectationKind.FOCUSED_CONTAINS,
                    "hello",
                ),
            ),
        ]
    )
    driver = InventoryDriver([observation], inventory)
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, type hello")

    assert not result.success
    assert result.error_code == "EXPLICIT_STEP_WINDOW_MISMATCH"
    assert not any(call[0] == "execute" for call in driver.calls)


@pytest.mark.parametrize(
    "task",
    [
        "In Claude Message, type hello",
        "In Claude's Message, type hello",
        "在 Claude 的 Message 输入 hello",
        "在 Message 输入 hello",
    ],
)
def test_explicit_text_field_blocks_a_different_focused_editor(task):
    inventory = [
        {"app": "claude-id", "display_name": "Claude", "process_name": "claude.exe"}
    ]
    observation = _dynamic_observation(
        "claude-id",
        1,
        "Claude",
        (DesktopElement("0", "Search", "Edit", value="", focused=True),),
    )
    planner = SequencePlanner(
        [
            DesktopDecision(DesktopDecisionKind.OBSERVE, "observe", app="claude-id"),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "wrong field",
                app="claude-id",
                action=DesktopAction(
                    DesktopActionType.TYPE_TEXT,
                    app="claude-id",
                    generation=1,
                    element_index="0",
                    text="hello",
                ),
                expectation=DesktopExpectation(
                    DesktopExpectationKind.FOCUSED_CONTAINS,
                    "hello",
                ),
            ),
        ]
    )
    driver = InventoryDriver([observation], inventory)
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert not result.success
    assert result.error_code == "EXPLICIT_TEXT_TARGET_MISMATCH"
    assert not any(call[0] == "execute" for call in driver.calls)


def test_natural_search_scope_blocks_another_browsers_address_bar():
    inventory = [
        {"app": "chrome-id", "display_name": "Google Chrome", "process_name": "chrome.exe"},
        {"app": "edge-id", "display_name": "Microsoft Edge", "process_name": "msedge.exe"},
    ]
    observation = _dynamic_observation(
        "edge-id",
        1,
        "Edge",
        (DesktopElement("0", "Address and search bar", "Edit", value="", focused=True),),
    )
    planner = SequencePlanner(
        [
            DesktopDecision(DesktopDecisionKind.OBSERVE, "observe", app="edge-id"),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "wrong browser",
                app="edge-id",
                action=DesktopAction(
                    DesktopActionType.SET_VALUE,
                    app="edge-id",
                    generation=1,
                    element_index="0",
                    value="OpenAI",
                ),
                expectation=DesktopExpectation(
                    DesktopExpectationKind.FOCUSED_CONTAINS,
                    "OpenAI",
                ),
            ),
        ]
    )
    driver = InventoryDriver([observation], inventory)
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("在 Chrome 搜索 OpenAI")

    assert not result.success
    assert result.error_code == "EXPLICIT_STEP_WINDOW_MISMATCH"
    assert not any(call[0] == "execute" for call in driver.calls)


def test_natural_search_requires_exact_fill_enter_and_fresh_result_state():
    inventory = [
        {"app": "chrome-id", "display_name": "Google Chrome", "process_name": "chrome.exe"}
    ]

    def search_observation(generation: int, value: str, title: str) -> DesktopObservation:
        elements = [
            DesktopElement(
                "0",
                "Address and search bar",
                "Edit",
                value=value,
                focused=True,
            )
        ]
        if "Search results" in title:
            elements.append(DesktopElement("1", "Results", "Text"))
        return _dynamic_observation(
            "chrome-id",
            generation,
            title,
            tuple(elements),
            local_window_id="hwnd:chrome",
        )

    observations = [
        search_observation(1, "", "Chrome"),
        search_observation(2, "", "Chrome"),
        search_observation(3, "OpenAI", "Chrome"),
        search_observation(4, "OpenAI", "Chrome"),
        search_observation(5, "OpenAI", "Search results - Chrome"),
        search_observation(6, "OpenAI", "Search results - Chrome"),
    ]
    planner = SequencePlanner(
        [
            DesktopDecision(DesktopDecisionKind.OBSERVE, "observe", app="chrome-id"),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "replace query",
                app="chrome-id",
                action=DesktopAction(
                    DesktopActionType.SET_VALUE,
                    app="chrome-id",
                    generation=1,
                    element_index="0",
                    value="OpenAI",
                ),
                expectation=DesktopExpectation(
                    DesktopExpectationKind.FOCUSED_CONTAINS,
                    "OpenAI",
                ),
            ),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "submit query",
                app="chrome-id",
                action=DesktopAction(
                    DesktopActionType.PRESS_KEY,
                    app="chrome-id",
                    generation=3,
                    element_index="0",
                    key="enter",
                ),
                expectation=DesktopExpectation(
                    DesktopExpectationKind.SEARCH_SUBMITTED,
                    "OpenAI",
                ),
            ),
            DesktopDecision(
                DesktopDecisionKind.DONE,
                "verified search",
                app="chrome-id",
                expectation=DesktopExpectation(
                    DesktopExpectationKind.SEARCH_SUBMITTED,
                    "OpenAI",
                ),
            ),
        ]
    )
    driver = InventoryDriver(observations, inventory)
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("在 Chrome 搜索 OpenAI")

    assert result.success
    assert [call[1] for call in driver.calls if call[0] == "execute"] == [
        "set_value",
        "press_key",
    ]


def test_explicit_app_terminal_condition_cannot_finish_in_another_app():
    inventory = [
        {"app": "claude-id", "display_name": "Claude", "process_name": "claude.exe"},
        {"app": "chrome-id", "display_name": "Google Chrome", "process_name": "chrome.exe"},
    ]
    before = _dynamic_observation(
        "chrome-id",
        1,
        "Chrome",
        (DesktopElement("0", "Foo", "Button"),),
        local_window_id="hwnd:chrome",
    )
    refresh = _dynamic_observation(
        "chrome-id",
        2,
        "Chrome",
        (DesktopElement("0", "Foo", "Button"),),
        local_window_id="hwnd:chrome",
    )
    after = _dynamic_observation(
        "chrome-id",
        3,
        "Chrome",
        (DesktopElement("0", "Foo", "Button"), DesktopElement("1", "Chat", "Text")),
        local_window_id="hwnd:chrome",
    )
    completion = _dynamic_observation(
        "chrome-id",
        4,
        "Chrome",
        (DesktopElement("0", "Foo", "Button"), DesktopElement("1", "Chat", "Text")),
        local_window_id="hwnd:chrome",
    )
    planner = SequencePlanner(
        [
            DesktopDecision(DesktopDecisionKind.OBSERVE, "observe", app="chrome-id"),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "bridge in wrong app",
                app="chrome-id",
                action=DesktopAction(
                    DesktopActionType.CLICK,
                    app="chrome-id",
                    generation=1,
                    element_index="0",
                ),
                expectation=DesktopExpectation(DesktopExpectationKind.TEXT_PRESENT, "Chat"),
            ),
            DesktopDecision(
                DesktopDecisionKind.DONE,
                "claim wrong app",
                app="chrome-id",
                expectation=DesktopExpectation(DesktopExpectationKind.TEXT_PRESENT, "Chat"),
            ),
        ]
    )
    driver = InventoryDriver([before, refresh, after, completion], inventory)
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("在 Claude 打开 Chat")

    assert not result.success
    assert result.error_code == "EXPLICIT_STEP_WINDOW_MISMATCH"
    assert not any(call[0] == "observe" for call in driver.calls)


def test_volatile_window_png_does_not_prevent_a_semantically_stable_action():
    base_elements = (DesktopElement("0", "Alpha", "Button", selected=False),)
    selected_elements = (DesktopElement("0", "Alpha", "Button", selected=True),)
    observations = [
        _dynamic_observation(
            "claude",
            1,
            "Claude",
            base_elements,
            local_window_id="hwnd:1",
            screenshot_png=b"png-one",
        ),
        _dynamic_observation(
            "claude",
            2,
            "Claude",
            base_elements,
            local_window_id="hwnd:1",
            screenshot_png=b"png-two",
        ),
        _dynamic_observation(
            "claude",
            3,
            "Claude",
            selected_elements,
            local_window_id="hwnd:1",
            screenshot_png=b"png-three",
        ),
        _dynamic_observation(
            "claude",
            4,
            "Claude",
            selected_elements,
            local_window_id="hwnd:1",
            screenshot_png=b"png-four",
        ),
    ]
    planner = SequencePlanner(
        [
            _observe_decision(),
            _selection_action("Alpha", 1),
            _done_decision(DesktopExpectationKind.ELEMENT_SELECTED, "Alpha"),
        ]
    )
    driver = FakeDriver(observations)
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
    )

    result = controller.run("In Claude, select Alpha")

    assert result.success
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "click", 2)
    ]


def test_chinese_sequence_prefixes_preserve_each_explicit_app_scope():
    task = "先在 Chrome 搜索 OpenAI，然后在 Claude 的 Message 输入总结"
    inventory = json.dumps(
        [
            {"app": "chrome-id", "display_name": "Google Chrome", "process_name": "chrome.exe"},
            {"app": "claude-id", "display_name": "Claude", "process_name": "claude.exe"},
        ],
        ensure_ascii=False,
    )

    assert user_action_step_count(task) == 2
    assert _explicit_step_window_scope(
        task,
        inventory=inventory,
        completed_steps=0,
    ) == (True, frozenset({"chrome-id"}))
    assert _explicit_step_window_scope(
        task,
        inventory=inventory,
        completed_steps=1,
    ) == (True, frozenset({"claude-id"}))


def test_chinese_cross_app_search_then_exact_field_input_completes_in_order():
    task = "先在 Chrome 搜索 OpenAI，然后在 Claude 的 Message 输入总结"
    inventory = [
        {"app": "chrome-id", "display_name": "Google Chrome", "process_name": "chrome.exe"},
        {"app": "claude-id", "display_name": "Claude", "process_name": "claude.exe"},
    ]

    def chrome(generation: int, value: str, title: str) -> DesktopObservation:
        elements = [
            DesktopElement(
                "0",
                "Address and search bar",
                "Edit",
                value=value,
                focused=True,
            )
        ]
        if "Search results" in title:
            elements.append(DesktopElement("1", "Results", "Text"))
        return _dynamic_observation(
            "chrome-id",
            generation,
            title,
            tuple(elements),
            local_window_id="hwnd:chrome",
        )

    def claude(generation: int, value: str) -> DesktopObservation:
        return _dynamic_observation(
            "claude-id",
            generation,
            "Claude",
            (DesktopElement("0", "Message", "Edit", value=value, focused=True),),
            local_window_id="hwnd:claude",
        )

    observations = [
        chrome(1, "", "Chrome"),
        chrome(2, "", "Chrome"),
        chrome(3, "OpenAI", "Chrome"),
        chrome(4, "OpenAI", "Chrome"),
        chrome(5, "OpenAI", "Search results - Chrome"),
        claude(6, ""),
        claude(7, ""),
        claude(8, "总结"),
        claude(9, "总结"),
    ]
    planner = SequencePlanner(
        [
            DesktopDecision(DesktopDecisionKind.OBSERVE, "observe search", app="chrome-id"),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "fill query",
                app="chrome-id",
                action=DesktopAction(
                    DesktopActionType.SET_VALUE,
                    app="chrome-id",
                    generation=1,
                    element_index="0",
                    value="OpenAI",
                ),
                expectation=DesktopExpectation(
                    DesktopExpectationKind.FOCUSED_CONTAINS,
                    "OpenAI",
                ),
            ),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "submit query",
                app="chrome-id",
                action=DesktopAction(
                    DesktopActionType.PRESS_KEY,
                    app="chrome-id",
                    generation=3,
                    element_index="0",
                    key="enter",
                ),
                expectation=DesktopExpectation(
                    DesktopExpectationKind.SEARCH_SUBMITTED,
                    "OpenAI",
                ),
            ),
            DesktopDecision(DesktopDecisionKind.OBSERVE, "observe composer", app="claude-id"),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "type exact text",
                app="claude-id",
                action=DesktopAction(
                    DesktopActionType.TYPE_TEXT,
                    app="claude-id",
                    generation=6,
                    element_index="0",
                    text="总结",
                ),
                expectation=DesktopExpectation(
                    DesktopExpectationKind.FOCUSED_CONTAINS,
                    "总结",
                ),
            ),
            DesktopDecision(
                DesktopDecisionKind.DONE,
                "all spoken steps verified",
                app="claude-id",
                expectation=DesktopExpectation(
                    DesktopExpectationKind.FOCUSED_CONTAINS,
                    "总结",
                ),
            ),
        ]
    )
    driver = InventoryDriver(observations, inventory)
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert result.success
    assert [call[1] for call in driver.calls if call[0] == "execute"] == [
        "set_value",
        "press_key",
        "type_text",
    ]


def _guard_button_observation(*labels: str) -> DesktopObservation:
    elements = tuple(
        DesktopElement(
            str(index),
            label,
            "Button",
            selected=False,
            enabled=True,
            addressable=True,
            supported_actions=(DesktopElementAction.CLICK,),
        )
        for index, label in enumerate(labels)
    )
    return _dynamic_observation(
        "claude",
        1,
        "Claude",
        elements,
        local_window_id="window-a",
    )


def _guard_button_click(label: str) -> DesktopDecision:
    return DesktopDecision(
        DesktopDecisionKind.ACTION,
        f"attempt to click {label}",
        app="claude",
        action=DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="0",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            label,
        ),
    )


def test_current_claude_step_blocks_observing_a_future_chrome_step() -> None:
    inventory = [
        {
            "app": "claude-id",
            "display_name": "Claude",
            "process_name": "claude.exe",
        },
        {
            "app": "chrome-id",
            "display_name": "Google Chrome",
            "process_name": "chrome.exe",
        },
    ]
    planner = SequencePlanner(
        [
            DesktopDecision(
                DesktopDecisionKind.OBSERVE,
                "incorrectly skip to the future browser step",
                app="chrome-id",
            )
        ]
    )
    driver = InventoryDriver([], inventory)
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(
        "In Claude, click Alpha, then in Chrome click Beta"
    )

    assert result.success is False
    assert result.error_code == "EXPLICIT_STEP_WINDOW_MISMATCH"
    assert not any(call[0] == "observe" for call in driver.calls)
    assert not any(call[0] == "execute" for call in driver.calls)


def test_negated_chrome_is_removed_before_resuming_trusted_context() -> None:
    inventory = [
        {
            "app": "claude-id",
            "display_name": "Claude",
            "process_name": "claude.exe",
        },
        {
            "app": "chrome-id",
            "display_name": "Google Chrome",
            "process_name": "chrome.exe",
        },
    ]
    planner = AppsRecordingPlanner(
        [DesktopDecision(DesktopDecisionKind.FAIL, "no action needed for this guard")]
    )
    driver = InventoryDriver([], inventory)
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )
    controller._trusted_app_context = "chrome-id"
    controller._trusted_window_id = "hwnd:chrome"

    result = controller.run("click Alpha, but do not operate Chrome")

    assert result.success is False
    assert result.error_code == "PLANNER_NO_SAFE_STEP"
    assert "chrome-id" not in planner.app_payloads[0]
    assert controller._trusted_context() is None
    assert not any(call[0] == "observe" for call in driver.calls)
    assert not any(call[0] == "execute" for call in driver.calls)


@pytest.mark.parametrize(
    "task",
    [
        "do not click Alpha",
        "不要点击 Alpha",
    ],
)
def test_pure_negation_fails_before_any_observation(task: str) -> None:
    planner = SequencePlanner([])
    driver = FakeDriver([])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert result.success is False
    assert result.error_code == "NO_POSITIVE_USER_ACTION"
    assert planner.calls == []
    assert not any(call[0] == "observe" for call in driver.calls)
    assert not any(call[0] == "execute" for call in driver.calls)


def test_generic_bridge_is_blocked_when_every_other_mutation_is_negated() -> None:
    initial = _guard_button_observation("Menu")
    planner = SequencePlanner(
        [_observe_decision(), _guard_button_click("Menu")]
    )
    driver = FakeDriver([initial])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(
        "click Open, but do not scroll/expand/click anything else"
    )

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert [call for call in driver.calls if call[0] == "observe"] == [
        ("observe", "claude")
    ]
    assert not any(call[0] == "execute" for call in driver.calls)


@pytest.mark.parametrize(
    "task",
    [
        "In Claude, open Beta, but do not click Alpha",
        "In Claude, click Alpha, then open Beta",
    ],
)
def test_visual_point_click_requires_one_unnegated_current_target(task: str) -> None:
    initial = _loop_visual_observation(1, "guarded-point")
    driver = FakeDriver([initial])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner(
            [_observe_decision(), _loop_visual_click_decision(1, 20, 30)]
        ),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert [call for call in driver.calls if call[0] == "observe"] == [
        ("observe", "claude")
    ]
    assert not any(call[0] == "execute" for call in driver.calls)


@pytest.mark.parametrize(
    "extra_action_type",
    [DesktopActionType.CLICK, DesktopActionType.SCROLL],
)
def test_extra_visual_mutation_is_blocked_after_all_spoken_steps(
    extra_action_type: DesktopActionType,
) -> None:
    def observation(generation: int, *, selected: bool) -> DesktopObservation:
        alpha = DesktopElement(
            "0",
            "Alpha",
            "Button",
            selected=selected,
            enabled=True,
            addressable=True,
            supported_actions=(DesktopElementAction.CLICK,),
        )
        viewport = DesktopElement(
            "2",
            "Visual screenshot viewport",
            "VisualViewport",
            plane=ElementPlane.CONTROL,
            visual_ocr=True,
            supported_actions=(
                DesktopElementAction.CLICK,
                DesktopElementAction.SCROLL,
            ),
            scroll_axes=("vertical",),
        )
        return _dynamic_observation(
            "claude",
            generation,
            "Claude",
            (alpha, viewport),
            local_window_id="window-a",
            screenshot_png=f"completed:{generation}:{selected}".encode(),
        )

    if extra_action_type == DesktopActionType.CLICK:
        extra_action = DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=3,
            element_index="2",
            x=20,
            y=30,
        )
    else:
        extra_action = DesktopAction(
            DesktopActionType.SCROLL,
            app="claude",
            generation=3,
            element_index="2",
            direction="down",
            pages=1.0,
        )
    planner = SequencePlanner(
        [
            _observe_decision(),
            _selection_action("Alpha", 1),
            DesktopDecision(
                DesktopDecisionKind.ACTION,
                "attempt one extra visual mutation",
                app="claude",
                action=extra_action,
                expectation=DesktopExpectation(
                    DesktopExpectationKind.LAST_ACTION_VERIFIED
                ),
            ),
        ]
    )
    driver = FakeDriver(
        [
            observation(1, selected=False),
            observation(2, selected=False),
            observation(3, selected=True),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run("In Claude, select Alpha")

    assert result.success is False
    assert result.error_code == "ACTION_AFTER_USER_STEPS_COMPLETE"
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "click", 2)
    ]


@pytest.mark.parametrize(
    ("task", "expected_error"),
    [
        ("if Alpha is visible, click Open", "CONDITIONAL_ACTION_UNSUPPORTED"),
        ("click Open unless Alpha is visible", "CONDITIONAL_ACTION_UNSUPPORTED"),
        ("the page says click Open", "REPORTED_ACTION_UNSUPPORTED"),
        ("he said click Open", "REPORTED_ACTION_UNSUPPORTED"),
        ("他说点击 Open", "REPORTED_ACTION_UNSUPPORTED"),
        ("do not, click Open", "NEGATED_ACTION_UNSUPPORTED"),
        ("do not; click Open", "NEGATED_ACTION_UNSUPPORTED"),
        ("do not… click Open", "NEGATED_ACTION_UNSUPPORTED"),
    ],
)
def test_unsupported_control_context_fails_before_driver_start(
    task: str,
    expected_error: str,
) -> None:
    router = _miss_router()
    planner = SequencePlanner([])
    driver = FakeDriver([])
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert result.success is False
    assert result.error_code == expected_error
    assert router.calls == []
    assert planner.calls == []
    assert driver.calls == []


@pytest.mark.parametrize(
    "task",
    [
        "Open Claude, after that click Chat",
        "打开 Claude，之后点击 Chat",
        "打开 Claude 并进入 Chat",
        "打开 Claude 再进入 Chat",
        "打开 Claude 接着进入 Chat",
        "打开 Claude进入 Chat",
    ],
)
def test_spoken_sequential_connectors_reach_desktop_routing(task: str) -> None:
    router = _miss_router()
    driver = FakeDriver([])
    controller = DesktopAgentLoopController(
        native_router=router,
        driver=driver,
        planner=SequencePlanner([_observe_decision()]),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert result.error_code != "CONDITIONAL_ACTION_UNSUPPORTED"
    assert router.calls == [task]
    assert ("start",) in driver.calls


@pytest.mark.parametrize(
    ("task", "expected_error", "expected_observes"),
    [
        ('In Claude, type "click Menu" into Message', "ACTION_NOT_BOUND_TO_TASK", 1),
        ("In Claude, click Alpha next to Menu", "ACTION_NOT_BOUND_TO_TASK", 1),
        ("In Claude, click Alpha to show Menu", "ACTION_NOT_BOUND_TO_TASK", 1),
        ("In Claude, click Alpha except Menu", "ACTION_NOT_BOUND_TO_TASK", 1),
        ("In Claude, click Alpha rather than Menu", "ACTION_NOT_BOUND_TO_TASK", 1),
        (
            "In Claude, click Alpha; later click Menu",
            "UNPARSED_ACTION_UNSUPPORTED",
            0,
        ),
        (
            "In Claude, click Alpha, afterwards click Menu",
            "UNPARSED_ACTION_UNSUPPORTED",
            0,
        ),
    ],
)
def test_generic_bridge_rejects_non_authorizing_target_mentions(
    task: str,
    expected_error: str,
    expected_observes: int,
) -> None:
    initial = _guard_button_observation("Menu")
    driver = FakeDriver([initial])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner(
            [_observe_decision(), _guard_button_click("Menu")]
        ),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert result.success is False
    assert result.error_code == expected_error
    assert len([call for call in driver.calls if call[0] == "observe"]) == (
        expected_observes
    )
    assert not any(call[0] == "execute" for call in driver.calls)


def test_visual_type_with_authored_outcome_cannot_use_last_action_verified() -> None:
    initial = _visual_input_observation(
        1,
        "outcome-guard",
        (DesktopElementAction.CLICK, DesktopElementAction.TYPE_TEXT),
    )
    driver = FakeDriver([initial])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner(
            [_observe_decision(), _visual_type_decision(1, "Alpha")]
        ),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(
        'In Claude, type "Alpha", which should show Results'
    )

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert [call for call in driver.calls if call[0] == "observe"] == [
        ("observe", "claude")
    ]
    assert not any(call[0] == "execute" for call in driver.calls)


def _unmentioned_target_observation(
    label: str,
    action_type: DesktopActionType,
) -> DesktopObservation:
    if action_type == DesktopActionType.CLICK:
        element = DesktopElement(
            "0",
            label,
            "Button",
            selected=False,
            enabled=True,
            addressable=True,
            supported_actions=(DesktopElementAction.CLICK,),
        )
    elif action_type == DesktopActionType.PERFORM_SECONDARY_ACTION:
        element = DesktopElement(
            "0",
            label,
            "TreeItem",
            enabled=True,
            addressable=True,
            supported_actions=(DesktopElementAction.EXPAND,),
            expand_collapse_state="collapsed",
        )
    else:
        assert action_type == DesktopActionType.SCROLL
        element = DesktopElement(
            "0",
            label,
            "ScrollBar",
            enabled=True,
            addressable=True,
            supported_actions=(DesktopElementAction.SCROLL,),
            scroll_axes=("vertical",),
        )
    return _dynamic_observation(
        "claude",
        1,
        "Claude",
        (element,),
        local_window_id="window-a",
    )


def _unmentioned_mutation_decision(
    label: str,
    action_type: DesktopActionType,
) -> DesktopDecision:
    if action_type == DesktopActionType.CLICK:
        action = DesktopAction(
            DesktopActionType.CLICK,
            app="claude",
            generation=1,
            element_index="0",
        )
        expectation = DesktopExpectation(
            DesktopExpectationKind.ELEMENT_SELECTED,
            label,
        )
    elif action_type == DesktopActionType.PERFORM_SECONDARY_ACTION:
        action = DesktopAction(
            DesktopActionType.PERFORM_SECONDARY_ACTION,
            app="claude",
            generation=1,
            element_index="0",
            action_name="expand",
        )
        expectation = DesktopExpectation(
            DesktopExpectationKind.LAST_ACTION_VERIFIED
        )
    else:
        assert action_type == DesktopActionType.SCROLL
        action = DesktopAction(
            DesktopActionType.SCROLL,
            app="claude",
            generation=1,
            element_index="0",
            direction="down",
            pages=1.0,
        )
        expectation = DesktopExpectation(
            DesktopExpectationKind.LAST_ACTION_VERIFIED
        )
    return DesktopDecision(
        DesktopDecisionKind.ACTION,
        f"attempt an unmentioned {action_type.value} on {label}",
        app="claude",
        action=action,
        expectation=expectation,
    )


@pytest.mark.parametrize(
    ("task", "action_type", "label"),
    [
        (
            "click Alpha; afterwards click it",
            DesktopActionType.CLICK,
            "Beta",
        ),
        (
            "click Alpha; afterwards scroll down",
            DesktopActionType.SCROLL,
            "Results",
        ),
        (
            "click Alpha; later expand it",
            DesktopActionType.PERFORM_SECONDARY_ACTION,
            "Beta",
        ),
    ],
)
def test_deferred_pronoun_clause_blocks_unmentioned_first_planner_action(
    task: str,
    action_type: DesktopActionType,
    label: str,
) -> None:
    planner = SequencePlanner(
        [
            _observe_decision(),
            _unmentioned_mutation_decision(label, action_type),
        ]
    )
    driver = FakeDriver(
        [_unmentioned_target_observation(label, action_type)]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert result.success is False
    assert result.error_code == "UNPARSED_ACTION_UNSUPPORTED"
    assert planner.calls == []
    assert driver.calls == []


@pytest.mark.parametrize(
    ("task", "action_type", "label"),
    [
        (
            "click Alpha, but there is no need to click it",
            DesktopActionType.CLICK,
            "Beta",
        ),
        (
            "click Alpha, but there is no need to click it",
            DesktopActionType.PERFORM_SECONDARY_ACTION,
            "Results",
        ),
        (
            "点击 Alpha，但不必点击它",
            DesktopActionType.CLICK,
            "Results",
        ),
        (
            "点击 Alpha，但不必滚动它",
            DesktopActionType.SCROLL,
            "Beta",
        ),
        (
            "click Alpha, but skip clicking it",
            DesktopActionType.CLICK,
            "Results",
        ),
        (
            "click Alpha, but skip clicking it",
            DesktopActionType.PERFORM_SECONDARY_ACTION,
            "Beta",
        ),
    ],
)
def test_pronoun_negation_blocks_unmentioned_planner_mutation(
    task: str,
    action_type: DesktopActionType,
    label: str,
) -> None:
    planner = SequencePlanner(
        [
            _observe_decision(),
            _unmentioned_mutation_decision(label, action_type),
        ]
    )
    driver = FakeDriver(
        [_unmentioned_target_observation(label, action_type)]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert [call for call in driver.calls if call[0] == "observe"] == [
        ("observe", "claude")
    ]
    assert not any(call[0] == "execute" for call in driver.calls)


def _semantic_search_observation(
    generation: int,
    *,
    value: str = "",
) -> DesktopObservation:
    search = DesktopElement(
        "0",
        "Search",
        "Edit",
        value=value,
        value_observed=True,
        focused=True,
        editable=True,
        enabled=True,
        addressable=True,
        automation_id="SearchBox",
        supported_actions=(
            DesktopElementAction.TYPE_TEXT,
            DesktopElementAction.PRESS_KEY,
        ),
    )
    return _dynamic_observation(
        "claude",
        generation,
        "Claude",
        (search,),
        local_window_id="window-a",
    )


def _semantic_query_decision(
    action_type: DesktopActionType,
    *,
    generation: int = 1,
) -> DesktopDecision:
    assert action_type in {
        DesktopActionType.TYPE_TEXT,
        DesktopActionType.SET_VALUE,
    }
    action = (
        DesktopAction(
            DesktopActionType.TYPE_TEXT,
            app="claude",
            generation=generation,
            element_index="0",
            text="Alpha",
        )
        if action_type == DesktopActionType.TYPE_TEXT
        else DesktopAction(
            DesktopActionType.SET_VALUE,
            app="claude",
            generation=generation,
            element_index="0",
            value="Alpha",
        )
    )
    return DesktopDecision(
        DesktopDecisionKind.ACTION,
        "put the exact query in the semantic SearchBox",
        app="claude",
        action=action,
        expectation=DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            "Alpha",
        ),
    )


def _semantic_search_enter_decision(generation: int = 3) -> DesktopDecision:
    return DesktopDecision(
        DesktopDecisionKind.ACTION,
        "submit the exact semantic search query",
        app="claude",
        action=DesktopAction(
            DesktopActionType.PRESS_KEY,
            app="claude",
            generation=generation,
            element_index="0",
            key="enter",
        ),
        expectation=DesktopExpectation(
            DesktopExpectationKind.SEARCH_SUBMITTED,
            "Alpha",
        ),
    )


@pytest.mark.parametrize(
    "task",
    [
        "search Alpha but do not submit",
        "search Alpha but skip pressing Enter",
        "search Alpha but do not hit Enter",
        "search Alpha but avoid pressing Enter",
    ],
)
def test_semantic_search_key_negation_allows_fill_but_blocks_enter(
    task: str,
) -> None:
    planner = SequencePlanner(
        [
            _observe_decision(),
            _semantic_query_decision(DesktopActionType.SET_VALUE),
            _semantic_search_enter_decision(),
        ]
    )
    driver = FakeDriver(
        [
            _semantic_search_observation(1),
            _semantic_search_observation(2),
            _semantic_search_observation(3, value="Alpha"),
        ]
    )
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert [call[1] for call in driver.calls if call[0] == "execute"] == [
        "set_value"
    ]


@pytest.mark.parametrize(
    "action_type",
    [DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE],
)
@pytest.mark.parametrize(
    "task",
    [
        "search Alpha but don't type anything",
        "search Alpha but do not enter it",
        "search Alpha but leave field empty",
        "open Alpha but do not search",
        "打开 Alpha，但不要用搜索框",
    ],
)
def test_semantic_search_mutation_negation_blocks_type_and_set_value(
    task: str,
    action_type: DesktopActionType,
) -> None:
    driver = FakeDriver([_semantic_search_observation(1)])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner(
            [_observe_decision(), _semantic_query_decision(action_type)]
        ),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert [call for call in driver.calls if call[0] == "observe"] == [
        ("observe", "claude")
    ]
    assert not any(call[0] == "execute" for call in driver.calls)


@pytest.mark.parametrize(
    "task",
    [
        "search Alpha but don't type anything",
        "search Alpha but do not enter it",
        "search Alpha but leave field empty",
        "open Alpha but do not search",
        "打开 Alpha，但不要用搜索框",
    ],
)
def test_rendered_search_mutation_negation_blocks_armed_viewport_type(
    task: str,
) -> None:
    initial = _visual_input_observation(
        1,
        "negated-rendered-query",
        (DesktopElementAction.CLICK, DesktopElementAction.TYPE_TEXT),
    )
    driver = FakeDriver([initial])
    controller = DesktopAgentLoopController(
        native_router=_miss_router(),
        driver=driver,
        planner=SequencePlanner(
            [_observe_decision(), _visual_type_decision(1, "Alpha")]
        ),
        safety=DesktopSafetyPolicy("local_unrestricted"),
    )

    result = controller.run(task)

    assert result.success is False
    assert result.error_code == "ACTION_NOT_BOUND_TO_TASK"
    assert [call for call in driver.calls if call[0] == "observe"] == [
        ("observe", "claude")
    ]
    assert not any(call[0] == "execute" for call in driver.calls)
