from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol

from ..config import Settings
from ..intents import DeterministicIntentParser
from ..models import ActionType, ExecutionResult, Plan, RiskLevel, clone_plan
from ..path_binding import bind_plan_paths, guard_plan_paths
from ..safety import SafetyPolicy
from ..windows.executor import WindowsExecutor


class NativeRouteStatus(StrEnum):
    """Terminal outcome of one deterministic routing attempt."""

    MISS = "miss"
    BLOCKED = "blocked"
    CONFIRMATION_REQUIRED = "confirmation_required"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class NativeSkillResult:
    """A locally evidenced result; a miss is the only valid planner fallback seam."""

    status: NativeRouteStatus
    message: str
    plan: Plan | None = None
    execution_results: tuple[ExecutionResult, ...] = ()

    @property
    def matched(self) -> bool:
        return self.status != NativeRouteStatus.MISS

    @property
    def success(self) -> bool:
        return self.status == NativeRouteStatus.SUCCEEDED

    @property
    def needs_confirmation(self) -> bool:
        return self.status == NativeRouteStatus.CONFIRMATION_REQUIRED


class NativeExecutor(Protocol):
    def prepare_plan(self, plan: Plan) -> Plan: ...

    def execute_plan(self, plan: Plan) -> list[ExecutionResult]: ...


class NativeSkillRouter:
    """Run allow-listed deterministic skills before any model-based planner.

    The router intentionally has no LLM or shell execution hook. A deterministic
    parser miss is returned to the caller, which may then choose a separately
    configured desktop agent. Once a deterministic plan matches, every target is
    prepared and safety-checked locally before the first UI action.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        parser: DeterministicIntentParser | None = None,
        executor: NativeExecutor | None = None,
        safety: SafetyPolicy | None = None,
    ) -> None:
        self.settings = settings
        self.parser = parser or DeterministicIntentParser()
        self.executor = executor or WindowsExecutor(settings)
        self.safety = safety or SafetyPolicy(settings.execution)

    def can_route(self, text: str) -> bool:
        """Report a deterministic match without preparing or executing it."""

        plan = self.parser.parse(text)
        return plan is not None and self._covers_full_request(text, plan)

    @staticmethod
    def execution_is_locally_verified(
        plan: Plan,
        results: tuple[ExecutionResult, ...],
    ) -> bool:
        """Require an explicit local postcondition for every dispatched action."""

        if len(results) != len(plan.actions):
            return False
        for action, result in zip(plan.actions, results, strict=True):
            if not result.success or result.action != action:
                return False
            evidence = result.evidence or {}
            if evidence.get("dry_run") is True:
                return False
            if not (
                evidence.get("postcondition_verified") is True or evidence.get("verified") is True
            ):
                return False
        return True

    def _covers_full_request(self, text: str, plan: Plan) -> bool:
        """Require exact parser coverage and a strongly verifiable action set."""

        action_types = {action.type for action in plan.actions}
        if action_types & {ActionType.START_NATIVE_VOICE, ActionType.SEND_PROMPT}:
            # These legacy native actions currently lack a strong, target-bound
            # local postcondition.  Let the generic desktop loop observe, act,
            # and verify them instead.  Runtime-internal controls are separate.
            return False
        for action in plan.actions:
            if action.type != ActionType.OPEN_MODE:
                continue
            profile = self.settings.apps.get((action.app or "").casefold())
            if profile is None:
                return False
            mapped_modes = {name.casefold() for name in profile.mode_names}
            if (action.mode or "").casefold() not in mapped_modes:
                return False
            if action.tab and action.tab.casefold() not in mapped_modes:
                return False
        coverage_check = getattr(self.parser, "covers_full_text", None)
        if not callable(coverage_check):
            return False
        try:
            return bool(coverage_check(text, plan))
        except (TypeError, ValueError):
            return False

    def route(
        self,
        text: str,
        *,
        explicit_submission: bool = False,
    ) -> NativeSkillResult:
        plan = self.parser.parse(text)
        if plan is None or not self._covers_full_request(text, plan):
            return NativeSkillResult(
                status=NativeRouteStatus.MISS,
                message="NATIVE_ROUTE_MISS: 未命中确定性本地操作",
            )

        try:
            prepared = self.executor.prepare_plan(plan)
        except Exception as exc:
            failed = replace(plan, risk=RiskLevel.BLOCKED, summary="本地目标解析失败")
            return NativeSkillResult(
                status=NativeRouteStatus.FAILED,
                message=f"NATIVE_PREPARE_FAILED: {type(exc).__name__}: {exc}",
                plan=failed,
            )

        evaluated = self.safety.evaluate(
            prepared,
            user_text=text,
            explicit_submission=explicit_submission,
        )
        safe_path_binding: str | None = None
        has_open_path = any(action.type == ActionType.OPEN_PATH for action in evaluated.actions)
        if evaluated.risk == RiskLevel.SAFE and has_open_path:
            # A deterministic route must not be a shortcut around the runtime's
            # path-identity boundary. Re-evaluate the exact target between two
            # bindings, then hold it stable through the executor call.
            try:
                before_classification = bind_plan_paths(evaluated)
                evaluated = self.safety.evaluate(
                    evaluated,
                    user_text=text,
                    explicit_submission=explicit_submission,
                )
                evaluated = clone_plan(evaluated)
                if evaluated.risk == RiskLevel.SAFE:
                    after_classification = bind_plan_paths(evaluated)
                    if after_classification != before_classification:
                        raise RuntimeError("path identity changed during safety classification")
                    safe_path_binding = after_classification
            except Exception as exc:
                return NativeSkillResult(
                    status=NativeRouteStatus.FAILED,
                    message=f"NATIVE_PATH_BINDING_FAILED: {type(exc).__name__}: {exc}",
                    plan=evaluated,
                )
        if evaluated.risk == RiskLevel.BLOCKED:
            return NativeSkillResult(
                status=NativeRouteStatus.BLOCKED,
                message=f"NATIVE_ROUTE_BLOCKED: {evaluated.summary}",
                plan=evaluated,
            )
        if evaluated.risk == RiskLevel.CONFIRM:
            return NativeSkillResult(
                status=NativeRouteStatus.CONFIRMATION_REQUIRED,
                message=f"NATIVE_CONFIRMATION_REQUIRED: {evaluated.summary}",
                plan=evaluated,
            )

        evaluated = clone_plan(evaluated)
        try:
            if has_open_path:
                if safe_path_binding is None:
                    raise RuntimeError("safe path execution is missing its identity binding")
                with guard_plan_paths(evaluated, safe_path_binding):
                    results = tuple(self.executor.execute_plan(evaluated))
            else:
                results = tuple(self.executor.execute_plan(evaluated))
        except Exception as exc:
            return NativeSkillResult(
                status=NativeRouteStatus.FAILED,
                message=f"NATIVE_EXECUTION_FAILED: {type(exc).__name__}: {exc}",
                plan=evaluated,
            )

        completed = self.execution_is_locally_verified(evaluated, results)
        if not completed:
            failure = next((result.message for result in results if not result.success), None)
            message = failure or "动作已请求，但缺少逐动作的本地后置条件证据"
            return NativeSkillResult(
                status=NativeRouteStatus.FAILED,
                message=f"NATIVE_EXECUTION_FAILED: {message}",
                plan=evaluated,
                execution_results=results,
            )
        return NativeSkillResult(
            status=NativeRouteStatus.SUCCEEDED,
            message=f"NATIVE_EXECUTION_SUCCEEDED: {evaluated.summary}",
            plan=evaluated,
            execution_results=results,
        )
