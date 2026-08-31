from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import replace

import pytest

import handsfree_pc.runtime as runtime_module
from handsfree_pc.computer_control import ComputerControlResult
from handsfree_pc.desktop.agent_loop import DesktopAgentLoopController
from handsfree_pc.desktop.native_skills import NativeRouteStatus, NativeSkillResult
from handsfree_pc.diagnostics import Diagnostics, tail_events
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
from handsfree_pc.session import JobOutcome, QueuedCommand, SessionState, WorkerState


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


def pending_controller_confirmation_instruction(runtime: VoiceRuntime) -> str:
    challenge = runtime._pending_controller_confirmation_challenge
    assert challenge is not None
    return runtime._confirmation_instruction(challenge)


def pending_plan_confirmation_instruction(runtime: VoiceRuntime) -> str:
    challenge = runtime._plan_confirmation_challenge
    assert challenge is not None
    return runtime._confirmation_instruction(challenge)


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


def test_control_failure_overlay_and_log_show_bounded_stage_without_raw_content(settings) -> None:
    class FakeDiagnostics:
        def __init__(self) -> None:
            self.events = []

        def event(self, **kwargs) -> None:
            self.events.append(kwargs)

    feedback = FakeFeedback()
    feedback.mode = FeedbackMode.BOTH
    diagnostics = FakeDiagnostics()
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=feedback,
        diagnostics=diagnostics,
    )
    runtime._voice_session_id = "session-1"
    command = QueuedCommand(
        "private spoken prompt",
        sequence=2,
        session_id="session-1",
        command_id="command-2",
    )
    private_value = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
    try:
        runtime._on_control_outcome(
            JobOutcome(
                command,
                success=False,
                message=f"FAILURE: 本地安全策略阻止读取该界面：{private_value}",
            )
        )

        overlay = feedback.events[-1][0]
        assert "[observe_safety / OBSERVATION_SAFETY_BLOCKED]" in overlay
        assert "本地界面安全检查未通过" in overlay
        assert "handsfreepc.jsonl" in overlay
        assert private_value not in overlay
        assert diagnostics.events == [
            {
                "stage": "observe_safety",
                "error_code": "OBSERVATION_SAFETY_BLOCKED",
                "safe_message": "本地界面安全检查未通过",
                "level": "error",
                "session_id": "session-1",
                "command_id": "command-2",
                "sequence": 2,
            }
        ]
        with runtime._voice_feedback_lock:
            queued_voice = [text for _kind, text in runtime._voice_feedback]
        assert queued_voice == ["第 2 条在界面安全检查阶段失败，队列已暂停"]
        assert private_value not in queued_voice[0]
    finally:
        runtime.stop()


def test_structured_controller_diagnostic_survives_queue_boundary(settings) -> None:
    class FakeDiagnostics:
        def __init__(self) -> None:
            self.events = []

        def event(self, **kwargs) -> None:
            self.events.append(kwargs)

    enable_computer_control(settings)
    diagnostics = FakeDiagnostics()
    feedback = FakeFeedback()
    controller = FakeController(
        responses=[
            ComputerControlResult(
                False,
                "FAILURE: private UIA body that must not be surfaced",
                stage="reobserve",
                error_code="FRESH_OBSERVATION_FAILED",
                safe_message="动作后无法刷新目标应用的界面状态",
                exception_type="WindowsUiaDriverError",
                app="claude",
                generation=7,
            )
        ]
    )
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=feedback,
        controller=controller,
        diagnostics=diagnostics,
    )
    runtime._voice_session_id = "session-structured"
    command = QueuedCommand(
        "private spoken prompt",
        sequence=3,
        session_id="session-structured",
        command_id="command-structured",
    )
    try:
        outcome = runtime._run_queued_control(command, threading.Event())

        assert outcome.stage == "reobserve"
        assert outcome.error_code == "FRESH_OBSERVATION_FAILED"
        assert outcome.safe_message == "动作后无法刷新目标应用的界面状态"
        assert outcome.exception_type == "WindowsUiaDriverError"
        assert outcome.app == "claude"
        assert outcome.generation == 7

        runtime._on_control_outcome(outcome)

        assert diagnostics.events == [
            {
                "stage": "reobserve",
                "error_code": "FRESH_OBSERVATION_FAILED",
                "safe_message": "动作后无法刷新目标应用的界面状态",
                "level": "error",
                "session_id": "session-structured",
                "command_id": "command-structured",
                "sequence": 3,
                "exception_type": "WindowsUiaDriverError",
                "app": "claude",
                "generation": 7,
            }
        ]
        assert "private UIA body" not in feedback.events[-1][0]
    finally:
        runtime.stop()


def test_real_controller_failure_reaches_runtime_jsonl_with_stage_and_code(
    settings,
    tmp_path,
) -> None:
    class MissRouter:
        @staticmethod
        def route(_instruction):
            return NativeSkillResult(NativeRouteStatus.MISS, "miss")

    class UnusedDriver:
        @staticmethod
        def cancel():
            return False

        @staticmethod
        def close():
            return None

    enable_computer_control(settings)
    path = tmp_path / "handsfreepc.jsonl"
    diagnostics = Diagnostics(path)
    controller = DesktopAgentLoopController(
        native_router=MissRouter(),
        driver=UnusedDriver(),
        planner=None,
    )
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=FakeFeedback(),
        controller=controller,
        diagnostics=diagnostics,
    )
    runtime._voice_session_id = "session-real-controller"
    command = QueuedCommand(
        "在 Claude 打开设计",
        sequence=1,
        session_id="session-real-controller",
        command_id="command-real-controller",
    )
    try:
        outcome = runtime._run_queued_control(command, threading.Event())
        runtime._on_control_outcome(outcome)

        assert outcome.stage == "plan"
        assert outcome.error_code == "PLANNER_NOT_CONFIGURED"
        diagnostics.close()
        events = tail_events(path)
        assert len(events) == 1
        assert events[0]["stage"] == "plan"
        assert events[0]["error_code"] == "PLANNER_NOT_CONFIGURED"
        assert events[0]["session_id"] == "session-real-controller"
        assert events[0]["command_id"] == "command-real-controller"
    finally:
        runtime.stop()
        diagnostics.close()


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


def test_voice_confirmation_requires_announced_action_and_one_time_challenge(settings) -> None:
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
        assert not runtime._pending_controller_confirmation_announced
        assert runtime._pending_controller_confirmation_started_at == 0

        instruction = pending_controller_confirmation_instruction(runtime)

        runtime._flush_voice_feedback(Speech())
        assert any("send the prepared form" in text for text in feedback.speaker.messages)
        assert any(instruction in text for text in feedback.speaker.messages)
        assert runtime._pending_controller_confirmation_announced
        assert runtime._pending_controller_confirmation_started_at > 0

        static_phrase = runtime.handle_session_text("确认执行")
        assert not static_phrase.success
        assert controller.calls == ["提交表单"]

        assert runtime.handle_session_text(instruction).success
        assert runtime.command_worker.drain(timeout=2)
        assert "send the prepared form" in controller.calls[1]
    finally:
        runtime.stop()


def test_one_shot_confirmation_code_is_consumed_by_exactly_one_concurrent_caller(
    settings,
) -> None:
    executor = FakeExecutor()
    runtime = VoiceRuntime(
        settings,
        executor,
        feedback=FakeFeedback(),
        confirmation_challenge_factory=lambda: "4827",
    )
    runtime.state = RuntimeState.AWAKE
    requested = runtime.handle_text("打开codex并使用应用内语音", require_wake=False)
    assert requested.state == RuntimeState.CONFIRMING
    instruction = pending_plan_confirmation_instruction(runtime)

    original_match = runtime._matches_confirmation_challenge
    rendezvous = threading.Barrier(2)

    def coordinated_match(text, challenge):
        with suppress(threading.BrokenBarrierError):
            rendezvous.wait(timeout=0.25)
        return original_match(text, challenge)

    runtime._matches_confirmation_challenge = coordinated_match
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                lambda _index: runtime.handle_text(instruction, require_wake=False),
                range(2),
            )
        )

    assert len(executor.plans) == 1
    assert sum(outcome.success for outcome in outcomes) == 1
    assert runtime.pending_plan is None


def test_continuous_confirmation_code_enqueues_exactly_one_concurrent_legacy_resume(
    settings,
) -> None:
    enable_computer_control(settings)
    controller = FakeController(
        responses=[
            ComputerControlResult(False, "NEEDS_CONFIRMATION: click exact approved button"),
            ComputerControlResult(True, "LOCAL_VERIFIED_COMPLETION: clicked"),
        ]
    )
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=FakeFeedback(),
        controller=controller,
        confirmation_challenge_factory=lambda: "4827",
    )
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("点击已批准按钮 over")
        wait_until(lambda: runtime.command_worker.state == WorkerState.PAUSED)
        runtime._mark_pending_controller_confirmation_announced("click exact approved button")
        instruction = pending_controller_confirmation_instruction(runtime)

        original_match = runtime._matches_confirmation_challenge
        rendezvous = threading.Barrier(2)

        def coordinated_match(text, challenge):
            with suppress(threading.BrokenBarrierError):
                rendezvous.wait(timeout=0.25)
            return original_match(text, challenge)

        runtime._matches_confirmation_challenge = coordinated_match
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(
                pool.map(lambda _index: runtime._confirm_controller_action(instruction), range(2))
            )

        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls[0] == "点击已批准按钮"
        assert len(controller.calls) == 2
        assert controller.calls[1].startswith("The user has explicitly confirmed")
        assert sum(outcome.success for outcome in outcomes) == 1
        assert runtime._pending_controller_confirmation is None
    finally:
        runtime.stop()


def test_voice_confirmation_prefers_bound_compact_announcement_over_static_mismatch(
    settings,
) -> None:
    enable_computer_control(settings)
    detail = (
        "enter exact user-spoken text; action=type_text; app=Claude; "
        'user-spoken-target="Prompt"; payload="DRAFT_SAMPLE"; '
        + "verified-context-digest=abcdef12; " * 12
        + "binding=0123456789"
    )
    controller = FakeController(
        responses=[ComputerControlResult(False, f"NEEDS_CONFIRMATION: {detail}")]
    )
    feedback = FakeFeedback()
    feedback.mode = FeedbackMode.VOICE
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=feedback,
        controller=controller,
        confirmation_challenge_factory=lambda: "4827",
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
        runtime.handle_session_text("在 Claude 的 Prompt 输入 DRAFT_SAMPLE over")
        wait_until(lambda: runtime.command_worker.state == WorkerState.PAUSED)

        rejected = runtime.handle_session_text("确认执行")
        assert not rejected.success
        assert not runtime._pending_controller_confirmation_announced

        runtime._flush_voice_feedback(Speech())
        spoken = feedback.speaker.messages[-1]
        assert len(spoken) <= 235
        assert "确认执行 4 8 2 7" in spoken
        assert "enter exact user-spoken text" in spoken
        assert "binding=0123456789" in spoken
        assert runtime._pending_controller_confirmation_announced
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
        instruction = pending_controller_confirmation_instruction(runtime)
        runtime._flush_voice_feedback(Speech())

        assert not runtime._pending_controller_confirmation_announced
        assert not runtime.handle_session_text(instruction).success
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
        instruction = pending_controller_confirmation_instruction(runtime)

        assert not runtime._pending_controller_confirmation_announced
        assert not runtime.handle_session_text(instruction).success
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

        instruction = pending_controller_confirmation_instruction(runtime)
        confirmed = runtime.handle_session_text(instruction)
        assert confirmed.success
        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls[0] == "第一条"
        assert controller.calls[1].startswith("The user has explicitly confirmed")
        assert 'the JSON string "send the form"' in controller.calls[1]
        assert controller.calls[2] == "第二条"
    finally:
        runtime.stop()


def test_typed_controller_confirmation_never_becomes_a_model_prompt(settings) -> None:
    enable_computer_control(settings)

    class ConfirmableController(FakeController):
        def __init__(self):
            super().__init__()
            self.confirmed_ids = []

        def run(self, instruction, *, cancel_event=None):
            del cancel_event
            self.calls.append(instruction)
            if instruction == "第一条":
                return ComputerControlResult(
                    False,
                    "NEEDS_CONFIRMATION: send exact prepared message",
                    needs_confirmation=True,
                    confirmation_id="desktop-deadbeef",
                )
            return ComputerControlResult(True, "LOCAL_VERIFIED_COMPLETION: done")

        def confirm(self, confirmation_id, *, cancel_event=None):
            del cancel_event
            self.confirmed_ids.append(confirmation_id)
            return ComputerControlResult(True, "LOCAL_VERIFIED_COMPLETION: confirmed")

    controller = ConfirmableController()
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=FakeFeedback(),
        controller=controller,
    )
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("第一条 over 第二条 over")
        wait_until(lambda: runtime.command_worker.state == WorkerState.PAUSED)

        instruction = pending_controller_confirmation_instruction(runtime)
        assert runtime.handle_session_text(instruction).success
        assert runtime.command_worker.drain(timeout=2)

        assert controller.confirmed_ids == ["desktop-deadbeef"]
        assert controller.calls == ["第一条", "第二条"]
        assert all("explicitly confirmed" not in item for item in controller.calls)
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
        instruction = pending_controller_confirmation_instruction(runtime)
        assert runtime.handle_session_text(instruction).success
        wait_until(lambda: runtime.session_state == SessionState.ARMED)
        assert controller.closed
        assert runtime._pending_controller_confirmation_challenge is None
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
        assert runtime._pending_controller_confirmation_challenge is None
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
        instruction = pending_controller_confirmation_instruction(runtime)
        runtime._pending_controller_confirmation_started_at = (
            time.monotonic() - settings.execution.confirmation_timeout_seconds - 1
        )

        outcome = runtime.handle_session_text(instruction)

        assert not outcome.success
        assert outcome.state == RuntimeState.PAUSED
        assert outcome.message.startswith("确认已超时")
        assert runtime._pending_controller_confirmation is None
        assert runtime._pending_controller_confirmation_challenge is None
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
            if "已完成本地验收" in text:
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
        completion = next(text for text in texts if "已完成本地验收" in text)
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


@pytest.mark.parametrize(
    "command",
    [
        "do not open Claude",
        "不要打开 Claude",
        "打开 Codex，然后保存当前文件",
    ],
)
def test_one_shot_runtime_never_executes_a_partial_deterministic_match(settings, command) -> None:
    runtime, executor, _ = build_runtime(settings)
    runtime.state = RuntimeState.AWAKE

    outcome = runtime.handle_text(command, require_wake=False)

    assert not outcome.success
    assert executor.plans == []


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
    instruction = pending_plan_confirmation_instruction(runtime)
    confirmed = runtime.handle_text(instruction, require_wake=False)
    assert confirmed.state == RuntimeState.PAUSED
    assert len(executor.plans) == 1


def test_static_confirmation_phrase_never_authorizes_pending_plan(settings) -> None:
    runtime, executor, _ = build_runtime(settings)
    runtime.state = RuntimeState.AWAKE
    requested = runtime.handle_text("打开codex并使用应用内语音", require_wake=False)
    assert requested.state == RuntimeState.CONFIRMING
    challenge = runtime._plan_confirmation_challenge
    assert challenge is not None

    rejected = runtime.handle_text(
        settings.execution.confirmation_phrases[0],
        require_wake=False,
    )

    assert rejected.state == RuntimeState.CONFIRMING
    assert executor.plans == []
    assert runtime.pending_plan is not None
    assert runtime._plan_confirmation_challenge == challenge


def test_stale_replayed_and_colliding_one_time_challenges_never_authorize(settings) -> None:
    challenges = iter(["4827", "4827", "9051"])
    executor = FakeExecutor()
    runtime = VoiceRuntime(
        settings,
        executor,
        feedback=FakeFeedback(),
        confirmation_challenge_factory=lambda: next(challenges),
    )
    runtime.state = RuntimeState.AWAKE

    first = runtime.handle_text("打开codex并使用应用内语音", require_wake=False)
    assert first.state == RuntimeState.CONFIRMING
    first_instruction = pending_plan_confirmation_instruction(runtime)
    cancelled = runtime.handle_text(
        settings.execution.cancellation_phrases[0],
        require_wake=False,
    )
    assert cancelled.state == RuntimeState.ARMED
    assert runtime.pending_plan is None
    assert runtime._plan_confirmation_challenge is None

    second = runtime.handle_text("打开codex并使用应用内语音", require_wake=False)
    assert second.state == RuntimeState.CONFIRMING
    second_instruction = pending_plan_confirmation_instruction(runtime)
    assert second_instruction != first_instruction

    stale = runtime.handle_text(first_instruction, require_wake=False)
    assert stale.state == RuntimeState.CONFIRMING
    assert executor.plans == []
    assert runtime.pending_plan is not None

    accepted = runtime.handle_text(second_instruction, require_wake=False)
    assert accepted.state == RuntimeState.PAUSED
    assert len(executor.plans) == 1
    assert runtime.pending_plan is None
    assert runtime._plan_confirmation_challenge is None

    replayed = runtime.handle_text(second_instruction, require_wake=False)
    assert not replayed.success
    assert len(executor.plans) == 1


def test_controller_confirmation_resamples_collision_and_rejects_old_code(settings) -> None:
    enable_computer_control(settings)
    challenges = iter(["4827", "4827", "5931"])
    first_controller = FakeController(
        responses=[ComputerControlResult(False, "NEEDS_CONFIRMATION: click First")]
    )
    second_controller = FakeController(
        responses=[
            ComputerControlResult(False, "NEEDS_CONFIRMATION: click Second"),
            ComputerControlResult(True, "VERIFIED_COMPLETION: Second clicked"),
        ]
    )
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=FakeFeedback(),
        controller=first_controller,
        controller_factory=lambda: second_controller,
        confirmation_challenge_factory=lambda: next(challenges),
    )

    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("执行第一个不同操作 over")
        wait_until(lambda: runtime._pending_controller_confirmation_challenge == "4827")
        first_instruction = pending_controller_confirmation_instruction(runtime)

        runtime.handle_session_text(settings.execution.cancellation_phrases[0])
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("执行第二个不同操作 over")
        wait_until(lambda: runtime._pending_controller_confirmation_challenge == "5931")
        runtime._mark_pending_controller_confirmation_announced("click Second")
        second_instruction = pending_controller_confirmation_instruction(runtime)
        assert second_instruction != first_instruction

        stale = runtime.handle_session_text(first_instruction)
        assert not stale.success
        assert second_controller.calls == ["执行第二个不同操作"]
        assert runtime._pending_controller_confirmation == "click Second"

        accepted = runtime.handle_session_text(second_instruction)
        assert accepted.success
        assert runtime.command_worker.drain(timeout=2)
        assert len(second_controller.calls) == 2
        assert "click Second" in second_controller.calls[1]
        assert runtime._pending_controller_confirmation is None
    finally:
        runtime.stop()


def test_exhausted_challenge_factory_fails_closed_without_half_pending_state(settings) -> None:
    enable_computer_control(settings)
    controller = FakeController(
        responses=[ComputerControlResult(False, "NEEDS_CONFIRMATION: click Protected")]
    )
    runtime = VoiceRuntime(
        settings,
        FakeExecutor(),
        feedback=FakeFeedback(),
        controller=controller,
        confirmation_challenge_factory=lambda: "4827",
    )

    try:
        assert runtime._create_confirmation_challenge() == "4827"

        runtime.state = RuntimeState.AWAKE
        rejected_plan = runtime.handle_text(
            "打开codex并使用应用内语音",
            require_wake=False,
        )
        assert not rejected_plan.success
        assert rejected_plan.state == RuntimeState.ARMED
        assert runtime.pending_plan is None
        assert runtime._plan_confirmation_challenge is None

        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("执行受保护操作 over")
        wait_until(lambda: runtime.command_worker.state == WorkerState.PAUSED)
        assert runtime._pending_controller_confirmation is None
        assert runtime._pending_controller_confirmation_id is None
        assert runtime._pending_controller_confirmation_challenge is None
        assert controller.cancelled
        assert controller.closed
    finally:
        runtime.stop()


def test_confirmation_phrase_must_be_the_entire_utterance(settings) -> None:
    runtime, executor, _ = build_runtime(settings)
    runtime.state = RuntimeState.AWAKE
    requested = runtime.handle_text("打开codex并使用应用内语音", require_wake=False)
    assert requested.state == RuntimeState.CONFIRMING
    instruction = pending_plan_confirmation_instruction(runtime)

    rejected = runtime.handle_text(f"不要{instruction}", require_wake=False)

    assert rejected.state == RuntimeState.CONFIRMING
    assert executor.plans == []
    assert runtime.pending_plan is not None


def test_stop_clears_pending_controller_confirmation_challenge(settings) -> None:
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
    runtime.handle_session_text("开始语音操作")
    runtime.handle_session_text("提交表单 over")
    wait_until(lambda: runtime.command_worker.state == WorkerState.PAUSED)
    instruction = pending_controller_confirmation_instruction(runtime)

    runtime.stop()

    assert runtime._pending_controller_confirmation is None
    assert runtime._pending_controller_confirmation_id is None
    assert runtime._pending_controller_confirmation_challenge is None
    assert runtime._pending_controller_confirmation_started_at == 0
    assert runtime.session_state == SessionState.STOPPED
    assert not runtime.handle_session_text(instruction).success


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


@pytest.mark.parametrize(
    ("command", "action"),
    [
        ("不要打开 Claude", Action(ActionType.ACTIVATE_APP, app="claude")),
        (
            "请勿在 Claude 里打开 Design",
            Action(ActionType.OPEN_MODE, app="claude", mode="design"),
        ),
        (
            "输入‘打开 Claude’到提示框",
            Action(ActionType.ACTIVATE_APP, app="claude"),
        ),
    ],
)
def test_legacy_cloud_fallback_cannot_execute_negated_or_quoted_ui_actions(
    settings, command, action
) -> None:
    class FixedPlanner:
        def plan(self, _command, *, context):
            del context
            return Plan("untrusted navigation", [action], source="claude")

    executor = FakeExecutor()
    runtime = VoiceRuntime(
        settings,
        executor,
        planner=FixedPlanner(),
        feedback=FakeFeedback(),
    )
    runtime.state = RuntimeState.AWAKE

    outcome = runtime.handle_text(command, require_wake=False)

    assert not outcome.success
    assert executor.plans == []


def test_confirmation_feedback_is_derived_from_actions_not_planner_summary(settings) -> None:
    runtime, executor, feedback = build_runtime(settings)
    plan = Plan(
        "harmless operation",
        [Action(ActionType.START_NATIVE_VOICE, app="codex")],
        risk=RiskLevel.CONFIRM,
        source="codex",
    )

    outcome = runtime._dispatch(plan, user_text="open Codex in-app voice")

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
    instruction = pending_plan_confirmation_instruction(runtime)
    runtime.confirmation_started_at = (
        time.monotonic() - settings.execution.confirmation_timeout_seconds - 1
    )

    outcome = runtime.handle_text(instruction, require_wake=False)

    assert outcome.state == RuntimeState.ARMED
    assert outcome.message == "确认已超时"
    assert runtime.pending_plan is None
    assert runtime._plan_confirmation_challenge is None
    assert executor.plans == []


def test_resolved_executable_requires_confirmation(settings, tmp_path) -> None:
    target = tmp_path / "installer.exe"
    target.write_bytes(b"trusted installer fixture")

    class ResolvingExecutor(FakeExecutor):
        def prepare_plan(self, plan):
            actions = [
                replace(action, path=str(target)) if action.type == ActionType.OPEN_PATH else action
                for action in plan.actions
            ]
            return replace(plan, actions=actions)

    executor = ResolvingExecutor()
    feedback = FakeFeedback()
    runtime = VoiceRuntime(settings, executor, feedback=feedback)
    runtime.state = RuntimeState.AWAKE

    outcome = runtime.handle_text("打开D盘的项目文件夹里的说明.txt", require_wake=False)

    assert outcome.state == RuntimeState.CONFIRMING
    assert executor.plans == []
    assert "installer.exe" in feedback.events[-1][0]


def test_confirmed_file_is_rebound_and_replacement_is_never_executed(settings, tmp_path) -> None:
    target = tmp_path / "installer.exe"
    target.write_bytes(b"trusted installer fixture")

    class ResolvingExecutor(FakeExecutor):
        def prepare_plan(self, plan):
            return replace(
                plan,
                actions=[
                    replace(action, path=str(target))
                    if action.type == ActionType.OPEN_PATH
                    else action
                    for action in plan.actions
                ],
            )

    executor = ResolvingExecutor()
    runtime = VoiceRuntime(settings, executor, feedback=FakeFeedback())
    plan = Plan(
        "open installer",
        [Action(ActionType.OPEN_PATH, path=str(target))],
        risk=RiskLevel.CONFIRM,
    )

    requested = runtime._dispatch(plan, user_text=f"打开 {target}")
    assert requested.state == RuntimeState.CONFIRMING
    instruction = pending_plan_confirmation_instruction(runtime)
    target.write_bytes(b"replaced payload")

    confirmed = runtime.handle_text(instruction, require_wake=False)

    assert not confirmed.success
    assert confirmed.state == RuntimeState.ARMED
    assert executor.plans == []
    assert runtime.pending_plan is None
    assert runtime._plan_confirmation_binding_digest is None


def test_confirmed_file_executes_only_when_plan_and_target_binding_are_unchanged(
    settings, tmp_path
) -> None:
    target = tmp_path / "installer.exe"
    target.write_bytes(b"stable installer fixture")

    class ResolvingExecutor(FakeExecutor):
        def prepare_plan(self, plan):
            return replace(
                plan,
                actions=[
                    replace(action, path=str(target))
                    if action.type == ActionType.OPEN_PATH
                    else action
                    for action in plan.actions
                ],
            )

    executor = ResolvingExecutor()
    runtime = VoiceRuntime(settings, executor, feedback=FakeFeedback())
    plan = Plan(
        "open installer",
        [Action(ActionType.OPEN_PATH, path=str(target))],
        risk=RiskLevel.CONFIRM,
    )

    requested = runtime._dispatch(plan, user_text=f"打开 {target}")
    instruction = pending_plan_confirmation_instruction(runtime)
    confirmed = runtime.handle_text(instruction, require_wake=False)

    assert requested.state == RuntimeState.CONFIRMING
    assert confirmed.success
    assert len(executor.plans) == 1
    assert executor.plans[0].actions[0].path == str(target)


def test_safe_directory_replaced_after_classification_is_never_opened(settings, tmp_path) -> None:
    target = tmp_path / "fixture.exe"
    target.mkdir()
    runtime, executor, _feedback = build_runtime(settings)
    original_execute = runtime._execute
    entered = threading.Event()
    release = threading.Event()

    def blocking_execute(plan, **kwargs):
        entered.set()
        assert release.wait(timeout=2)
        return original_execute(plan, **kwargs)

    runtime._execute = blocking_execute
    plan = Plan(
        "open fixture directory",
        [Action(ActionType.OPEN_PATH, path=str(target))],
        risk=RiskLevel.SAFE,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime._dispatch, plan, user_text=f"打开 {target}")
        assert entered.wait(timeout=2)
        target.rmdir()
        target.write_bytes(b"replacement executable")
        release.set()
        result = future.result(timeout=2)

    assert not result.success
    assert executor.plans == []


def test_stop_during_initial_confirmation_binding_cannot_publish_pending_state(settings) -> None:
    runtime, executor, _feedback = build_runtime(settings)
    runtime.state = RuntimeState.AWAKE
    entered = threading.Event()
    release = threading.Event()

    def blocking_binding(_plan):
        entered.set()
        assert release.wait(timeout=2)
        return "bound"

    runtime._plan_confirmation_binding = blocking_binding
    plan = Plan(
        "voice",
        [Action(ActionType.START_NATIVE_VOICE, app="claude")],
        risk=RiskLevel.CONFIRM,
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime._dispatch, plan, user_text="打开 Claude 语音")
        assert entered.wait(timeout=2)
        stopped = runtime.handle_text(settings.app.stop_phrases[0], require_wake=False)
        release.set()
        result = future.result(timeout=2)

    assert stopped.state == RuntimeState.PAUSED
    assert not result.success
    assert runtime.pending_plan is None
    assert runtime.state == RuntimeState.PAUSED
    assert executor.plans == []


def test_stop_during_confirmed_rebind_wins_before_executor_call(settings) -> None:
    runtime, executor, _feedback = build_runtime(settings)
    runtime.state = RuntimeState.AWAKE
    original_binding = runtime._plan_confirmation_binding
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def blocking_second_binding(plan):
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
            assert release.wait(timeout=2)
        return original_binding(plan)

    runtime._plan_confirmation_binding = blocking_second_binding
    plan = Plan(
        "voice",
        [Action(ActionType.START_NATIVE_VOICE, app="claude")],
        risk=RiskLevel.CONFIRM,
    )
    runtime._dispatch(plan, user_text="打开 Claude 语音")
    instruction = pending_plan_confirmation_instruction(runtime)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.handle_text, instruction, require_wake=False)
        assert entered.wait(timeout=2)
        stopped = runtime.handle_text(settings.app.stop_phrases[0], require_wake=False)
        release.set()
        result = future.result(timeout=2)

    assert stopped.state == RuntimeState.PAUSED
    assert not result.success
    assert executor.plans == []


def test_confirmed_execution_uses_private_snapshot_not_mutable_returned_plan(settings) -> None:
    entered = threading.Event()
    release = threading.Event()

    class BarrierExecutor(PreparingFakeExecutor):
        def execute_plan(self, plan):
            entered.set()
            assert release.wait(timeout=2)
            return super().execute_plan(plan)

    executor = BarrierExecutor()
    runtime = VoiceRuntime(settings, executor, feedback=FakeFeedback())
    plan = Plan(
        "voice",
        [Action(ActionType.START_NATIVE_VOICE, app="claude")],
        risk=RiskLevel.CONFIRM,
    )
    waiting = runtime._dispatch(plan, user_text="打开 Claude 语音")
    leaked_pending_copy = runtime.pending_plan
    assert waiting.plan is not None and leaked_pending_copy is not None
    instruction = pending_plan_confirmation_instruction(runtime)

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runtime.handle_text, instruction, require_wake=False)
        assert entered.wait(timeout=2)
        waiting.plan.actions[0].app = "codex"
        leaked_pending_copy.actions[0].app = "codex"
        release.set()
        result = future.result(timeout=2)

    assert result.success
    assert [executed.actions[0].app for executed in executor.plans] == ["claude"]
