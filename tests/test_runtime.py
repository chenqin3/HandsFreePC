from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

import handsfree_pc.runtime as runtime_module
from handsfree_pc.audio import PhraseDetection
from handsfree_pc.control import ControlResult
from handsfree_pc.models import FeedbackMode
from handsfree_pc.runtime import VoiceRuntime, _merge_control_phrase_transcript
from handsfree_pc.session import JobOutcome, QueuedCommand, SessionState, WorkerState


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
        self.speaker = FakeSpeaker()

    def emit(self, text, **kwargs):
        self.events.append((text, kwargs))

    def close(self):
        pass

    @property
    def texts(self) -> list[str]:
        return [text for text, _kwargs in self.events]


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
            return ControlResult(False, "cancelled", cancelled=True)
        if self.responses:
            return self.responses.pop(0)
        return ControlResult(True, "已完成", session_id=self.session_id)

    def cancel(self):
        self.cancelled = True
        return True

    def close(self):
        self.closed = True


class FakeDiagnostics:
    def __init__(self) -> None:
        self.events = []

    def event(self, **kwargs) -> None:
        self.events.append(kwargs)


def wait_until(predicate, *, timeout=2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true")
        time.sleep(0.01)


def build_runtime(settings, controller=None, **kwargs):
    feedback = FakeFeedback()
    controller = controller or FakeController()
    runtime = VoiceRuntime(settings, feedback=feedback, controller=controller, **kwargs)
    return runtime, controller, feedback


# -- session protocol ------------------------------------------------------------


def test_session_queues_only_at_over_and_stays_active(settings) -> None:
    runtime, controller, _feedback = build_runtime(settings)
    try:
        assert runtime.handle_session_text("开始语音操作").success
        assert runtime.session_state == SessionState.ACTIVE

        pending = runtime.handle_session_text("打开记事本")
        assert pending.message == "已记录，等待 over"
        assert controller.calls == []

        assert runtime.handle_session_text("over").success
        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls == ["打开记事本"]
        assert runtime.session_state == SessionState.ACTIVE
    finally:
        runtime.stop()


def test_wake_with_command_suffix_in_the_same_utterance(settings) -> None:
    runtime, controller, _feedback = build_runtime(settings)
    try:
        outcome = runtime.handle_session_text("开始语音操作 打开 Claude over")

        assert outcome.success
        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls == ["打开 Claude"]
        assert runtime.session_state == SessionState.ACTIVE
    finally:
        runtime.stop()


def test_custom_wake_phrase(settings) -> None:
    settings.app.wake_phrases = ["芝麻开门"]
    runtime, controller, _feedback = build_runtime(settings)
    try:
        assert runtime.handle_session_text("芝麻开门 打开 Claude over").success
        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls == ["打开 Claude"]
    finally:
        runtime.stop()


@pytest.mark.parametrize(
    "utterance",
    ["不要开始语音操作", "我说的是开始语音操作", "他说开始语音操作", "“开始语音操作”"],
)
def test_non_invocation_wake_is_rejected(settings, utterance: str) -> None:
    runtime, controller, _feedback = build_runtime(settings)
    try:
        outcome = runtime.handle_session_text(utterance)

        assert outcome.handled is False
        assert outcome.success is False
        assert runtime.session_state == SessionState.ARMED
        assert controller.calls == []
    finally:
        runtime.stop()


def test_commands_are_ignored_outside_a_session(settings) -> None:
    runtime, controller, _feedback = build_runtime(settings)
    try:
        outcome = runtime.handle_session_text("打开记事本 over")
        assert outcome.handled is False
        assert controller.calls == []
    finally:
        runtime.stop()


@pytest.mark.parametrize(
    ("pending_fragment", "prefix_transcript", "expected"),
    [
        ("", "打开记事本", "打开记事本"),
        ("", "搜索欧文", "搜索欧文"),
        ("搜索", "欧文", "搜索 欧文"),
    ],
)
def test_sample_bound_marker_preserves_prefix_text(
    settings, pending_fragment, prefix_transcript, expected
) -> None:
    runtime, controller, _feedback = build_runtime(settings)
    try:
        runtime.handle_session_text("开始语音操作")
        if pending_fragment:
            runtime.handle_session_text(pending_fragment)

        outcome = runtime._handle_marked_session_utterance(
            prefix_transcript, "over", had_pending_before=bool(pending_fragment)
        )

        assert outcome.success
        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls == [expected]
    finally:
        runtime.stop()


def test_marker_before_text_keeps_suffix_for_the_next_prompt(settings) -> None:
    runtime, controller, _feedback = build_runtime(settings)
    try:
        runtime.handle_session_text("开始语音操作")

        runtime._handle_marked_session_segments(["", "打开记事本"], marker_count=1)

        assert controller.calls == []
        assert runtime.prompt_assembler.pending_text == "打开记事本"

        runtime._handle_marked_session_segments(["", ""], marker_count=1)
        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls == ["打开记事本"]
    finally:
        runtime.stop()


def test_multiple_markers_preserve_fifo_and_unfinished_suffix(settings) -> None:
    runtime, controller, _feedback = build_runtime(settings)
    try:
        runtime.handle_session_text("开始语音操作")

        runtime._handle_marked_session_segments(["第一条", "第二条", "第三条"], marker_count=2)
        assert runtime.prompt_assembler.pending_text == "第三条"
        runtime._handle_marked_session_segments(["", ""], marker_count=1)

        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls == ["第一条", "第二条", "第三条"]
    finally:
        runtime.stop()


def test_textual_and_sample_bound_marker_never_enqueue_twice(settings) -> None:
    runtime, controller, _feedback = build_runtime(settings)
    try:
        runtime.handle_session_text("开始语音操作")

        runtime._handle_marked_session_segments(["打开记事本 over", ""], marker_count=1)

        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls == ["打开记事本"]
    finally:
        runtime.stop()


def test_repetitive_asr_tail_after_over_is_ignored(settings) -> None:
    runtime, controller, _feedback = build_runtime(settings)
    try:
        runtime.handle_session_text("开始语音操作")

        runtime._handle_marked_session_segments(
            ["切换到 Claude", "Codex, Codex, Codex, Codex, Codex.com"], marker_count=1
        )

        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls == ["切换到 Claude"]
        assert not runtime.prompt_assembler.has_pending
    finally:
        runtime.stop()


def test_oversized_prefix_is_reported_not_masked(settings) -> None:
    settings.app.max_prompt_chars = 4
    runtime, controller, _feedback = build_runtime(settings)
    try:
        runtime.handle_session_text("开始语音操作")

        outcome = runtime._handle_marked_session_segments(["超过最大长度", ""], marker_count=1)

        assert not outcome.success
        assert outcome.message == "指令过长"
        assert controller.calls == []
        assert not runtime.prompt_assembler.has_pending
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
        ("取消所有操作", "所有操作", "取消所有操作"),
        ("结束语音操作", "语音操作", "结束语音操作"),
        ("开始语音操作", "包子。", "开始语音操作"),
    ]
    for matched, transcript, expected in cases:
        assert _merge_control_phrase_transcript(matched, transcript) == expected


def test_wake_only_asr_hallucination_does_not_pollute_first_prompt(settings) -> None:
    runtime, controller, _feedback = build_runtime(settings)
    try:
        runtime.handle_session_text(_merge_control_phrase_transcript("开始语音操作", "包子。"))
        runtime.handle_session_text("切换到 Claude over")
        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls == ["切换到 Claude"]
    finally:
        runtime.stop()


def test_end_session_homophone_ends_continuous_input(settings) -> None:
    runtime, _controller, _feedback = build_runtime(settings)
    try:
        runtime.handle_session_text("开始语音操作")
        outcome = runtime.handle_session_text("接触语音操作。")
        assert outcome.success is True
        assert runtime.session_state == SessionState.ARMED
    finally:
        runtime.stop()


def test_slow_wake_preserves_exact_path_in_queued_prompt(settings) -> None:
    runtime, controller, _feedback = build_runtime(settings)
    try:
        merged = _merge_control_phrase_transcript(
            "开始语音操作", r"操作，打开 C:\My Folder 里的 Design Review over"
        )
        runtime.handle_session_text(merged)
        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls == [r"打开 C:\My Folder 里的 Design Review"]
    finally:
        runtime.stop()


def test_feedback_mode_switch_is_local_with_or_without_over(settings) -> None:
    runtime, controller, feedback = build_runtime(settings)
    try:
        runtime.handle_session_text("开始语音操作")

        assert runtime.handle_session_text("切换到语音反馈").success
        assert feedback.mode == FeedbackMode.VOICE
        assert runtime.handle_session_text("切换到屏幕反馈 over").success
        assert feedback.mode == FeedbackMode.OVERLAY
        assert controller.calls == []
    finally:
        runtime.stop()


# -- the queue ------------------------------------------------------------------


def test_records_while_first_job_executes_and_preserves_fifo(settings) -> None:
    gate = threading.Event()
    runtime, controller, _feedback = build_runtime(settings, FakeController(gate=gate))
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


def test_success_notice_uses_the_executor_note(settings) -> None:
    controller = FakeController(responses=[ControlResult(True, "已把文件发给文件传输助手")])
    runtime, _controller, feedback = build_runtime(settings, controller)
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("把文件发到微信 over")
        assert runtime.command_worker.drain(timeout=2)
        wait_until(lambda: any("已完成：" in text for text in feedback.texts))

        assert "第 1 条已完成：已把文件发给文件传输助手" in feedback.texts
    finally:
        runtime.stop()


def test_continue_policy_keeps_later_command_moving(settings) -> None:
    controller = FakeController(
        responses=[
            ControlResult(
                False,
                "FAILURE: nope",
                stage="kimi_agent",
                error_code="KIMI_REPORTED_FAILURE",
                safe_message="Kimi reported the task failed",
            ),
            ControlResult(True, "done"),
        ]
    )
    runtime, _controller, feedback = build_runtime(settings, controller)
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("第一条 over")
        runtime.handle_session_text("第二条 over")

        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls == ["第一条", "第二条"]
        assert any("后续已入队指令会继续执行" in text for text in feedback.texts)
    finally:
        runtime.stop()


def test_pause_policy_holds_the_queue_until_a_resume_phrase(settings) -> None:
    settings.app.failure_policy = "pause"
    controller = FakeController(
        responses=[ControlResult(False, "FAILURE: nope"), ControlResult(True, "done")]
    )
    runtime, _controller, feedback = build_runtime(settings, controller)
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("第一条 over")
        wait_until(lambda: runtime.command_worker.state == WorkerState.PAUSED)
        runtime.handle_session_text("第二条 over")
        assert controller.calls == ["第一条"]
        assert any("队列已暂停" in text for text in feedback.texts)

        assert runtime.handle_session_text("继续队列").success
        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls == ["第一条", "第二条"]
    finally:
        runtime.stop()


def test_structured_failure_reaches_overlay_and_log_without_raw_content(settings) -> None:
    private_value = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
    controller = FakeController(
        responses=[
            ControlResult(
                False,
                f"FAILURE: private stderr {private_value}",
                stage="kimi_agent",
                error_code="KIMI_TIMEOUT",
                safe_message="Kimi timed out after 600 s",
                exception_type="TimeoutExpired",
            )
        ]
    )
    diagnostics = FakeDiagnostics()
    runtime, _controller, feedback = build_runtime(settings, controller, diagnostics=diagnostics)
    runtime._voice_session_id = "session-1"
    command = QueuedCommand("private prompt", sequence=2, session_id="session-1", command_id="c-2")
    try:
        outcome = runtime._run_queued_control(command, threading.Event())
        assert outcome.stage == "kimi_agent"
        assert outcome.error_code == "KIMI_TIMEOUT"

        runtime._on_control_outcome(outcome)

        overlay = feedback.events[-1][0]
        assert "[kimi_agent / KIMI_TIMEOUT]" in overlay
        assert "Kimi timed out after 600 s" in overlay
        assert private_value not in overlay
        assert [event["error_code"] for event in diagnostics.events] == [
            "CONTROL_STARTED",
            "KIMI_TIMEOUT",
        ]
        assert diagnostics.events[-1] == {
            "stage": "kimi_agent",
            "error_code": "KIMI_TIMEOUT",
            "safe_message": "Kimi timed out after 600 s",
            "level": "error",
            "session_id": "session-1",
            "command_id": "c-2",
            "sequence": 2,
            "exception_type": "TimeoutExpired",
        }
    finally:
        runtime.stop()


def test_unstructured_failure_falls_back_to_a_generic_status(settings) -> None:
    feedback = FakeFeedback()
    feedback.mode = FeedbackMode.BOTH
    diagnostics = FakeDiagnostics()
    runtime = VoiceRuntime(settings, feedback=feedback, diagnostics=diagnostics)
    runtime._voice_session_id = "session-1"
    command = QueuedCommand("private prompt", sequence=3, session_id="session-1")
    private_value = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
    try:
        runtime._on_control_outcome(
            JobOutcome(command, success=False, message=f"FAILURE: {private_value}")
        )

        overlay = feedback.events[-1][0]
        assert "[runtime / CONTROL_COMMAND_FAILED]" in overlay
        assert private_value not in overlay
        assert diagnostics.events[-1]["error_code"] == "CONTROL_COMMAND_FAILED"
        with runtime._voice_feedback_lock:
            queued_voice = [text for _kind, text in runtime._voice_feedback]
        assert queued_voice == ["第 3 条在运行时阶段失败，后续队列继续"]
    finally:
        runtime.stop()


def test_end_session_discards_half_prompt_and_drains_queue(settings) -> None:
    gate = threading.Event()
    runtime, controller, _feedback = build_runtime(settings, FakeController(gate=gate))
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


def test_draining_failure_never_ends_with_success_feedback(settings) -> None:
    controller = FakeController(responses=[ControlResult(False, "FAILURE: target missing")])
    runtime, _controller, feedback = build_runtime(settings, controller)
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
    first = FakeController()
    second = FakeController()
    controllers = iter([first, second])
    runtime = VoiceRuntime(
        settings, feedback=FakeFeedback(), controller_factory=lambda: next(controllers)
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


def test_stop_phrase_during_execution_cancels_running_and_queued_work(settings) -> None:
    gate = threading.Event()
    runtime, controller, _feedback = build_runtime(settings, FakeController(gate=gate))
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("当前任务 over")
        wait_until(lambda: controller.calls == ["当前任务"])
        runtime.handle_session_text("排队任务 over")

        outcome = runtime.handle_session_text("电脑停止")

        assert outcome.success
        assert outcome.state == SessionState.PAUSED
        assert runtime.command_worker.pending_count == 0
        assert controller.cancelled
        assert controller.closed
    finally:
        gate.set()
        runtime.stop()


def test_late_failure_from_stopped_session_cannot_pause_new_session_queue(settings) -> None:
    started = threading.Event()
    release = threading.Event()

    class StubbornFailureController(FakeController):
        def run(self, instruction, *, cancel_event=None):
            del cancel_event
            self.calls.append(instruction)
            started.set()
            assert release.wait(timeout=2)
            return ControlResult(False, "FAILURE: stale failure")

    first = StubbornFailureController()
    second = FakeController()
    runtime = VoiceRuntime(
        settings, feedback=FakeFeedback(), controller=first, controller_factory=lambda: second
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


def test_emergency_stop_is_last_feedback_across_outcome_race(settings) -> None:
    class BlockingOutcomeFeedback(FakeFeedback):
        def __init__(self):
            super().__init__()
            self.mode = FeedbackMode.VOICE
            self.outcome_entered = threading.Event()
            self.release_outcome = threading.Event()

        def emit(self, text, **kwargs):
            super().emit(text, **kwargs)
            if "已完成：" in text:
                self.outcome_entered.set()
                assert self.release_outcome.wait(timeout=2)

    feedback = BlockingOutcomeFeedback()
    runtime = VoiceRuntime(settings, feedback=feedback, controller=FakeController())
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
        texts = feedback.texts
        completion = next(text for text in texts if "已完成：" in text)
        stop_text = next(text for text in texts if "已请求立即停止" in text)
        assert texts.index(completion) < texts.index(stop_text)
        with runtime._voice_feedback_lock:
            queued_voice = [text for _kind, text in runtime._voice_feedback]
        assert queued_voice == [stop_text]
    finally:
        feedback.release_outcome.set()
        runtime.stop()


# -- executor wiring -----------------------------------------------------------------


def test_kimi_controller_is_built_from_settings_on_first_command(settings, monkeypatch) -> None:
    import handsfree_pc.kimi_agent as kimi_module

    built = []
    controller = FakeController()

    def fake_build(kimi_settings, *, diagnostics=None, on_progress=None):
        built.append((kimi_settings, diagnostics, on_progress))
        return controller

    monkeypatch.setattr(kimi_module, "build_kimi_controller", fake_build)
    diagnostics = FakeDiagnostics()
    runtime = VoiceRuntime(settings, feedback=FakeFeedback(), diagnostics=diagnostics)
    try:
        runtime.handle_session_text("开始语音操作")
        runtime.handle_session_text("打开记事本 over")
        assert runtime.command_worker.drain(timeout=2)

        assert controller.calls == ["打开记事本"]
        assert built == [(settings.kimi, diagnostics, runtime._on_controller_progress)]
    finally:
        runtime.stop()


def test_controller_progress_is_shown_on_the_overlay_only(settings) -> None:
    runtime, _controller, feedback = build_runtime(settings)
    feedback.mode = FeedbackMode.BOTH
    try:
        runtime._on_controller_progress("Kimi 第 2 步：Bash")

        assert feedback.events[-1][0] == "Kimi 第 2 步：Bash"
        assert feedback.events[-1][1]["kind"] == "executing"
        with runtime._voice_feedback_lock:
            assert not runtime._voice_feedback
    finally:
        runtime.stop()


# -- spoken feedback ---------------------------------------------------------------


def test_spoken_feedback_is_deferred_until_microphone_boundary(settings) -> None:
    runtime, _controller, feedback = build_runtime(settings)
    feedback.mode = FeedbackMode.VOICE

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


def test_spoken_feedback_plays_only_the_most_important_latest_message(settings) -> None:
    runtime, _controller, feedback = build_runtime(settings)
    feedback.mode = FeedbackMode.VOICE

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
        runtime._emit_continuous("第一个失败", kind="error")
        runtime._emit_continuous("第二个失败", kind="error")

        runtime._flush_voice_feedback(Speech())

        assert feedback.speaker.messages == ["第二个失败"]
        assert not runtime._voice_feedback_event.is_set()
    finally:
        runtime.stop()


# -- the microphone loop -------------------------------------------------------------


def test_microphone_loop_reports_ready_only_after_models_and_mic_enter(
    settings, monkeypatch
) -> None:
    runtime, _controller, feedback = build_runtime(settings)

    class Speech:
        def __init__(self, *_args, phrases, marker_phrases, **_kwargs):
            assert "over" not in phrases
            assert "开始语音操作" in phrases
            assert "恢复监听" in phrases
            assert marker_phrases == ["over"]

        def __enter__(self):
            feedback.events.append(("MIC_ENTERED", {}))
            runtime.stop_event.set()
            return self

        def __exit__(self, *_args):
            pass

    monkeypatch.setattr(runtime_module, "LocalSpeechSession", Speech)
    try:
        runtime.run_microphone()

        messages = feedback.texts
        assert messages[0] == "正在加载语音模型…"
        assert messages.index("MIC_ENTERED") < messages.index("已就绪，说「开始语音操作」即可开始")
        ready = next(kwargs for text, kwargs in feedback.events if text.startswith("已就绪"))
        assert ready["duration"] > 0
    finally:
        runtime.stop()


def test_microphone_loop_routes_post_marker_audio_to_next_prompt(settings, monkeypatch) -> None:
    runtime, controller, _feedback = build_runtime(settings)
    runtime.handle_session_text("开始语音操作")

    class Speech:
        last_marker_phrase = "over"
        last_marker_events = (PhraseDetection("over", 1600, 3200),)

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def listen_utterance(self, **_kwargs):
            return object()

        def transcribe_marked_segments(self):
            runtime.stop_event.set()
            return ["打开记事本", "然后打开计算器"]

    monkeypatch.setattr(runtime_module, "LocalSpeechSession", Speech)
    try:
        runtime.run_microphone()

        assert runtime.command_worker.drain(timeout=2)
        assert controller.calls == ["打开记事本"]
        assert runtime.prompt_assembler.pending_text == "然后打开计算器"
    finally:
        runtime.stop()


def test_opted_in_runtime_records_every_marker_segment(settings, monkeypatch) -> None:
    settings.privacy.save_transcripts = True
    saved = []

    class Journal:
        def record(self, **kwargs):
            saved.append(kwargs)

    runtime, _controller, _feedback = build_runtime(settings, transcript_journal=Journal())
    runtime.handle_session_text("开始语音操作")

    class Speech:
        last_marker_phrase = "over"
        last_marker_events = (PhraseDetection("over", 1600, 3200),)
        last_marker_segment_transcribed = (True, False)

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def listen_utterance(self, **_kwargs):
            return object()

        def transcribe_marked_segments(self):
            runtime.stop_event.set()
            return ["  打开 Claude  ", ""]

    monkeypatch.setattr(runtime_module, "LocalSpeechSession", Speech)
    try:
        runtime.run_microphone()
    finally:
        runtime.stop()

    assert [entry["source"] for entry in saved] == ["marker_segment", "marker_segment"]
    assert [entry["text"] for entry in saved] == ["  打开 Claude  ", ""]
    assert [entry["segment_index"] for entry in saved] == [0, 1]
    assert all(entry["segment_count"] == 2 for entry in saved)
    assert all(entry["session_id"] for entry in saved)
    assert [entry["transcribed"] for entry in saved] == [True, False]
    assert saved[0]["skip_reason"] is None
    assert saved[1]["skip_reason"] == "silence_energy_gate"


def test_opted_in_runtime_records_wake_and_command_returns(settings, monkeypatch) -> None:
    settings.privacy.save_transcripts = True
    saved = []

    class Journal:
        def record(self, **kwargs):
            saved.append(kwargs)

    runtime, _controller, _feedback = build_runtime(settings, transcript_journal=Journal())

    class Speech:
        class Source:
            @staticmethod
            def drain():
                pass

        source = Source()
        last_marker_phrase = None
        last_marker_events = ()

        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def wait_for_phrase(self, **_kwargs):
            return "开始语音操作", object()

        def listen_utterance(self, **_kwargs):
            return object()

        def transcribe(self, _audio):
            if runtime.session_state == SessionState.ARMED:
                return "  开始语音操作  "
            runtime.stop_event.set()
            return "  切换到 Claude，打开 Chat and Cowork  "

    monkeypatch.setattr(runtime_module, "LocalSpeechSession", Speech)
    try:
        runtime.run_microphone()
    finally:
        runtime.stop()

    assert [entry["source"] for entry in saved] == ["wake_utterance", "command_utterance"]
    assert [entry["text"] for entry in saved] == [
        "  开始语音操作  ",
        "  切换到 Claude，打开 Chat and Cowork  ",
    ]
    assert saved[0]["session_id"] is None
    assert saved[1]["session_id"]


def test_transcript_journal_is_not_constructed_without_opt_in(settings, monkeypatch) -> None:
    settings.privacy.save_transcripts = False

    def forbidden_journal():
        raise AssertionError("transcript journal must remain disabled")

    monkeypatch.setattr(runtime_module, "TranscriptJournal", forbidden_journal)
    runtime = VoiceRuntime(settings, feedback=FakeFeedback())
    try:
        assert runtime.transcript_journal is None
    finally:
        runtime.stop()


def test_microphone_guard_pauses_the_session_and_resumes_when_free(settings) -> None:
    runtime, _controller, feedback = build_runtime(settings)

    class Guard:
        enabled = True
        poll_seconds = 0.01

        def __init__(self):
            self.busy = "zoom.exe"

        def busy_app(self):
            return self.busy

    class Speech:
        def __init__(self):
            self.paused = 0
            self.resumed = 0

        def pause_microphone(self):
            self.paused += 1

        def resume_microphone(self):
            self.resumed += 1

    guard = Guard()
    speech = Speech()
    runtime._microphone_guard = guard
    try:
        runtime.handle_session_text("开始语音操作")

        assert runtime._guard_microphone(speech) is True
        assert speech.paused == 1
        assert runtime.session_state == SessionState.ARMED
        assert any("zoom.exe 正在使用麦克风" in text for text in feedback.texts)
        assert runtime._guard_microphone(speech) is True
        assert speech.paused == 1

        guard.busy = None
        assert runtime._guard_microphone(speech) is False
        assert speech.resumed == 1
        assert feedback.texts[-1] == "麦克风已空闲，恢复监听。"
        assert runtime._guard_microphone(speech) is False
    finally:
        runtime.stop()


def test_stop_closes_controller_and_journal(settings, tmp_path: Path) -> None:
    class Journal:
        def __init__(self):
            self.closed = False

        def record(self, **_kwargs):
            pass

        def close(self):
            self.closed = True

    settings.privacy.save_transcripts = True
    journal = Journal()
    runtime, controller, _feedback = build_runtime(settings, transcript_journal=journal)
    runtime.handle_session_text("开始语音操作")
    runtime.handle_session_text("任务 over")
    assert runtime.command_worker.drain(timeout=2)

    runtime.stop()

    assert runtime.session_state == SessionState.STOPPED
    assert controller.closed
    assert journal.closed is False  # an injected journal stays owned by the caller
