from __future__ import annotations

import threading
import time
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import PureWindowsPath
from typing import Any

from .audio import AudioError, ControlPhraseDetected, LocalSpeechSession
from .config import Settings
from .feedback import FeedbackController
from .intents import DeterministicIntentParser
from .models import Action, ActionType, ExecutionResult, Plan, RiskLevel, RuntimeState
from .normalize import (
    compact_text,
    phrase_equals,
    phrase_in_text,
    strip_control_prefix,
    wake_suffix,
)
from .planner import Planner, PlannerError, build_planner
from .safety import SafetyPolicy


@dataclass(slots=True)
class TurnOutcome:
    handled: bool
    state: RuntimeState
    message: str
    plan: Plan | None = None
    results: list[ExecutionResult] = field(default_factory=list)
    success: bool = True


class VoiceRuntime:
    def __init__(
        self,
        settings: Settings,
        executor: Any,
        *,
        parser: DeterministicIntentParser | None = None,
        planner: Planner | None = None,
        feedback: FeedbackController | Any | None = None,
    ) -> None:
        self.settings = settings
        self.executor = executor
        self.parser = parser or DeterministicIntentParser()
        self.planner = planner if planner is not None else build_planner(settings.planner)
        self.feedback = feedback or FeedbackController(settings.app.feedback_mode)
        self.safety = SafetyPolicy(settings.execution)
        self.state = RuntimeState.ARMED
        self.pending_plan: Plan | None = None
        self.stop_event = threading.Event()
        self.last_awake_at = 0.0
        self.confirmation_started_at = 0.0

    def handle_text(self, text: str, *, require_wake: bool = True) -> TurnOutcome:
        if self.state == RuntimeState.STOPPED:
            return TurnOutcome(False, self.state, "运行时已停止", success=False)
        if expired := self._expire_timeouts():
            return expired
        if phrase_in_text(text, self.settings.app.stop_phrases):
            self.pending_plan = None
            self.state = RuntimeState.PAUSED
            self.feedback.emit("已停止操作。说唤醒词可重新开始。", kind="success")
            return TurnOutcome(True, self.state, "已暂停")

        if self.state == RuntimeState.PAUSED:
            resume_phrases = ["恢复语音操作", "恢复监听", *self.settings.app.wake_phrases]
            if phrase_in_text(text, resume_phrases):
                self.state = RuntimeState.ARMED
                self.feedback.emit("语音操作已恢复", kind="success")
                return TurnOutcome(True, self.state, "已恢复")
            return TurnOutcome(False, self.state, "暂停中", success=False)

        if self.state == RuntimeState.CONFIRMING:
            if phrase_in_text(text, self.settings.execution.cancellation_phrases):
                self.pending_plan = None
                self.confirmation_started_at = 0.0
                self.state = RuntimeState.ARMED
                self.feedback.emit("已取消", kind="success")
                return TurnOutcome(True, self.state, "已取消")
            # Confirmation grants authority. Require the complete normalized
            # utterance so "不要确认执行" cannot consent by substring.
            if phrase_equals(text, self.settings.execution.confirmation_phrases):
                plan = self.pending_plan
                self.pending_plan = None
                self.confirmation_started_at = 0.0
                if plan is None:
                    self.state = RuntimeState.ARMED
                    return TurnOutcome(False, self.state, "没有待确认操作")
                return self._execute(plan)
            self.feedback.emit("等待确认。说“确认执行”或“取消操作”。", kind="confirm")
            return TurnOutcome(True, self.state, "等待确认")

        if self.state == RuntimeState.ARMED and require_wake:
            phrase, suffix = wake_suffix(text, self.settings.app.wake_phrases)
            if phrase is None:
                return TurnOutcome(False, self.state, "未检测到唤醒词", success=False)
            self.state = RuntimeState.AWAKE
            self.last_awake_at = time.monotonic()
            self.feedback.emit("我在听", kind="listening", duration=2.0)
            if not suffix:
                return TurnOutcome(True, self.state, "已唤醒")
            text = suffix

        if self.state == RuntimeState.DICTATION:
            return self._handle_dictation(text)

        if self.state == RuntimeState.AWAKE or not require_wake:
            return self._handle_command(text)
        return TurnOutcome(False, self.state, "当前状态不接受命令", success=False)

    def _handle_dictation(self, text: str) -> TurnOutcome:
        compact = compact_text(text)
        prefixes = [compact_text(item) for item in self.settings.app.control_prefixes]
        is_control = any(compact.startswith(prefix) for prefix in prefixes)
        if is_control:
            command = strip_control_prefix(text, self.settings.app.control_prefixes)
            if any(token in command for token in ("退出听写", "结束听写", "停止听写")):
                self.state = RuntimeState.ARMED
                self.feedback.emit("已退出听写", kind="success")
                return TurnOutcome(True, self.state, "已退出听写")
            plan = self.parser.parse(command)
            if plan is not None:
                explicit_submission = any(
                    action.type == ActionType.SEND_PROMPT for action in plan.actions
                )
                plan = self.safety.evaluate(
                    plan, user_text=command, explicit_submission=explicit_submission
                )
                return self._dispatch(
                    plan,
                    user_text=command,
                    explicit_submission=explicit_submission,
                )
        action = Action(ActionType.TYPE_TEXT, text=text)
        plan = Plan("输入听写文本", [action], source="dictation")
        return self._execute(plan, keep_dictation=True)

    def _handle_command(self, text: str) -> TurnOutcome:
        self.feedback.emit(f"识别：{text}", kind="recognized")
        plan = self.parser.parse(text)
        if plan is None and self.planner is not None:
            try:
                plan = self.planner.plan(text, context=self._planner_context())
            except PlannerError as exc:
                self.state = RuntimeState.ARMED
                self.feedback.emit("规划失败，请换一种说法", kind="error")
                return TurnOutcome(True, self.state, str(exc), success=False)
            except Exception:
                self.state = RuntimeState.ARMED
                self.feedback.emit("规划失败，请换一种说法", kind="error")
                return TurnOutcome(True, self.state, "规划器发生内部异常", success=False)
        if plan is None:
            self.state = RuntimeState.ARMED
            self.feedback.emit("没有理解。请说得更具体。", kind="error")
            return TurnOutcome(True, self.state, "无法解析命令", success=False)
        plan = self.safety.evaluate(plan, user_text=text)
        return self._dispatch(plan, user_text=text)

    def _dispatch(
        self,
        plan: Plan,
        *,
        user_text: str,
        explicit_submission: bool = False,
    ) -> TurnOutcome:
        if plan.risk == RiskLevel.BLOCKED:
            self.state = RuntimeState.ARMED
            self.feedback.emit(plan.summary or "该操作已被安全策略阻止", kind="error")
            return TurnOutcome(True, self.state, plan.summary, plan=plan, success=False)
        if hasattr(self.executor, "prepare_plan"):
            prior_risk = plan.risk
            try:
                plan = self.executor.prepare_plan(plan)
            except Exception as exc:
                self.state = RuntimeState.ARMED
                self.feedback.emit("目标解析失败。路径不存在或存在多个候选。", kind="error")
                return TurnOutcome(True, self.state, str(exc), plan=plan, success=False)
            # Recompute risk against resolved targets.  Never trust the suffix
            # of a fuzzy, aliased, or planner-provided path.
            risk_rank = {
                RiskLevel.SAFE: 0,
                RiskLevel.CONFIRM: 1,
                RiskLevel.BLOCKED: 2,
            }
            if risk_rank[plan.risk] < risk_rank[prior_risk]:
                plan = replace(plan, risk=prior_risk)
            plan = self.safety.evaluate(
                plan,
                user_text=user_text,
                explicit_submission=explicit_submission,
            )
            if plan.risk == RiskLevel.BLOCKED:
                self.state = RuntimeState.ARMED
                self.feedback.emit(plan.summary or "该操作已被安全策略阻止", kind="error")
                return TurnOutcome(True, self.state, plan.summary, plan=plan, success=False)
        if plan.risk == RiskLevel.CONFIRM:
            self.pending_plan = plan
            self.confirmation_started_at = time.monotonic()
            self.state = RuntimeState.CONFIRMING
            confirmation_summary = self._confirmation_summary(plan)
            self.feedback.emit(
                f"需要确认：{confirmation_summary}。请说“确认执行”。",
                kind="confirm",
                duration=8,
            )
            return TurnOutcome(True, self.state, "等待确认", plan=plan)
        return self._execute(plan)

    @staticmethod
    def _confirmation_summary(plan: Plan) -> str:
        """Build consent text from validated actions, never planner prose."""

        labels: list[str] = []
        for action in plan.actions:
            if action.type == ActionType.OPEN_PATH:
                target_name = PureWindowsPath(action.path or "").name
                labels.append(
                    f"打开需确认的文件 {target_name}"
                    if target_name
                    else "打开一个需确认的文件或目录"
                )
            elif action.type == ActionType.START_NATIVE_VOICE:
                labels.append(f"开启 {action.app or '目标应用'} 的应用内语音")
            elif action.type == ActionType.SEND_PROMPT:
                labels.append("提交当前提示")
            else:
                labels.append(action.type.value)
        return "；".join(labels) or "执行当前计划"

    def _expire_timeouts(self) -> TurnOutcome | None:
        now = time.monotonic()
        if (
            self.state == RuntimeState.AWAKE
            and self.last_awake_at > 0
            and now - self.last_awake_at > self.settings.app.awake_timeout_seconds
        ):
            self.state = RuntimeState.ARMED
            self.last_awake_at = 0.0
            self.feedback.emit("等待命令超时，已重新进入待唤醒状态", kind="armed")
            return TurnOutcome(True, self.state, "唤醒已超时")
        if (
            self.state == RuntimeState.CONFIRMING
            and self.confirmation_started_at > 0
            and now - self.confirmation_started_at
            > self.settings.execution.confirmation_timeout_seconds
        ):
            self.pending_plan = None
            self.confirmation_started_at = 0.0
            self.state = RuntimeState.ARMED
            self.feedback.emit("确认已超时，操作已取消", kind="success")
            return TurnOutcome(True, self.state, "确认已超时")
        return None

    def _execute(self, plan: Plan, *, keep_dictation: bool = False) -> TurnOutcome:
        try:
            plan.validate()
        except ValueError:
            self.state = RuntimeState.ARMED
            self.feedback.emit("计划字段未通过本地校验", kind="error")
            return TurnOutcome(True, self.state, "计划字段未通过本地校验", plan=plan, success=False)
        self.state = RuntimeState.EXECUTING
        starts_native_voice = any(
            action.type == ActionType.START_NATIVE_VOICE for action in plan.actions
        )
        if starts_native_voice:
            # Do not open the target application's microphone while any prior
            # SAPI feedback is still queued or playing.
            while self.feedback.speaker.speaking.is_set():
                time.sleep(0.05)
        display_summary = (
            "正在打开已核验的路径"
            if any(action.type == ActionType.OPEN_PATH for action in plan.actions)
            else plan.summary
        )
        self.feedback.emit(
            display_summary,
            kind="executing",
            duration=0,
            allow_voice=not starts_native_voice,
        )
        try:
            if hasattr(self.executor, "execute_plan"):
                results = list(self.executor.execute_plan(plan))
            else:
                results = [self.executor.execute(action) for action in plan.actions]
        except Exception as exc:
            self.state = RuntimeState.PAUSED if starts_native_voice else RuntimeState.ARMED
            self.feedback.emit(
                "操作失败。目标未找到、存在歧义或未通过核验。",
                kind="error",
                allow_voice=not starts_native_voice,
            )
            return TurnOutcome(True, self.state, str(exc), plan=plan, success=False)
        success = all(result.success for result in results)
        if not success:
            failed = next(result for result in results if not result.success)
            native_may_be_active = starts_native_voice or any(
                result.success
                and result.action is not None
                and result.action.type == ActionType.START_NATIVE_VOICE
                for result in results
            )
            self.state = RuntimeState.PAUSED if native_may_be_active else RuntimeState.ARMED
            self.feedback.emit(
                "操作未完成。目标未找到、存在歧义或未通过核验。",
                kind="error",
                allow_voice=not native_may_be_active,
            )
            return TurnOutcome(
                True,
                self.state,
                failed.message,
                plan=plan,
                results=results,
                success=False,
            )

        for action in plan.actions:
            if action.type == ActionType.SET_FEEDBACK_MODE and action.feedback_mode:
                self.feedback.set_mode(
                    action.feedback_mode,
                    allow_voice=not starts_native_voice,
                )
        enters_dictation = any(action.type == ActionType.ENTER_DICTATION for action in plan.actions)
        exits_dictation = any(
            action.type == ActionType.PAUSE and action.mode == "dictation"
            for action in plan.actions
        )
        pauses_controller = any(
            action.type == ActionType.PAUSE and action.mode != "dictation"
            for action in plan.actions
        )
        resumes_controller = any(action.type == ActionType.RESUME for action in plan.actions)
        if starts_native_voice:
            # Keep only the low-cost local wake/stop detector active while the
            # target application's own voice session owns the interaction.
            self.state = RuntimeState.PAUSED
            message = "应用内语音已开启。说唤醒词可返回 HandsFreePC。"
        elif pauses_controller:
            self.state = RuntimeState.PAUSED
            message = "语音操作已暂停。说唤醒词可重新开始。"
        elif resumes_controller:
            self.state = RuntimeState.ARMED
            message = "语音操作已恢复"
        elif (keep_dictation or enters_dictation) and not exits_dictation:
            self.state = RuntimeState.DICTATION
            message = "听写已开启。说“电脑发送提示”提交。"
        else:
            self.state = RuntimeState.ARMED
            message = "操作完成"
        self.feedback.emit(message, kind="success", allow_voice=not starts_native_voice)
        return TurnOutcome(True, self.state, message, plan=plan, results=results)

    def _planner_context(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "configured_apps": sorted(self.settings.apps),
            "feedback_mode": self.feedback.mode.value,
        }

    def run_microphone(self) -> None:
        phrases = [
            *self.settings.app.wake_phrases,
            *self.settings.app.stop_phrases,
            "恢复语音操作",
            "恢复监听",
        ]
        base_dir = self.settings.config_path.parent
        self.feedback.emit("HandsFreePC 已就绪", kind="armed", duration=2)
        with LocalSpeechSession(self.settings.speech, base_dir=base_dir, phrases=phrases) as speech:
            while not self.stop_event.is_set():
                try:
                    if self.feedback.speaker.speaking.wait(timeout=0.05):
                        while self.feedback.speaker.speaking.is_set():
                            time.sleep(0.05)
                        speech.source.drain()
                    if self.state in {RuntimeState.ARMED, RuntimeState.PAUSED}:
                        matched, audio = speech.wait_for_phrase(stop_event=self.stop_event)
                        if phrase_in_text(matched, self.settings.app.stop_phrases):
                            self.handle_text(matched, require_wake=False)
                            continue
                        if self.state == RuntimeState.PAUSED:
                            self.handle_text(matched, require_wake=False)
                            continue
                        transcript = speech.transcribe(audio)
                        self.handle_text(transcript or matched, require_wake=True)
                    elif self.state in {
                        RuntimeState.AWAKE,
                        RuntimeState.DICTATION,
                        RuntimeState.CONFIRMING,
                    }:
                        timeout_seconds: float | None = None
                        if self.state == RuntimeState.AWAKE and self.last_awake_at > 0:
                            timeout_seconds = max(
                                0.0,
                                self.settings.app.awake_timeout_seconds
                                - (time.monotonic() - self.last_awake_at),
                            )
                        elif (
                            self.state == RuntimeState.CONFIRMING
                            and self.confirmation_started_at > 0
                        ):
                            timeout_seconds = max(
                                0.0,
                                self.settings.execution.confirmation_timeout_seconds
                                - (time.monotonic() - self.confirmation_started_at),
                            )
                        if timeout_seconds == 0:
                            self._expire_timeouts()
                            continue
                        audio = speech.listen_utterance(
                            timeout_seconds=timeout_seconds,
                            interrupt_phrases=self.settings.app.stop_phrases,
                        )
                        transcript = speech.transcribe(audio)
                        self.handle_text(transcript, require_wake=False)
                    else:
                        time.sleep(0.05)
                except ControlPhraseDetected as exc:
                    self.handle_text(exc.phrase, require_wake=False)
                except AudioError as exc:
                    if self._expire_timeouts() is None:
                        self.feedback.emit(str(exc), kind="error")
                    time.sleep(0.25)
                except Exception:
                    if self.state != RuntimeState.PAUSED:
                        self.state = RuntimeState.ARMED
                        self.pending_plan = None
                        self.confirmation_started_at = 0.0
                    with suppress(Exception):
                        speech.source.drain()
                    self.feedback.emit(
                        "本地语音处理异常，已恢复到安全监听状态",
                        kind="error",
                    )
                    time.sleep(0.25)

    def stop(self) -> None:
        self.stop_event.set()
        self.state = RuntimeState.STOPPED
        self.feedback.close()
