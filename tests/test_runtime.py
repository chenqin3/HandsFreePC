from __future__ import annotations

import threading
import time
from dataclasses import replace

import handsfree_pc.runtime as runtime_module
from handsfree_pc.computer_control import ComputerControlResult
from handsfree_pc.models import (
    Action,
    ActionType,
    ExecutionResult,
    FeedbackMode,
    Plan,
    RiskLevel,
    RuntimeState,
)
from handsfree_pc.runtime import VoiceRuntime, _merge_control_phrase_transcript
from handsfree_pc.session import SessionState, WorkerState


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
        self.messages = []

    def speak(self, text) -> None:
        self.messages.append(text)


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


class FakeController:
    def __init__(self, responses=None, gate=None) -> None:
        self.calls = []
        self.responses = list(responses or [])
        self.gate = gate
        self.closed = False
        self.cancelled = False
        self.session_id = "fake-session"

    def run(self, instruction, *, cancel_event=None):
        self.calls.append(instruction)
        if self.gate is not None and len(self.calls) == 1:
            self.gate.wait(timeout=2)
        if cancel_event is not None and cancel_event.is_set():
            return ComputerControlResult(False, "cancelled", cancelled=True)
        if self.responses:
            return self.responses.pop(0)
        return ComputerControlResult(True, "verified completion", session_id=self.session_id)

    execute = run

    def cancel(self):
        self.cancelled = True
        return True

    def close(self):
        self.closed = True


def enable_computer_control(settings) -> None:
    settings.execution.dry_run = False
    settings.privacy.allow_cloud_planner = True
    settings.computer_control.enabled = True
    settings.computer_control.allow_screen_context_to_cloud = True


def wait_until(predicate, *, timeout=2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        time.sleep(0.01)


def build_runtime(settings):
    executor = FakeExecutor()
    feedback = FakeFeedback()
    return VoiceRuntime(settings, executor, feedback=feedback), executor, feedback


def test_continuous_session_queues_only_at_over_and_stays_active(settings) -> None:
    enable_computer_control(settings)
    controller = FakeController()
    feedback = FakeFeedback()
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=feedback,
        controller=controller,
    )
    try:
        assert runtime.handle_session_text("开始语音操作").success
        assert runtime.session_state == SessionState.ACTIVE

        pending = runtime.handle_session_text("打开记事本")
        assert pending.message == "已记录，等待 over"
        assert controller.calls == []

        queued = runtime.handle_session_text("over")
        assert queued.success
        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls == ["打开记事本"]
        assert runtime.session_state == SessionState.ACTIVE
    finally:
        runtime.stop()


def test_control_phrase_transcript_overlap_preserves_real_command_text() -> None:
    cases = [
        ("开始语音操作", "操作", "开始语音操作"),
        ("开始语音操作", "语音操作", "开始语音操作"),
        ("开始语音操作", "开始语音操作", "开始语音操作"),
        (
            "开始语音操作",
            r"操作，打开 C:\My Folder 里的 Design Review",
            r"开始语音操作 打开 C:\My Folder 里的 Design Review",
        ),
        ("确认执行", "执行", "确认执行"),
        ("取消所有操作", "所有操作", "取消所有操作"),
        ("结束语音操作", "语音操作", "结束语音操作"),
    ]

    for matched, transcript, expected in cases:
        assert _merge_control_phrase_transcript(matched, transcript) == expected


def test_slow_wake_preserves_exact_path_in_queued_controller_prompt(settings) -> None:
    enable_computer_control(settings)
    controller = FakeController()
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=FakeFeedback(),
        controller=controller,
    )
    try:
        merged = _merge_control_phrase_transcript(
            "开始语音操作",
            r"操作，打开 C:\My Folder 里的 Design Review over",
        )
        runtime.handle_session_text(merged)
        assert runtime.command_worker.drain(timeout=2)

        assert controller.calls == [r"打开 C:\My Folder 里的 Design Review"]
    finally:
        runtime.stop()


def test_feedback_mode_switch_is_local_with_or_without_over(settings) -> None:
    enable_computer_control(settings)
    controller = FakeController()
    feedback = FakeFeedback()
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=feedback,
        controller=controller,
    )
    try:
        runtime.handle_session_text("开始语音操作")

        assert runtime.handle_session_text("切换到语音反馈").success
        assert feedback.mode == FeedbackMode.VOICE
        assert controller.calls == []
        assert runtime.handle_session_text("切换到屏幕反馈 over").success
        assert feedback.mode == FeedbackMode.OVERLAY
        assert controller.calls == []
    finally:
        runtime.stop()


def test_spoken_feedback_is_deferred_until_microphone_boundary(settings) -> None:
    enable_computer_control(settings)
    feedback = FakeFeedback()
    feedback.mode = FeedbackMode.VOICE
    runtime = VoiceRuntime(settings, FakeExecutor(), feedback=feedback, controller=FakeController())

    class Source:
        def __init__(self):
            self.drains = 0

        def drain(self):
            self.drains += 1

    class Speech:
        def __init__(self):
            self.source = Source()
            self.resets = 0

        def reset_control_detector(self):
            self.resets += 1

    speech = Speech()
    try:
        runtime._emit_continuous("队列完成", kind="success")
        assert feedback.speaker.messages == []
        assert runtime._voice_feedback_event.is_set()

        runtime._flush_voice_feedback(speech)

        assert feedback.speaker.messages == ["队列完成"]
        assert speech.source.drains == 1
        assert speech.resets == 1
        assert not runtime._voice_feedback_event.is_set()
    finally:
        runtime.stop()


def test_spoken_feedback_boundary_plays_only_highest_priority_latest_message(settings) -> None:
    enable_computer_control(settings)
    feedback = FakeFeedback()
    feedback.mode = FeedbackMode.VOICE
    runtime = VoiceRuntime(settings, FakeExecutor(), feedback=feedback, controller=FakeController())

    class Speech:
        class Source:
            def drain(self):
                pass

        source = Source()

        def reset_control_detector(self):
            pass

    try:
        runtime._emit_continuous("低优先级识别", kind="recognized")
        runtime._emit_continuous("普通完成", kind="success")
        runtime._emit_continuous("失败说明", kind="error")
        runtime._emit_continuous("需要确认的准确动作", kind="confirm")

        runtime._flush_voice_feedback(Speech())

        assert feedback.speaker.messages == ["需要确认的准确动作"]
        assert not runtime._voice_feedback_event.is_set()
    finally:
        runtime.stop()


def test_voice_confirmation_is_rejected_until_exact_action_is_spoken(settings) -> None:
    enable_computer_control(settings)
    controller = FakeController(
        responses=[
            ComputerControlResult(False, "NEEDS_CONFIRMATION: send the prepared form"),
            ComputerControlResult(True, "VERIFIED_COMPLETION: prepared form sent"),
        ]
    )
    feedback = FakeFeedback()
    feedback.mode = FeedbackMode.VOICE
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=feedback,
        controller=controller,
    )

    class Speech:
        class Source:
            def drain(self):
                pass

        source = Source()

        def reset_control_detector(self):
            pass

    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("提交表单 over")
        wait_until(lambda: runtime.command_worker.state == WorkerState.PAUSED)

        premature = runtime.handle_session_text("确认执行")
        assert not premature.success
        assert controller.calls == ["提交表单"]
        assert not runtime._pending_controller_confirmation_announced
        assert runtime._pending_controller_confirmation_started_at == 0

        runtime._flush_voice_feedback(Speech())
        assert any("send the prepared form" in text for text in feedback.speaker.messages)
        assert runtime._pending_controller_confirmation_announced
        assert runtime._pending_controller_confirmation_started_at > 0

        assert runtime.handle_session_text("确认执行").success
        assert runtime.command_worker.drain(timeout=2)
        assert "send the prepared form" in controller.calls[1]
    finally:
        runtime.stop()


def test_failed_voice_feedback_never_unlocks_confirmation(settings) -> None:
    enable_computer_control(settings)
    controller = FakeController(
        responses=[ComputerControlResult(False, "NEEDS_CONFIRMATION: send the private form")]
    )
    feedback = FakeFeedback()
    feedback.mode = FeedbackMode.VOICE

    class FailedSpeaker(FakeSpeaker):
        last_error = RuntimeError("SAPI unavailable")

        def speak(self, text):
            self.messages.append(text)
            return False

    feedback.speaker = FailedSpeaker()
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=feedback,
        controller=controller,
    )

    class Speech:
        class Source:
            def drain(self):
                pass

        source = Source()

        def reset_control_detector(self):
            pass

    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("提交表单 over")
        wait_until(lambda: runtime.command_worker.state == WorkerState.PAUSED)
        runtime._flush_voice_feedback(Speech())

        assert not runtime._pending_controller_confirmation_announced
        assert not runtime.handle_session_text("确认执行").success
        assert controller.calls == ["提交表单"]
        assert any("语音反馈失败" in text for text, _kwargs in feedback.events)
    finally:
        runtime.stop()


def test_failed_overlay_feedback_never_unlocks_confirmation(settings) -> None:
    enable_computer_control(settings)
    controller = FakeController(
        responses=[ComputerControlResult(False, "NEEDS_CONFIRMATION: send private form")]
    )
    feedback = FakeFeedback()

    def failed_emit(text, **kwargs):
        feedback.events.append((text, kwargs))
        return False

    feedback.emit = failed_emit
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=feedback,
        controller=controller,
    )
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("提交表单 over")
        wait_until(lambda: runtime.command_worker.state == WorkerState.PAUSED)

        assert not runtime._pending_controller_confirmation_announced
        assert not runtime.handle_session_text("确认执行").success
        assert controller.calls == ["提交表单"]
    finally:
        runtime.stop()


def test_continuous_session_records_while_first_job_executes_and_preserves_fifo(settings) -> None:
    enable_computer_control(settings)
    gate = threading.Event()
    controller = FakeController(gate=gate)
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=FakeFeedback(),
        controller=controller,
    )
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("第一条 over")
        wait_until(lambda: controller.calls == ["第一条"])

        runtime.handle_session_text("第二条 over 第三条 over")
        assert runtime.command_worker.pending_count == 2
        gate.set()
        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls == ["第一条", "第二条", "第三条"]
        assert runtime.session_state == SessionState.ACTIVE
    finally:
        gate.set()
        runtime.stop()


def test_end_session_discards_half_prompt_and_drains_queue(settings) -> None:
    enable_computer_control(settings)
    gate = threading.Event()
    controller = FakeController(gate=gate)
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=FakeFeedback(),
        controller=controller,
    )
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("已提交指令 over")
        wait_until(lambda: bool(controller.calls))
        runtime.handle_session_text("没有说完的半条")

        ended = runtime.handle_session_text("结束语音操作")
        assert ended.success
        assert runtime.session_state == SessionState.DRAINING
        assert not runtime.prompt_assembler.has_pending
        gate.set()
        wait_until(lambda: runtime.session_state == SessionState.ARMED)
        assert controller.closed
    finally:
        gate.set()
        runtime.stop()


def test_controller_confirmation_runs_before_later_fifo_jobs(settings) -> None:
    enable_computer_control(settings)
    controller = FakeController(
        responses=[
            ComputerControlResult(False, "NEEDS_CONFIRMATION: send the form"),
            ComputerControlResult(True, "confirmed action verified"),
            ComputerControlResult(True, "second action verified"),
        ]
    )
    feedback = FakeFeedback()
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=feedback,
        controller=controller,
    )
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("第一条 over 第二条 over")
        wait_until(lambda: runtime.command_worker.state == WorkerState.PAUSED)
        assert any("需要确认：send the form" in text for text, _kwargs in feedback.events)

        refused = runtime.handle_session_text("继续队列")
        assert not refused.success
        assert runtime.command_worker.state == WorkerState.PAUSED
        assert controller.calls == ["第一条"]

        confirmed = runtime.handle_session_text("确认执行")
        assert confirmed.success
        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls[0] == "第一条"
        assert controller.calls[1].startswith("The user has explicitly confirmed")
        assert 'the JSON string "send the form"' in controller.calls[1]
        assert controller.calls[2] == "第二条"
    finally:
        runtime.stop()


def test_end_session_waits_for_confirmation_on_last_job(settings) -> None:
    enable_computer_control(settings)
    controller = FakeController(
        responses=[
            ComputerControlResult(False, "NEEDS_CONFIRMATION: send the final form"),
            ComputerControlResult(True, "VERIFIED_COMPLETION: final form sent"),
        ]
    )
    feedback = FakeFeedback()
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=feedback,
        controller=controller,
    )
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("提交表单 over")
        runtime.handle_session_text("结束语音操作")
        wait_until(lambda: runtime.command_worker.state == WorkerState.PAUSED)

        assert runtime.session_state == SessionState.DRAINING
        assert not controller.closed
        assert runtime.handle_session_text("确认执行").success
        wait_until(lambda: runtime.session_state == SessionState.ARMED)
        assert controller.closed
        assert controller.calls[0] == "提交表单"
        assert "send the final form" in controller.calls[1]
    finally:
        runtime.stop()


def test_draining_failure_never_ends_with_success_feedback(settings) -> None:
    enable_computer_control(settings)
    controller = FakeController(responses=[ComputerControlResult(False, "FAILURE: target missing")])
    feedback = FakeFeedback()
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=feedback,
        controller=controller,
    )
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("失败任务 over")
        runtime.handle_session_text("结束语音操作")
        wait_until(lambda: runtime.session_state == SessionState.ARMED)

        assert feedback.events[-1][0] == "本轮队列已结束，但最后一条未完成"
        assert feedback.events[-1][1]["kind"] == "error"
    finally:
        runtime.stop()


def test_emergency_stop_discards_old_controller_session(settings) -> None:
    enable_computer_control(settings)
    first = FakeController()
    second = FakeController()
    controllers = iter([first, second])
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=FakeFeedback(),
        controller_factory=lambda: next(controllers),
    )
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("第一轮 over")
        assert runtime.command_worker.drain(timeout=2)

        stopped = runtime.handle_session_text("取消所有操作")
        assert stopped.success
        assert first.cancelled
        assert first.closed
        assert runtime._controller is None

        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("第二轮 over")
        assert runtime.command_worker.drain(timeout=2)
        assert first.calls == ["第一轮"]
        assert second.calls == ["第二轮"]
    finally:
        runtime.stop()


def test_late_confirmation_result_after_emergency_cannot_restore_pending_action(settings) -> None:
    enable_computer_control(settings)
    started = threading.Event()
    release = threading.Event()

    class StubbornController(FakeController):
        def run(self, instruction, *, cancel_event=None):
            del cancel_event
            self.calls.append(instruction)
            started.set()
            assert release.wait(timeout=2)
            return ComputerControlResult(False, "NEEDS_CONFIRMATION: stale destructive action")

    controller = StubbornController()
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=FakeFeedback(),
        controller=controller,
    )
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("第一轮 over")
        assert started.wait(timeout=2)

        stopped = runtime.handle_session_text("取消所有操作")
        assert stopped.success
        release.set()
        assert runtime.command_worker.drain(timeout=2)

        assert runtime._pending_controller_confirmation is None
        assert not runtime.handle_session_text("确认执行").success
    finally:
        release.set()
        runtime.stop()


def test_continuous_cancellation_phrase_uses_emergency_stop_path(settings) -> None:
    enable_computer_control(settings)
    gate = threading.Event()
    controller = FakeController(gate=gate)
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=FakeFeedback(),
        controller=controller,
    )
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("当前任务 over")
        wait_until(lambda: controller.calls == ["当前任务"])
        runtime.handle_session_text("排队任务 over")

        outcome = runtime.handle_session_text("取消操作")

        assert outcome.success
        assert outcome.state == RuntimeState.PAUSED
        assert runtime.command_worker.pending_count == 0
        assert controller.cancelled
        assert controller.closed
    finally:
        gate.set()
        runtime.stop()


def test_late_failure_from_stopped_session_cannot_pause_new_session_queue(settings) -> None:
    enable_computer_control(settings)
    started = threading.Event()
    release = threading.Event()

    class StubbornFailureController(FakeController):
        def run(self, instruction, *, cancel_event=None):
            del cancel_event
            self.calls.append(instruction)
            started.set()
            assert release.wait(timeout=2)
            return ComputerControlResult(False, "FAILURE: stale failure")

    first = StubbornFailureController()
    second = FakeController()
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=FakeFeedback(),
        controller=first,
        controller_factory=lambda: second,
    )
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("旧会话任务 over")
        assert started.wait(timeout=2)

        runtime.handle_session_text("取消所有操作")
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("新会话任务 over")
        release.set()

        assert runtime.command_worker.drain(timeout=2)
        assert first.calls == ["旧会话任务"]
        assert second.calls == ["新会话任务"]
        assert runtime.command_worker.state == WorkerState.IDLE
    finally:
        release.set()
        runtime.stop()


def test_continuous_confirmation_timeout_cancels_session_and_queued_work(settings) -> None:
    enable_computer_control(settings)
    controller = FakeController(
        responses=[ComputerControlResult(False, "NEEDS_CONFIRMATION: send prepared form")]
    )
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=FakeFeedback(),
        controller=controller,
    )
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("提交表单 over")
        wait_until(lambda: runtime.command_worker.state == WorkerState.PAUSED)
        runtime.handle_session_text("稍后打开另一个窗口 over")
        assert runtime.command_worker.pending_count == 1
        assert runtime._pending_controller_confirmation_announced
        runtime._pending_controller_confirmation_started_at = (
            time.monotonic() - settings.execution.confirmation_timeout_seconds - 1
        )

        outcome = runtime.handle_session_text("确认执行")

        assert not outcome.success
        assert outcome.state == RuntimeState.PAUSED
        assert outcome.message.startswith("确认已超时")
        assert runtime._pending_controller_confirmation is None
        assert runtime.command_worker.pending_count == 0
        assert runtime.command_worker.unfinished_count == 0
        assert controller.closed
        assert controller.calls == ["提交表单"]
    finally:
        runtime.stop()


def test_emergency_stop_is_last_feedback_across_outcome_race(settings) -> None:
    enable_computer_control(settings)

    class BlockingOutcomeFeedback(FakeFeedback):
        def __init__(self):
            super().__init__()
            self.mode = FeedbackMode.VOICE
            self.outcome_entered = threading.Event()
            self.release_outcome = threading.Event()

        def emit(self, text, **kwargs):
            super().emit(text, **kwargs)
            if "Codex 报告" in text and "已完成" in text:
                self.outcome_entered.set()
                assert self.release_outcome.wait(timeout=2)

    feedback = BlockingOutcomeFeedback()
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=feedback,
        controller=FakeController(),
    )
    stop_outcomes = []
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("会产生竞态的任务 over")
        assert feedback.outcome_entered.wait(timeout=2)

        stopper = threading.Thread(
            target=lambda: stop_outcomes.append(runtime.handle_session_text("取消所有操作"))
        )
        stopper.start()
        time.sleep(0.02)
        assert stopper.is_alive()
        feedback.release_outcome.set()
        stopper.join(timeout=2)

        assert not stopper.is_alive()
        assert stop_outcomes[0].success
        texts = [text for text, _kwargs in feedback.events]
        completion = next(text for text in texts if "Codex 报告" in text and "已完成" in text)
        assert texts.index(completion) < texts.index(
            next(text for text in texts if "已请求立即停止" in text)
        )
        with runtime._voice_feedback_lock:
            queued_voice = [text for _kind, text in runtime._voice_feedback]
        assert queued_voice == [next(text for text in texts if "已请求立即停止" in text)]
    finally:
        feedback.release_outcome.set()
        runtime.stop()


def test_dry_run_is_never_reported_as_real_completion(settings) -> None:
    class DryExecutor:
        def execute_plan(self, plan):
            return [
                ExecutionResult(True, "would execute", action=action, evidence={"dry_run": True})
                for action in plan.actions
            ]

    feedback = FakeFeedback()
    runtime = VoiceRuntime(settings, DryExecutor(), feedback=feedback)
    runtime.state = RuntimeState.AWAKE

    outcome = runtime.handle_text("打开D盘", require_wake=False)

    assert outcome.success
    assert outcome.message == "模拟完成，未操作电脑"
    assert outcome.state == RuntimeState.ARMED
    assert feedback.events[-1][0] == "模拟完成，未操作电脑"


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
    settings.app.awake_timeout_seconds = 20
    runtime, executor, _ = build_runtime(settings)
    runtime.state = RuntimeState.AWAKE
    runtime.last_awake_at = time.monotonic() - settings.app.awake_timeout_seconds - 1

    outcome = runtime.handle_text("打开D盘", require_wake=False)

    assert outcome.state == RuntimeState.ARMED
    assert outcome.message == "唤醒已超时"
    assert executor.plans == []


def test_zero_awake_timeout_means_no_timeout(settings) -> None:
    settings.app.awake_timeout_seconds = 0
    runtime, executor, _ = build_runtime(settings)
    runtime.state = RuntimeState.AWAKE
    runtime.last_awake_at = time.monotonic() - 3600

    outcome = runtime.handle_text("打开D盘", require_wake=False)

    assert outcome.message != "唤醒已超时"
    assert executor.plans


def test_zero_awake_timeout_microphone_loop_still_listens(settings, monkeypatch) -> None:
    runtime, _executor, _feedback = build_runtime(settings)
    runtime.state = RuntimeState.AWAKE
    runtime.last_awake_at = time.monotonic()
    settings.app.awake_timeout_seconds = 0
    listen_timeouts = []

    class Speech:
        class Source:
            def drain(self):
                pass

        source = Source()

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def listen_utterance(self, *, timeout_seconds=None, interrupt_phrases=None):
            del interrupt_phrases
            listen_timeouts.append(timeout_seconds)
            runtime.stop_event.set()
            return b"audio"

        def transcribe(self, _audio):
            return ""

    monkeypatch.setattr(runtime_module, "LocalSpeechSession", Speech)

    runtime.run_microphone()

    assert listen_timeouts == [None]


def test_compatibility_microphone_merges_wake_match_with_preroll_transcript(
    settings, monkeypatch
) -> None:
    runtime, _executor, _feedback = build_runtime(settings)
    captured = []

    class Speech:
        class Source:
            def drain(self):
                pass

        source = Source()

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def wait_for_phrase(self, *, stop_event):
            del stop_event
            return "开始语音操作", b"preroll"

        def transcribe(self, _audio):
            runtime.stop_event.set()
            return r"操作，打开 C:\My Folder"

    def capture(text, *, require_wake=True):
        captured.append((text, require_wake))
        return None

    monkeypatch.setattr(runtime_module, "LocalSpeechSession", Speech)
    monkeypatch.setattr(runtime, "handle_text", capture)

    runtime.run_microphone()

    assert captured == [(r"开始语音操作 打开 C:\My Folder", True)]


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
