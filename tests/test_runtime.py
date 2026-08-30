from __future__ import annotations

import threading
import time
from dataclasses import replace

from handsfree_pc.models import (
    Action,
    ActionType,
    ExecutionResult,
    FeedbackMode,
    Plan,
    RiskLevel,
    RuntimeState,
)
from handsfree_pc.runtime import VoiceRuntime


class FakeExecutor:
    def __init__(self) -> None:
        self.plans = []

    def execute_plan(self, plan):
        self.plans.append(plan)
        return [ExecutionResult(True, "ok", action=action) for action in plan.actions]


class PreparingFakeExecutor(FakeExecutor):
    def prepare_plan(self, plan):
        return plan


class FakeSpeaker:
    def __init__(self) -> None:
        self.speaking = threading.Event()


class FakeFeedback:
    def __init__(self) -> None:
        self.mode = FeedbackMode.OVERLAY
        self.events = []
        self.mode_calls = []
        self.speaker = FakeSpeaker()

    def emit(self, text, **kwargs):
        self.events.append((text, kwargs))

    def set_mode(self, mode, *, allow_voice=True):
        self.mode = mode
        self.mode_calls.append((mode, allow_voice))

    def close(self):
        pass


def build_runtime(settings):
    executor = FakeExecutor()
    feedback = FakeFeedback()
    return VoiceRuntime(settings, executor, feedback=feedback), executor, feedback


def test_requires_wake_phrase(settings) -> None:
    runtime, executor, _ = build_runtime(settings)
    outcome = runtime.handle_text("打开D盘", require_wake=True)
    assert outcome.handled is False
    assert executor.plans == []


def test_wake_and_command_same_utterance(settings) -> None:
    runtime, executor, _ = build_runtime(settings)
    outcome = runtime.handle_text("现在开始语音操作 打开D盘", require_wake=True)
    assert outcome.handled is True
    assert outcome.state == RuntimeState.ARMED
    assert len(executor.plans) == 1


def test_codex_command_enters_own_dictation(settings) -> None:
    runtime, executor, _ = build_runtime(settings)
    outcome = runtime.handle_text(
        "现在开始语音操作 切换到codex app，打开演示项目下的语音设计对话，打开语音输入"
    )
    assert outcome.state == RuntimeState.DICTATION
    outcome = runtime.handle_text("请检查这个项目里的测试", require_wake=False)
    assert outcome.state == RuntimeState.DICTATION
    assert executor.plans[-1].actions[0].text == "请检查这个项目里的测试"


def test_dictation_control_prefix_sends(settings) -> None:
    executor = PreparingFakeExecutor()
    runtime = VoiceRuntime(settings, executor, feedback=FakeFeedback())
    runtime.state = RuntimeState.DICTATION
    outcome = runtime.handle_text("电脑发送提示", require_wake=False)
    assert outcome.state == RuntimeState.ARMED
    assert executor.plans[-1].actions[0].type.value == "send_prompt"


def test_negated_dictation_submit_is_typed_not_executed(settings) -> None:
    runtime, executor, _ = build_runtime(settings)
    runtime.state = RuntimeState.DICTATION

    outcome = runtime.handle_text("电脑不要发送提示", require_wake=False)

    assert outcome.state == RuntimeState.DICTATION
    assert executor.plans[-1].actions[0].type == ActionType.TYPE_TEXT
    assert executor.plans[-1].actions[0].text == "电脑不要发送提示"


def test_native_voice_waits_for_confirmation(settings) -> None:
    runtime, executor, _ = build_runtime(settings)
    runtime.state = RuntimeState.AWAKE
    outcome = runtime.handle_text("打开codex并使用应用内语音", require_wake=False)
    assert outcome.state == RuntimeState.CONFIRMING
    assert executor.plans == []
    confirmed = runtime.handle_text("确认执行", require_wake=False)
    assert confirmed.state == RuntimeState.PAUSED
    assert len(executor.plans) == 1


def test_confirmation_phrase_must_be_the_entire_utterance(settings) -> None:
    runtime, executor, _ = build_runtime(settings)
    runtime.state = RuntimeState.AWAKE
    requested = runtime.handle_text("打开codex并使用应用内语音", require_wake=False)
    assert requested.state == RuntimeState.CONFIRMING

    rejected = runtime.handle_text("不要确认执行", require_wake=False)

    assert rejected.state == RuntimeState.CONFIRMING
    assert executor.plans == []
    assert runtime.pending_plan is not None


def test_pause_action_changes_runtime_state(settings) -> None:
    runtime, executor, _ = build_runtime(settings)

    outcome = runtime.handle_text("暂停监听", require_wake=False)

    assert outcome.state == RuntimeState.PAUSED
    assert executor.plans[-1].actions[0].type == ActionType.PAUSE


def test_unexpected_planner_exception_does_not_escape_runtime(settings) -> None:
    class ExplodingPlanner:
        def plan(self, _command, *, context):
            del context
            raise RuntimeError("private prompt details")

    executor = FakeExecutor()
    runtime = VoiceRuntime(
        settings,
        executor,
        planner=ExplodingPlanner(),
        feedback=FakeFeedback(),
    )
    runtime.state = RuntimeState.AWAKE

    outcome = runtime.handle_text("无法由确定性解析器处理的请求", require_wake=False)

    assert outcome.state == RuntimeState.ARMED
    assert outcome.success is False
    assert outcome.message == "规划器发生内部异常"
    assert executor.plans == []


def test_confirmation_feedback_is_derived_from_actions_not_planner_summary(settings) -> None:
    runtime, executor, feedback = build_runtime(settings)
    plan = Plan(
        "harmless operation",
        [Action(ActionType.START_NATIVE_VOICE, app="codex")],
        risk=RiskLevel.CONFIRM,
        source="codex",
    )

    outcome = runtime._dispatch(plan, user_text="open voice")

    assert outcome.state == RuntimeState.CONFIRMING
    assert executor.plans == []
    confirmation = feedback.events[-1][0]
    assert "harmless operation" not in confirmation
    assert "codex" in confirmation
    assert "应用内语音" in confirmation


def test_native_voice_cannot_be_combined_with_feedback_mode(settings) -> None:
    runtime, executor, feedback = build_runtime(settings)
    plan = Plan(
        "switch mode then open microphone",
        [
            Action(ActionType.SET_FEEDBACK_MODE, feedback_mode=FeedbackMode.BOTH),
            Action(ActionType.START_NATIVE_VOICE, app="codex"),
        ],
        source="codex",
    )
    plan = runtime.safety.evaluate(plan)

    outcome = runtime._dispatch(plan, user_text="open voice")

    assert outcome.state == RuntimeState.ARMED
    assert outcome.success is False
    assert executor.plans == []
    assert feedback.mode_calls == []


def test_native_voice_failure_stays_paused_and_never_speaks(settings) -> None:
    class PartialFailureExecutor(FakeExecutor):
        def execute_plan(self, plan):
            return [
                ExecutionResult(True, "microphone may be open", action=plan.actions[0]),
                ExecutionResult(False, "later failure", action=plan.actions[1]),
            ]

    feedback = FakeFeedback()
    runtime = VoiceRuntime(settings, PartialFailureExecutor(), feedback=feedback)
    plan = Plan(
        "invalid defensive test",
        [
            Action(ActionType.START_NATIVE_VOICE, app="codex"),
            Action(ActionType.WAIT, seconds=0.1),
        ],
    )

    outcome = runtime._execute(plan)

    assert outcome.state == RuntimeState.PAUSED
    assert outcome.success is False
    assert all(event[1].get("allow_voice") is False for event in feedback.events)


def test_stop_is_global_and_wake_resumes(settings) -> None:
    runtime, _, _ = build_runtime(settings)
    runtime.state = RuntimeState.DICTATION
    assert runtime.handle_text("停止所有操作", require_wake=False).state == RuntimeState.PAUSED
    assert runtime.handle_text("现在开始语音操作", require_wake=False).state == RuntimeState.ARMED


def test_awake_timeout_returns_to_armed(settings) -> None:
    runtime, executor, _ = build_runtime(settings)
    runtime.state = RuntimeState.AWAKE
    runtime.last_awake_at = time.monotonic() - settings.app.awake_timeout_seconds - 1

    outcome = runtime.handle_text("打开D盘", require_wake=False)

    assert outcome.state == RuntimeState.ARMED
    assert outcome.message == "唤醒已超时"
    assert executor.plans == []


def test_confirmation_timeout_cancels_pending_plan(settings) -> None:
    runtime, executor, _ = build_runtime(settings)
    runtime.state = RuntimeState.AWAKE
    requested = runtime.handle_text("打开codex并使用应用内语音", require_wake=False)
    assert requested.state == RuntimeState.CONFIRMING
    runtime.confirmation_started_at = (
        time.monotonic() - settings.execution.confirmation_timeout_seconds - 1
    )

    outcome = runtime.handle_text("确认执行", require_wake=False)

    assert outcome.state == RuntimeState.ARMED
    assert outcome.message == "确认已超时"
    assert runtime.pending_plan is None
    assert executor.plans == []


def test_resolved_executable_requires_confirmation(settings) -> None:
    class ResolvingExecutor(FakeExecutor):
        def prepare_plan(self, plan):
            actions = [
                replace(action, path=r"C:\safe-test\installer.exe")
                if action.type == ActionType.OPEN_PATH
                else action
                for action in plan.actions
            ]
            return replace(plan, actions=actions)

    executor = ResolvingExecutor()
    feedback = FakeFeedback()
    runtime = VoiceRuntime(settings, executor, feedback=feedback)
    runtime.state = RuntimeState.AWAKE

    outcome = runtime.handle_text("打开C盘的safe-test文件夹里的installer", require_wake=False)

    assert outcome.state == RuntimeState.CONFIRMING
    assert executor.plans == []
    assert "installer.exe" in feedback.events[-1][0]
