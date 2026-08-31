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
    redact_credential_like_text,
)
from .safety import (
    DesktopActionBinding,
    DesktopConfirmation,
    DesktopSafetyDisposition,
    DesktopSafetyPolicy,
    DesktopSafetyProfile,
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
    stale_replans: int = 0


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

_EXPLICIT_APP_SCOPE_SLOT_PATTERNS = (
    re.compile(
        r"\b(?:go|navigate|switch)\s+to\s+(?:the\s+)?"
        r"(?P<app>[A-Za-z][A-Za-z0-9_.+ -]{0,63}?)"
        r"(?:\s+(?:app|application))?"
        r"(?=\s*(?:[,;]\s*)?(?:(?:and|then)\s+)?(?:click|open|switch|"
        r"enter|type|press|scroll|activate|focus)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:open|launch|select)\s+(?:the\s+)?"
        r"(?P<app>[A-Za-z][A-Za-z0-9_.+ -]{0,63}?)"
        r"(?:\s+(?:app|application))?"
        r"(?=\s*(?:[,;]\s*)?(?:(?:and|then)\s+)?(?:click|open|switch|"
        r"enter|type|press|scroll|activate|focus)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:use|using|with)\s+(?:the\s+)?"
        r"(?P<app>[A-Za-z][A-Za-z0-9_.+ -]{0,63}?)\s*"
        r"(?=(?:to\s+)?(?:click|open|switch|select|choose|enter|type|press|"
        r"scroll|activate|focus)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:using|with)\s+(?:the\s+)?"
        r"(?P<app>[A-Za-z][A-Za-z0-9_.+ -]{0,63}?)\s*"
        r"(?=$|[,.。；;:：]|\s+(?:to|and|then)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:in|inside|within|on)\s+(?:the\s+)?"
        r"(?P<app>[A-Za-z][A-Za-z0-9_.+ -]{0,63}?)"
        r"(?:\s+(?:app|application))?\s*"
        r"(?=[,;:，；：]|\b(?:click|open|switch|select|choose|enter|type|press|"
        r"scroll|activate|focus)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:in|inside|within|on)\s+(?:the\s+)?"
        r"(?P<app>[A-Za-z][A-Za-z0-9_.+ -]{0,63}?)"
        r"(?:\s+(?:app|application))?\s*"
        r"(?=$|[,.。；;:：]|\s+(?:to|and|then)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\w)(?:打开|启动|选择|进入|(?:切换|导航)(?:到|至)?)\s*"
        r"(?P<app>[\w.+ -]{1,64}?)(?:\s*(?:app|应用))?"
        r"(?=\s*(?:[,，]\s*)?(?:然后|并且|并|再)\s*"
        r"(?:点击|打开|切换|选择|进入|输入|按|滚动))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\w)(?:在|于)\s*(?P<app>[\w.+ -]{1,64}?)"
        r"(?:\s*(?:app|应用))?\s*(?:里面|里|中|内|上)?"
        r"\s*(?:的\s*)?"
        r"(?=\s*(?:点击|打开|切换|选择|进入|输入|按|滚动))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\w)(?:在|于)\s*(?P<app>[\w.+ -]{1,64}?)"
        r"(?:\s*(?:app|应用))?\s*(?:里面|里|中|内|上)?"
        r"\s*(?:的\s*)?"
        r"\s*(?=$|[,，。；;:：]|(?:以便|然后|并且|并|来|从而))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:点击|打开|切换|选择|进入)\s*"
        r"(?P<app>[\w.+ -]{1,64}?)(?:\s*(?:app|应用))?"
        r"(?:里面|里|中|内|上)的",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\w)(?:使用|用|去到|到|去)\s*"
        r"(?P<app>[\w.+ -]{1,64}?)(?:\s*(?:app|应用))?"
        r"(?=\s*(?:点击|打开|切换|选择|进入|输入|按|滚动))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[,.!?;，。！？；]\s*)"
        r"(?P<app>[A-Za-z\u4e00-\u9fff][\w.+ -]{0,63}?)\s*[:：]"
        r"(?=\s*(?:click|open|switch|select|choose|enter|type|press|scroll|"
        r"activate|focus|点击|打开|切换|选择|进入|输入|按|滚动))",
        re.IGNORECASE,
    ),
)

_APP_SCOPE_CANDIDATE_ACTION_RE = re.compile(
    r"(?:点击|打开|切换|选择|进入|输入|按|滚动|"
    r"\b(?:click|open|switch|select|choose|enter|type|press|scroll|activate|focus)\b)",
    re.IGNORECASE,
)

_SAFE_LOCAL_EXCEPTION_TYPES = frozenset(
    {
        "AmbiguousWindowError",
        "ForegroundIntegrityBoundary",
        "PasswordFieldError",
        "UIAUnavailableError",
        "WindowNotFoundError",
        "WindowsUiaDriverError",
        "WindowsUiaStaleObservation",
    }
)

_MAX_STALE_REPLANS = 2


def _safe_exception_message(exc: Exception) -> str:
    name = type(exc).__name__
    if name not in _SAFE_LOCAL_EXCEPTION_TYPES:
        return name
    value = redact_credential_like_text(str(exc).strip()) or name
    return value[:240]


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


def _app_scope_is_affirmative(candidate: str, task: str) -> bool:
    """Apply shared quote, payload, and negation gates to alternate scope grammar."""

    if affirmatively_authorized_app_scope(candidate, task):
        return True
    escaped = re.escape(candidate)
    rewrites = (
        re.sub(
            rf"\b(?:use|using|with)\s+(?:the\s+)?{escaped}\b",
            f"in {candidate}",
            task,
            flags=re.IGNORECASE,
        ),
        re.sub(
            rf"\b(?:go|navigate|switch)\s+to\s+(?:the\s+)?{escaped}(?=$|[\s,.;:])",
            f"in {candidate}",
            task,
            flags=re.IGNORECASE,
        ),
        re.sub(
            rf"\b(?:open|launch|select)\s+(?:the\s+)?{escaped}(?=$|[\s,.;:])",
            f"in {candidate}",
            task,
            flags=re.IGNORECASE,
        ),
        re.sub(
            rf"(?<!\w)(?:使用|用|去到|到|去)\s*{escaped}",
            f"在{candidate}中",
            task,
            flags=re.IGNORECASE,
        ),
        re.sub(
            rf"(?<!\w)(?:打开|启动|选择|进入|(?:切换|导航)(?:到|至)?)\s*"
            rf"{escaped}(?:\s*(?:app|应用))?\s*(?:[,，]\s*)?"
            rf"(?:然后|并且|并|再)(?=\s*(?:点击|打开|切换|选择|进入|输入|按|滚动))",
            f"在{candidate}中",
            task,
            flags=re.IGNORECASE,
        ),
        re.sub(
            rf"(?<!\w)(?:打开|启动|选择|进入|(?:切换|导航)(?:到|至)?)\s*{escaped}",
            f"在{candidate}中",
            task,
            flags=re.IGNORECASE,
        ),
        re.sub(
            rf"(?<![\w]){escaped}\s*[:：]",
            f"In {candidate},",
            task,
            flags=re.IGNORECASE,
        ),
    )
    return any(
        rewritten != task
        and affirmatively_authorized_app_scope(candidate, rewritten)
        for rewritten in rewrites
    )


def _explicitly_named_apps(task: str, visible_apps: tuple[str, ...]) -> frozenset[str]:
    matched: set[str] = set()
    for app in visible_apps:
        aliases = _APP_ALIASES.get(app, (app,))
        if any(_app_scope_is_affirmative(alias, task) for alias in aliases):
            matched.add(app)
    return frozenset(matched)


def _unsupported_explicit_app_scopes(
    task: str,
    known_apps: tuple[str, ...],
) -> tuple[str, ...]:
    """Detect an explicit location slot that cannot map to a configured app.

    This is local fail-closed parsing only: unknown names are never forwarded
    and must not silently inherit a previous trusted application.
    """

    known_aliases = {
        alias.casefold()
        for app in known_apps
        for alias in _APP_ALIASES.get(app, (app,))
    }
    unknown: list[str] = []
    anaphoric_references = frozenset(
        {"其", "其中", "这", "这个", "那", "那个", "该", "本", "此", "上述", "前述"}
    )

    for pattern in _EXPLICIT_APP_SCOPE_SLOT_PATTERNS:
        for match in pattern.finditer(task):
            candidate = " ".join(match.group("app").strip().casefold().split())
            if re.sub(r"\s+", "", candidate) in anaphoric_references:
                # These words refer back to an already named object/app.  They are
                # not a new explicit application scope and must never be guessed or
                # fuzzy-mapped to one.
                continue
            if (
                candidate
                and _APP_SCOPE_CANDIDATE_ACTION_RE.search(candidate) is None
                and candidate not in known_aliases
                and candidate not in unknown
                and _app_scope_is_affirmative(candidate, task)
            ):
                unknown.append(candidate)
    return tuple(unknown)


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
        sleeper: object = time.sleep,
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
        self._sleep = sleeper
        self._session_id = str(uuid.uuid4())
        self._execution_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._current_cancel: threading.Event | None = None
        self._pending: _PendingConfirmation | None = None
        self._closed = False
        self._trusted_app_context: str | None = None
        self._trusted_window_id: str | None = None

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
    def _failure(
        message: str,
        *,
        stage: str,
        error_code: str,
        timed_out: bool = False,
        exception_type: str | None = None,
        app: str | None = None,
        generation: int | None = None,
    ) -> ComputerControlResult:
        safe_message = (redact_credential_like_text(message) or "desktop control failed")[:320]
        return ComputerControlResult(
            success=False,
            message=f"FAILURE: {safe_message}",
            timed_out=timed_out,
            stage=stage,
            error_code=error_code,
            safe_message=safe_message,
            exception_type=exception_type,
            app=app,
            generation=generation,
        )

    @staticmethod
    def _cancelled_result(
        message: str = "桌面任务已取消",
        *,
        stage: str = "runtime",
    ) -> ComputerControlResult:
        safe_message = message[:320]
        return ComputerControlResult(
            success=False,
            message=f"FAILURE: {safe_message}",
            cancelled=True,
            stage=stage,
            error_code="CANCELLED",
            safe_message=safe_message,
        )

    def _publish_native_success(
        self,
        message: str,
        *,
        cancel_event: threading.Event | None,
        context_expected: bool,
        context_refreshed: bool,
    ) -> ComputerControlResult:
        """Linearize native completion against cancellation under one lock."""

        with self._lifecycle_lock:
            current = self._current_cancel
            cancelled = bool(
                self._closed
                or (current is not None and current.is_set())
                or (cancel_event is not None and cancel_event.is_set())
            )
            if cancelled:
                self._trusted_app_context = None
                self._trusted_window_id = None
                return self._cancelled_result(
                    "本机动作完成检查期间收到取消；不发布成功状态",
                    stage="native_route",
                )
            if context_expected and not context_refreshed:
                message += "；未建立连续窗口上下文，下一条指令必须再次明确应用"
            return ComputerControlResult(success=True, message=message)

    def _set_driver_task_context(self, task: str | None) -> None:
        if self.driver is None:
            return
        setter = getattr(self.driver, "set_task_context", None)
        if callable(setter):
            try:
                setter(None if task is None else task[:16000])
            except Exception:
                # Task relevance only affects bounded-element priority; it is
                # never allowed to weaken binding or make the driver unusable.
                return

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
                return self._cancelled_result()
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
            return self._cancelled_result(stage="native_route")
        try:
            binding = self._native_binding(plan)
        except (OSError, RuntimeError, ValueError):
            return self._failure(
                "无法建立本机确认目标的稳定身份",
                stage="native_route",
                error_code="NATIVE_CONFIRMATION_BINDING_FAILED",
            )
        if self._cancelled(cancel_event):
            return self._cancelled_result(stage="native_route")
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
        if result.plan is not None and any(action.app for action in result.plan.actions):
            # An explicit app-scoped native request supersedes the preceding
            # window even when the router executed a switch concurrently with
            # cancellation, or this request later fails or needs approval.
            self._clear_trusted_context()
        if self._cancelled(cancel_event):
            return self._cancelled_result(stage="native_route")
        if result.status == NativeRouteStatus.MISS:
            return None
        if result.status == NativeRouteStatus.SUCCEEDED:
            context_expected = bool(
                result.plan is not None
                and self.driver is not None
                and self.safety.profile == DesktopSafetyProfile.PERSONAL_TRUSTED
                and len(
                    {
                        action.app.strip().casefold()
                        for action in result.plan.actions
                        if isinstance(action.app, str) and action.app.strip()
                    }
                )
                == 1
            )
            context_refreshed = self._refresh_trusted_context_after_native(
                result.plan,
                user_text=instruction,
                cancel_event=cancel_event,
            )
            return self._publish_native_success(
                "LOCAL_VERIFIED_COMPLETION: 确定性本机技能已完成并返回本地证据",
                cancel_event=cancel_event,
                context_expected=context_expected,
                context_refreshed=context_refreshed,
            )
        if result.status == NativeRouteStatus.CONFIRMATION_REQUIRED and result.plan is not None:
            return self._native_confirmation(
                result.plan,
                instruction,
                cancel_event=cancel_event,
            )
        if result.status == NativeRouteStatus.BLOCKED:
            return self._failure(
                "确定性本机安全策略阻止了该操作",
                stage="native_route",
                error_code="NATIVE_ROUTE_BLOCKED",
            )
        return self._failure(
            "确定性本机技能未能完成该操作",
            stage="native_route",
            error_code="NATIVE_ROUTE_FAILED",
        )

    def _ensure_generic_components(self) -> ComputerControlResult | None:
        if self.driver is None:
            return self._failure(
                "没有配置可用的本地桌面驱动",
                stage="runtime",
                error_code="DRIVER_NOT_CONFIGURED",
            )
        if self.planner is None:
            return self._failure(
                "该命令未命中本机技能，且没有启用单步规划器",
                stage="plan",
                error_code="PLANNER_NOT_CONFIGURED",
            )
        return None

    def _remaining(self, started_at: float) -> float:
        return max(0.0, self.timeout_seconds - (float(self._monotonic()) - started_at))

    def _trusted_context(self) -> tuple[str, str] | None:
        if self.safety.profile != DesktopSafetyProfile.PERSONAL_TRUSTED:
            return None
        with self._lifecycle_lock:
            if self._trusted_app_context is None or self._trusted_window_id is None:
                return None
            return self._trusted_app_context, self._trusted_window_id

    def _clear_trusted_context(self) -> None:
        with self._lifecycle_lock:
            self._trusted_app_context = None
            self._trusted_window_id = None

    def _remember_trusted_context(
        self,
        state: _TaskState,
        result: ComputerControlResult,
    ) -> None:
        if (
            not result.success
            or self.safety.profile != DesktopSafetyProfile.PERSONAL_TRUSTED
            or state.observation is None
            or not state.observation.local_window_id
        ):
            return
        with self._lifecycle_lock:
            if self._closed or (
                self._current_cancel is not None and self._current_cancel.is_set()
            ):
                return
            self._trusted_app_context = state.observation.app.strip().casefold()
            self._trusted_window_id = state.observation.local_window_id

    def _refresh_trusted_context_after_native(
        self,
        plan: Plan | None,
        *,
        user_text: str,
        cancel_event: threading.Event | None,
    ) -> bool:
        """Bind a deterministic app action to one freshly inspected HWND.

        Native execution has its own local postconditions, but it previously did
        not give the next queued utterance a desktop scope.  The context is
        established only from a fresh UIA observation after the native action.
        """

        self._clear_trusted_context()
        if (
            plan is None
            or self.driver is None
            or self.safety.profile != DesktopSafetyProfile.PERSONAL_TRUSTED
            or self._cancelled(cancel_event)
        ):
            return False
        apps = {
            action.app.strip().casefold()
            for action in plan.actions
            if isinstance(action.app, str) and action.app.strip()
        }
        if len(apps) != 1:
            return False
        app = next(iter(apps))
        try:
            self._set_driver_task_context(user_text)
            self.driver.start()
            observation = self.driver.observe(
                app,
                cancel_event=self._current_cancel,
            )
        except Exception:
            return False
        if (
            self._cancelled(cancel_event)
            or observation.app.strip().casefold() != app
            or not observation.local_window_id
        ):
            return False
        inspection = self.safety.inspect_observation(
            observation,
            user_text=user_text,
        )
        if inspection.disposition == DesktopSafetyDisposition.BLOCK:
            return False
        with self._lifecycle_lock:
            if self._closed or self._cancelled(cancel_event):
                return False
            self._trusted_app_context = app
            self._trusted_window_id = observation.local_window_id
        return True

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
                return self._cancelled_result(stage="runtime")
            remaining = deadline - float(self._monotonic())
            if remaining <= 0:
                return self._failure(
                    "桌面任务超过时间上限",
                    stage="runtime",
                    error_code="DESKTOP_TASK_TIMEOUT",
                    timed_out=True,
                )
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
                return self._failure(
                    f"单步规划器失败：{_safe_exception_message(exc)}",
                    stage="plan",
                    error_code="PLANNER_FAILED",
                    exception_type=type(exc).__name__,
                )
            except Exception as exc:
                return self._failure(
                    f"单步规划器发生内部错误：{type(exc).__name__}",
                    stage="plan",
                    error_code="PLANNER_INTERNAL_ERROR",
                    exception_type=type(exc).__name__,
                )
            state.steps += 1

            if decision.kind == DesktopDecisionKind.FAIL:
                return self._failure(
                    "单步规划器无法提出安全且可本地核验的下一步",
                    stage="plan",
                    error_code="PLANNER_NO_SAFE_STEP",
                )

            if decision.kind == DesktopDecisionKind.OBSERVE:
                assert decision.app is not None
                if decision.app.strip().casefold() not in state.allowed_apps:
                    return self._failure(
                        "规划器请求观察未在本次口述中明确授权的应用",
                        stage="plan",
                        error_code="PLANNER_APP_SCOPE_VIOLATION",
                    )
                try:
                    observation = self.driver.observe(
                        decision.app,
                        cancel_event=self._current_cancel,
                    )
                except Exception as exc:
                    return self._failure(
                        f"桌面观察失败：{_safe_exception_message(exc)}",
                        stage="observe_driver",
                        error_code="OBSERVE_DRIVER_FAILED",
                        exception_type=type(exc).__name__,
                        app=decision.app,
                    )
                if observation.app.strip().casefold() not in state.allowed_apps:
                    return self._failure(
                        "桌面驱动返回了本次任务授权范围外的应用",
                        stage="observe_driver",
                        error_code="OBSERVED_APP_SCOPE_MISMATCH",
                        app=observation.app,
                        generation=observation.generation,
                    )
                inspection = self.safety.inspect_observation(
                    observation,
                    user_text=state.task,
                )
                if inspection.disposition == DesktopSafetyDisposition.BLOCK:
                    return self._failure(
                        f"本地安全策略阻止读取该界面：{inspection.reason}",
                        stage="observe_safety",
                        error_code="OBSERVATION_BLOCKED",
                        app=observation.app,
                        generation=observation.generation,
                    )
                state.observation = observation
                state.history.append(
                    f"observed {observation.app} generation {observation.generation}"
                )
                continue

            if decision.kind == DesktopDecisionKind.ACTION:
                if state.observation is None or decision.action is None:
                    return self._failure(
                        "单步规划器在没有当前观察时要求动作",
                        stage="plan",
                        error_code="ACTION_WITHOUT_OBSERVATION",
                    )
                action = decision.action
                if action.app.strip().casefold() not in state.allowed_apps:
                    return self._failure(
                        "规划器动作超出本次口述授权的应用范围",
                        stage="plan",
                        error_code="PLANNER_APP_SCOPE_VIOLATION",
                    )
                target = next(
                    (
                        element
                        for element in state.observation.elements
                        if element.index == action.element_index
                    ),
                    None,
                )
                target_label = target.name if target is not None else ""
                direct_action_match = action_matches_next_user_step(
                    action,
                    target_label,
                    state.task,
                    completed_steps=state.verified_action_count,
                )
                direct_expectation_match = bool(
                    decision.expectation is not None
                    and expectation_matches_user_step(
                        action,
                        target_label,
                        decision.expectation,
                        state.task,
                        completed_steps=state.verified_action_count,
                    )
                )
                binding = (
                    DesktopActionBinding.USER_STEP
                    if direct_action_match and direct_expectation_match
                    else self.safety.classify_personal_action_binding(
                        action,
                        target,
                        decision.expectation,
                        user_text=state.task,
                        completed_steps=state.verified_action_count,
                    )
                )
                if binding is None:
                    return self._failure(
                        "规划器动作未对应用户要求或可信本机导航步骤",
                        stage="action_safety",
                        error_code="ACTION_NOT_BOUND_TO_TASK",
                        app=state.observation.app,
                        generation=state.observation.generation,
                    )
                if decision.expectation is None:
                    return self._failure(
                        "动作缺少可本地核验的后置条件",
                        stage="plan",
                        error_code="ACTION_POSTCONDITION_MISSING",
                    )
                safety_result = self.safety.evaluate(
                    action,
                    state.observation,
                    user_text=state.task,
                    expectation=decision.expectation,
                )
                if safety_result.disposition == DesktopSafetyDisposition.BLOCK:
                    return self._failure(
                        f"本地安全策略阻止动作：{safety_result.reason}",
                        stage="action_safety",
                        error_code="ACTION_BLOCKED",
                        app=state.observation.app,
                        generation=state.observation.generation,
                    )
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
                    counts_as_user_step=binding == DesktopActionBinding.USER_STEP,
                    cancel_event=cancel_event,
                )
                if action_result is not None:
                    return action_result
                continue

            if decision.kind == DesktopDecisionKind.DONE:
                if state.observation is None:
                    return self._failure(
                        "规划器报告完成，但没有可核验的本地观察",
                        stage="verify_completion",
                        error_code="COMPLETION_OBSERVATION_MISSING",
                    )
                if state.verified_action_count == 0:
                    return self._failure(
                        "通用桌面任务尚无任何经过本地验收的动作",
                        stage="verify_completion",
                        error_code="NO_VERIFIED_ACTIONS",
                    )
                if state.verified_action_count != user_action_step_count(state.task):
                    return self._failure(
                        "尚未按顺序完成用户明确要求的全部桌面步骤",
                        stage="verify_completion",
                        error_code="USER_STEPS_INCOMPLETE",
                    )
                if decision.app is not None and (
                    decision.app.strip().casefold() not in state.allowed_apps
                ):
                    return self._failure(
                        "规划器完成条件超出本次口述授权的应用范围",
                        stage="verify_completion",
                        error_code="COMPLETION_APP_SCOPE_MISMATCH",
                    )
                if state.verified_action_count > 0 and (
                    state.last_action_expectation is None
                    or decision.expectation != state.last_action_expectation
                ):
                    return self._failure(
                        "完成条件必须与最后一个已建立的任务后置条件完全一致",
                        stage="verify_completion",
                        error_code="COMPLETION_CONDITION_CHANGED",
                    )
                terminal_condition_is_bound = expectation_is_terminal_user_condition(
                    decision.expectation,
                    state.task,
                    last_action=state.last_action,
                    last_action_target=state.last_action_target,
                ) or self.safety.accepts_personal_terminal_condition(
                    decision.expectation,
                    user_text=state.task,
                    last_action=state.last_action,
                )
                if not terminal_condition_is_bound:
                    return self._failure(
                        "完成条件没有绑定到用户要求的最后一个正向动作",
                        stage="verify_completion",
                        error_code="COMPLETION_NOT_BOUND_TO_TASK",
                    )
                verified = self.verifier.verify_completion(
                    decision,
                    state.observation,
                    last_action_result=state.last_verification,
                )
                if not verified.verified:
                    return self._failure(
                        f"本地完成条件未成立：{verified.reason}",
                        stage="verify_completion",
                        error_code="COMPLETION_NOT_VERIFIED",
                        app=state.observation.app,
                        generation=state.observation.generation,
                    )
                return ComputerControlResult(
                    True,
                    f"LOCAL_VERIFIED_COMPLETION: {verified.reason}",
                )
        return self._failure(
            "桌面任务达到最大单步数，未满足本地完成条件",
            stage="verify_completion",
            error_code="MAX_STEPS_REACHED",
        )

    def _perform_action(
        self,
        state: _TaskState,
        action: DesktopAction,
        *,
        expectation: DesktopExpectation | None,
        counts_as_user_step: bool = True,
        confirmed_binding: DesktopConfirmation | None = None,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult | None:
        assert self.driver is not None and state.observation is not None
        planner_observation = state.observation
        if expectation is None:
            return self._failure(
                "桌面动作缺少任务相关的本地后置条件",
                stage="plan",
                error_code="ACTION_POSTCONDITION_MISSING",
            )
        if self._cancelled(cancel_event):
            return self._cancelled_result(stage="execute")
        try:
            before = self.driver.observe(
                action.app,
                cancel_event=self._current_cancel,
            )
        except Exception as exc:
            return self._failure(
                f"执行前桌面观察失败：{_safe_exception_message(exc)}",
                stage="observe_driver",
                error_code="PRE_ACTION_OBSERVE_FAILED",
                exception_type=type(exc).__name__,
                app=action.app,
            )
        if (
            before.app.strip().casefold() not in state.allowed_apps
            or before.app.strip().casefold() != planner_observation.app.strip().casefold()
        ):
            return self._failure(
                "执行前桌面驱动返回了授权范围外的应用，已拒绝动作",
                stage="observe_driver",
                error_code="PRE_ACTION_APP_CHANGED",
                app=before.app,
                generation=before.generation,
            )
        inspection = self.safety.inspect_observation(before, user_text=state.task)
        if inspection.disposition == DesktopSafetyDisposition.BLOCK:
            return self._failure(
                f"执行前本地安全策略阻止读取界面：{inspection.reason}",
                stage="observe_safety",
                error_code="PRE_ACTION_OBSERVATION_BLOCKED",
                app=before.app,
                generation=before.generation,
            )
        if before.fingerprint != planner_observation.fingerprint:
            if (
                not planner_observation.local_window_id
                or not before.local_window_id
                or before.local_window_id != planner_observation.local_window_id
            ):
                return self._failure(
                    "执行前界面已经变化，无法证明仍是同一窗口，已拒绝使用过期规划",
                    stage="observe_driver",
                    error_code="STALE_WINDOW_CHANGED",
                    app=before.app,
                    generation=before.generation,
                )
            state.stale_replans += 1
            if state.stale_replans > _MAX_STALE_REPLANS:
                return self._failure(
                    "同一窗口在执行前反复变化，已停止继续规划",
                    stage="observe_driver",
                    error_code="UI_STATE_UNSTABLE",
                    app=before.app,
                    generation=before.generation,
                )
            state.observation = before
            state.history.append(
                "refreshed changed UI in the same local window; previous action was not executed"
            )
            return None
        rebound = replace(action, generation=before.generation)
        fresh_safety = self.safety.evaluate(
            rebound,
            before,
            user_text=state.task,
            expectation=expectation,
        )
        if confirmed_binding is None:
            if fresh_safety.disposition != DesktopSafetyDisposition.ALLOW:
                return self._failure(
                    "执行前本地安全分类不再允许该动作",
                    stage="action_safety",
                    error_code="FRESH_ACTION_NOT_ALLOWED",
                    app=before.app,
                    generation=before.generation,
                )
        else:
            if (
                fresh_safety.disposition != DesktopSafetyDisposition.CONFIRM
                or fresh_safety.confirmation is None
                or fresh_safety.confirmation.action_digest != confirmed_binding.action_digest
                or fresh_safety.confirmation.summary != confirmed_binding.summary
            ):
                return self._failure(
                    "确认前本地安全分类发生变化，已拒绝执行",
                    stage="action_safety",
                    error_code="CONFIRMATION_BINDING_CHANGED",
                    app=before.app,
                    generation=before.generation,
                )
        already_true = self.verifier.verify_expectation(
            expectation,
            before,
            last_action_result=None,
        )
        if already_true.verified:
            return self._failure(
                "动作后置条件在执行前已经成立，不能证明本次动作完成",
                stage="verify_action",
                error_code="POSTCONDITION_ALREADY_TRUE",
                app=before.app,
                generation=before.generation,
            )
        try:
            if self._cancelled(cancel_event):
                return self._cancelled_result(stage="execute")
            receipt = self.driver.execute(
                rebound,
                before,
                cancel_event=self._current_cancel,
            )
        except Exception as exc:
            return self._failure(
                f"桌面动作失败：{_safe_exception_message(exc)}",
                stage="execute",
                error_code="ACTION_EXECUTION_FAILED",
                exception_type=type(exc).__name__,
                app=before.app,
                generation=before.generation,
            )
        if self._cancelled(cancel_event):
            return self._cancelled_result(stage="execute")
        after: DesktopObservation | None = None
        reobserve_error: Exception | None = None
        for attempt, delay in enumerate((0.0, 0.15, 0.35)):
            if delay:
                self._sleep(delay)
            if self._cancelled(cancel_event):
                return self._cancelled_result(stage="reobserve")
            try:
                after = self.driver.observe(
                    rebound.app,
                    cancel_event=self._current_cancel,
                )
                break
            except Exception as exc:
                reobserve_error = exc
                if attempt == 2:
                    break
        if after is None:
            assert reobserve_error is not None
            return self._failure(
                f"动作后重新观察失败：{_safe_exception_message(reobserve_error)}",
                stage="reobserve",
                error_code="REOBSERVE_FAILED",
                exception_type=type(reobserve_error).__name__,
                app=rebound.app,
            )
        verified = self.verifier.verify_action(rebound, receipt, before, after)
        if not verified.verified:
            return self._failure(
                f"动作后本地验收失败：{verified.reason}",
                stage="verify_action",
                error_code="ACTION_NOT_VERIFIED",
                app=after.app,
                generation=after.generation,
            )
        expected_result = self.verifier.verify_expectation(
            expectation,
            after,
            last_action_result=verified,
        )
        if not expected_result.verified:
            return self._failure(
                f"动作后任务条件未成立：{expected_result.reason}",
                stage="verify_action",
                error_code="POSTCONDITION_NOT_VERIFIED",
                app=after.app,
                generation=after.generation,
            )
        inspection = self.safety.inspect_observation(after, user_text=state.task)
        if inspection.disposition == DesktopSafetyDisposition.BLOCK:
            return self._failure(
                f"动作后界面不能发送给规划器：{inspection.reason}",
                stage="observe_safety",
                error_code="POST_ACTION_OBSERVATION_BLOCKED",
                app=after.app,
                generation=after.generation,
            )
        state.observation = after
        state.last_verification = expected_result
        state.last_action_expectation = expectation
        state.last_action = rebound
        target = next(
            (element for element in before.elements if element.index == rebound.element_index),
            None,
        )
        state.last_action_target = target.name if target is not None else None
        if counts_as_user_step:
            state.verified_action_count += 1
        state.stale_replans = 0
        state.history.append(f"locally verified {rebound.type.value}: {expected_result.reason}")
        return None

    def run(
        self,
        instruction: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ComputerControlResult:
        if not isinstance(instruction, str) or not instruction.strip():
            return self._failure(
                "电脑控制指令为空",
                stage="runtime",
                error_code="EMPTY_INSTRUCTION",
            )
        with self._execution_lock:
            with self._lifecycle_lock:
                if self._closed:
                    return self._failure(
                        "桌面控制器已经关闭",
                        stage="runtime",
                        error_code="CONTROLLER_CLOSED",
                    )
                if self._pending is not None:
                    return self._failure(
                        "上一动作仍在等待精确确认",
                        stage="runtime",
                        error_code="CONFIRMATION_PENDING",
                    )
                self._current_cancel = threading.Event()
            started_at = float(self._monotonic())
            try:
                self._set_driver_task_context(instruction.strip())
                try:
                    native_result = self._run_native(
                        instruction.strip(),
                        cancel_event=cancel_event,
                    )
                except Exception as exc:
                    return self._failure(
                        "确定性本机路由发生内部错误",
                        stage="native_route",
                        error_code="NATIVE_ROUTE_INTERNAL_ERROR",
                        exception_type=type(exc).__name__,
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
                    return self._failure(
                        f"本地桌面驱动不可用：{_safe_exception_message(exc)}",
                        stage="list_apps",
                        error_code="APP_INVENTORY_FAILED",
                        exception_type=type(exc).__name__,
                    )
                configured_profiles = getattr(self.driver, "profiles", {})
                configured_apps = (
                    tuple(configured_profiles)
                    if isinstance(configured_profiles, dict)
                    else ()
                )
                known_apps = tuple(
                    dict.fromkeys((*visible_apps, *configured_apps, *_APP_ALIASES))
                )
                explicit_apps = _explicitly_named_apps(instruction, known_apps)
                unsupported_scopes = _unsupported_explicit_app_scopes(
                    instruction,
                    known_apps,
                )
                if explicit_apps or unsupported_scopes:
                    # A newly spoken explicit scope must never fall back to a
                    # previously verified application after this command fails.
                    self._clear_trusted_context()
                if unsupported_scopes:
                    return self._failure(
                        "本次口述明确指定了未配置的应用，不能继承上一应用",
                        stage="list_apps",
                        error_code="APP_SCOPE_UNSUPPORTED",
                    )
                allowed_apps = explicit_apps.intersection(visible_apps)
                observation: DesktopObservation | None = None
                history: list[str] = []
                if len(explicit_apps) > 1:
                    return self._failure(
                        "通用桌面任务必须指定唯一一个当前可见应用",
                        stage="list_apps",
                        error_code="APP_SCOPE_AMBIGUOUS",
                    )
                if explicit_apps and not allowed_apps:
                    return self._failure(
                        "本次口述明确指定的应用当前不可见，不能继承上一应用",
                        stage="list_apps",
                        error_code="APP_SCOPE_NOT_VISIBLE",
                        app=next(iter(explicit_apps)),
                    )
                if not allowed_apps:
                    trusted_context = self._trusted_context()
                    if trusted_context is None:
                        return self._failure(
                            "通用桌面任务必须在本次口述中明确且肯定地指定唯一一个当前可见应用",
                            stage="list_apps",
                            error_code="APP_SCOPE_REQUIRED",
                        )
                    trusted_app, trusted_window_id = trusted_context
                    if trusted_app not in visible_apps:
                        self._clear_trusted_context()
                        return self._failure(
                            "上一条已验证应用当前不可见，不能继承控制范围",
                            stage="list_apps",
                            error_code="SESSION_APP_NOT_VISIBLE",
                            app=trusted_app,
                        )
                    try:
                        observation = self.driver.observe(
                            trusted_app,
                            cancel_event=self._current_cancel,
                        )
                    except Exception as exc:
                        return self._failure(
                            f"继承应用前桌面观察失败：{_safe_exception_message(exc)}",
                            stage="observe_driver",
                            error_code="SESSION_CONTEXT_OBSERVE_FAILED",
                            exception_type=type(exc).__name__,
                            app=trusted_app,
                        )
                    if observation.app.strip().casefold() != trusted_app:
                        self._clear_trusted_context()
                        return self._failure(
                            "桌面驱动返回了与上一条已验证应用不同的应用",
                            stage="observe_driver",
                            error_code="SESSION_APP_CHANGED",
                            app=observation.app,
                            generation=observation.generation,
                        )
                    if (
                        not observation.local_window_id
                        or observation.local_window_id != trusted_window_id
                    ):
                        self._clear_trusted_context()
                        return self._failure(
                            "上一条已验证应用窗口已经变化，不能继承控制范围",
                            stage="observe_driver",
                            error_code="SESSION_WINDOW_CHANGED",
                            app=observation.app,
                            generation=observation.generation,
                        )
                    inspection = self.safety.inspect_observation(
                        observation,
                        user_text=instruction,
                    )
                    if inspection.disposition == DesktopSafetyDisposition.BLOCK:
                        return self._failure(
                            f"本地安全策略阻止继承该界面：{inspection.reason}",
                            stage="observe_safety",
                            error_code="SESSION_OBSERVATION_BLOCKED",
                            app=observation.app,
                            generation=observation.generation,
                        )
                    allowed_apps = frozenset({trusted_app})
                    history.append("resumed the same locally verified app window")
                apps = json.dumps(
                    [{"app": app, "visible_window_count": 1} for app in sorted(allowed_apps)],
                    ensure_ascii=False,
                    sort_keys=True,
                )
                state = _TaskState(
                    task=instruction.strip(),
                    apps=apps,
                    allowed_apps=allowed_apps,
                    observation=observation,
                    history=history,
                    last_verification=None,
                    last_action_expectation=None,
                    last_action=None,
                    last_action_target=None,
                    steps=0,
                    verified_action_count=0,
                    remaining_seconds=self._remaining(started_at),
                )
                result = self._drive(state, cancel_event=cancel_event)
                self._remember_trusted_context(state, result)
                return result
            finally:
                try:
                    self._set_driver_task_context(None)
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
            return self._cancelled_result(
                "确认后的本机动作已取消",
                stage="native_route",
            )
        try:
            prepared = self.native_router.executor.prepare_plan(clone_plan(pending.native_plan))
            evaluated = self.native_router.safety.evaluate(
                prepared,
                user_text=pending.native_user_text,
            )
            evaluated = clone_plan(evaluated)
            if self._cancelled(cancel_event):
                return self._cancelled_result(
                    "确认后的本机动作已取消",
                    stage="native_route",
                )
            if evaluated.risk != RiskLevel.CONFIRM:
                return self._failure(
                    "确认时本机安全分类已经变化",
                    stage="native_route",
                    error_code="NATIVE_CONFIRMATION_CLASSIFICATION_CHANGED",
                )
            if self._native_binding(evaluated) != pending.native_binding_digest:
                return self._failure(
                    "确认时本机目标身份或内容已经变化",
                    stage="native_route",
                    error_code="NATIVE_CONFIRMATION_BINDING_CHANGED",
                )
            if self._cancelled(cancel_event):
                return self._cancelled_result(
                    "确认后的本机动作已取消",
                    stage="native_route",
                )
            with guard_plan_paths(
                evaluated,
                pending.native_binding_digest,
            ):
                if self._cancelled(cancel_event):
                    return self._cancelled_result(
                        "确认后的本机动作已取消",
                        stage="native_route",
                    )
                results = tuple(self.native_router.executor.execute_plan(evaluated))
        except Exception as exc:
            return self._failure(
                f"确认后的本机动作失败：{type(exc).__name__}",
                stage="native_route",
                error_code="NATIVE_CONFIRMATION_EXECUTION_FAILED",
                exception_type=type(exc).__name__,
            )
        if self._cancelled(cancel_event):
            return self._cancelled_result(
                "确认后的本机动作已取消",
                stage="native_route",
            )
        if not NativeSkillRouter.execution_is_locally_verified(
            evaluated,
            results,
        ):
            return self._failure(
                "确认后的本机动作没有完成全部后置检查",
                stage="native_route",
                error_code="NATIVE_CONFIRMATION_NOT_VERIFIED",
            )
        if self._cancelled(cancel_event):
            self._clear_trusted_context()
            return self._cancelled_result(
                "确认后的本机动作在验证期间被取消",
                stage="native_route",
            )
        context_expected = bool(
            self.driver is not None
            and self.safety.profile == DesktopSafetyProfile.PERSONAL_TRUSTED
            and len(
                {
                    action.app.strip().casefold()
                    for action in evaluated.actions
                    if isinstance(action.app, str) and action.app.strip()
                }
            )
            == 1
        )
        context_refreshed = self._refresh_trusted_context_after_native(
            evaluated,
            user_text=pending.native_user_text,
            cancel_event=cancel_event,
        )
        return self._publish_native_success(
            "LOCAL_VERIFIED_COMPLETION: 已执行精确确认的本机动作",
            cancel_event=cancel_event,
            context_expected=context_expected,
            context_refreshed=context_refreshed,
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
            return self._failure(
                "确认动作丢失了原始观察",
                stage="runtime",
                error_code="CONFIRMATION_STATE_MISSING",
            )
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
        result = self._drive(state, cancel_event=cancel_event)
        self._remember_trusted_context(state, result)
        return result

    def confirm(
        self,
        confirmation_id: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ComputerControlResult:
        with self._execution_lock:
            with self._lifecycle_lock:
                if self._closed:
                    return self._failure(
                        "桌面控制器已经关闭",
                        stage="runtime",
                        error_code="CONTROLLER_CLOSED",
                    )
                pending = self._pending
                if pending is None or confirmation_id != pending.confirmation_id:
                    return self._failure(
                        "确认标识不匹配或已经使用",
                        stage="runtime",
                        error_code="CONFIRMATION_ID_INVALID",
                    )
                self._pending = None
                self._current_cancel = threading.Event()
            try:
                confirmation_task = (
                    pending.native_user_text
                    if pending.native_plan is not None
                    else pending.state.task
                    if pending.state is not None
                    else None
                )
                self._set_driver_task_context(confirmation_task)
                if float(self._monotonic()) > pending.expires_at:
                    return self._failure(
                        "确认已经超时",
                        stage="runtime",
                        error_code="CONFIRMATION_EXPIRED",
                    )
                if pending.native_plan is not None:
                    return self._confirm_native(pending, cancel_event=cancel_event)
                return self._confirm_desktop(pending, cancel_event=cancel_event)
            finally:
                try:
                    self._set_driver_task_context(None)
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
            self._trusted_app_context = None
            self._trusted_window_id = None
        self.cancel()
        if self.driver is not None:
            self.driver.close()
