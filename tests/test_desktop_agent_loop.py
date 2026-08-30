from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from handsfree_pc.desktop.agent_loop import (
    DesktopAgentLoopController,
    _explicitly_named_apps,
)
from handsfree_pc.desktop.native_skills import NativeRouteStatus, NativeSkillResult
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


def _observation(generation, text, *, title="Claude", elements=()):
    return DesktopObservation(
        app="claude",
        generation=generation,
        accessibility_text=text,
        window_title=title,
        elements=tuple(elements),
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
    assert "下一个明确步骤" in result.message
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
