"""The always-on voice runtime.

Wake phrase -> continuous session -> every ``over``-terminated prompt joins a
strict FIFO queue -> the Kimi Code agent carries it out on the desktop.
Control phrases (end, stop, resume, feedback mode) are recognised locally and
never reach the agent.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from .audio import AudioError, ControlPhraseDetected, FeedbackPending, LocalSpeechSession
from .config import Settings
from .control import Controller
from .diagnostics import classify_control_failure, stage_display_name
from .feedback import FeedbackController
from .mic_guard import MicrophoneGuard
from .models import FeedbackMode
from .normalize import compact_text, confirm_control_phrase, phrase_equals, wake_suffix
from .session import (
    CommandWorker,
    JobOutcome,
    PromptAssembler,
    QueuedCommand,
    SessionState,
    WorkerState,
)
from .transcripts import TranscriptJournal

_FEEDBACK_MODE_PHRASES: dict[FeedbackMode, tuple[str, ...]] = {
    FeedbackMode.OVERLAY: ("切换到屏幕反馈", "屏幕反馈", "大字模式", "遮罩反馈"),
    FeedbackMode.VOICE: ("切换到语音反馈", "语音反馈", "声音反馈", "说话反馈"),
    FeedbackMode.BOTH: ("大字和语音两种都开", "两种反馈", "两种都开", "大字和语音"),
    FeedbackMode.SILENT: ("切换到静默模式", "静默模式", "安静模式", "不要反馈"),
}
_FEEDBACK_MODE_LABELS = {
    FeedbackMode.OVERLAY: "屏幕大字",
    FeedbackMode.VOICE: "语音",
    FeedbackMode.BOTH: "屏幕大字和语音",
    FeedbackMode.SILENT: "静默",
}
# The command ASR sometimes hears the end phrase as this homophone.
_END_SESSION_ASR_ALIASES = ("接触语音操作",)
# When several spoken notices queue up behind one utterance, say the most important one.
_VOICE_PRIORITY = {
    "error": 4,
    "success": 3,
    "executing": 2,
    "listening": 1,
    "armed": 1,
    "recognized": 0,
}


def _merge_control_phrase_transcript(matched: str, transcript: str) -> str:
    """Join a detector phrase only with a transcript that overlaps that phrase.

    The wake detector and the command ASR inspect the same pre-roll.  A command
    model can hallucinate an unrelated short phrase from wake-only audio.  Such
    a transcript is not evidence of a suffix and must never become the first
    fragment of the new continuous session.
    """

    phrase_compact = compact_text(matched)
    transcript_compact = compact_text(transcript)
    if not transcript_compact:
        return matched
    if phrase_compact in transcript_compact:
        return transcript.strip()
    for size in range(min(len(phrase_compact), len(transcript_compact)), 1, -1):
        if phrase_compact[-size:] == transcript_compact[:size]:
            compact_prefix = transcript_compact[:size]
            consumed = ""
            remainder = transcript
            for index, character in enumerate(transcript):
                consumed += compact_text(character)
                if len(consumed) >= len(compact_prefix):
                    if consumed == compact_prefix:
                        remainder = transcript[index + 1 :].lstrip(" \t\r\n,，.。;；!！?？:：、")
                    break
            return f"{matched} {remainder}".strip()
    return matched


def _looks_like_repetitive_asr_tail(value: str) -> bool:
    """Detect a repeated-token decoder loop after a sample-bound ``over`` marker."""

    tokens = re.findall(r"[a-z]+|\d+|[一-鿿]+", value.casefold())
    if len(tokens) >= 4:
        counts = {token: tokens.count(token) for token in set(tokens)}
        if max(counts.values(), default=0) >= 3 and len(counts) / len(tokens) <= 0.5:
            return True
    compact = re.sub(r"[\s,，.。;；!?！？:：、]+", "", value.casefold())
    repeated = re.search(r"(.{2,16}?)(?:\1){2,}", compact)
    return bool(repeated and len(repeated.group(0)) >= max(6, int(len(compact) * 0.6)))


def _strip_sample_bound_delimiter(value: str, delimiters: list[str]) -> str:
    """Remove ASR leakage of the already sample-bound marker from one prefix."""

    result = value.rstrip()
    for delimiter in sorted(delimiters, key=len, reverse=True):
        escaped = re.escape(delimiter.strip())
        if not escaped:
            continue
        boundary = r"(?<![A-Za-z0-9])" if delimiter.isascii() else ""
        match = re.search(
            rf"{boundary}{escaped}\s*[,，.。;；!?！？:：、]*$",
            result,
            re.IGNORECASE,
        )
        if match is not None:
            return result[: match.start()].rstrip(" \t\r\n,，.。;；!！?？:：、")
    return result


@dataclass(slots=True)
class TurnOutcome:
    handled: bool
    state: SessionState
    message: str
    success: bool = True


class VoiceRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        feedback: FeedbackController | Any | None = None,
        controller: Controller | None = None,
        controller_factory: Callable[[], Controller] | None = None,
        diagnostics: Any | None = None,
        transcript_journal: TranscriptJournal | Any | None = None,
    ) -> None:
        self.settings = settings
        self.feedback = feedback or FeedbackController(settings.app.feedback_mode)
        self.diagnostics = diagnostics
        self.stop_event = threading.Event()
        self.session_state = SessionState.ARMED
        self.prompt_assembler = PromptAssembler(settings.app.prompt_delimiters)
        self._session_lock = threading.RLock()
        self._voice_session_id: str | None = None
        self._voice_sequence = 0
        self._controller = controller
        self._controller_factory = controller_factory or self._build_controller
        self._voice_feedback: deque[tuple[str, str]] = deque(maxlen=32)
        self._voice_feedback_lock = threading.Lock()
        self._voice_feedback_event = threading.Event()
        self._microphone_guard_paused = False
        self._microphone_guard = MicrophoneGuard(
            enabled=settings.app.auto_pause_when_microphone_busy,
            poll_seconds=settings.app.microphone_guard_poll_seconds,
            ignore=settings.app.microphone_guard_ignore,
        )
        self.transcript_journal: TranscriptJournal | Any | None = None
        self._owns_transcript_journal = False
        if settings.privacy.save_transcripts:
            self.transcript_journal = transcript_journal or TranscriptJournal()
            self._owns_transcript_journal = transcript_journal is None
        self.command_worker = CommandWorker(
            self._run_queued_control,
            max_queue_size=settings.app.max_queue_size,
            failure_policy=settings.app.failure_policy,
            on_outcome=self._on_control_outcome,
            on_state_change=self._on_worker_state,
        )
        self.command_worker.start()

    # -- executor -----------------------------------------------------------------

    def _build_controller(self) -> Controller:
        from .kimi_agent import build_kimi_controller

        return build_kimi_controller(
            self.settings.kimi,
            diagnostics=self.diagnostics,
            on_progress=self._on_controller_progress,
        )

    def _on_controller_progress(self, text: str) -> None:
        self._emit_continuous(text, kind="executing", duration=0, allow_voice=False)

    def _ensure_controller(self, session_id: str | None) -> Controller | None:
        with self._session_lock:
            if session_id is None or session_id != self._voice_session_id:
                return None
            if self._controller is None:
                self._controller = self._controller_factory()
            return self._controller

    # -- feedback and records --------------------------------------------------------

    def _emit_continuous(
        self,
        text: str,
        *,
        kind: str,
        duration: float = 4.0,
        allow_voice: bool = True,
        voice_text: str | None = None,
    ) -> bool:
        # Overlay feedback is immediate. Spoken feedback is deferred until the microphone
        # thread reaches an utterance boundary, so SAPI never starts halfway through speech.
        if self._microphone_guard_paused:
            # Someone is in a meeting or a call: never talk over it.
            allow_voice = False
        displayed = self.feedback.emit(
            text,
            kind=kind,
            duration=duration,
            allow_voice=False,
            force_visible_when_voice_blocked=False,
        )
        if allow_voice and self.feedback.mode in {FeedbackMode.VOICE, FeedbackMode.BOTH}:
            with self._voice_feedback_lock:
                entry = (kind, voice_text if voice_text is not None else text)
                if not self._voice_feedback or self._voice_feedback[-1] != entry:
                    self._voice_feedback.append(entry)
                self._voice_feedback_event.set()
        return displayed is not False

    def _record_diagnostic(
        self,
        *,
        stage: str,
        error_code: str,
        safe_message: str,
        level: str = "error",
        session_id: str | None = None,
        command_id: str | None = None,
        sequence: int | None = None,
        exception_type: object = None,
    ) -> None:
        if self.diagnostics is None:
            return
        with suppress(Exception):
            event_fields: dict[str, object] = {
                "stage": stage,
                "error_code": error_code,
                "safe_message": safe_message,
                "level": level,
            }
            optional_fields = {
                "session_id": session_id,
                "command_id": command_id,
                "sequence": sequence,
                "exception_type": exception_type,
            }
            event_fields.update(
                {key: value for key, value in optional_fields.items() if value is not None}
            )
            self.diagnostics.event(**event_fields)

    def _record_transcript(
        self,
        source: str,
        text: str,
        *,
        segment_index: int | None = None,
        segment_count: int | None = None,
        transcribed: bool | None = None,
        skip_reason: str | None = None,
    ) -> None:
        """Persist the exact local ASR text only when the user opted in."""

        if self.transcript_journal is None:
            return
        self.transcript_journal.record(
            source=source,
            text=text,
            session_id=self._voice_session_id,
            segment_index=segment_index,
            segment_count=segment_count,
            transcribed=transcribed,
            skip_reason=skip_reason,
        )

    def _clear_voice_feedback(self) -> None:
        with self._voice_feedback_lock:
            self._voice_feedback.clear()
            self._voice_feedback_event.clear()

    def _flush_voice_feedback(self, speech: Any) -> None:
        """Speak the most important pending notice; runs only on the microphone thread."""

        with self._voice_feedback_lock:
            pending = list(self._voice_feedback)
            self._voice_feedback.clear()
            self._voice_feedback_event.clear()
        if not pending or self.feedback.mode not in {FeedbackMode.VOICE, FeedbackMode.BOTH}:
            return
        _index, (_kind, text) = max(
            enumerate(pending),
            key=lambda item: (_VOICE_PRIORITY.get(item[1][0], 0), item[0]),
        )
        spoken_text = text if len(text) <= 240 else f"{text[:237]}..."
        accepted = self.feedback.speaker.speak(spoken_text)
        while self.feedback.speaker.speaking.is_set() and not self.stop_event.is_set():
            time.sleep(0.05)
        speech.source.drain()
        speech.reset_control_detector()
        delivered = (
            accepted is not False
            and not self.feedback.speaker.speaking.is_set()
            and not self.stop_event.is_set()
            and getattr(self.feedback.speaker, "last_error", None) is None
        )
        if not delivered:
            self.feedback.emit(
                "语音反馈失败，请切换到屏幕反馈。",
                kind="error",
                allow_voice=False,
                force_visible_when_voice_blocked=True,
            )

    def _feedback_mode_for_text(self, text: str) -> FeedbackMode | None:
        candidate = compact_text(text)
        for delimiter in self.settings.app.prompt_delimiters:
            suffix = compact_text(delimiter)
            if suffix and candidate.endswith(suffix):
                candidate = candidate[: -len(suffix)]
                break
        for mode, phrases in _FEEDBACK_MODE_PHRASES.items():
            if any(candidate == compact_text(phrase) for phrase in phrases):
                return mode
        return None

    def _set_feedback_mode(self, mode: FeedbackMode) -> TurnOutcome:
        self.feedback.mode = mode
        if mode not in {FeedbackMode.VOICE, FeedbackMode.BOTH}:
            self._clear_voice_feedback()
        message = f"已切换到{_FEEDBACK_MODE_LABELS[mode]}反馈"
        self._emit_continuous(message, kind="success")
        return TurnOutcome(True, self.session_state, message)

    def _set_session_state(self, state: SessionState) -> None:
        with self._session_lock:
            self.session_state = state

    # -- the continuous session ------------------------------------------------------

    def start_continuous_session(self) -> TurnOutcome:
        with self._session_lock:
            if self.session_state == SessionState.DRAINING:
                self._emit_continuous("上一轮队列仍在执行，请稍候", kind="error")
                return TurnOutcome(True, self.session_state, "队列仍在排空", success=False)
            self.prompt_assembler.discard_pending()
            self._voice_session_id = str(uuid.uuid4())
            self._voice_sequence = 0
            self._clear_voice_feedback()
            if self.command_worker.state == WorkerState.PAUSED:
                self.command_worker.resume()
            self._set_session_state(SessionState.ACTIVE)
            session_id = self._voice_session_id
        self._record_diagnostic(
            stage="runtime",
            error_code="VOICE_SESSION_STARTED",
            safe_message="Continuous voice command session started",
            level="info",
            session_id=session_id,
        )
        self._emit_continuous(
            "持续语音操作已开始。每条指令说完请说 over。",
            kind="listening",
            duration=8,
        )
        return TurnOutcome(True, self.session_state, "持续语音操作已开始")

    def handle_session_text(self, text: str) -> TurnOutcome:
        """Handle one local ASR fragment of the continuous voice-control protocol."""

        value = text.strip()
        if not value:
            return TurnOutcome(False, self.session_state, "空语音片段", success=False)
        app = self.settings.app

        if phrase_equals(value, app.stop_phrases):
            return self._emergency_stop_session()

        if self.session_state in {SessionState.ARMED, SessionState.PAUSED}:
            wake, suffix = wake_suffix(value, app.wake_phrases)
            if wake is None and phrase_equals(value, app.resume_phrases) is None:
                return TurnOutcome(False, self.session_state, "未检测到开始口令", success=False)
            outcome = self.start_continuous_session()
            if not outcome.success or not suffix:
                return outcome
            value = suffix

        if phrase_equals(value, [*app.end_session_phrases, *_END_SESSION_ASR_ALIASES]):
            return self._end_continuous_input()

        if phrase_equals(value, app.resume_phrases):
            if self.command_worker.resume():
                self._emit_continuous("队列已继续", kind="success")
                return TurnOutcome(True, self.session_state, "队列已继续")
            return TurnOutcome(False, self.session_state, "队列当前没有暂停", success=False)

        if self.session_state != SessionState.ACTIVE:
            return TurnOutcome(False, self.session_state, "当前不接受新的语音指令", success=False)

        if (
            not self.prompt_assembler.has_pending
            and (feedback_mode := self._feedback_mode_for_text(value)) is not None
        ):
            return self._set_feedback_mode(feedback_mode)

        self._record_diagnostic(
            stage="runtime",
            error_code="COMMAND_FRAGMENT_ACCEPTED",
            safe_message="A local command transcript fragment was accepted",
            level="info",
            session_id=self._voice_session_id,
        )

        projected = len(self.prompt_assembler.pending_text) + len(value)
        if projected > app.max_prompt_chars:
            self.prompt_assembler.discard_pending()
            self._emit_continuous("当前指令过长，已丢弃，请拆成多条", kind="error")
            return TurnOutcome(True, self.session_state, "指令过长", success=False)

        delimiter_detected = self.prompt_assembler.contains_delimiter(value)
        completed = self.prompt_assembler.feed(value)
        if delimiter_detected:
            self._record_prompt_delimiter()
        if not completed:
            pending = self.prompt_assembler.pending_text
            message = "已记录，等待 over" if pending else "没有可入队的指令"
            if self.feedback.mode in {FeedbackMode.OVERLAY, FeedbackMode.BOTH}:
                self._emit_continuous(message, kind="recognized", allow_voice=False)
            return TurnOutcome(True, self.session_state, message)

        return self._accept_completed_prompts(completed)

    def _record_prompt_delimiter(self) -> None:
        self._record_diagnostic(
            stage="runtime",
            error_code="PROMPT_DELIMITER_DETECTED",
            safe_message="A configured prompt delimiter was detected locally",
            level="info",
            session_id=self._voice_session_id,
        )

    def _accept_completed_prompts(self, completed: list[str]) -> TurnOutcome:
        accepted = 0
        local = 0
        for prompt in completed:
            if (feedback_mode := self._feedback_mode_for_text(prompt)) is not None:
                self._set_feedback_mode(feedback_mode)
                local += 1
            elif self._enqueue_prompt(prompt):
                accepted += 1
        if accepted + local != len(completed):
            self._emit_continuous("队列已满，部分指令未入队，请稍后重说", kind="error")
            return TurnOutcome(True, self.session_state, "队列已满", success=False)
        if accepted:
            message = f"已入队 {accepted} 条，当前未完成 {self.command_worker.unfinished_count} 条"
            self._emit_continuous(message, kind="recognized")
        else:
            message = f"已完成 {local} 条本地设置"
        return TurnOutcome(True, self.session_state, message)

    def _handle_marked_session_utterance(
        self,
        transcript: str,
        marker: str,
        *,
        had_pending_before: bool,
    ) -> TurnOutcome:
        """Compatibility wrapper for one already boundary-separated marker prefix."""

        del marker, had_pending_before
        return self._handle_marked_session_segments([transcript, ""], marker_count=1)

    def _handle_marked_session_segments(
        self,
        transcripts: list[str],
        *,
        marker_count: int,
    ) -> TurnOutcome:
        """Apply ordered ASR segments separated by local sample-bound marker events."""

        if marker_count < 1 or len(transcripts) != marker_count + 1:
            raise ValueError("Marker transcripts must contain one more segment than markers")
        last_outcome: TurnOutcome | None = None
        first_failure: TurnOutcome | None = None
        for index, transcript in enumerate(transcripts):
            value = transcript.strip()
            if index == marker_count and value and _looks_like_repetitive_asr_tail(value):
                self._record_diagnostic(
                    stage="transcribe",
                    error_code="ASR_REPETITIVE_MARKER_TAIL_IGNORED",
                    safe_message="A repetitive ASR tail after the prompt marker was ignored",
                    session_id=self._voice_session_id,
                    level="warning",
                )
                continue
            if index < marker_count and value:
                value = _strip_sample_bound_delimiter(
                    value,
                    self.settings.app.prompt_delimiters,
                )
            if value:
                last_outcome = self.handle_session_text(value)
                if self.session_state != SessionState.ACTIVE:
                    return last_outcome
                if not last_outcome.success and first_failure is None:
                    first_failure = last_outcome
            if index >= marker_count:
                continue
            self._record_prompt_delimiter()
            completed = self.prompt_assembler.finalize()
            if completed is not None:
                last_outcome = self._accept_completed_prompts([completed])
                if not last_outcome.success and first_failure is None:
                    first_failure = last_outcome
        if first_failure is not None:
            return first_failure
        if last_outcome is not None:
            return last_outcome
        message = "已听到 over，但当前没有可入队的指令"
        self._emit_continuous(message, kind="recognized", allow_voice=False)
        return TurnOutcome(True, self.session_state, message)

    # -- the queue ----------------------------------------------------------------

    def _enqueue_prompt(self, prompt: str) -> bool:
        with self._session_lock:
            self._voice_sequence += 1
            command = QueuedCommand(
                text=prompt,
                sequence=self._voice_sequence,
                session_id=self._voice_session_id,
            )
        accepted = self.command_worker.enqueue(command)
        if accepted:
            self._record_diagnostic(
                stage="runtime",
                error_code="COMMAND_ENQUEUED",
                safe_message="A voice command entered the local FIFO queue",
                level="info",
                session_id=command.session_id,
                command_id=command.command_id,
                sequence=command.sequence,
            )
        return accepted

    def _run_queued_control(
        self, command: QueuedCommand, cancel_event: threading.Event
    ) -> JobOutcome:
        started_at = time.monotonic()
        self._record_diagnostic(
            stage="runtime",
            error_code="CONTROL_STARTED",
            safe_message="A queued command was handed to the desktop executor",
            level="info",
            session_id=command.session_id,
            command_id=command.command_id,
            sequence=command.sequence,
        )
        self._emit_continuous(
            f"第 {command.sequence} 条已交给 Kimi 执行，请稍候…",
            kind="executing",
            duration=0,
            voice_text=f"第 {command.sequence} 条开始执行",
        )
        controller = self._ensure_controller(command.session_id)
        if controller is None:
            return JobOutcome(
                command=command,
                success=False,
                message="Stale voice session was cancelled before control started",
                cancelled=True,
                started_at=started_at,
            )
        result = controller.run(command.text, cancel_event=cancel_event)
        with self._session_lock:
            if command.session_id != self._voice_session_id:
                return JobOutcome(
                    command=command,
                    success=False,
                    message="Stale voice session result was discarded",
                    cancelled=True,
                    started_at=started_at,
                )
        cancelled = bool(result.cancelled)
        return JobOutcome(
            command=command,
            success=bool(result.success) and not cancelled,
            message=result.message,
            cancelled=cancelled,
            stage=result.stage,
            error_code=result.error_code,
            safe_message=result.safe_message,
            exception_type=result.exception_type,
            started_at=started_at,
        )

    def _on_control_outcome(self, outcome: JobOutcome) -> None:
        with self._session_lock:
            if outcome.command.session_id != self._voice_session_id:
                return
            sequence = outcome.command.sequence
            if outcome.success:
                self._record_diagnostic(
                    stage="runtime",
                    error_code="CONTROL_COMPLETED",
                    safe_message="The desktop executor reported the command done",
                    level="info",
                    session_id=outcome.command.session_id,
                    command_id=outcome.command.command_id,
                    sequence=sequence,
                )
                note = " ".join(outcome.message.split())
                summary = note[:60] if note else " ".join(outcome.command.text.split())[:40]
                self._emit_continuous(
                    f"第 {sequence} 条已完成：{summary}", kind="success", duration=6
                )
            elif outcome.cancelled:
                self._emit_continuous(f"第 {sequence} 条已取消", kind="error")
            else:
                status = classify_control_failure(
                    outcome.message,
                    error_type=outcome.error_type,
                    stage=outcome.stage,
                    error_code=outcome.error_code,
                    safe_message=outcome.safe_message,
                )
                self._record_diagnostic(
                    stage=status.stage,
                    error_code=status.error_code,
                    safe_message=status.safe_message,
                    session_id=outcome.command.session_id,
                    command_id=outcome.command.command_id,
                    sequence=sequence,
                    exception_type=outcome.exception_type or outcome.error_type,
                )
                stage_name = stage_display_name(status.stage)
                queue_paused = self.settings.app.failure_policy == "pause"
                queue_status = (
                    "队列已暂停。说继续队列或取消所有操作。"
                    if queue_paused
                    else "后续已入队指令会继续执行。"
                )
                self._emit_continuous(
                    f"第 {sequence} 条失败\n"
                    f"[{status.stage} / {status.error_code}]\n"
                    f"{status.safe_message}\n"
                    f"{queue_status}",
                    kind="error",
                    duration=10,
                    voice_text=(
                        f"第 {sequence} 条在{stage_name}阶段失败，队列已暂停"
                        if queue_paused
                        else f"第 {sequence} 条在{stage_name}阶段失败，后续队列继续"
                    ),
                )
        if outcome.success:
            self._finish_draining_if_idle()
        else:
            self._finish_draining_if_idle(
                final_message="本轮队列已结束，但最后一条未完成",
                final_kind="error",
            )

    def _on_worker_state(self, state: WorkerState) -> None:
        if state == WorkerState.STOPPED and self.session_state != SessionState.STOPPED:
            self._set_session_state(SessionState.PAUSED)

    def _end_continuous_input(self) -> TurnOutcome:
        discarded = self.prompt_assembler.discard_pending()
        self._set_session_state(SessionState.DRAINING)
        worker = self.command_worker
        remaining = worker.unfinished_count
        if worker.state == WorkerState.PAUSED and remaining > 0:
            message = (
                f"已停止接收新指令，仍监听控制口令；剩余 {remaining} 条处于暂停状态，请说继续队列"
            )
        else:
            message = f"已停止接收新指令，仍监听控制口令；剩余 {remaining} 条继续执行"
        if discarded:
            message += "；未说 over 的半条指令已丢弃"
        self._emit_continuous(message, kind="executing", duration=8)
        self._finish_draining_if_idle()
        return TurnOutcome(True, self.session_state, message)

    def _finish_draining_if_idle(
        self,
        *,
        final_message: str = "本轮语音操作和队列已结束",
        final_kind: str = "success",
    ) -> None:
        with self._session_lock:
            should_finish = (
                self.session_state == SessionState.DRAINING
                and self.command_worker.unfinished_count == 0
            )
            if not should_finish:
                return
            controller = self._controller
            self._controller = None
            self._voice_session_id = None
            self._set_session_state(SessionState.ARMED)
        if controller is not None:
            controller.close()
        self._emit_continuous(final_message, kind=final_kind)

    def _emergency_stop_session(self) -> TurnOutcome:
        discarded = self.prompt_assembler.discard_pending()
        worker = self.command_worker
        with self._session_lock:
            controller = self._controller
            self._controller = None
            self._voice_session_id = None
            self._set_session_state(SessionState.PAUSED)
        self._clear_voice_feedback()
        worker.cancel_current()
        cancelled = worker.cancel_pending(reason="Emergency voice stop")
        self._clear_voice_feedback()
        if controller is not None:
            controller.cancel()
            controller.close()
        count = len(cancelled) + (1 if discarded else 0)
        self._emit_continuous(
            f"已请求立即停止，清理 {count} 条未完成输入；已发生的点击无法撤销",
            kind="error",
            duration=8,
        )
        return TurnOutcome(True, self.session_state, "已请求立即停止")

    # -- the microphone ---------------------------------------------------------------

    def _microphone_guard_interrupt(self) -> bool:
        """Consulted on every audio block so a meeting is noticed within one poll."""

        guard = self._microphone_guard
        if guard is None or not guard.enabled or self._microphone_guard_paused:
            return False
        return guard.busy_app() is not None

    def _guard_microphone(self, speech: Any) -> bool:
        """Release the microphone while another app captures; return True while paused.

        Meeting and call apps show up in Windows' microphone consent store the
        moment they start capturing. Listening stops (no transcription, no
        spoken feedback) until they release the device, then resumes on its own.
        """

        guard = self._microphone_guard
        if not hasattr(speech, "pause_microphone"):
            return False
        if guard is None or not guard.enabled:
            return False
        busy = guard.busy_app()
        if busy is None:
            if self._microphone_guard_paused:
                try:
                    speech.resume_microphone()
                except Exception as exc:
                    self._record_diagnostic(
                        stage="runtime",
                        error_code="MICROPHONE_GUARD_RESUME_FAILED",
                        safe_message="The microphone could not be reopened after another app",
                        exception_type=exc,
                    )
                    self.stop_event.wait(guard.poll_seconds)
                    return True
                self._microphone_guard_paused = False
                self._record_diagnostic(
                    stage="runtime",
                    error_code="MICROPHONE_GUARD_RESUMED",
                    safe_message="Another app released the microphone; listening resumed",
                    level="info",
                )
                self._emit_continuous("麦克风已空闲，恢复监听。", kind="armed", duration=4)
            return False
        if not self._microphone_guard_paused:
            self._microphone_guard_paused = True
            if self.session_state in {SessionState.ACTIVE, SessionState.DRAINING}:
                self._end_continuous_input()
            with suppress(Exception):
                speech.pause_microphone()
            self._record_diagnostic(
                stage="runtime",
                error_code="MICROPHONE_GUARD_PAUSED",
                safe_message="Another app is using the microphone; listening paused",
                level="info",
            )
            self._emit_continuous(
                f"{busy} 正在使用麦克风，已暂停监听，结束后自动恢复",
                kind="armed",
                duration=6,
                allow_voice=False,
            )
        self.stop_event.wait(guard.poll_seconds)
        return True

    def _confirm_detected_phrase(self, matched: str, transcript: str) -> str | None:
        """Decide what the spotter's phrase becomes once the accurate transcriber has spoken.

        Strict (default): the transcript must start with the phrase, so chatter that
        merely resembled it, negations and quotations never open a session.  Lenient:
        keep the spotter's word even when the transcript disagrees.
        """

        if not self.settings.app.strict_wake_phrase:
            return _merge_control_phrase_transcript(matched, transcript) or matched
        confirmed = confirm_control_phrase(transcript, matched)
        if confirmed is None:
            self._record_diagnostic(
                stage="runtime",
                error_code="CONTROL_PHRASE_UNCONFIRMED",
                safe_message="The spotter heard a control phrase the transcript did not confirm",
                level="info",
            )
        return confirmed

    def run_microphone(self) -> None:
        """Listen until ``stop`` is called."""

        app = self.settings.app
        phrases = [
            *app.wake_phrases,
            *app.end_session_phrases,
            *app.stop_phrases,
            *app.resume_phrases,
        ]
        interrupts = [*app.end_session_phrases, *app.stop_phrases]
        base_dir = self.settings.config_path.parent
        self._emit_continuous("正在加载语音模型…", kind="executing", duration=0)
        with LocalSpeechSession(
            self.settings.speech,
            base_dir=base_dir,
            phrases=phrases,
            marker_phrases=app.prompt_delimiters,
        ) as speech:
            source = getattr(speech, "source", None)
            if source is not None:
                source.interrupt_check = self._microphone_guard_interrupt
            self._record_diagnostic(
                stage="runtime",
                error_code="MICROPHONE_READY",
                safe_message="Local speech models and microphone are ready",
                level="info",
            )
            # A short, self-hiding notice: the listener starts with Windows and must
            # not leave a banner on the desktop while it waits for the wake phrase.
            self._emit_continuous(
                "已就绪，说「开始语音操作」即可开始",
                kind="armed",
                duration=6,
                voice_text="HandsFreePC 已就绪，说开始语音操作即可开始",
            )
            while not self.stop_event.is_set():
                try:
                    if self._guard_microphone(speech):
                        continue
                    if self._voice_feedback_event.is_set():
                        self._flush_voice_feedback(speech)
                        if self.stop_event.is_set():
                            break
                    if self.session_state == SessionState.ACTIVE:
                        self._listen_for_commands(speech, interrupts)
                        continue
                    matched, audio = speech.wait_for_phrase(
                        stop_event=self.stop_event,
                        feedback_event=self._voice_feedback_event,
                    )
                    transcript = speech.transcribe(audio)
                    self._record_transcript("wake_utterance", transcript)
                    self._record_diagnostic(
                        stage="runtime",
                        error_code="WAKE_OR_CONTROL_PHRASE_DETECTED",
                        safe_message="A configured local wake or control phrase was detected",
                        level="info",
                    )
                    control_text = self._confirm_detected_phrase(matched, transcript)
                    if control_text is None:
                        continue
                    self.handle_session_text(control_text)
                except ControlPhraseDetected as exc:
                    self.handle_session_text(exc.phrase)
                except FeedbackPending:
                    continue
                except AudioError as exc:
                    if "No complete utterance detected" not in str(exc):
                        self._record_diagnostic(
                            stage="runtime",
                            error_code="AUDIO_INPUT_FAILED",
                            safe_message="本地音频输入或语音分段未能完成",
                            session_id=self._voice_session_id,
                            exception_type=exc,
                        )
                        self._emit_continuous(str(exc), kind="error")
                    time.sleep(0.1)
                except Exception as exc:
                    self._record_diagnostic(
                        stage="runtime",
                        error_code="CONTINUOUS_VOICE_PROCESSING_FAILED",
                        safe_message="持续语音处理发生内部错误并已暂停",
                        session_id=self._voice_session_id,
                        exception_type=exc,
                    )
                    with suppress(Exception):
                        speech.source.drain()
                    self._set_session_state(SessionState.PAUSED)
                    self._emit_continuous(
                        "持续语音处理异常，已暂停；说开始语音操作可恢复",
                        kind="error",
                        duration=8,
                    )
                    time.sleep(0.25)

    def _listen_for_commands(self, speech: Any, interrupts: list[str]) -> None:
        """One utterance inside an active session, split at sample-bound ``over`` markers."""

        audio = speech.listen_utterance(
            timeout_seconds=None,
            interrupt_phrases=interrupts,
            marker_phrases=self.settings.app.prompt_delimiters,
        )
        marker_events = tuple(getattr(speech, "last_marker_events", ()))
        marker = getattr(speech, "last_marker_phrase", None)
        if marker_events:
            transcripts = speech.transcribe_marked_segments()
            transcribed_flags = tuple(
                getattr(speech, "last_marker_segment_transcribed", (True,) * len(transcripts))
            )
            segment_count = len(transcripts)
            for index, transcript in enumerate(transcripts):
                was_transcribed = (
                    transcribed_flags[index] if index < len(transcribed_flags) else True
                )
                self._record_transcript(
                    "marker_segment",
                    transcript,
                    segment_index=index,
                    segment_count=segment_count,
                    transcribed=was_transcribed,
                    skip_reason=None if was_transcribed else "silence_energy_gate",
                )
            self._handle_marked_session_segments(transcripts, marker_count=len(marker_events))
            return
        transcript = speech.transcribe(audio)
        self._record_transcript("command_utterance", transcript)
        if marker:
            # Injected speech sessions may only expose the legacy single-marker attribute.
            self._handle_marked_session_utterance(transcript, marker, had_pending_before=False)
        elif transcript:
            self.handle_session_text(transcript)

    def stop(self) -> None:
        self.stop_event.set()
        with self._session_lock:
            self._voice_session_id = None
            self._set_session_state(SessionState.STOPPED)
        self.command_worker.stop(timeout=5.0, cancel_pending=True)
        if self._controller is not None:
            self._controller.close()
            self._controller = None
        self._clear_voice_feedback()
        self.feedback.close()
        if self._owns_transcript_journal and self.transcript_journal is not None:
            self.transcript_journal.close()
            self.transcript_journal = None
