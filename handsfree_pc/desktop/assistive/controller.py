from __future__ import annotations

import inspect
import json
import os
import re
import threading
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass, field, replace
from pathlib import Path, PureWindowsPath
from typing import Any

from ...computer_control import ComputerControlResult
from ...windows.native import DesktopUnavailableError, ForegroundIntegrityBoundary
from ...windows.uia import PasswordFieldError
from ..native_skills import (
    NativeRouteStatus,
    NativeSkillResult,
    NativeSkillRouter,
    _is_path_identity_change,
)
from ..protocol import (
    DesktopAction,
    DesktopActionType,
    DesktopElement,
    DesktopElementAction,
    DesktopObservation,
    ElementPlane,
    element_plane,
    redact_credential_like_text,
)
from ..visual_ocr import SensitiveVisualSurfaceError
from .models import ActionOutcome, AssistiveDecisionKind, Goal, GoalKind, TaskSpec
from .planner import AssistiveStepPlanner
from .policy import ActionBinding, AssistivePolicy, PolicyDisposition
from .retry import ProgressProbe, WaitResult, wait_for_outcome
from .skills.app_activation import AppActivationSkill
from .skills.app_launch import AppLaunchSkill
from .skills.browser import BrowserNavigationSkill
from .skills.conversation import (
    CLAUDE_DEFAULT_MODE,
    CLAUDE_MODE_BUTTONS,
    PALETTE_HOTKEYS,
    PaletteConversationSkill,
    WeChatConversationSkill,
)
from .skills.explorer import ExplorerSkill
from .skills.wechat_files import WeChatSendFileSkill
from .spoken_paths import EXECUTABLE_SUFFIXES, SpokenPathResolver, has_location_cue
from .task_parser import DeterministicTaskParser
from .verifier import GoalCheck, GoalVerifier, VerificationResult, _entry_matches_app


@dataclass(slots=True)
class AssistiveMetrics:
    actions: int = 0
    retries: int = 0
    replans: int = 0
    polls: int = 0
    native_fallbacks: int = 0
    screenshots: int = 0
    failure_stage: str | None = None

    def to_dict(self) -> dict[str, int | str | None]:
        return {
            "actions": self.actions,
            "retries": self.retries,
            "replans": self.replans,
            "polls": self.polls,
            "native_fallbacks": self.native_fallbacks,
            "screenshots": self.screenshots,
            "failure_stage": self.failure_stage,
        }


@dataclass(slots=True)
class _RunState:
    task: TaskSpec
    inventory: str
    observation: DesktopObservation | None
    verification: VerificationResult
    history: list[str]
    deadline: float
    metrics: AssistiveMetrics = field(default_factory=AssistiveMetrics)
    zero_progress_rounds: int = 0
    planner_failures: int = 0
    observation_failures: int = 0
    passive_observations: int = 0
    # Goals a deterministic skill completed with its own evidence (a clicked
    # conversation row, a pasted file); no UIA verifier can re-prove them.
    skill_completed: set[Goal] = field(default_factory=set)


@dataclass(slots=True)
class _PendingAction:
    confirmation_id: str
    phrase: str
    binding: ActionBinding | None
    action: DesktopAction | None
    observation: DesktopObservation | None
    state: _RunState


_CHAT_FAMILIES = ("wechat", "claude", "codex")


def _safe_message(value: object, *, maximum: int = 320) -> str:
    text = redact_credential_like_text(str(value)) or "assistive desktop control failed"
    return " ".join(text.split())[:maximum]


def _inventory_entries(raw: str) -> tuple[dict[str, Any], ...]:
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(value, list):
        return ()
    return tuple(item for item in value if isinstance(item, dict))


def _normalized(value: object) -> str:
    return "".join(str(value or "").casefold().split())


def _is_hard_desktop_boundary(exc: BaseException) -> bool:
    """Classify boundaries that must never fall through to another actuator."""

    return isinstance(
        exc,
        (
            DesktopUnavailableError,
            ForegroundIntegrityBoundary,
            PasswordFieldError,
            SensitiveVisualSurfaceError,
        ),
    )


def _app_aliases(target: str) -> tuple[str, ...]:
    aliases = {
        "claude": ("claude", "克劳德"),
        "codex": ("codex", "chatgpt", "chatgpt.exe"),
        "chrome": ("chrome", "googlechrome", "谷歌浏览器"),
        "explorer": ("explorer", "fileexplorer", "资源管理器"),
        "wechat": ("wechat", "weixin", "微信"),
    }
    normalized = _normalized(target)
    for name, values in aliases.items():
        if normalized == _normalized(name) or any(
            normalized == _normalized(item) for item in values
        ):
            return tuple(_normalized(item) for item in (name, *values))
    return (normalized,)


def _entry_matches(entry: dict[str, Any], target: str) -> bool:
    aliases = _app_aliases(target)
    for key in ("app", "display_name", "process_name", "class_name"):
        value = _normalized(entry.get(key))
        executable_stem = value.removesuffix(".exe")
        if any(
            alias
            and (
                value == alias
                or executable_stem == alias
                or value.startswith(f"{alias}-")
            )
            for alias in aliases
        ):
            return True
    return False


# Only these verbs turn an unknown app name into a place to open; "切换到 X"
# never resolves to a folder.
_OPEN_PLACE_RE = re.compile(r"^\s*(?:请)?(?:帮我|给我)?(?:打开|进入|查看|启动)")
# Window titles of each chat app's main window. WeChat also opens image
# viewers and mini-programs as top-level windows of the same process.
_CHAT_MAIN_TITLES: dict[str, tuple[str, ...]] = {
    "wechat": ("微信", "wechat", "weixin"),
    "claude": ("claude",),
    "codex": ("codex", "chatgpt"),
}


class AssistiveController:
    """Task-goal desktop controller used only by ``engine=assistive_v1``.

    It intentionally has no proof-step counter, expectation ledger, or old
    action-order termination conditions. A final success comes only from the
    task-level GoalVerifier; planner ``done`` is merely a suggestion.
    """

    def __init__(
        self,
        *,
        native_router: NativeSkillRouter,
        driver: Any | None,
        planner: AssistiveStepPlanner | None,
        timeout_seconds: float = 300.0,
        max_steps: int = 20,
        diagnostics: object | None = None,
        send_policy: dict[str, str] | None = None,
        confirmation_timeout_seconds: float = 15.0,
        verifier: GoalVerifier | None = None,
        task_parser: DeterministicTaskParser | None = None,
        policy: AssistivePolicy | None = None,
        monotonic: Any = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if not 1 <= max_steps <= 100:
            raise ValueError("max_steps must be between 1 and 100")
        self.native_router = native_router
        self.driver = driver
        self.planner = planner
        self.timeout_seconds = float(timeout_seconds)
        self.max_steps = int(max_steps)
        self.diagnostics = diagnostics
        self._monotonic = monotonic
        # Heavy web apps (ChatGPT, Claude) can take several seconds to render
        # their composer after the address bar already shows the new URL.
        self.composer_wait_polls = 20
        self.composer_wait_interval = 0.5
        self.send_policy = dict(send_policy or {})
        settings = getattr(native_router, "settings", None)
        execution = getattr(settings, "execution", None)
        visual = getattr(settings, "visual_ocr", None)
        self._visual_profile_apps = frozenset(
            str(item).strip().casefold()
            for item in (getattr(visual, "apps", ()) or ())
            if bool(getattr(visual, "enabled", False)) and str(item).strip()
        )
        path_aliases = getattr(execution, "path_aliases", None)
        self.task_parser = task_parser or DeterministicTaskParser(
            path_aliases=(
                {str(name): str(target) for name, target in path_aliases.items()}
                if isinstance(path_aliases, dict)
                else None
            )
        )
        self.verifier = verifier or GoalVerifier()
        self.policy = policy or AssistivePolicy(
            send_policy=self.send_policy,
            confirmation_timeout_seconds=confirmation_timeout_seconds,
        )
        self._session_id = str(uuid.uuid4())
        self._execution_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._current_cancel: threading.Event | None = None
        self._pending: _PendingAction | None = None
        self._closed = False
        self._last_metrics = AssistiveMetrics()
        self._debug_log: Any | None = None
        diagnostic_settings = getattr(settings, "diagnostics", None)
        if diagnostic_settings is not None:
            try:
                from .debug_log import LocalDebugLog

                self._debug_log = LocalDebugLog(
                    enabled=bool(getattr(diagnostic_settings, "debug_log_enabled", False)),
                    local_only=bool(getattr(diagnostic_settings, "debug_log_local_only", True)),
                )
            except (ImportError, OSError, ValueError):
                self._debug_log = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def last_metrics(self) -> dict[str, int | str | None]:
        return self._last_metrics.to_dict()

    @property
    def pending_confirmation_id(self) -> str | None:
        with self._lifecycle_lock:
            pending = self._pending
            if (
                pending is not None
                and pending.binding is not None
                and self.policy.binding_expired(pending.binding)
            ):
                self._pending = None
                return None
            return pending.confirmation_id if pending is not None else None

    def _cancelled(self, external: threading.Event | None) -> bool:
        current = self._current_cancel
        return bool(
            (current is not None and current.is_set())
            or (external is not None and external.is_set())
        )

    def _trace(
        self,
        *,
        stage: str,
        error_code: str,
        safe_message: str,
        level: str = "info",
        observation: DesktopObservation | None = None,
    ) -> None:
        event = getattr(self.diagnostics, "event", None)
        if not callable(event):
            return
        try:
            event(
                stage=stage,
                error_code=error_code,
                safe_message=_safe_message(safe_message),
                level=level,
                session_id=self._session_id,
                app=observation.app if observation is not None else None,
                generation=observation.generation if observation is not None else None,
            )
        except Exception:
            return

    def _debug_exception(self, exc: BaseException, *, stage: str) -> None:
        log = self._debug_log
        record = getattr(log, "exception", None)
        if callable(record):
            with suppress(Exception):
                record(exc, stage=stage)

    def _debug_note(self, stage: str, message: str) -> None:
        note = getattr(self._debug_log, "note", None)
        if callable(note):
            with suppress(Exception):
                note(stage, message)

    def _failure(
        self,
        message: str,
        *,
        stage: str,
        error_code: str,
        metrics: AssistiveMetrics | None = None,
        exception: BaseException | None = None,
        timed_out: bool = False,
        observation: DesktopObservation | None = None,
    ) -> ComputerControlResult:
        safe = _safe_message(message)
        active_metrics = metrics or AssistiveMetrics()
        active_metrics.failure_stage = stage
        self._last_metrics = active_metrics
        if exception is not None:
            self._debug_exception(exception, stage=stage)
        self._trace(
            stage=stage,
            error_code=error_code,
            safe_message=safe,
            level="error",
            observation=observation,
        )
        return ComputerControlResult(
            False,
            f"FAILURE: {safe}",
            session_id=self._session_id,
            timed_out=timed_out,
            stage=stage,
            error_code=error_code,
            safe_message=safe,
            exception_type=type(exception).__name__ if exception is not None else None,
            app=observation.app if observation is not None else None,
            generation=observation.generation if observation is not None else None,
        )

    def _success(self, message: str, state: _RunState) -> ComputerControlResult:
        self._last_metrics = state.metrics
        self._trace(
            stage="goal_verifier",
            error_code="ASSISTIVE_GOALS_COMPLETED",
            safe_message=message,
            observation=state.observation,
        )
        return ComputerControlResult(
            True,
            f"LOCAL_VERIFIED_COMPLETION: {_safe_message(message)}",
            session_id=self._session_id,
            stage="goal_verifier",
            error_code="ASSISTIVE_GOALS_COMPLETED",
            safe_message=_safe_message(message),
            app=state.observation.app if state.observation is not None else None,
            generation=(state.observation.generation if state.observation is not None else None),
        )

    def _cancelled_result(self, metrics: AssistiveMetrics | None = None) -> ComputerControlResult:
        active = metrics or AssistiveMetrics()
        active.failure_stage = "runtime"
        self._last_metrics = active
        return ComputerControlResult(
            False,
            "FAILURE: 桌面任务已取消",
            session_id=self._session_id,
            cancelled=True,
            stage="runtime",
            error_code="CANCELLED",
            safe_message="桌面任务已取消",
        )

    def _native_backend(self) -> Any | None:
        if self.driver is not None:
            method = getattr(self.driver, "_native_backend", None)
            if callable(method):
                try:
                    return method()
                except Exception:
                    pass
        executor = getattr(self.native_router, "executor", None)
        method = getattr(executor, "_native_backend", None)
        if callable(method):
            try:
                return method()
            except Exception:
                return None
        return None

    def _observe(
        self,
        app: str,
        *,
        cancel_event: threading.Event | None,
        screenshot: bool = False,
    ) -> DesktopObservation:
        assert self.driver is not None
        observation = self._observe_once(app, cancel_event=cancel_event, screenshot=screenshot)
        if (
            observation.class_name == "Chrome_WidgetWin_1"
            and len(observation.elements) < 12
            and not self._cancelled(cancel_event)
        ):
            # Chromium/Electron builds its accessibility tree lazily: the very
            # first UIA query can return a near-empty shell. One short retry
            # avoids misreading a rich app as a screenshot-only surface.
            time.sleep(0.4)
            observation = self._observe_once(app, cancel_event=cancel_event, screenshot=screenshot)
        return observation

    def _observe_once(
        self,
        app: str,
        *,
        cancel_event: threading.Event | None,
        screenshot: bool,
    ) -> DesktopObservation:
        assert self.driver is not None
        if screenshot:
            try:
                return self.driver.observe(
                    app,
                    cancel_event=cancel_event,
                    capture_screenshot=True,
                )
            except TypeError as exc:
                if "capture_screenshot" not in str(exc):
                    raise
        return self.driver.observe(app, cancel_event=cancel_event)

    @staticmethod
    def _chat_window(inventory: str, family: str) -> str | None:
        """Pick a chat app's main window; viewers and mini-programs share its process."""

        entries = [
            entry
            for entry in _inventory_entries(inventory)
            if isinstance(entry.get("app"), str) and _entry_matches(entry, family)
        ]
        if not entries:
            return None
        titles = {_normalized(title) for title in _CHAT_MAIN_TITLES.get(family, (family,))}
        for entry in entries:
            if _normalized(entry.get("window_title")) in titles:
                return str(entry["app"])
        for entry in entries:
            if "mainwindow" in _normalized(entry.get("class_name")):
                return str(entry["app"])
        foreground = [entry for entry in entries if entry.get("foreground") is True]
        return str((foreground or entries)[0]["app"])

    @staticmethod
    def _foreground_app(inventory: str, preferred: str | None = None) -> str | None:
        foreground = [
            entry
            for entry in _inventory_entries(inventory)
            if entry.get("foreground") is True and isinstance(entry.get("app"), str)
        ]
        if preferred:
            matched = [entry for entry in foreground if _entry_matches(entry, preferred)]
            if len(matched) == 1:
                return str(matched[0]["app"])
        if len(foreground) == 1:
            return str(foreground[0]["app"])
        return None

    def _initial_observation(
        self,
        task: TaskSpec,
        inventory: str,
        *,
        cancel_event: threading.Event | None,
    ) -> DesktopObservation | None:
        needs_window_state = any(
            goal.kind
            not in {
                GoalKind.APP_FOREGROUND,
                GoalKind.PATH_OPEN,
            }
            for goal in task.goals
        )
        if not needs_window_state:
            return None
        preferred = next((goal.app for goal in task.goals if goal.app), None)
        if preferred is not None and not self._family_foreground(inventory, preferred):
            # The goals live in another app; reading whatever is in front now
            # costs a full UIA walk and proves nothing. Activation observes
            # the right window afterwards.
            return None
        app = self._foreground_app(inventory, preferred)
        if app is None:
            return None
        try:
            return self._observe(app, cancel_event=cancel_event)
        except Exception as exc:
            if _is_hard_desktop_boundary(exc):
                raise
            return None

    def _visual_profile_requires_screenshot(
        self,
        observation: DesktopObservation,
    ) -> bool:
        if not self._visual_profile_apps or observation.screenshot_png is not None:
            return False
        if "*" in self._visual_profile_apps:
            return True
        identity = _normalized(
            " ".join(
                value
                for value in (
                    observation.app,
                    observation.process_name,
                    observation.window_title,
                )
                if value
            )
        )
        return any(
            _normalized(profile) and _normalized(profile) in identity
            for profile in self._visual_profile_apps
        )

    def _verify(
        self,
        state: _RunState,
        *,
        cancel_event: threading.Event | None,
        refresh_inventory: bool = True,
    ) -> VerificationResult:
        assert self.driver is not None
        if refresh_inventory:
            state.inventory = self.driver.list_apps(cancel_event=cancel_event)
        verification = self.verifier.evaluate(
            state.task,
            self.driver,
            inventory=state.inventory,
            observation=state.observation,
            cancel_event=cancel_event,
        )
        state.verification = self._finish_verification(state, verification)
        return state.verification

    @staticmethod
    def _finish_verification(
        state: _RunState,
        verification: VerificationResult,
    ) -> VerificationResult:
        if not state.skill_completed:
            return verification
        checks = tuple(
            GoalCheck(check.goal, True, "completed by a deterministic skill")
            if not check.satisfied and check.goal in state.skill_completed
            else check
            for check in verification.checks
        )
        return VerificationResult(checks, verification.inventory, verification.observation)

    def _probe(
        self,
        state: _RunState,
        *,
        preferred_app: str,
        cancel_event: threading.Event | None,
        screenshot: bool = False,
    ) -> ProgressProbe:
        assert self.driver is not None
        inventory = self.driver.list_apps(cancel_event=cancel_event)
        observation: DesktopObservation | None = None
        candidates = [preferred_app]
        foreground = self._foreground_app(inventory)
        if foreground and foreground not in candidates:
            candidates.append(foreground)
        for app in candidates:
            try:
                observation = self._observe(
                    app,
                    cancel_event=cancel_event,
                    screenshot=screenshot,
                )
            except Exception as exc:
                if _is_hard_desktop_boundary(exc):
                    raise
                continue
            else:
                if screenshot:
                    state.metrics.screenshots += 1
                break
        state.inventory = inventory
        if observation is not None:
            state.observation = observation
        verification = self._finish_verification(
            state,
            self.verifier.evaluate(
                state.task,
                self.driver,
                inventory=inventory,
                observation=state.observation,
                cancel_event=cancel_event,
            ),
        )
        state.verification = verification
        return ProgressProbe(
            goals_complete=verification.completed,
            satisfied_goal_count=verification.satisfied_count,
            observation=state.observation,
            inventory=inventory,
            verification=verification,
            note=verification.summary,
        )

    @staticmethod
    def _before_probe(state: _RunState) -> ProgressProbe:
        return ProgressProbe(
            goals_complete=state.verification.completed,
            satisfied_goal_count=state.verification.satisfied_count,
            observation=state.observation,
            inventory=state.inventory,
            verification=state.verification,
            note=state.verification.summary,
        )

    def _wait_after_action(
        self,
        state: _RunState,
        *,
        app: str,
        before: ProgressProbe,
        cancel_event: threading.Event | None,
        screenshot: bool = False,
    ) -> WaitResult:
        capture_next = screenshot

        def probe() -> ProgressProbe:
            nonlocal capture_next
            requested = capture_next
            capture_next = False
            return self._probe(
                state,
                preferred_app=app,
                cancel_event=cancel_event,
                screenshot=requested,
            )

        result = wait_for_outcome(
            probe,
            before=before,
            cancel_requested=lambda: self._cancelled(cancel_event),
            abort_on_exception=_is_hard_desktop_boundary,
        )
        state.metrics.polls += result.polls
        if isinstance(result.probe.verification, VerificationResult):
            state.verification = result.probe.verification
        if result.probe.observation is not None:
            state.observation = result.probe.observation
        state.inventory = result.probe.inventory or state.inventory
        return result

    def _wait_for_goal_completion(
        self,
        state: _RunState,
        *,
        app: str,
        cancel_event: threading.Event | None,
        timeout_seconds: float = 6.0,
        max_probes: int = 8,
        until: Any = None,
    ) -> bool:
        """Keep probing until the final goals hold, not merely until something moved.

        ``until`` lets a skill stop as soon as its own goal is proved when the
        task still has later goals for other skills.

        A navigation submits and then loads; the first probe after Enter sees
        a changed but unfinished page. Returning "progress" there would hand a
        nearly finished deterministic task to the slow planner.
        """

        deadline = self._monotonic() + timeout_seconds
        for _probe_index in range(max_probes):
            probe = self._probe(state, preferred_app=app, cancel_event=cancel_event)
            state.metrics.polls += 1
            if probe.goals_complete:
                return True
            if until is not None and until(state.verification):
                return True
            if self._cancelled(cancel_event) or self._monotonic() >= deadline:
                return False
            time.sleep(0.3)
        return False

    @staticmethod
    def _target(action: DesktopAction, observation: DesktopObservation) -> DesktopElement | None:
        return next(
            (item for item in observation.elements if item.index == action.element_index),
            None,
        )

    @classmethod
    def _rebind_action(
        cls,
        action: DesktopAction,
        old_observation: DesktopObservation,
        new_observation: DesktopObservation,
    ) -> DesktopAction | None:
        if old_observation.local_window_id != new_observation.local_window_id:
            return None
        old_target = cls._target(action, old_observation)
        if old_target is None:
            return None
        matches = [
            item
            for item in new_observation.elements
            if old_target.local_identity and item.local_identity == old_target.local_identity
        ]
        if not matches:
            matches = [
                item
                for item in new_observation.elements
                if item.name == old_target.name
                and item.control_type == old_target.control_type
                and item.enabled
            ]
        if len(matches) != 1:
            return None
        return replace(
            action,
            app=new_observation.app,
            generation=new_observation.generation,
            element_index=matches[0].index,
        )

    @classmethod
    def _same_target_retry(
        cls,
        action: DesktopAction,
        old_observation: DesktopObservation,
        new_observation: DesktopObservation | None,
    ) -> DesktopAction | None:
        if new_observation is None:
            return None
        rebound = cls._rebind_action(action, old_observation, new_observation)
        if rebound is None:
            return None
        target = cls._target(rebound, new_observation)
        if action.type in {DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE}:
            payload = action.text if action.type == DesktopActionType.TYPE_TEXT else action.value
            if (
                target is None
                or target.focused is not True
                or target.value_observed is not True
            ):
                return None
            if payload and payload in (target.value or ""):
                return None
        return rebound

    @classmethod
    def _alternate_action(
        cls,
        action: DesktopAction,
        old_observation: DesktopObservation,
        new_observation: DesktopObservation | None,
    ) -> DesktopAction | None:
        if action.type != DesktopActionType.CLICK or new_observation is None:
            return None
        rebound = cls._rebind_action(action, old_observation, new_observation)
        if rebound is None:
            return None
        target = cls._target(rebound, new_observation)
        if target is None or target.supported_actions is None:
            return None
        for capability in (
            DesktopElementAction.INVOKE,
            DesktopElementAction.SELECT,
        ):
            if capability in target.supported_actions:
                return DesktopAction(
                    DesktopActionType.PERFORM_SECONDARY_ACTION,
                    app=new_observation.app,
                    generation=new_observation.generation,
                    element_index=target.index,
                    action_name=capability.value,
                )
        return None

    def _execute_once(
        self,
        state: _RunState,
        action: DesktopAction,
        observation: DesktopObservation,
        *,
        cancel_event: threading.Event | None,
    ) -> WaitResult | ComputerControlResult:
        assert self.driver is not None
        before = self._before_probe(state)
        try:
            receipt = self.driver.execute(
                action,
                observation,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            if _is_hard_desktop_boundary(exc):
                return self._failure(
                    "安全桌面、权限边界或凭据字段阻止了该动作",
                    stage="execute",
                    error_code="ASSISTIVE_HARD_BLOCK",
                    metrics=state.metrics,
                    exception=exc,
                    observation=observation,
                )
            self._debug_exception(exc, stage="execute")
            state.history.append(f"action raised transient {type(exc).__name__}; replan")
            return WaitResult(
                outcome=ActionOutcome.UNOBSERVABLE,
                probe=before,
                polls=0,
                elapsed_seconds=0.0,
                meaningful_change=False,
            )
        state.metrics.actions += 1
        if not receipt.accepted:
            state.history.append("driver did not accept the bound action")
        target = self._target(action, observation)
        try:
            result = self._wait_after_action(
                state,
                app=action.app,
                before=before,
                cancel_event=cancel_event,
                screenshot=bool(target is not None and target.visual_ocr),
            )
        except Exception as exc:
            if not _is_hard_desktop_boundary(exc):
                raise
            return self._failure(
                "动作后观察遇到安全桌面、权限边界或凭据字段",
                stage="observe",
                error_code="ASSISTIVE_HARD_BLOCK",
                metrics=state.metrics,
                exception=exc,
                observation=observation,
            )
        return result

    def _perform_action(
        self,
        state: _RunState,
        action: DesktopAction,
        observation: DesktopObservation,
        *,
        cancel_event: threading.Event | None,
        confirmed_binding: ActionBinding | None = None,
        confirmation_phrase: str | None = None,
    ) -> ComputerControlResult | None:
        decision = (
            self.policy.confirm(
                confirmed_binding,
                confirmation_phrase or "",
                action,
                observation,
            )
            if confirmed_binding is not None
            else self.policy.evaluate(state.task, action, observation)
        )
        if decision.disposition == PolicyDisposition.BLOCK:
            return self._failure(
                decision.reason,
                stage="policy",
                error_code=(
                    "ASSISTIVE_HARD_BLOCK"
                    if decision.hard_block
                    else "ASSISTIVE_POLICY_REJECTED"
                ),
                metrics=state.metrics,
                observation=observation,
            )
        if decision.disposition == PolicyDisposition.CONFIRM:
            assert decision.binding is not None
            pending = _PendingAction(
                confirmation_id=decision.binding.confirmation_id,
                phrase=decision.binding.phrase,
                binding=decision.binding,
                action=action,
                observation=observation,
                state=state,
            )
            with self._lifecycle_lock:
                self._pending = pending
            self._last_metrics = state.metrics
            return ComputerControlResult(
                False,
                f"NEEDS_CONFIRMATION: {decision.reason}",
                session_id=self._session_id,
                needs_confirmation=True,
                confirmation_id=pending.confirmation_id,
                confirmation_phrase=pending.phrase,
                stage="policy",
                error_code="ASSISTIVE_CONFIRMATION_REQUIRED",
                safe_message=_safe_message(decision.reason),
                app=observation.app,
                generation=observation.generation,
            )

        first = self._execute_once(
            state,
            action,
            observation,
            cancel_event=cancel_event,
        )
        if isinstance(first, ComputerControlResult):
            return first
        if first.outcome.value == "completed" or state.verification.completed:
            return self._success(state.verification.summary, state)
        if first.meaningful_change:
            state.zero_progress_rounds = 0
            state.history.append(
                f"progress after {action.type.value}: {state.verification.summary}"
            )
            return None

        latest = state.observation
        retry_action = self._same_target_retry(action, observation, latest)
        if retry_action is not None:
            retry_observation = latest
            assert retry_observation is not None
            retry_policy = self.policy.evaluate(state.task, retry_action, retry_observation)
            if retry_policy.disposition == PolicyDisposition.BLOCK:
                return self._failure(
                    retry_policy.reason,
                    stage="policy",
                    error_code=(
                        "ASSISTIVE_HARD_BLOCK"
                        if retry_policy.hard_block
                        else "ASSISTIVE_POLICY_REJECTED"
                    ),
                    metrics=state.metrics,
                    observation=retry_observation,
                )
            if retry_policy.disposition == PolicyDisposition.AUTO:
                state.metrics.retries += 1
                second = self._execute_once(
                    state,
                    retry_action,
                    retry_observation,
                    cancel_event=cancel_event,
                )
                if isinstance(second, ComputerControlResult):
                    return second
                if second.outcome.value == "completed" or state.verification.completed:
                    return self._success(state.verification.summary, state)
                if second.meaningful_change:
                    state.zero_progress_rounds = 0
                    state.history.append("same-target retry produced task progress")
                    return None
                latest = state.observation

        alternate = self._alternate_action(action, observation, latest)
        if alternate is not None and latest is not None:
            alternate_policy = self.policy.evaluate(state.task, alternate, latest)
            if alternate_policy.disposition == PolicyDisposition.BLOCK:
                return self._failure(
                    alternate_policy.reason,
                    stage="policy",
                    error_code=(
                        "ASSISTIVE_HARD_BLOCK"
                        if alternate_policy.hard_block
                        else "ASSISTIVE_POLICY_REJECTED"
                    ),
                    metrics=state.metrics,
                    observation=latest,
                )
            if alternate_policy.disposition == PolicyDisposition.AUTO:
                state.metrics.retries += 1
                changed = self._execute_once(
                    state,
                    alternate,
                    latest,
                    cancel_event=cancel_event,
                )
                if isinstance(changed, ComputerControlResult):
                    return changed
                if changed.outcome.value == "completed" or state.verification.completed:
                    return self._success(state.verification.summary, state)
                if changed.meaningful_change:
                    state.zero_progress_rounds = 0
                    state.history.append("alternate Invoke/Select produced task progress")
                    return None

        state.zero_progress_rounds += 1
        state.metrics.replans += 1
        state.history.append("no effect after bounded retry/alternate; replan from fresh state")
        if state.zero_progress_rounds >= 2:
            return self._failure(
                "连续两轮动作均无任务进展",
                stage="verify",
                error_code="ASSISTIVE_NO_PROGRESS",
                metrics=state.metrics,
                observation=state.observation,
            )
        return None

    def _drive(
        self,
        state: _RunState,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult:
        assert self.driver is not None and self.planner is not None
        task_payload = json.dumps(
            state.task.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        while state.metrics.actions < self.max_steps:
            if self._cancelled(cancel_event):
                return self._cancelled_result(state.metrics)
            if self._monotonic() >= state.deadline:
                return self._failure(
                    "桌面任务超过总时间上限",
                    stage="runtime",
                    error_code="ASSISTIVE_TASK_TIMEOUT",
                    metrics=state.metrics,
                    timed_out=True,
                    observation=state.observation,
                )
            try:
                verification = self._verify(state, cancel_event=cancel_event)
            except Exception as exc:
                if _is_hard_desktop_boundary(exc):
                    return self._failure(
                        "安全桌面、权限边界或凭据字段阻止了任务验证",
                        stage="goal_verifier",
                        error_code="ASSISTIVE_HARD_BLOCK",
                        metrics=state.metrics,
                        exception=exc,
                        observation=state.observation,
                    )
                return self._failure(
                    "无法读取任务级目标状态",
                    stage="goal_verifier",
                    error_code="ASSISTIVE_VERIFY_FAILED",
                    metrics=state.metrics,
                    exception=exc,
                    observation=state.observation,
                )
            if verification.completed:
                return self._success(verification.summary, state)

            observation = state.observation
            if observation is not None and observation.screenshot_png is None and (
                self._visual_profile_requires_screenshot(observation)
                or not any(
                    element.enabled and element.addressable
                    for element in observation.elements
                )
            ):
                try:
                    observation = self._observe(
                        observation.app,
                        cancel_event=cancel_event,
                        screenshot=True,
                    )
                except Exception as exc:
                    if _is_hard_desktop_boundary(exc):
                        return self._failure(
                            "安全桌面、权限边界或凭据字段阻止了截图观察",
                            stage="observe",
                            error_code="ASSISTIVE_HARD_BLOCK",
                            metrics=state.metrics,
                            exception=exc,
                            observation=state.observation,
                        )
                    self._debug_exception(exc, stage="observe")
                else:
                    state.observation = observation
                    state.metrics.screenshots += 1
            try:
                decision = self.planner.decide(
                    task_payload,
                    apps=state.inventory,
                    observation=state.observation,
                    history=state.history,
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                state.planner_failures += 1
                state.metrics.replans += 1
                self._debug_exception(exc, stage="plan")
                state.history.append(
                    f"planner failed transiently: {type(exc).__name__}; retry from fresh state"
                )
                if state.planner_failures >= 2:
                    return self._failure(
                        "单步规划器连续两次未返回可执行决定",
                        stage="plan",
                        error_code="ASSISTIVE_PLANNER_FAILED",
                        metrics=state.metrics,
                        exception=exc,
                        observation=state.observation,
                    )
                continue
            self._debug_note(
                "plan",
                f"decision={decision.kind.value} app={decision.app or '-'} "
                f"action={decision.action.type.value if decision.action else '-'} "
                f"elements={len(state.observation.elements) if state.observation else 0} "
                f"img={bool(state.observation and state.observation.screenshot_png)} "
                f"why={decision.reason[:160]}",
            )

            if decision.kind == AssistiveDecisionKind.DONE:
                if state.verification.pending_unverifiable and state.observation is not None:
                    # No local signal can decide these goals. The planner has
                    # inspected the window, so its completion judgement stands.
                    return self._success(
                        "规划器确认任务已完成（目标无法本地验证，以规划器判断为准）",
                        state,
                    )
                # Verifiable goals never complete on a planner claim alone.
                state.history.append("planner suggested done; unresolved goals remain")
                state.planner_failures += 1
                if state.planner_failures >= 2:
                    return self._failure(
                        "规划器连续声称完成，但最终目标仍未成立",
                        stage="goal_verifier",
                        error_code="ASSISTIVE_GOALS_NOT_MET",
                        metrics=state.metrics,
                        observation=state.observation,
                    )
                continue
            if decision.kind in {
                AssistiveDecisionKind.OBSERVE,
                AssistiveDecisionKind.SCREENSHOT,
            }:
                assert decision.app is not None
                before_observe = self._before_probe(state)
                if (
                    decision.kind == AssistiveDecisionKind.SCREENSHOT
                    and state.observation is not None
                    and state.observation.screenshot_png is not None
                    and state.observation.app.casefold() == decision.app.casefold()
                ):
                    viewport = next(
                        (
                            element.index
                            for element in state.observation.elements
                            if element.control_type == "VisualViewport"
                        ),
                        None,
                    )
                    state.history.append(
                        "a fresh screenshot of this window was already attached; "
                        + (
                            f"act on it: click VisualViewport element {viewport} with x/y"
                            if viewport is not None
                            else "act on it or observe another app"
                        )
                    )
                activation = self._activate_for_observation(
                    state,
                    decision.app,
                    cancel_event=cancel_event,
                )
                if activation is not None:
                    return activation
                try:
                    state.observation = self._observe(
                        decision.app,
                        cancel_event=cancel_event,
                        screenshot=decision.kind == AssistiveDecisionKind.SCREENSHOT,
                    )
                    if decision.kind == AssistiveDecisionKind.SCREENSHOT:
                        state.metrics.screenshots += 1
                except Exception as exc:
                    if _is_hard_desktop_boundary(exc):
                        return self._failure(
                            "安全桌面、权限边界或凭据字段阻止了窗口观察",
                            stage="observe",
                            error_code="ASSISTIVE_HARD_BLOCK",
                            metrics=state.metrics,
                            exception=exc,
                            observation=state.observation,
                        )
                    self._debug_exception(exc, stage="observe")
                    state.history.append(
                        f"observation of selected app failed transiently: {type(exc).__name__}"
                    )
                    state.observation_failures += 1
                    if state.observation_failures >= 2:
                        return self._failure(
                            "连续两轮无法观察规划器选择的窗口",
                            stage="observe",
                            error_code="ASSISTIVE_OBSERVE_FAILED",
                            metrics=state.metrics,
                            exception=exc,
                        )
                else:
                    state.planner_failures = 0
                    state.observation_failures = 0
                    state.passive_observations += 1
                    try:
                        self._verify(state, cancel_event=cancel_event)
                    except Exception as exc:
                        if _is_hard_desktop_boundary(exc):
                            return self._failure(
                                "安全桌面、权限边界或凭据字段阻止了任务验证",
                                stage="observe",
                                error_code="ASSISTIVE_HARD_BLOCK",
                                metrics=state.metrics,
                                exception=exc,
                                observation=state.observation,
                            )
                        self._debug_exception(exc, stage="observe")
                    if state.verification.completed:
                        return self._success(state.verification.summary, state)
                    after_observe = self._before_probe(state)
                    if (
                        after_observe.satisfied_goal_count
                        > before_observe.satisfied_goal_count
                    ):
                        state.history.append(
                            "requested observation exposed task-relevant fresh state"
                        )
                    else:
                        state.history.append(
                            "requested observation was unchanged and is not an action round"
                        )
                    # UIA -> screenshot -> action is a legitimate escalation
                    # sequence. Passive reads do not consume the two failed
                    # action rounds, but a planner may not observe forever.
                    if state.passive_observations >= 4:
                        return self._failure(
                            "规划器连续三次只观察而未提出动作",
                            stage="observe",
                            error_code="ASSISTIVE_OBSERVE_LOOP",
                            metrics=state.metrics,
                            observation=state.observation,
                        )
                continue

            if decision.kind == AssistiveDecisionKind.FAIL:
                state.planner_failures += 1
                state.metrics.replans += 1
                state.history.append(
                    f"planner could not find a step: {_safe_message(decision.reason)}"
                )
                if state.planner_failures >= 2:
                    return self._failure(
                        "规划器在两次新状态评估中都无法找到可行步骤",
                        stage="plan",
                        error_code="ASSISTIVE_NO_PLAN",
                        metrics=state.metrics,
                        observation=state.observation,
                    )
                if state.observation is not None and state.observation.screenshot_png is None:
                    try:
                        state.observation = self._observe(
                            state.observation.app,
                            cancel_event=cancel_event,
                            screenshot=True,
                        )
                    except Exception as exc:
                        if _is_hard_desktop_boundary(exc):
                            return self._failure(
                                "安全桌面、权限边界或凭据字段阻止了截图观察",
                                stage="observe",
                                error_code="ASSISTIVE_HARD_BLOCK",
                                metrics=state.metrics,
                                exception=exc,
                                observation=state.observation,
                            )
                    else:
                        state.metrics.screenshots += 1
                continue

            assert decision.action is not None
            state.planner_failures = 0
            state.observation_failures = 0
            state.passive_observations = 0
            if state.observation is None:
                return self._failure(
                    "规划器动作缺少当前窗口观察",
                    stage="plan",
                    error_code="ASSISTIVE_ACTION_WITHOUT_OBSERVATION",
                    metrics=state.metrics,
                )
            result = self._perform_action(
                state,
                decision.action,
                state.observation,
                cancel_event=cancel_event,
            )
            if result is not None:
                return result

        return self._failure(
            "桌面任务达到动作上限但最终目标仍未成立",
            stage="runtime",
            error_code="ASSISTIVE_MAX_ACTIONS",
            metrics=state.metrics,
            observation=state.observation,
        )

    def _resolve_task_goals(self, task: TaskSpec, inventory: str) -> TaskSpec:
        """Demote an app goal nobody can satisfy into one free-form goal.

        The parser cannot know whether "周报生成聊天框" names a window or a
        chat inside one. A configured app or alias family stays an app goal
        even when no window is open yet; any other spoken name must match a
        visible window (process, class, or title) or the whole utterance is
        handed to the planner as free-form intent instead of failing forever.
        """

        entries = _inventory_entries(inventory)
        settings = getattr(self.native_router, "settings", None)
        configured = {
            _normalized(name) for name in (getattr(settings, "apps", None) or {})
        }
        opens_place = _OPEN_PLACE_RE.match(task.raw_text) is not None
        goals = list(task.goals)
        changed = False
        resolver: SpokenPathResolver | None = None
        for index, goal in enumerate(goals):
            if goal.kind == GoalKind.FILE_SENT:
                if Path(os.path.expandvars(goal.target)).is_file():
                    continue
                resolver = resolver or self._spoken_path_resolver()
                match = resolver.resolve(goal.target)
                if match is not None and match.path.is_file():
                    goals[index] = Goal(GoalKind.FILE_SENT, str(match.path), app=goal.app)
                    changed = True
                continue
            if goal.kind != GoalKind.APP_FOREGROUND:
                continue
            target = _normalized(goal.target)
            if target in configured or len(_app_aliases(goal.target)) > 1:
                continue
            if any(_entry_matches_app(entry, goal.target) for entry in entries):
                continue
            if opens_place:
                # "打开d盘研究数据库那个文件夹" names a place, not a window: try the
                # project map and spoken base folders before giving up on it.
                # Without a location cue only an unmistakable match counts, so
                # "打开记事本" is never hijacked by a similarly named project.
                resolver = resolver or self._spoken_path_resolver()
                match = resolver.resolve(goal.target, strict=not has_location_cue(goal.target))
                if match is not None:
                    goals[index] = Goal(GoalKind.PATH_OPEN, str(match.path))
                    changed = True
                    continue
                if self._launchable(goal.target):
                    # No window yet, but an installed app answers to the name;
                    # the activation skill launches it first.
                    continue
            return TaskSpec(
                goals=(Goal(GoalKind.FREE_FORM, task.raw_text),),
                forbid_submit=task.forbid_submit,
                side_effect=task.side_effect,
                raw_text=task.raw_text,
            )
        if not changed:
            return task
        return TaskSpec(
            goals=tuple(goals),
            forbid_submit=task.forbid_submit,
            side_effect=task.side_effect,
            raw_text=task.raw_text,
        )

    def _spoken_path_resolver(self) -> SpokenPathResolver:
        settings = getattr(self.native_router, "settings", None)
        execution = getattr(settings, "execution", None)
        aliases = getattr(execution, "path_aliases", None)
        return SpokenPathResolver(
            workmap_index=getattr(self.native_router, "workmap_index", None),
            path_aliases=(
                {str(name): str(target) for name, target in aliases.items()}
                if isinstance(aliases, dict)
                else None
            ),
        )

    def _launchable(self, name: str) -> bool:
        native = self._native_backend()
        if native is None or not callable(getattr(native, "shell_execute_path", None)):
            return False
        settings = getattr(self.native_router, "settings", None)
        profiles = getattr(settings, "apps", None) or {}
        try:
            return AppLaunchSkill(native, profiles).resolve(name) is not None
        except Exception:
            return False

    def _file_goal_problem(self, task: TaskSpec) -> tuple[str, str] | None:
        """Reject impossible file deliveries before any window is touched."""

        for goal in task.goals:
            if goal.kind != GoalKind.FILE_SENT:
                continue
            family = self._family_of(goal.app)
            if goal.app is not None and family != "wechat":
                return (
                    f"暂不支持向 {goal.app} 发送文件，目前只支持微信",
                    "ASSISTIVE_UNSUPPORTED_TARGET",
                )
            if Path(os.path.expandvars(goal.target)).is_file():
                continue
            suggestions = self._spoken_path_resolver().suggest(goal.target)
            hint = f"；最接近的是：{'、'.join(suggestions)}" if suggestions else ""
            return (
                f"没有找到要发送的文件“{goal.target}”{hint}",
                "ASSISTIVE_FILE_NOT_FOUND",
            )
        return None

    @staticmethod
    def _missing_path_goal(task: TaskSpec) -> Goal | None:
        """Return an absolute path goal whose target does not exist locally."""

        for goal in task.goals:
            if goal.kind != GoalKind.PATH_OPEN:
                continue
            expanded = os.path.expandvars(goal.target)
            if not PureWindowsPath(expanded).drive and not expanded.startswith("\\\\"):
                continue
            try:
                if not Path(expanded).exists():
                    return goal
            except OSError:
                return goal
        return None

    @staticmethod
    def _is_foreground(inventory: str, app: str) -> bool:
        return any(
            entry.get("foreground") is True
            and str(entry.get("app") or "").casefold() == app.casefold()
            for entry in _inventory_entries(inventory)
        )

    @staticmethod
    def _family_foreground(inventory: str, app: str) -> bool:
        return any(
            entry.get("foreground") is True and _entry_matches_app(entry, app)
            for entry in _inventory_entries(inventory)
        )

    @staticmethod
    def _family_of(app: str | None) -> str | None:
        if not app:
            return None
        aliases = _app_aliases(app)
        return aliases[0] if len(aliases) > 1 else None

    @classmethod
    def _foreground_family(cls, inventory: str) -> str | None:
        for entry in _inventory_entries(inventory):
            if entry.get("foreground") is not True:
                continue
            for family in _CHAT_FAMILIES:
                if _entry_matches(entry, family):
                    return family
        return None

    def _try_conversation_skill(
        self,
        state: _RunState,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult | None:
        """Open a named conversation in the chat app that is (now) in front."""

        goal = next(
            (
                item
                for item in state.verification.pending
                if item.kind == GoalKind.CONVERSATION_SELECTED
            ),
            None,
        )
        native = self._native_backend()
        if goal is None or native is None or self.driver is None or self._cancelled(cancel_event):
            return None
        family = self._family_of(goal.app) if goal.app else self._foreground_family(state.inventory)
        if family not in _CHAT_FAMILIES or not self._family_foreground(state.inventory, family):
            self._debug_note(
                "conversation_skill",
                f"idle: family={family} foreground="
                f"{self._family_foreground(state.inventory, family) if family else None}",
            )
            return None
        app_id = self._chat_window(state.inventory, family) or self._foreground_app(
            state.inventory, family
        )
        activate = getattr(self.driver, "activate_app", None)
        if app_id is None or not callable(activate):
            self._debug_note("conversation_skill", f"idle: no window for family={family}")
            return None
        try:
            hwnd = activate(app_id, cancel_event=cancel_event)
        except Exception as exc:
            if _is_hard_desktop_boundary(exc):
                return self._failure(
                    "安全桌面或权限边界阻止了聊天窗口激活",
                    stage="conversation_skill",
                    error_code="ASSISTIVE_HARD_BLOCK",
                    metrics=state.metrics,
                    exception=exc,
                    observation=state.observation,
                )
            self._debug_exception(exc, stage="conversation_skill")
            return None
        if family == "wechat":
            skill: Any = WeChatConversationSkill(native, self.driver, self.policy)
        else:
            # Claude keeps Code sessions and Chat and Cowork chats on separate
            # surfaces; an unqualified request means Code.
            mode = (goal.mode or CLAUDE_DEFAULT_MODE) if family == "claude" else None
            skill = PaletteConversationSkill(
                native,
                self.driver,
                self.policy,
                hotkey=PALETTE_HOTKEYS.get(family, "ctrl+k"),
                mode=mode,
                mode_buttons=CLAUDE_MODE_BUTTONS if family == "claude" else None,
            )
        result = skill.open(
            state.task,
            app=app_id,
            name=goal.target,
            hwnd=int(hwnd),
            cancel_event=cancel_event,
        )
        if result.observation is not None:
            state.observation = result.observation
        if result.status == "hard_failure":
            return self._failure(
                result.message,
                stage="conversation_skill",
                error_code="ASSISTIVE_HARD_BLOCK",
                metrics=state.metrics,
                observation=state.observation,
            )
        if result.status == "policy_rejected":
            return self._failure(
                result.message,
                stage="conversation_skill",
                error_code="ASSISTIVE_POLICY_REJECTED",
                metrics=state.metrics,
                observation=state.observation,
            )
        if result.status != "succeeded":
            state.metrics.native_fallbacks += 1
            state.history.append(
                f"{family} conversation skill failed transiently; generic planner takes over"
            )
            self._debug_note(
                "conversation_skill",
                f"retryable: {result.message} | "
                f"{result.details.get('error_type', '-')}: {result.details.get('error', '-')}",
            )
            return None
        state.metrics.actions += int(result.details.get("actions", 1))
        state.history.append(
            f"opened conversation via {family} skill: {result.details.get('item')}"
        )
        # The skill clicked the exact named entry; that is the completion
        # evidence for a goal no UIA-level verifier can prove in these apps.
        state.skill_completed.add(goal)
        try:
            self._verify(state, cancel_event=cancel_event)
        except Exception as exc:
            if _is_hard_desktop_boundary(exc):
                return self._failure(
                    "安全桌面、权限边界或凭据字段阻止了会话验证",
                    stage="conversation_skill",
                    error_code="ASSISTIVE_HARD_BLOCK",
                    metrics=state.metrics,
                    exception=exc,
                    observation=state.observation,
                )
            self._debug_exception(exc, stage="conversation_skill")
        if state.verification.completed:
            return self._success(f"已打开会话：{goal.target}", state)
        state.history.append("conversation opened; remaining goals continue")
        return None

    def _try_send_file_skill(
        self,
        state: _RunState,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult | None:
        """Hand a resolved local file to the WeChat conversation opened just before."""

        goal = next(
            (item for item in state.verification.pending if item.kind == GoalKind.FILE_SENT),
            None,
        )
        if goal is None or self.driver is None or self._cancelled(cancel_event):
            return None
        if any(item.kind == GoalKind.CONVERSATION_SELECTED for item in state.verification.pending):
            return None
        family = self._family_of(goal.app) or self._foreground_family(state.inventory)
        native = self._native_backend()
        if native is None:
            return None
        if family != "wechat":
            return self._failure(
                f"暂不支持向 {family or '当前应用'} 发送文件，目前只支持微信",
                stage="send_file_skill",
                error_code="ASSISTIVE_UNSUPPORTED_TARGET",
                metrics=state.metrics,
            )
        if not self._family_foreground(state.inventory, family):
            return None
        app_id = self._chat_window(state.inventory, family) or self._foreground_app(
            state.inventory, family
        )
        activate = getattr(self.driver, "activate_app", None)
        if app_id is None or not callable(activate):
            return None
        path = Path(os.path.expandvars(goal.target))
        if not path.is_file():
            return self._failure(
                "没有找到要发送的本地文件",
                stage="send_file_skill",
                error_code="ASSISTIVE_FILE_NOT_FOUND",
                metrics=state.metrics,
            )
        try:
            hwnd = activate(app_id, cancel_event=cancel_event)
        except Exception as exc:
            if _is_hard_desktop_boundary(exc):
                return self._failure(
                    "安全桌面或权限边界阻止了微信窗口激活",
                    stage="send_file_skill",
                    error_code="ASSISTIVE_HARD_BLOCK",
                    metrics=state.metrics,
                    exception=exc,
                )
            self._debug_exception(exc, stage="send_file_skill")
            return None
        conversation = next(
            (
                item.target
                for item in state.task.goals
                if item.kind == GoalKind.CONVERSATION_SELECTED
            ),
            None,
        )
        result = WeChatSendFileSkill(native, self.driver, self.policy).send(
            state.task,
            app=app_id,
            hwnd=int(hwnd),
            path=path,
            conversation=conversation,
            cancel_event=cancel_event,
        )
        if result.observation is not None:
            state.observation = result.observation
        if result.status == "hard_failure":
            return self._failure(
                result.message,
                stage="send_file_skill",
                error_code="ASSISTIVE_HARD_BLOCK",
                metrics=state.metrics,
                observation=state.observation,
            )
        if result.status == "policy_rejected":
            return self._failure(
                result.message,
                stage="send_file_skill",
                error_code="ASSISTIVE_POLICY_REJECTED",
                metrics=state.metrics,
                observation=state.observation,
            )
        if result.details.get("wrong_conversation"):
            return self._failure(
                f"当前打开的微信聊天不是“{conversation}”，已中止发送",
                stage="send_file_skill",
                error_code="ASSISTIVE_WRONG_CONVERSATION",
                metrics=state.metrics,
                observation=state.observation,
            )
        if result.status == "unverified":
            state.metrics.actions += int(result.details.get("actions", 1))
            return self._failure(
                f"已尝试把 {path.name} 发出，但聊天区没有看到它，请检查微信",
                stage="send_file_skill",
                error_code="ASSISTIVE_SEND_UNVERIFIED",
                metrics=state.metrics,
                observation=state.observation,
            )
        if result.status != "succeeded":
            state.metrics.native_fallbacks += 1
            state.history.append(
                "WeChat file sending failed transiently; generic planner takes over"
            )
            self._debug_note(
                "send_file_skill",
                f"retryable: {result.message} | "
                f"{result.details.get('error_type', '-')}: {result.details.get('error', '-')}",
            )
            return None
        state.metrics.actions += int(result.details.get("actions", 1))
        state.skill_completed.add(goal)
        try:
            self._verify(state, cancel_event=cancel_event)
        except Exception as exc:
            if _is_hard_desktop_boundary(exc):
                return self._failure(
                    "安全桌面、权限边界或凭据字段阻止了发送验证",
                    stage="send_file_skill",
                    error_code="ASSISTIVE_HARD_BLOCK",
                    metrics=state.metrics,
                    exception=exc,
                    observation=state.observation,
                )
            self._debug_exception(exc, stage="send_file_skill")
        if result.details.get("attached") and result.details.get("sent") is False:
            return self._success(f"文件 {path.name} 已附加到输入框，按要求没有发送", state)
        return self._success(f"已发送文件：{path.name}，聊天窗口中已看到该文件", state)

    @staticmethod
    def _composer_element(observation: DesktopObservation) -> DesktopElement | None:
        """Pick the one editable input a dictated draft should go into."""

        candidates = [
            element
            for element in observation.elements
            if element.enabled
            and element.addressable
            and element.editable is not False
            and not element.visual_ocr
            and not element.password
            and not element.secret_labeled
            # The browser address bar is an input too, but a dictated chat
            # message never belongs there.
            and not element.browser_chrome
            and (element.composer or element_plane(element) == ElementPlane.INPUT)
        ]
        flagged = [element for element in candidates if element.composer]
        pool = flagged or candidates
        if len(pool) == 1:
            return pool[0]
        focused = [element for element in pool if element.focused is True]
        return focused[0] if len(focused) == 1 else None

    def _try_composer_draft_skill(
        self,
        state: _RunState,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult | None:
        """Type a dictated draft into the app's single composer without the planner.

        Focus first when needed, then insert the exact text. Each step goes
        through the same policy check and bounded wait as a planner action, and
        the INPUT_CONTAINS goal is still proved by the local verifier.
        """

        pending = [
            goal for goal in state.verification.pending if goal.kind == GoalKind.INPUT_CONTAINS
        ]
        if len(pending) != 1 or self.driver is None or self._cancelled(cancel_event):
            return None
        goal = pending[0]
        if any(
            item.kind == GoalKind.URL_LOADED and item.app == goal.app
            for item in state.verification.pending
        ):
            # The page that will hold the draft has not been opened yet.
            return None
        # A draft that follows navigation ("open chatgpt.com, then ask …") sees
        # a stale address-bar observation; the composer renders only after the
        # page loads. Re-observe the foreground app so its composer is visible.
        app = self._foreground_app(state.inventory, goal.app) or (
            state.observation.app if state.observation is not None else None
        )
        observation = state.observation
        if app is not None:
            try:
                observation = self._observe(app, cancel_event=cancel_event)
                state.observation = observation
            except Exception as exc:
                if _is_hard_desktop_boundary(exc):
                    raise
                observation = state.observation
        if observation is None:
            self._debug_note("composer_skill", f"idle: no observation for app={app}")
            return None
        if goal.app and not GoalVerifier._app_scope_matches(goal, observation):
            self._debug_note(
                "composer_skill",
                f"idle: goal app {goal.app} does not match observation "
                f"{observation.app}/{observation.process_name}",
            )
            return None
        # Right after navigation the page may still be rendering; give the
        # composer a few seconds to appear before handing over to the planner.
        for _wait in range(self.composer_wait_polls):
            if self._composer_element(observation) is not None or app is None:
                break
            time.sleep(self.composer_wait_interval)
            try:
                # An unconfigured browser window is identified by its title,
                # which changes as the page loads; re-read the inventory so a
                # fresh window id is used for every poll.
                state.inventory = self.driver.list_apps(cancel_event=cancel_event)
                app = self._foreground_app(state.inventory, goal.app) or app
                observation = self._observe(app, cancel_event=cancel_event)
                state.observation = observation
            except Exception as exc:
                if _is_hard_desktop_boundary(exc):
                    raise
        for _attempt in range(2):
            composer = self._composer_element(observation)
            if composer is None:
                inputs = [
                    (
                        element.index,
                        element.control_type,
                        element.name[:24],
                        element.focused,
                        element.composer,
                        element.browser_chrome,
                        element.editable,
                    )
                    for element in observation.elements
                    if element.composer or element_plane(element) == ElementPlane.INPUT
                ]
                self._debug_note(
                    "composer_skill",
                    f"idle: no unique composer in {observation.app} "
                    f"({len(observation.elements)} elements); inputs={inputs[:8]}",
                )
                return None
            if composer.focused is not True:
                focus = DesktopAction(
                    DesktopActionType.CLICK,
                    app=observation.app,
                    generation=observation.generation,
                    element_index=composer.index,
                )
                result = self._perform_action(state, focus, observation, cancel_event=cancel_event)
                if result is not None:
                    return result
                observation = state.observation
                if observation is None:
                    return None
                continue
            draft = DesktopAction(
                DesktopActionType.TYPE_TEXT,
                app=observation.app,
                generation=observation.generation,
                element_index=composer.index,
                text=goal.target,
            )
            state.history.append("composer draft skill typed the dictated text")
            return self._perform_action(state, draft, observation, cancel_event=cancel_event)
        return None

    def _activate_for_observation(
        self,
        state: _RunState,
        app: str,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult | None:
        """Bring a planner-selected window forward as one explicit action.

        Observation itself never changes the desktop. When the planner turns
        to another listed window, activation is a separate bounded step so a
        later action there has the foreground it requires.
        """

        if self._is_foreground(state.inventory, app):
            return None
        activate = getattr(self.driver, "activate_app", None)
        if not callable(activate):
            return None
        try:
            activate(app, cancel_event=cancel_event)
        except Exception as exc:
            if _is_hard_desktop_boundary(exc):
                return self._failure(
                    "安全桌面或权限边界阻止了窗口激活",
                    stage="observe",
                    error_code="ASSISTIVE_HARD_BLOCK",
                    metrics=state.metrics,
                    exception=exc,
                    observation=state.observation,
                )
            self._debug_exception(exc, stage="observe")
            state.history.append(
                f"activation of the selected window failed transiently: {type(exc).__name__}"
            )
            return None
        state.metrics.actions += 1
        state.history.append("activated the planner-selected window before observing it")
        try:
            state.inventory = self.driver.list_apps(cancel_event=cancel_event)
        except Exception as exc:
            if _is_hard_desktop_boundary(exc):
                return self._failure(
                    "安全桌面或权限边界阻止了桌面清单刷新",
                    stage="observe",
                    error_code="ASSISTIVE_HARD_BLOCK",
                    metrics=state.metrics,
                    exception=exc,
                    observation=state.observation,
                )
            self._debug_exception(exc, stage="observe")
        return None

    @staticmethod
    def _only_file_paths_pending(state: _RunState) -> bool:
        pending = state.verification.pending
        return bool(pending) and all(
            goal.kind == GoalKind.PATH_OPEN and Path(os.path.expandvars(goal.target)).is_file()
            for goal in pending
        )

    def _try_open_path_skill(
        self,
        state: _RunState,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult | None:
        """Open resolved local paths directly: folders in Explorer, files in their app."""

        pending = [goal for goal in state.verification.pending if goal.kind == GoalKind.PATH_OPEN]
        native = self._native_backend()
        if not pending or native is None or self._cancelled(cancel_event):
            return None
        for goal in pending:
            target = Path(os.path.expandvars(goal.target))
            if not target.exists():
                return None
            try:
                if target.is_dir():
                    result = ExplorerSkill(native).open_directory(target)
                    if result.status == "hard_failure":
                        return self._failure(
                            result.message,
                            stage="open_path_skill",
                            error_code="ASSISTIVE_HARD_BLOCK",
                            metrics=state.metrics,
                        )
                    if result.status != "succeeded":
                        state.metrics.native_fallbacks += 1
                        state.history.append(
                            f"Explorer could not show the folder: {result.message}"
                        )
                        return None
                    state.metrics.actions += 1
                else:
                    if target.suffix.casefold() in EXECUTABLE_SUFFIXES:
                        return self._failure(
                            f"{target.name} 是可执行文件，assistive_v1 不会自动运行它",
                            stage="open_path_skill",
                            error_code="ASSISTIVE_POLICY_REJECTED",
                            metrics=state.metrics,
                        )
                    native.open_path(target)
                    state.metrics.actions += 1
                    state.skill_completed.add(goal)
            except Exception as exc:
                if _is_hard_desktop_boundary(exc):
                    return self._failure(
                        "安全桌面或权限边界阻止了打开本地路径",
                        stage="open_path_skill",
                        error_code="ASSISTIVE_HARD_BLOCK",
                        metrics=state.metrics,
                        exception=exc,
                    )
                self._debug_exception(exc, stage="open_path_skill")
                state.history.append(f"opening the path failed transiently: {type(exc).__name__}")
                return None
        try:
            self._verify(state, cancel_event=cancel_event)
        except Exception as exc:
            if _is_hard_desktop_boundary(exc):
                return self._failure(
                    "安全桌面、权限边界或凭据字段阻止了路径验证",
                    stage="open_path_skill",
                    error_code="ASSISTIVE_HARD_BLOCK",
                    metrics=state.metrics,
                    exception=exc,
                )
            self._debug_exception(exc, stage="open_path_skill")
            return None
        if state.verification.completed:
            return self._success(state.verification.summary, state)
        state.history.append("resolved path opened; remaining goals continue")
        return None

    @staticmethod
    def _root_drive(task: TaskSpec) -> str | None:
        if len(task.goals) != 1 or task.goals[0].kind != GoalKind.PATH_OPEN:
            return None
        target = task.goals[0].target.strip()
        try:
            path = PureWindowsPath(target)
        except ValueError:
            return None
        return str(path) if path.drive and str(path) == f"{path.drive}\\" else None

    def _try_explorer_skill(
        self,
        state: _RunState,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult | None:
        native = self._native_backend()
        if native is None or self._cancelled(cancel_event):
            return None
        activate = bool(
            len(state.task.goals) == 1
            and state.task.goals[0].kind == GoalKind.APP_FOREGROUND
            and _normalized(state.task.goals[0].target) in _app_aliases("explorer")
        )
        drive = self._root_drive(state.task)
        if not activate and drive is None:
            return None
        skill = ExplorerSkill(native)
        result = skill.activate_or_open() if activate else skill.open_drive(drive or "")
        if result.status == "hard_failure":
            return self._failure(
                result.message,
                stage="explorer_skill",
                error_code="ASSISTIVE_HARD_BLOCK",
                metrics=state.metrics,
            )
        if result.status == "retryable_failure":
            state.metrics.native_fallbacks += 1
            state.history.append(
                "Explorer skill failed transiently; generic assistive planner takes over"
            )
            return None
        try:
            self._verify(state, cancel_event=cancel_event)
        except Exception as exc:
            if _is_hard_desktop_boundary(exc):
                return self._failure(
                    "安全桌面、权限边界或凭据字段阻止了 Explorer 验证",
                    stage="explorer_skill",
                    error_code="ASSISTIVE_HARD_BLOCK",
                    metrics=state.metrics,
                    exception=exc,
                    observation=state.observation,
                )
            state.metrics.native_fallbacks += 1
            self._debug_exception(exc, stage="explorer_skill")
            return None
        if state.verification.completed:
            return self._success(state.verification.summary, state)
        state.metrics.native_fallbacks += 1
        state.history.append("Explorer skill returned progress but final goals remain")
        return None

    def _route_native(
        self,
        instruction: str,
        *,
        cancel_event: threading.Event | None,
    ) -> NativeSkillResult:
        route = self.native_router.route
        kwargs: dict[str, Any] = {}
        try:
            parameters = inspect.signature(route).parameters.values()
            if any(
                parameter.name == "cancel_event" or parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            ):
                kwargs["cancel_event"] = cancel_event
        except (TypeError, ValueError):
            pass
        return route(instruction, **kwargs)

    def _try_native(
        self,
        state: _RunState,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult | None:
        try:
            result = self._route_native(state.task.raw_text, cancel_event=cancel_event)
        except Exception as exc:
            if _is_path_identity_change(exc) or _is_hard_desktop_boundary(exc):
                return self._failure(
                    "本地目标身份、安全桌面或权限边界在执行前发生变化",
                    stage="native_route",
                    error_code="ASSISTIVE_HARD_BLOCK",
                    metrics=state.metrics,
                    exception=exc,
                    observation=state.observation,
                )
            state.metrics.native_fallbacks += 1
            state.history.append(f"native router raised {type(exc).__name__}; generic fallback")
            self._debug_exception(exc, stage="native_route")
            return None
        if self._cancelled(cancel_event):
            return self._cancelled_result(state.metrics)
        if result.status == NativeRouteStatus.BLOCKED:
            return self._failure(
                "本机安全边界阻止了该操作",
                stage="native_route",
                error_code="ASSISTIVE_HARD_BLOCK",
                metrics=state.metrics,
            )
        if result.status == NativeRouteStatus.CONFIRMATION_REQUIRED:
            # A native Plan does not carry the exact UIA HWND/control binding
            # required by assistive phrase confirmation.  Never create an
            # unbound capability or replay the text after consent; let the
            # generic loop observe the exact target and ask through policy.py.
            state.metrics.native_fallbacks += 1
            state.history.append(
                "native route requires confirmation; rebind exact UI action in generic planner"
            )
            return None
        if result.status == NativeRouteStatus.SUCCEEDED:
            try:
                self._verify(state, cancel_event=cancel_event)
            except Exception as exc:
                if _is_hard_desktop_boundary(exc):
                    return self._failure(
                        "安全桌面、权限边界或凭据字段阻止了 native 结果验证",
                        stage="native_route",
                        error_code="ASSISTIVE_HARD_BLOCK",
                        metrics=state.metrics,
                        exception=exc,
                        observation=state.observation,
                    )
                state.metrics.native_fallbacks += 1
                self._debug_exception(exc, stage="native_route")
                return None
            if state.verification.completed:
                return self._success(state.verification.summary, state)
            if state.verification.pending_unverifiable or self._only_file_paths_pending(state):
                # The native skill verified its own local postcondition and the
                # remaining goals have no local verifier of their own (a file
                # handed to its default application has no window identity).
                return self._success("本机技能已完成该操作并通过其本地后置条件", state)
            state.metrics.native_fallbacks += 1
            state.history.append("native action completed but task goals remain; generic fallback")
            return None
        if result.status in {
            NativeRouteStatus.MISS,
            NativeRouteStatus.RETRYABLE_FAILURE,
            NativeRouteStatus.FAILED,
        }:
            if result.status != NativeRouteStatus.MISS:
                state.metrics.native_fallbacks += 1
                state.history.append("native skill failed transiently; generic assistive fallback")
            return None
        return None

    def _try_app_activation_skill(
        self,
        state: _RunState,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult | None:
        pending = next(
            (
                check.goal
                for check in state.verification.checks
                if not check.satisfied and check.goal.kind == GoalKind.APP_FOREGROUND
            ),
            None,
        )
        target = pending.target if pending is not None else None
        if target is None:
            # "在 Claude 里输入…" names the app only as goal scope; bringing it
            # forward is still a deterministic step, not planner work.
            scoped = next(
                (
                    check.goal
                    for check in state.verification.checks
                    if not check.satisfied and check.goal.app
                ),
                None,
            )
            if scoped is not None and not self._family_foreground(state.inventory, scoped.app):
                target = scoped.app
        native = self._native_backend()
        if target is None or native is None or self._cancelled(cancel_event):
            return None
        settings = getattr(self.native_router, "settings", None)
        profiles = getattr(settings, "apps", {})
        result = AppActivationSkill(native, profiles).activate(target)
        if result.status == "retryable_failure" and "No visible top-level window" in result.message:
            # Nothing to activate yet: start the installed app, then bind to
            # the window it opens.
            launched = AppLaunchSkill(native, profiles).launch(target)
            if launched.status == "succeeded":
                state.metrics.actions += 1
                state.history.append(f"launched {target} because no window of it existed")
                result = AppActivationSkill(native, profiles).activate(target)
            elif launched.status == "hard_failure":
                return self._failure(
                    launched.message,
                    stage="app_launch_skill",
                    error_code="ASSISTIVE_HARD_BLOCK",
                    metrics=state.metrics,
                )
        if result.status == "hard_failure":
            return self._failure(
                result.message,
                stage="app_activation_skill",
                error_code="ASSISTIVE_HARD_BLOCK",
                metrics=state.metrics,
            )
        if result.status == "retryable_failure":
            state.history.append(
                "exact app activation failed transiently; generic planner may inspect a new state"
            )
            return None

        state.metrics.actions += int(bool(result.details.get("activated")))
        hwnd = result.details.get("hwnd")
        bind = getattr(self.driver, "bind_app_window", None)
        if callable(bind) and isinstance(hwnd, int) and not isinstance(hwnd, bool):
            try:
                bind(target, hwnd)
            except Exception as exc:
                if _is_hard_desktop_boundary(exc):
                    return self._failure(
                        "安全桌面或权限边界阻止了应用窗口绑定",
                        stage="app_activation_skill",
                        error_code="ASSISTIVE_HARD_BLOCK",
                        metrics=state.metrics,
                        exception=exc,
                        observation=state.observation,
                    )
                self._debug_exception(exc, stage="app_activation_skill")
        try:
            state.inventory = self.driver.list_apps(cancel_event=cancel_event)
            selected_app = self._foreground_app(state.inventory, target)
            if selected_app is not None:
                state.observation = self._observe(
                    selected_app,
                    cancel_event=cancel_event,
                )
            self._verify(
                state,
                cancel_event=cancel_event,
                refresh_inventory=False,
            )
        except Exception as exc:
            if _is_hard_desktop_boundary(exc):
                return self._failure(
                    "安全桌面、权限边界或凭据字段阻止了应用激活验证",
                    stage="app_activation_skill",
                    error_code="ASSISTIVE_HARD_BLOCK",
                    metrics=state.metrics,
                    exception=exc,
                    observation=state.observation,
                )
            state.history.append(
                f"activated app but refresh failed transiently: {type(exc).__name__}"
            )
            self._debug_exception(exc, stage="app_activation_skill")
            return None
        if state.verification.completed:
            return self._success(state.verification.summary, state)
        state.history.append("exact application window activated; remaining goals continue")
        return None

    def _try_browser_navigation_skill(
        self,
        state: _RunState,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult | None:
        pending = [
            check.goal
            for check in state.verification.checks
            if not check.satisfied and check.goal.kind == GoalKind.URL_LOADED
        ]
        if len(pending) != 1 or self.driver is None or self._cancelled(cancel_event):
            return None
        goal = pending[0]
        canonical_app = goal.app or self._foreground_app(state.inventory)
        if _normalized(canonical_app) not in _app_aliases("chrome"):
            return None
        app = self._foreground_app(state.inventory, canonical_app) or canonical_app
        native = self._native_backend()
        if native is None:
            return None
        settings = getattr(self.native_router, "settings", None)
        profiles = getattr(settings, "apps", {})
        result = BrowserNavigationSkill(
            native,
            self.driver,
            self.policy,
            profiles,
        ).navigate(
            state.task,
            app=app or "chrome",
            raw_url=goal.target,
            observation=state.observation,
            cancel_event=cancel_event,
        )
        if result.observation is not None:
            state.observation = result.observation
        if result.status == "hard_failure":
            return self._failure(
                result.message,
                stage="browser_navigation_skill",
                error_code="ASSISTIVE_HARD_BLOCK",
                metrics=state.metrics,
                observation=state.observation,
            )
        if result.status == "policy_rejected":
            return self._failure(
                result.message,
                stage="browser_navigation_skill",
                error_code="ASSISTIVE_POLICY_REJECTED",
                metrics=state.metrics,
                observation=state.observation,
            )
        if result.status == "retryable_failure":
            state.metrics.native_fallbacks += 1
            state.history.append(
                "deterministic browser navigation failed transiently; generic planner takes over"
            )
            self._debug_note(
                "browser_navigation_skill",
                f"retryable: {result.message} | "
                f"{result.details.get('error_type', '-')}: {result.details.get('error', '-')}",
            )
            return None

        state.metrics.actions += int(result.details.get("actions", 1))

        def url_verified(verification: VerificationResult) -> bool:
            return all(
                check.satisfied
                for check in verification.checks
                if check.goal.kind == GoalKind.URL_LOADED
            )

        try:
            completed = self._wait_for_goal_completion(
                state,
                app=app or "chrome",
                cancel_event=cancel_event,
                # A heavy web app can take well over six seconds to load.
                timeout_seconds=12.0,
                max_probes=16,
                until=url_verified,
            )
        except Exception as exc:
            if _is_hard_desktop_boundary(exc):
                return self._failure(
                    "安全桌面、权限边界或凭据字段阻止了浏览器导航验证",
                    stage="browser_navigation_skill",
                    error_code="ASSISTIVE_HARD_BLOCK",
                    metrics=state.metrics,
                    exception=exc,
                    observation=state.observation,
                )
            state.metrics.native_fallbacks += 1
            self._debug_exception(exc, stage="browser_navigation_skill")
            return None
        if state.verification.completed:
            return self._success(state.verification.summary, state)
        if completed or url_verified(state.verification):
            state.history.append("browser navigation verified; remaining goals continue")
            return None
        state.metrics.native_fallbacks += 1
        state.history.append(
            "browser address navigation produced no verified final URL; generic planner takes over"
        )
        return None

    def _initialize_state(
        self,
        task: TaskSpec,
        *,
        cancel_event: threading.Event | None,
    ) -> _RunState | ComputerControlResult:
        if self.driver is None:
            return self._failure(
                "没有配置可用的本地桌面驱动",
                stage="runtime",
                error_code="DRIVER_NOT_CONFIGURED",
            )
        try:
            self.driver.start()
            set_context = getattr(self.driver, "set_task_context", None)
            if callable(set_context):
                set_context(task.raw_text)
            inventory = self.driver.list_apps(cancel_event=cancel_event)
            task = self._resolve_task_goals(task, inventory)
            problem = self._file_goal_problem(task)
            if problem is not None:
                message, error_code = problem
                return self._failure(message, stage="task_parser", error_code=error_code)
            observation = self._initial_observation(
                task,
                inventory,
                cancel_event=cancel_event,
            )
            verification = self.verifier.evaluate(
                task,
                self.driver,
                inventory=inventory,
                observation=observation,
                cancel_event=cancel_event,
            )
        except Exception as exc:
            if _is_hard_desktop_boundary(exc):
                return self._failure(
                    "安全桌面、权限边界或凭据字段阻止了桌面清单读取",
                    stage="list_apps",
                    error_code="ASSISTIVE_HARD_BLOCK",
                    exception=exc,
                )
            return self._failure(
                "本地桌面清单或任务目标读取失败",
                stage="list_apps",
                error_code="ASSISTIVE_INVENTORY_FAILED",
                exception=exc,
            )
        return _RunState(
            task=task,
            inventory=inventory,
            observation=observation,
            verification=verification,
            history=[],
            deadline=self._monotonic() + self.timeout_seconds,
        )

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
        if len(instruction) > 8000:
            return self._failure(
                "电脑控制指令过长",
                stage="runtime",
                error_code="INSTRUCTION_TOO_LONG",
            )
        with self._lifecycle_lock:
            if self._closed:
                return self._failure(
                    "桌面控制器已经关闭",
                    stage="runtime",
                    error_code="CONTROLLER_CLOSED",
                )
            if self._pending is not None:
                return self._failure(
                    "上一动作仍在等待口头确认",
                    stage="runtime",
                    error_code="CONFIRMATION_PENDING",
                )
        if not self._execution_lock.acquire(blocking=False):
            return self._failure(
                "桌面控制器正在执行另一条指令",
                stage="runtime",
                error_code="CONTROLLER_BUSY",
            )
        try:
            with self._lifecycle_lock:
                self._current_cancel = threading.Event()
            if self._cancelled(cancel_event):
                return self._cancelled_result()
            try:
                task = self.task_parser.parse(instruction)
            except Exception as exc:
                return self._failure(
                    "整句任务解析失败",
                    stage="task_parser",
                    error_code="ASSISTIVE_TASK_PARSE_FAILED",
                    exception=exc,
                )
            if self._missing_path_goal(task) is not None:
                # No planner can open a path that is absent from the disk.
                return self._failure(
                    "指定的本地路径不存在",
                    stage="task_parser",
                    error_code="ASSISTIVE_PATH_NOT_FOUND",
                )
            initialized = self._initialize_state(task, cancel_event=cancel_event)
            if isinstance(initialized, ComputerControlResult):
                return initialized
            state = initialized
            # A task that is already true is a valid zero-action success.
            if state.verification.completed:
                return self._success(state.verification.summary, state)
            if explorer := self._try_explorer_skill(state, cancel_event=cancel_event):
                return explorer
            if opened := self._try_open_path_skill(state, cancel_event=cancel_event):
                return opened
            if native := self._try_native(state, cancel_event=cancel_event):
                return native
            # Deterministic skills each own one goal kind. A compound task
            # ("go to Chrome, open chatgpt.com, draft a question") is finished
            # by re-running the chain while it keeps satisfying goals.
            skills = (
                self._try_app_activation_skill,
                self._try_conversation_skill,
                self._try_send_file_skill,
                # Navigation before drafting: a draft only exists once the page
                # it belongs to is loaded.
                self._try_browser_navigation_skill,
                self._try_composer_draft_skill,
            )
            for _round in range(4):
                satisfied_before = state.verification.satisfied_count
                for skill in skills:
                    outcome = skill(state, cancel_event=cancel_event)
                    if outcome is not None:
                        return outcome
                if state.verification.completed:
                    return self._success(state.verification.summary, state)
                if state.verification.satisfied_count <= satisfied_before:
                    break
            if self.planner is None:
                return self._failure(
                    "确定性技能未完成，且没有配置 assistive 单步规划器",
                    stage="plan",
                    error_code="PLANNER_NOT_CONFIGURED",
                    metrics=state.metrics,
                )
            return self._drive(state, cancel_event=cancel_event)
        finally:
            with self._lifecycle_lock:
                self._current_cancel = None
            self._execution_lock.release()

    execute = run

    def confirm(
        self,
        confirmation_id: str,
        *,
        phrase: str | None = None,
        cancel_event: threading.Event | None = None,
    ) -> ComputerControlResult:
        if not self._execution_lock.acquire(blocking=False):
            return self._failure(
                "桌面控制器正在执行另一条指令",
                stage="runtime",
                error_code="CONTROLLER_BUSY",
            )
        try:
            with self._lifecycle_lock:
                if self._closed:
                    return self._failure(
                        "桌面控制器已经关闭",
                        stage="runtime",
                        error_code="CONTROLLER_CLOSED",
                    )
                pending = self._pending
                self._current_cancel = threading.Event()
                if pending is None or pending.confirmation_id != confirmation_id:
                    return self._failure(
                        "没有匹配的待确认动作",
                        stage="policy",
                        error_code="CONFIRMATION_NOT_FOUND",
                    )
                if not isinstance(phrase, str):
                    phrase = ""
                # Consume before execution. A replay can never reuse this capability.
                self._pending = None
            if self._cancelled(cancel_event):
                return self._cancelled_result(pending.state.metrics)
            assert pending.binding is not None
            assert pending.action is not None
            assert pending.observation is not None
            result = self._perform_action(
                pending.state,
                pending.action,
                pending.observation,
                cancel_event=cancel_event,
                confirmed_binding=pending.binding,
                confirmation_phrase=phrase,
            )
            if result is not None:
                return result
            if self.planner is None:
                return self._failure(
                    "已确认动作后目标仍未完成且没有规划器",
                    stage="plan",
                    error_code="PLANNER_NOT_CONFIGURED",
                    metrics=pending.state.metrics,
                )
            return self._drive(pending.state, cancel_event=cancel_event)
        finally:
            with self._lifecycle_lock:
                self._current_cancel = None
            self._execution_lock.release()

    def arm_confirmation(self, confirmation_id: str) -> bool:
        """Restart expiry only after the exact prompt reached screen or speech."""

        with self._lifecycle_lock:
            pending = self._pending
            if (
                pending is None
                or pending.confirmation_id != confirmation_id
                or pending.binding is None
            ):
                return False
            refreshed = self.policy.refresh_binding(pending.binding)
            self._pending = replace(
                pending,
                phrase=refreshed.phrase,
                binding=refreshed,
            )
            return True

    def cancel(self) -> bool:
        with self._lifecycle_lock:
            pending = self._pending is not None
            self._pending = None
            current = self._current_cancel
            if current is not None:
                current.set()
        driver_cancelled = False
        cancel = getattr(self.driver, "cancel", None)
        if callable(cancel):
            with suppress(Exception):
                driver_cancelled = bool(cancel())
        return bool(pending or current is not None or driver_cancelled)

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._pending = None
            current = self._current_cancel
            if current is not None:
                current.set()
        close = getattr(self.driver, "close", None)
        if callable(close):
            close()
        debug_close = getattr(self._debug_log, "close", None)
        if callable(debug_close):
            debug_close()


__all__ = ["AssistiveController", "AssistiveMetrics"]
