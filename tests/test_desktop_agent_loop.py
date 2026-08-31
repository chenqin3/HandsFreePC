from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import pytest

from handsfree_pc.desktop.agent_loop import (
    DesktopAgentLoopController,
    _explicitly_named_apps,
    _unsupported_explicit_app_scopes,
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
    DesktopExpectation,
    DesktopExpectationKind,
    DesktopObservation,
)
from handsfree_pc.desktop.safety import DesktopSafetyPolicy
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
        self.executor = FakeNativeExecutor()
        self.safety = type(
            "FakeNativeSafety",
            (),
            {"evaluate": staticmethod(lambda plan, **_kwargs: plan)},
        )()

    def route(self, text):
        self.calls.append(text)
        return self.result


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


class ContextRecordingDriver(FakeDriver):
    def __init__(self, observations):
        super().__init__(observations)
        self.task_contexts = []
        self.profiles = {"claude": object(), "codex": object()}

    def set_task_context(self, task):
        self.task_contexts.append(task)


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

    assert _unsupported_explicit_app_scopes(task, ("codex", "claude")) == (
        "cloud storage",
    )


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
        '输入“打开记事本然后点击 Code”到 Prompt',
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
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "click", 2)
    ]


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
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "click", 2)
    ]


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
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "click", 2)
    ]


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
    router = FakeNativeRouter(
        NativeSkillResult(NativeRouteStatus.SUCCEEDED, "done", plan=plan)
    )
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
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "click", 2)
    ]
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
    assert [call for call in driver.calls if call[0] == "execute"] == [
        ("execute", "click", 3)
    ]


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
    router = FakeNativeRouter(
        NativeSkillResult(NativeRouteStatus.SUCCEEDED, "done", plan=plan)
    )

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
