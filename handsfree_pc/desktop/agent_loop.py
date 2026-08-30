from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, replace

from ..computer_control import ComputerControlResult
from ..models import Plan, RiskLevel, clone_plan
from ..path_binding import bind_plan_paths, guard_plan_paths
from .native_skills import NativeRouteStatus, NativeSkillRouter
from .protocol import (
    DesktopAction,
    DesktopDecisionKind,
    DesktopDriver,
    DesktopExpectation,
    DesktopObservation,
)
from .safety import (
    DesktopConfirmation,
    DesktopSafetyDisposition,
    DesktopSafetyPolicy,
    action_matches_next_user_step,
    affirmatively_authorized_app_scope,
    expectation_is_terminal_user_condition,
    expectation_matches_user_step,
    user_action_step_count,
)
from .step_planner import DesktopPlannerError, DesktopStepPlanner
from .verifier import DesktopVerifier, VerificationResult


@dataclass(slots=True)
class _TaskState:
    task: str
    apps: str
    allowed_apps: frozenset[str]
    observation: DesktopObservation | None
    history: list[str]
    last_verification: VerificationResult | None
    last_action_expectation: DesktopExpectation | None
    last_action: DesktopAction | None
    last_action_target: str | None
    steps: int
    verified_action_count: int
    remaining_seconds: float


@dataclass(slots=True)
class _PendingConfirmation:
    confirmation_id: str
    summary: str
    expires_at: float
    state: _TaskState | None = None
    action: DesktopAction | None = None
    binding: DesktopConfirmation | None = None
    action_expectation: DesktopExpectation | None = None
    native_plan: Plan | None = None
    native_user_text: str | None = None
    native_binding_digest: str | None = None


_APP_ALIASES: dict[str, tuple[str, ...]] = {
    "codex": ("codex", "科德克斯", "代码助手"),
    "claude": ("claude", "克劳德"),
}


def _visible_apps(payload: str) -> tuple[str, ...]:
    """Parse the driver's local app inventory without forwarding free-form text."""

    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("desktop driver returned a non-JSON app inventory") from exc
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("desktop driver returned an invalid app inventory")
    names: list[str] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("app"), str):
            raise ValueError("desktop driver app inventory contains an invalid entry")
        name = item["app"].strip().casefold()
        if not name or len(name) > 128 or not re.fullmatch(r"[\w .+-]+", name):
            raise ValueError("desktop driver app inventory contains an unsafe identifier")
        if name not in names:
            names.append(name)
    return tuple(names)


def _explicitly_named_apps(task: str, visible_apps: tuple[str, ...]) -> frozenset[str]:
    matched: set[str] = set()
    for app in visible_apps:
        aliases = _APP_ALIASES.get(app, (app,))
        if any(affirmatively_authorized_app_scope(alias, task) for alias in aliases):
            matched.add(app)
    return frozenset(matched)


class DesktopAgentLoopController:
    """Persistent observe -> one action -> observe -> local verify controller."""

    def __init__(
        self,
        *,
        native_router: NativeSkillRouter,
        driver: DesktopDriver | None,
        planner: DesktopStepPlanner | None,
        verifier: DesktopVerifier | None = None,
        safety: DesktopSafetyPolicy | None = None,
        timeout_seconds: float = 300.0,
        confirmation_timeout_seconds: float = 15.0,
        max_steps: int = 20,
        monotonic: object = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if confirmation_timeout_seconds <= 0:
            raise ValueError("confirmation_timeout_seconds must be positive")
        if max_steps < 1 or max_steps > 100:
            raise ValueError("max_steps must be between 1 and 100")
        self.native_router = native_router
        self.driver = driver
        self.planner = planner
        self.verifier = verifier or DesktopVerifier()
        self.safety = safety or DesktopSafetyPolicy()
        self.timeout_seconds = float(timeout_seconds)
        self.confirmation_timeout_seconds = float(confirmation_timeout_seconds)
        self.max_steps = int(max_steps)
        self._monotonic = monotonic
        self._session_id = str(uuid.uuid4())
        self._execution_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._current_cancel: threading.Event | None = None
        self._pending: _PendingConfirmation | None = None
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def pending_confirmation_id(self) -> str | None:
        with self._lifecycle_lock:
            return self._pending.confirmation_id if self._pending is not None else None

    def _cancelled(self, external: threading.Event | None) -> bool:
        current = self._current_cancel
        return bool(
            (current is not None and current.is_set())
            or (external is not None and external.is_set())
        )

    @staticmethod
    def _failure(message: str, *, timed_out: bool = False) -> ComputerControlResult:
        return ComputerControlResult(
            success=False,
            message=f"FAILURE: {message}",
            timed_out=timed_out,
        )

    def _confirmation_result(
        self,
        pending: _PendingConfirmation,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult:
        with self._lifecycle_lock:
            current = self._current_cancel
            if (
                self._closed
                or current is None
                or current.is_set()
                or (cancel_event is not None and cancel_event.is_set())
            ):
                return ComputerControlResult(
                    False,
                    "FAILURE: 桌面任务已取消",
                    cancelled=True,
                )
            self._pending = pending
        return ComputerControlResult(
            success=False,
            message=f"NEEDS_CONFIRMATION: {pending.summary}",
            needs_confirmation=True,
            confirmation_id=pending.confirmation_id,
        )

    @staticmethod
    def _native_binding(plan: Plan) -> str:
        return bind_plan_paths(plan)

    @staticmethod
    def _native_confirmation_summary(plan: Plan, digest: str) -> str:
        actions = ",".join(action.type.value for action in plan.actions)
        targets = [action.path for action in plan.actions if action.path]
        target = targets[0] if len(targets) == 1 else f"{len(targets)} validated targets"
        target_display = (target or "configured UI target")[:180]
        return f"native action={actions[:64]}; exact target={target_display}; binding={digest[:10]}"

    def _native_confirmation(
        self,
        plan: Plan,
        user_text: str,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult:
        plan = clone_plan(plan)
        if self._cancelled(cancel_event):
            return ComputerControlResult(False, "FAILURE: 桌面任务已取消", cancelled=True)
        try:
            binding = self._native_binding(plan)
        except (OSError, RuntimeError, ValueError):
            return self._failure("无法建立本机确认目标的稳定身份")
        if self._cancelled(cancel_event):
            return ComputerControlResult(False, "FAILURE: 桌面任务已取消", cancelled=True)
        confirmation_id = f"native-{uuid.uuid4().hex}"
        return self._confirmation_result(
            _PendingConfirmation(
                confirmation_id=confirmation_id,
                summary=self._native_confirmation_summary(plan, binding),
                expires_at=float(self._monotonic()) + self.confirmation_timeout_seconds,
                native_plan=clone_plan(plan),
                native_user_text=user_text,
                native_binding_digest=binding,
            ),
            cancel_event=cancel_event,
        )

    def _run_native(
        self,
        instruction: str,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult | None:
        result = self.native_router.route(instruction)
        if self._cancelled(cancel_event):
            return ComputerControlResult(False, "FAILURE: 桌面任务已取消", cancelled=True)
        if result.status == NativeRouteStatus.MISS:
            return None
        if result.status == NativeRouteStatus.SUCCEEDED:
            return ComputerControlResult(
                success=True,
                message="LOCAL_VERIFIED_COMPLETION: 确定性本机技能已完成并返回本地证据",
            )
        if result.status == NativeRouteStatus.CONFIRMATION_REQUIRED and result.plan is not None:
            return self._native_confirmation(
                result.plan,
                instruction,
                cancel_event=cancel_event,
            )
        if result.status == NativeRouteStatus.BLOCKED:
            return self._failure("确定性本机安全策略阻止了该操作")
        return self._failure("确定性本机技能未能完成该操作")

    def _ensure_generic_components(self) -> ComputerControlResult | None:
        if self.driver is None:
            return self._failure("没有配置可用的本地桌面驱动")
        if self.planner is None:
            return self._failure("该命令未命中本机技能，且没有启用单步规划器")
        return None

    def _remaining(self, started_at: float) -> float:
        return max(0.0, self.timeout_seconds - (float(self._monotonic()) - started_at))

    def _drive(
        self,
        state: _TaskState,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult:
        assert self.driver is not None and self.planner is not None
        deadline = float(self._monotonic()) + state.remaining_seconds
        while state.steps < self.max_steps:
            if self._cancelled(cancel_event):
                return ComputerControlResult(False, "FAILURE: 桌面任务已取消", cancelled=True)
            remaining = deadline - float(self._monotonic())
            if remaining <= 0:
                return self._failure("桌面任务超过时间上限", timed_out=True)
            state.remaining_seconds = remaining
            try:
                planner_observation = (
                    self.safety.planner_observation(
                        state.observation,
                        user_text=state.task,
                    )
                    if state.observation is not None
                    else None
                )
                decision = self.planner.decide(
                    state.task,
                    apps=state.apps,
                    observation=planner_observation,
                    history=state.history,
                    cancel_event=self._current_cancel,
                )
            except DesktopPlannerError as exc:
                return self._failure(f"单步规划器失败：{type(exc).__name__}")
            except Exception as exc:
                return self._failure(f"单步规划器发生内部错误：{type(exc).__name__}")
            state.steps += 1

            if decision.kind == DesktopDecisionKind.FAIL:
                return self._failure("单步规划器无法提出安全且可本地核验的下一步")

            if decision.kind == DesktopDecisionKind.OBSERVE:
                assert decision.app is not None
                if decision.app.strip().casefold() not in state.allowed_apps:
                    return self._failure("规划器请求观察未在本次口述中明确授权的应用")
                try:
                    observation = self.driver.observe(
                        decision.app,
                        cancel_event=self._current_cancel,
                    )
                except Exception as exc:
                    return self._failure(f"桌面观察失败：{type(exc).__name__}")
                if observation.app.strip().casefold() not in state.allowed_apps:
                    return self._failure("桌面驱动返回了本次任务授权范围外的应用")
                inspection = self.safety.inspect_observation(
                    observation,
                    user_text=state.task,
                )
                if inspection.disposition == DesktopSafetyDisposition.BLOCK:
                    return self._failure(f"本地安全策略阻止读取该界面：{inspection.reason}")
                state.observation = observation
                state.history.append(
                    f"observed {observation.app} generation {observation.generation}"
                )
                continue

            if decision.kind == DesktopDecisionKind.ACTION:
                if state.observation is None or decision.action is None:
                    return self._failure("单步规划器在没有当前观察时要求动作")
                action = decision.action
                if action.app.strip().casefold() not in state.allowed_apps:
                    return self._failure("规划器动作超出本次口述授权的应用范围")
                target = next(
                    (
                        element
                        for element in state.observation.elements
                        if element.index == action.element_index
                    ),
                    None,
                )
                target_label = target.name if target is not None else ""
                if not action_matches_next_user_step(
                    action,
                    target_label,
                    state.task,
                    completed_steps=state.verified_action_count,
                ):
                    return self._failure("规划器动作未对应用户要求的下一个明确步骤")
                if decision.expectation is None or not expectation_matches_user_step(
                    action,
                    target_label,
                    decision.expectation,
                    state.task,
                    completed_steps=state.verified_action_count,
                ):
                    return self._failure("动作后置条件未绑定到同一个用户步骤")
                safety_result = self.safety.evaluate(
                    action,
                    state.observation,
                    user_text=state.task,
                    expectation=decision.expectation,
                )
                if safety_result.disposition == DesktopSafetyDisposition.BLOCK:
                    return self._failure(f"本地安全策略阻止动作：{safety_result.reason}")
                if safety_result.needs_confirmation:
                    assert safety_result.confirmation is not None
                    pending = _PendingConfirmation(
                        confirmation_id=f"desktop-{uuid.uuid4().hex}",
                        summary=safety_result.confirmation.summary[:320],
                        expires_at=float(self._monotonic()) + self.confirmation_timeout_seconds,
                        state=state,
                        action=action,
                        binding=safety_result.confirmation,
                        action_expectation=decision.expectation,
                    )
                    return self._confirmation_result(pending, cancel_event=cancel_event)
                action_result = self._perform_action(
                    state,
                    action,
                    expectation=decision.expectation,
                    cancel_event=cancel_event,
                )
                if action_result is not None:
                    return action_result
                continue

            if decision.kind == DesktopDecisionKind.DONE:
                if state.observation is None:
                    return self._failure("规划器报告完成，但没有可核验的本地观察")
                if state.verified_action_count == 0:
                    return self._failure("通用桌面任务尚无任何经过本地验收的动作")
                if state.verified_action_count != user_action_step_count(state.task):
                    return self._failure("尚未按顺序完成用户明确要求的全部桌面步骤")
                if decision.app is not None and (
                    decision.app.strip().casefold() not in state.allowed_apps
                ):
                    return self._failure("规划器完成条件超出本次口述授权的应用范围")
                if state.verified_action_count > 0 and (
                    state.last_action_expectation is None
                    or decision.expectation != state.last_action_expectation
                ):
                    return self._failure("完成条件必须与最后一个已建立的任务后置条件完全一致")
                if not expectation_is_terminal_user_condition(
                    decision.expectation,
                    state.task,
                    last_action=state.last_action,
                    last_action_target=state.last_action_target,
                ):
                    return self._failure("完成条件没有绑定到用户要求的最后一个正向动作")
                verified = self.verifier.verify_completion(
                    decision,
                    state.observation,
                    last_action_result=state.last_verification,
                )
                if not verified.verified:
                    return self._failure(f"本地完成条件未成立：{verified.reason}")
                return ComputerControlResult(
                    True,
                    f"LOCAL_VERIFIED_COMPLETION: {verified.reason}",
                )
        return self._failure("桌面任务达到最大单步数，未满足本地完成条件")

    def _perform_action(
        self,
        state: _TaskState,
        action: DesktopAction,
        *,
        expectation: DesktopExpectation | None,
        confirmed_binding: DesktopConfirmation | None = None,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult | None:
        assert self.driver is not None and state.observation is not None
        planner_observation = state.observation
        if expectation is None:
            return self._failure("桌面动作缺少任务相关的本地后置条件")
        if self._cancelled(cancel_event):
            return ComputerControlResult(False, "FAILURE: 桌面任务已取消", cancelled=True)
        try:
            before = self.driver.observe(
                action.app,
                cancel_event=self._current_cancel,
            )
            if before.fingerprint != planner_observation.fingerprint:
                return self._failure("执行前界面已经变化，已拒绝使用过期规划")
            inspection = self.safety.inspect_observation(before, user_text=state.task)
            if inspection.disposition == DesktopSafetyDisposition.BLOCK:
                return self._failure(f"执行前本地安全策略阻止读取界面：{inspection.reason}")
            rebound = replace(action, generation=before.generation)
            fresh_safety = self.safety.evaluate(
                rebound,
                before,
                user_text=state.task,
                expectation=expectation,
            )
            if confirmed_binding is None:
                if fresh_safety.disposition != DesktopSafetyDisposition.ALLOW:
                    return self._failure("执行前本地安全分类不再允许该动作")
            else:
                if (
                    fresh_safety.disposition != DesktopSafetyDisposition.CONFIRM
                    or fresh_safety.confirmation is None
                    or fresh_safety.confirmation.action_digest != confirmed_binding.action_digest
                    or fresh_safety.confirmation.summary != confirmed_binding.summary
                ):
                    return self._failure("确认前本地安全分类发生变化，已拒绝执行")
            already_true = self.verifier.verify_expectation(
                expectation,
                before,
                last_action_result=None,
            )
            if already_true.verified:
                return self._failure("动作后置条件在执行前已经成立，不能证明本次动作完成")
            with self._lifecycle_lock:
                if self._cancelled(cancel_event):
                    return ComputerControlResult(
                        False,
                        "FAILURE: 桌面任务已取消",
                        cancelled=True,
                    )
                receipt = self.driver.execute(
                    rebound,
                    before,
                    cancel_event=self._current_cancel,
                )
            after = self.driver.observe(
                rebound.app,
                cancel_event=self._current_cancel,
            )
        except Exception as exc:
            return self._failure(f"桌面动作或刷新失败：{type(exc).__name__}")
        verified = self.verifier.verify_action(rebound, receipt, before, after)
        if not verified.verified:
            return self._failure(f"动作后本地验收失败：{verified.reason}")
        expected_result = self.verifier.verify_expectation(
            expectation,
            after,
            last_action_result=verified,
        )
        if not expected_result.verified:
            return self._failure(f"动作后任务条件未成立：{expected_result.reason}")
        inspection = self.safety.inspect_observation(after, user_text=state.task)
        if inspection.disposition == DesktopSafetyDisposition.BLOCK:
            return self._failure(f"动作后界面不能发送给规划器：{inspection.reason}")
        state.observation = after
        state.last_verification = expected_result
        state.last_action_expectation = expectation
        state.last_action = rebound
        target = next(
            (element for element in before.elements if element.index == rebound.element_index),
            None,
        )
        state.last_action_target = target.name if target is not None else None
        state.verified_action_count += 1
        state.history.append(f"locally verified {rebound.type.value}: {expected_result.reason}")
        return None

    def run(
        self,
        instruction: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ComputerControlResult:
        if not isinstance(instruction, str) or not instruction.strip():
            return self._failure("电脑控制指令为空")
        with self._execution_lock:
            with self._lifecycle_lock:
                if self._closed:
                    return self._failure("桌面控制器已经关闭")
                if self._pending is not None:
                    return self._failure("上一动作仍在等待精确确认")
                self._current_cancel = threading.Event()
            started_at = float(self._monotonic())
            try:
                native_result = self._run_native(
                    instruction.strip(),
                    cancel_event=cancel_event,
                )
                if native_result is not None:
                    return native_result
                if failure := self._ensure_generic_components():
                    return failure
                assert self.driver is not None
                try:
                    self.driver.start()
                    inventory = self.driver.list_apps(cancel_event=self._current_cancel)
                    visible_apps = _visible_apps(inventory)
                except Exception as exc:
                    return self._failure(f"本地桌面驱动不可用：{type(exc).__name__}")
                allowed_apps = _explicitly_named_apps(instruction, visible_apps)
                if len(allowed_apps) != 1:
                    return self._failure(
                        "通用桌面任务必须在本次口述中明确且肯定地指定唯一一个当前可见应用"
                    )
                apps = json.dumps(
                    [{"app": app, "visible_window_count": 1} for app in sorted(allowed_apps)],
                    ensure_ascii=False,
                    sort_keys=True,
                )
                state = _TaskState(
                    task=instruction.strip(),
                    apps=apps,
                    allowed_apps=allowed_apps,
                    observation=None,
                    history=[],
                    last_verification=None,
                    last_action_expectation=None,
                    last_action=None,
                    last_action_target=None,
                    steps=0,
                    verified_action_count=0,
                    remaining_seconds=self._remaining(started_at),
                )
                return self._drive(state, cancel_event=cancel_event)
            finally:
                with self._lifecycle_lock:
                    self._current_cancel = None

    def execute(
        self,
        instruction: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ComputerControlResult:
        return self.run(instruction, cancel_event=cancel_event)

    def _confirm_native(
        self,
        pending: _PendingConfirmation,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult:
        assert pending.native_plan is not None
        assert pending.native_user_text is not None and pending.native_binding_digest is not None
        if self._cancelled(cancel_event):
            return ComputerControlResult(False, "FAILURE: 确认后的本机动作已取消", cancelled=True)
        try:
            prepared = self.native_router.executor.prepare_plan(clone_plan(pending.native_plan))
            evaluated = self.native_router.safety.evaluate(
                prepared,
                user_text=pending.native_user_text,
            )
            evaluated = clone_plan(evaluated)
            if self._cancelled(cancel_event):
                return ComputerControlResult(
                    False,
                    "FAILURE: 确认后的本机动作已取消",
                    cancelled=True,
                )
            if evaluated.risk != RiskLevel.CONFIRM:
                return self._failure("确认时本机安全分类已经变化")
            if self._native_binding(evaluated) != pending.native_binding_digest:
                return self._failure("确认时本机目标身份或内容已经变化")
            if self._cancelled(cancel_event):
                return ComputerControlResult(
                    False,
                    "FAILURE: 确认后的本机动作已取消",
                    cancelled=True,
                )
            with (
                guard_plan_paths(
                    evaluated,
                    pending.native_binding_digest,
                ),
                self._lifecycle_lock,
            ):
                if self._cancelled(cancel_event):
                    return ComputerControlResult(
                        False,
                        "FAILURE: 确认后的本机动作已取消",
                        cancelled=True,
                    )
                results = tuple(self.native_router.executor.execute_plan(evaluated))
        except Exception as exc:
            return self._failure(f"确认后的本机动作失败：{type(exc).__name__}")
        if not NativeSkillRouter.execution_is_locally_verified(
            evaluated,
            results,
        ):
            return self._failure("确认后的本机动作没有完成全部后置检查")
        return ComputerControlResult(
            True,
            "LOCAL_VERIFIED_COMPLETION: 已执行精确确认的本机动作",
        )

    def _confirm_desktop(
        self,
        pending: _PendingConfirmation,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult:
        assert self.driver is not None
        assert (
            pending.state is not None and pending.action is not None and pending.binding is not None
        )
        state = pending.state
        if state.observation is None:
            return self._failure("确认动作丢失了原始观察")
        state.remaining_seconds = max(state.remaining_seconds, 0.1)
        failure = self._perform_action(
            state,
            pending.action,
            expectation=pending.action_expectation,
            confirmed_binding=pending.binding,
            cancel_event=cancel_event,
        )
        if failure is not None:
            return failure
        return self._drive(state, cancel_event=cancel_event)

    def confirm(
        self,
        confirmation_id: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ComputerControlResult:
        with self._execution_lock:
            with self._lifecycle_lock:
                if self._closed:
                    return self._failure("桌面控制器已经关闭")
                pending = self._pending
                if pending is None or confirmation_id != pending.confirmation_id:
                    return self._failure("确认标识不匹配或已经使用")
                self._pending = None
                self._current_cancel = threading.Event()
            try:
                if float(self._monotonic()) > pending.expires_at:
                    return self._failure("确认已经超时")
                if pending.native_plan is not None:
                    return self._confirm_native(pending, cancel_event=cancel_event)
                return self._confirm_desktop(pending, cancel_event=cancel_event)
            finally:
                with self._lifecycle_lock:
                    self._current_cancel = None

    def cancel(self) -> bool:
        with self._lifecycle_lock:
            event = self._current_cancel
            pending = self._pending
            self._pending = None
        if event is not None:
            event.set()
        driver_cancelled = self.driver.cancel() if self.driver is not None else False
        return bool(event is not None or pending is not None or driver_cancelled)

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
        self.cancel()
        if self.driver is not None:
            self.driver.close()
