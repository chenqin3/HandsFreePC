from __future__ import annotations

import os
import platform
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .computer_control import ComputerControlResult, Controller
from .models import FeedbackMode
from .session import JobOutcome, SessionState


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    """One live, user-visible acceptance scenario.

    The catalogue contains commands and observable acceptance limits only.  It
    deliberately does not contain desktop setup that closes windows or edits
    user data.  A scenario whose required state is absent is reported as
    ``skipped`` and therefore cannot make the overall run pass.
    """

    scenario: str
    instruction: str
    description: str
    max_elapsed_seconds: float | None = None
    min_elapsed_seconds: float | None = None
    deterministic: bool = False
    precondition: str | None = None
    forbidden_result_text: tuple[str, ...] = ()
    expect_zero_actions: bool = False
    execution_mode: str = "single"


@dataclass(frozen=True, slots=True)
class _AttemptSetup:
    """Prove that one requested repeat has a real, safe starting state."""

    status: str = "ready"
    message: str = ""
    instruction: str | None = None
    pre_state_unsatisfied: bool = False
    restore_foreground_hwnd: int | None = None
    strategy: str = "declared_precondition"


SCENARIOS: tuple[ScenarioDefinition, ...] = (
    ScenarioDefinition(
        scenario="claude_foreground_zero_action",
        instruction="切换到 Claude",
        description="Claude 已在前台时，由任务级 GoalVerifier 零动作完成。",
        max_elapsed_seconds=1.0,
        deterministic=True,
        precondition="claude_foreground",
        expect_zero_actions=True,
    ),
    ScenarioDefinition(
        scenario="explorer_from_no_window",
        instruction="打开资源管理器",
        description="没有 Explorer 文件窗口时启动一个 Explorer 窗口。",
        max_elapsed_seconds=3.0,
        deterministic=True,
        precondition="no_explorer_window",
    ),
    ScenarioDefinition(
        scenario="open_d_drive",
        instruction="打开D盘",
        description="打开 D:\\ 并由 Explorer 路径状态完成验收。",
        max_elapsed_seconds=4.0,
        deterministic=True,
        precondition="d_drive_exists",
    ),
    ScenarioDefinition(
        scenario="chrome_google_compound",
        instruction="切换到 Chrome 打开 Google 网页",
        description="无标点的两目标指令完成 Chrome 激活和 Google 页面加载。",
        max_elapsed_seconds=6.0,
        deterministic=True,
        forbidden_result_text=("ACTION_AFTER_USER_STEPS_COMPLETE",),
    ),
    ScenarioDefinition(
        scenario="wechat_delayed_transition",
        instruction="切换到微信 打开文件传输助手",
        description="在人工准备的 1.5–3 秒微信延迟目标上验证有界轮询。",
        min_elapsed_seconds=1.5,
        max_elapsed_seconds=8.0,
        precondition="wechat_delay_fixture",
        forbidden_result_text=("ACTION_NOT_VERIFIED",),
    ),
    ScenarioDefinition(
        scenario="native_retryable_fallback",
        instruction="切换到 Chrome 打开 Google 网页",
        description="受控注入一次 native retryable_failure，随后由通用 assistive agent 完成。",
        max_elapsed_seconds=12.0,
        precondition="native_fallback_injection",
        execution_mode="native_fallback",
    ),
    ScenarioDefinition(
        scenario="wechat_open_conversation",
        instruction="切换到微信 打开文件传输助手",
        description="通过微信搜索框 + 本地 OCR 行框定位并打开指定会话（无 UIA 控件的窗口）。",
        max_elapsed_seconds=20.0,
        deterministic=True,
        precondition="wechat_window_and_ocr",
        forbidden_result_text=("ASSISTIVE_PLANNER_FAILED", "ASSISTIVE_NO_PLAN"),
    ),
    ScenarioDefinition(
        scenario="queue_continues_after_failure",
        instruction="切换到 Claude",
        description=(
            "VoiceRuntime 文本注入经真实 CommandWorker 调用同一 controller："
            "普通任务失败后下一条仍执行；不使用 ASR 音频。"
        ),
        precondition="continue_policy_and_claude",
        execution_mode="failure_then_next",
    ),
    ScenarioDefinition(
        scenario="open_workmap_folder",
        instruction="打开下载文件夹",
        description=(
            "口语文件夹描述解析为本地路径并在 Explorer 打开。默认打开 Downloads；用 "
            "HANDSFREEPC_SCENARIO_FOLDER_COMMAND / HANDSFREEPC_SCENARIO_FOLDER_PATH "
            "换成项目地图里的缩写和对应目录。"
        ),
        max_elapsed_seconds=4.0,
        deterministic=True,
        precondition="scenario_folder_exists",
    ),
    ScenarioDefinition(
        scenario="chrome_chatgpt_draft",
        instruction="去chrome打开chatgpt网页然后开一个新对话，问一下测试问题（但是不要发送）",
        description="复合指令：激活 Chrome、加载 chatgpt.com、把草稿打进输入框且不发送。",
        max_elapsed_seconds=12.0,
        precondition="chrome_window",
    ),
    ScenarioDefinition(
        scenario="wechat_send_file_self",
        instruction="把下载文件夹里的<文件描述>发送到微信的文件传输助手",
        description=(
            "把 Downloads 里模糊描述的文件发到自己的文件传输助手；有副作用，只有在 "
            "HANDSFREEPC_SCENARIO_WECHAT_SEND_COMMAND 给出指向本机真实文件的指令时才运行。"
        ),
        max_elapsed_seconds=40.0,
        precondition="wechat_send_opt_in",
    ),
)
_FOLDER_COMMAND = "HANDSFREEPC_SCENARIO_FOLDER_COMMAND"
_FOLDER_PATH = "HANDSFREEPC_SCENARIO_FOLDER_PATH"
_WECHAT_SEND_COMMAND = "HANDSFREEPC_SCENARIO_WECHAT_SEND_COMMAND"

_SCENARIO_BY_NAME = {item.scenario: item for item in SCENARIOS}
_EXPLORER_CLASSES = {"cabinetwclass", "explorewclass"}
_WECHAT_DELAY_READY = "HANDSFREEPC_SCENARIO_WECHAT_DELAY_READY"
_WECHAT_DELAY_COMMAND = "HANDSFREEPC_SCENARIO_WECHAT_DELAY_COMMAND"
_VOICE_RUNTIME_BOUNDARY = (
    "VoiceRuntime.handle_session_text→CommandWorker→controller; no ASR audio"
)


def scenario_catalog() -> list[dict[str, Any]]:
    """Return the public catalogue without inspecting or changing the desktop."""

    return [
        {
            "scenario": item.scenario,
            "instruction": item.instruction,
            "description": item.description,
            "max_elapsed_seconds": item.max_elapsed_seconds,
            "min_elapsed_seconds": item.min_elapsed_seconds,
            "deterministic": item.deterministic,
            "precondition": item.precondition,
        }
        for item in SCENARIOS
    ]


def inject_runtime_text(
    runtime: object,
    text: str,
    *,
    require_wake: bool = False,
    continuous_control: bool = False,
) -> object:
    """Shared no-audio text injection facade used by simulate and scenarios.

    Ordinary simulation retains ``VoiceRuntime.handle_text`` semantics. Live
    assistive scenarios select the continuous-control branch, which feeds the
    same text fragments and configured delimiter consumed after local ASR.
    """

    if not continuous_control:
        return runtime.handle_text(text, require_wake=require_wake)

    if getattr(runtime, "session_state", None) != SessionState.ACTIVE:
        started = runtime.start_continuous_session()
        if not bool(getattr(started, "success", False)):
            return started
    fragment = runtime.handle_session_text(text)
    if not bool(getattr(fragment, "success", False)):
        return fragment
    delimiters = tuple(getattr(runtime.settings.app, "prompt_delimiters", ()) or ())
    delimiter = delimiters[0] if delimiters else "over"
    return runtime.handle_session_text(delimiter)


def _selected_definitions(names: Sequence[str] | None) -> list[ScenarioDefinition]:
    if not names:
        return list(SCENARIOS)
    selected: list[ScenarioDefinition] = []
    seen: set[str] = set()
    for name in names:
        if name not in _SCENARIO_BY_NAME:
            raise ValueError(f"Unknown scenario: {name}")
        if name in seen:
            continue
        seen.add(name)
        selected.append(_SCENARIO_BY_NAME[name])
    return selected


def _result_record(
    definition: ScenarioDefinition,
    *,
    result: str,
    success: bool,
    elapsed_seconds: float,
    retry_count: int,
    failure_stage: str | None,
    message: str,
    attempt: int,
    executed: bool = False,
) -> dict[str, Any]:
    return {
        "scenario": definition.scenario,
        "result": result,
        "success": bool(success),
        "elapsed_seconds": round(max(0.0, float(elapsed_seconds)), 3),
        "retry_count": max(0, int(retry_count)),
        "failure_stage": failure_stage,
        "message": message[:500],
        "attempt": attempt,
        "executed": bool(executed),
    }


def _window_matches_profile(window: object, profile: object) -> bool:
    process_name = str(getattr(window, "process_name", "") or "").casefold()
    title = str(getattr(window, "title", "") or "").casefold()
    processes = {
        str(item).casefold()
        for item in (getattr(profile, "process_names", ()) or ())
        if str(item).strip()
    }
    patterns = tuple(
        str(item).casefold()
        for item in (getattr(profile, "title_patterns", ()) or ())
        if str(item).strip()
    )
    return bool(
        (process_name and process_name in processes)
        or any(item in title for item in patterns)
    )


def _is_explorer_window(window: object) -> bool:
    return (
        str(getattr(window, "process_name", "") or "").casefold() == "explorer.exe"
        and str(getattr(window, "class_name", "") or "").casefold() in _EXPLORER_CLASSES
    )


def _controller_component(controller: object, *names: str) -> object | None:
    for name in names:
        value = getattr(controller, name, None)
        if value is not None:
            return value
    return None


def _check_precondition(
    definition: ScenarioDefinition,
    *,
    settings: object,
    controller: Controller,
    native: object | None,
    environ: Mapping[str, str],
) -> str | None:
    name = definition.precondition
    if name is None:
        return None
    if name == "claude_foreground":
        if native is None:
            return "Win32 foreground inspection is unavailable"
        profile = getattr(settings, "apps", {}).get("claude")
        foreground = native.get_foreground_window_info()
        if (
            profile is None
            or foreground is None
            or not _window_matches_profile(foreground, profile)
        ):
            return "Claude must already be the foreground window; no window was changed"
        return None
    if name == "no_explorer_window":
        if native is None:
            return "Win32 Explorer inspection is unavailable"
        if any(_is_explorer_window(item) for item in native.enumerate_windows()):
            return "an Explorer file window is already open; existing windows were not closed"
        return None
    if name == "d_drive_exists":
        if not Path("D:\\").exists():
            return "D:\\ is not available on this machine"
        return None
    if name == "wechat_delay_fixture":
        if environ.get(_WECHAT_DELAY_READY, "").strip() != "1":
            return (
                "prepare a harmless WeChat target with a measured 1.5–3 second transition and set "
                f"{_WECHAT_DELAY_READY}=1"
            )
        return None
    if name == "native_fallback_injection":
        router = _controller_component(
            controller,
            "native_router",
            "_native_router",
        )
        planner = _controller_component(controller, "planner", "_planner")
        if router is None or not callable(getattr(router, "route", None)):
            return "controller does not expose the bounded native-router test seam"
        if planner is None or not callable(getattr(planner, "decide", None)):
            return "a generic assistive planner is required for fallback verification"
        return None
    if name == "wechat_window_and_ocr":
        if native is None:
            return "Win32 WeChat inspection is unavailable"
        if not any(
            str(getattr(item, "process_name", "") or "").casefold() in {"weixin.exe", "wechat.exe"}
            for item in native.enumerate_windows()
        ):
            return "a visible WeChat window is required"
        visual = getattr(settings, "visual_ocr", None)
        if not bool(getattr(visual, "enabled", False)) or not bool(
            getattr(visual, "ocr_regions_enabled", False)
        ):
            return "visual_ocr.enabled and visual_ocr.ocr_regions_enabled must both be true"
        endpoint = str(getattr(visual, "endpoint", "") or "")
        health = endpoint.rsplit("/", 1)[0] + "/health" if "/" in endpoint else ""
        try:
            import urllib.request

            with urllib.request.urlopen(health, timeout=3) as response:  # noqa: S310
                if int(response.status) != 200:
                    return f"visual OCR health check returned {response.status}"
        except Exception as exc:
            return f"visual OCR service is not reachable: {type(exc).__name__}"
        return None
    if name == "continue_policy_and_claude":
        control = getattr(settings, "computer_control", None)
        if str(getattr(control, "failure_policy", "")).casefold() != "continue":
            return "computer_control.failure_policy must be continue"
        if native is None:
            return "Win32 Claude inspection is unavailable"
        profile = getattr(settings, "apps", {}).get("claude")
        if profile is None or not any(
            _window_matches_profile(item, profile) for item in native.enumerate_windows()
        ):
            return "a visible Claude window is required for the next queued task"
        return None
    if name == "scenario_folder_exists":
        folder = environ.get(_FOLDER_PATH, "").strip() or str(Path.home() / "Downloads")
        if not Path(os.path.expandvars(folder)).is_dir():
            return f"the scenario folder {folder} is not available on this machine"
        return None
    if name == "chrome_window":
        if native is None:
            return "Win32 Chrome inspection is unavailable"
        if not any(
            str(getattr(item, "process_name", "") or "").casefold() == "chrome.exe"
            for item in native.enumerate_windows()
        ):
            return "a visible Chrome window is required"
        return None
    if name == "wechat_send_opt_in":
        if not environ.get(_WECHAT_SEND_COMMAND, "").strip():
            return (
                "sending a file to WeChat has a side effect; set "
                f"{_WECHAT_SEND_COMMAND} to a spoken instruction naming a real local file"
            )
        if native is None:
            return "Win32 WeChat inspection is unavailable"
        if not any(
            str(getattr(item, "process_name", "") or "").casefold() in {"weixin.exe", "wechat.exe"}
            for item in native.enumerate_windows()
        ):
            return "a visible WeChat window is required"
        return None
    return f"unknown scenario precondition: {name}"


def _prepare_attempt(
    definition: ScenarioDefinition,
    *,
    native: object | None,
) -> _AttemptSetup:
    """Classify repeat readiness without mutating or closing user windows."""

    if definition.scenario == "chrome_google_compound":
        # A fresh URL makes every repeat observably different without clearing
        # history, closing tabs, or otherwise destroying browser state.
        return _AttemptSetup(
            instruction=_unique_google_instruction(),
            pre_state_unsatisfied=True,
            strategy="unique_google_url",
        )

    if definition.scenario == "explorer_from_no_window":
        if native is None:
            return _AttemptSetup(
                status="skipped",
                message="Win32 Explorer inspection is unavailable",
                strategy="no_explorer_window",
            )
        try:
            existing = any(
                _is_explorer_window(item) for item in native.enumerate_windows()
            )
        except Exception as exc:
            return _AttemptSetup(
                status="skipped",
                message=f"Explorer pre-state inspection failed: {type(exc).__name__}",
                strategy="no_explorer_window",
            )
        if existing:
            return _AttemptSetup(
                status="already_satisfied",
                message=(
                    "an Explorer file window already exists; it was not closed merely "
                    "to manufacture another repeat"
                ),
                strategy="no_explorer_window",
            )
        return _AttemptSetup(
            pre_state_unsatisfied=True,
            strategy="no_explorer_window",
        )

    if definition.scenario == "open_d_drive":
        if native is None:
            return _AttemptSetup(
                status="skipped",
                message="exact D:\\ foreground-path inspection is unavailable",
                strategy="restore_foreground",
            )
        try:
            state = native.path_open_state("D:\\")
            foreground = native.get_foreground_window_info()
        except Exception as exc:
            return _AttemptSetup(
                status="skipped",
                message=f"D:\\ pre-state inspection failed: {type(exc).__name__}",
                strategy="restore_foreground",
            )
        if not isinstance(state, Mapping):
            return _AttemptSetup(
                status="skipped",
                message="D:\\ pre-state inspection returned no exact path evidence",
                strategy="restore_foreground",
            )
        if state.get("verified") is True:
            return _AttemptSetup(
                status="already_satisfied",
                message="D:\\ is already verified in the foreground Explorer window",
                strategy="restore_foreground",
            )
        hwnd = getattr(foreground, "hwnd", None)
        return _AttemptSetup(
            pre_state_unsatisfied=True,
            restore_foreground_hwnd=(
                int(hwnd)
                if isinstance(hwnd, int) and not isinstance(hwnd, bool) and hwnd > 0
                else None
            ),
            strategy="restore_foreground",
        )

    return _AttemptSetup(instruction=definition.instruction)


def _restore_attempt_foreground(
    setup: _AttemptSetup,
    *,
    native: object | None,
) -> tuple[bool | None, str]:
    """Best-effort reversible setup for drive repeats; never closes a window."""

    if setup.strategy != "restore_foreground":
        return None, "not required"
    hwnd = setup.restore_foreground_hwnd
    if native is None or hwnd is None:
        return False, "the original foreground window was unavailable for safe restoration"
    try:
        current = native.get_foreground_window_info()
        if getattr(current, "hwnd", None) != hwnd:
            native.activate_window(hwnd)
            assert_foreground = getattr(native, "assert_foreground", None)
            if callable(assert_foreground):
                assert_foreground(hwnd)
        state = native.path_open_state("D:\\")
    except Exception as exc:
        return False, f"foreground restoration failed: {type(exc).__name__}"
    if isinstance(state, Mapping) and state.get("verified") is True:
        return (
            False,
            "the original HWND now represents D:\\; its prior folder cannot be safely restored",
        )
    return True, "original foreground HWND restored"


def _counter_from(value: object, names: Sequence[str]) -> int | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        for name in names:
            candidate = value.get(name)
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                return max(0, candidate)
        return None
    for name in names:
        candidate = getattr(value, name, None)
        if isinstance(candidate, int) and not isinstance(candidate, bool):
            return max(0, candidate)
    return None


def _controller_counter(controller: object, names: Sequence[str]) -> int | None:
    for source in (
        getattr(controller, "last_metrics", None),
        getattr(controller, "metrics", None),
        controller,
    ):
        if (value := _counter_from(source, names)) is not None:
            return value
    return None


def _retry_count(
    controller: object,
    result: object,
    before: int | None,
) -> int:
    direct = _counter_from(result, ("retry_count", "retries"))
    if direct is not None:
        return direct
    after = _controller_counter(controller, ("retry_count", "retries", "total_retries"))
    if after is None:
        return 0
    if before is None:
        return after
    return max(0, after - before)


class _ZeroActionProbe:
    """Count all observable controller action seams and restore them afterward."""

    def __init__(self, controller: object) -> None:
        self.controller = controller
        self.action_count = 0
        self.instrumented = False
        self._patches: list[tuple[object, str, object]] = []
        self._patch_keys: set[tuple[int, str]] = set()

    def _wrap(self, target: object | None, attribute: str) -> None:
        if target is None:
            return
        key = (id(target), attribute)
        if key in self._patch_keys:
            return
        original = getattr(target, attribute, None)
        if not callable(original):
            return

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.action_count += 1
            return original(*args, **kwargs)

        try:
            setattr(target, attribute, counted)
        except (AttributeError, TypeError):
            return
        self._patches.append((target, attribute, original))
        self._patch_keys.add(key)
        self.instrumented = True

    def __enter__(self) -> _ZeroActionProbe:
        driver = _controller_component(self.controller, "driver", "_driver")
        router = _controller_component(self.controller, "native_router", "_native_router")
        executor = getattr(router, "executor", None)
        self._wrap(driver, "execute")
        self._wrap(executor, "execute_plan")

        native_targets: list[object] = []
        for owner in (driver, executor):
            resolver = getattr(owner, "_native_backend", None)
            if not callable(resolver):
                continue
            try:
                candidate = resolver()
            except Exception:
                continue
            if candidate is not None:
                native_targets.append(candidate)
        for native in native_targets:
            for method in (
                "activate_window",
                "shell_execute_path",
                "open_path",
                "send_hotkey",
                "send_text",
            ):
                self._wrap(native, method)
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        for target, attribute, original in reversed(self._patches):
            setattr(target, attribute, original)


class _NativeRetryableFailure:
    """Inject exactly one transient native failure and then restore the router."""

    def __init__(self, controller: object) -> None:
        self.router = _controller_component(controller, "native_router", "_native_router")
        self.original: object | None = None
        self.used = False

    def __enter__(self) -> _NativeRetryableFailure:
        route = getattr(self.router, "route", None)
        if not callable(route):
            return self
        from .desktop.native_skills import NativeRouteStatus, NativeSkillResult

        self.original = route

        def fail_once(*args: Any, **kwargs: Any) -> Any:
            if self.used:
                return route(*args, **kwargs)
            self.used = True
            return NativeSkillResult(
                status=NativeRouteStatus.RETRYABLE_FAILURE,
                message="SCENARIO_INJECTED_NATIVE_RETRYABLE_FAILURE",
            )

        self.router.route = fail_once
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        if self.original is not None:
            self.router.route = self.original


class _PlannerDecisionProbe:
    """Prove that generic fallback reached the configured one-step planner."""

    def __init__(self, controller: object) -> None:
        self.planner = _controller_component(controller, "planner", "_planner")
        self.original: object | None = None
        self.instrumented = False
        self.call_count = 0

    def __enter__(self) -> _PlannerDecisionProbe:
        decide = getattr(self.planner, "decide", None)
        if not callable(decide):
            return self
        self.original = decide

        def counted(*args: Any, **kwargs: Any) -> Any:
            self.call_count += 1
            return decide(*args, **kwargs)

        try:
            self.planner.decide = counted
        except (AttributeError, TypeError):
            self.original = None
            return self
        self.instrumented = True
        return self

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        if self.original is not None:
            self.planner.decide = self.original


def _safe_result_message(result: object) -> str:
    safe_message = getattr(result, "safe_message", None)
    if isinstance(safe_message, str) and safe_message.strip():
        return safe_message.strip()
    message = getattr(result, "message", None)
    if isinstance(message, str) and message.strip():
        return message.strip()
    return type(result).__name__


class _ScenarioFeedback:
    """Silent local feedback adapter; scenarios never open audio output."""

    mode = FeedbackMode.SILENT

    @staticmethod
    def emit(_text: str, **_kwargs: object) -> bool:
        return True

    @staticmethod
    def close() -> None:
        return None


def _new_scenario_runtime(settings: object, controller: Controller) -> object:
    from .runtime import VoiceRuntime

    class CapturingVoiceRuntime(VoiceRuntime):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.scenario_outcomes: list[JobOutcome] = []
            self._scenario_outcome_condition = threading.Condition()
            super().__init__(*args, **kwargs)

        def _on_control_outcome(self, outcome: JobOutcome) -> None:
            with self._scenario_outcome_condition:
                self.scenario_outcomes.append(outcome)
                self._scenario_outcome_condition.notify_all()
            super()._on_control_outcome(outcome)

        def wait_for_scenario_outcomes(self, count: int, timeout: float) -> bool:
            deadline = time.monotonic() + max(0.0, timeout)
            with self._scenario_outcome_condition:
                while len(self.scenario_outcomes) < count:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._scenario_outcome_condition.wait(remaining)
                return True

    router = _controller_component(controller, "native_router", "_native_router")
    executor = getattr(router, "executor", None) or object()
    return CapturingVoiceRuntime(
        settings,
        executor,
        planner=object(),
        feedback=_ScenarioFeedback(),
        controller=controller,
    )


def _stop_scenario_runtime(runtime: object) -> None:
    # VoiceRuntime normally owns and closes its controller. The scenario suite
    # reuses one controller across requested repeats, so detach only that shared
    # object while still stopping the runtime-owned CommandWorker deterministically.
    runtime._controller = None
    runtime.stop()


def _run_runtime_instruction(
    settings: object,
    controller: Controller,
    instruction: str,
    *,
    hard_timeout_seconds: float,
    monotonic: Callable[[], float],
) -> tuple[object, float, int]:
    before_retries = _controller_counter(
        controller,
        ("retry_count", "retries", "total_retries"),
    )
    started = monotonic()
    runtime: object | None = None
    result: object
    try:
        runtime = _new_scenario_runtime(settings, controller)
        turn = inject_runtime_text(
            runtime,
            instruction,
            continuous_control=True,
        )
        worker = getattr(runtime, "command_worker", None)
        accepted = bool(getattr(turn, "success", False) and worker is not None)
        drained = bool(
            accepted and worker.drain(timeout=max(0.0, hard_timeout_seconds))
        )
        callback_delivered = bool(
            drained and runtime.wait_for_scenario_outcomes(1, timeout=1.0)
        )
        outcomes = list(getattr(runtime, "scenario_outcomes", ()))
        if accepted and drained and callback_delivered and len(outcomes) == 1:
            result = outcomes[0]
        else:
            result = ComputerControlResult(
                success=False,
                message=(
                    "VoiceRuntime text injection did not produce exactly one terminal "
                    "CommandWorker outcome"
                ),
                stage="voice_runtime",
                error_code=(
                    "RUNTIME_DRAIN_TIMEOUT"
                    if accepted and not drained
                    else "INGRESS_FAILED"
                ),
            )
    except Exception as exc:
        result = ComputerControlResult(
            success=False,
            message=f"VoiceRuntime text injection raised {type(exc).__name__}",
            stage="voice_runtime",
            error_code=type(exc).__name__,
            exception_type=type(exc).__name__,
        )
    finally:
        elapsed = max(0.0, monotonic() - started)
        if runtime is not None:
            _stop_scenario_runtime(runtime)
    return result, elapsed, _retry_count(controller, result, before_retries)


def _unique_google_instruction() -> str:
    # The Google home page canonicalizes away unknown query parameters, so a
    # token there can never be re-read from the omnibox. A search URL keeps
    # its ``q`` parameter verbatim and still makes every repeat distinct.
    token = uuid.uuid4().hex[:12]
    return f"切换到 Chrome 打开 https://www.google.com/search?q=handsfreepc-scenario-{token}"


def _missing_path_instruction(settings: object) -> str:
    config_path = Path(getattr(settings, "config_path", Path("C:\\")))
    anchor = config_path.anchor or "C:\\"
    target = Path(anchor) / f"__handsfreepc_scenario_missing_{uuid.uuid4().hex}__"
    return f"打开路径 {target}"


def _evaluate_single(
    definition: ScenarioDefinition,
    *,
    settings: object,
    controller: Controller,
    instruction: str,
    attempt: int,
    monotonic: Callable[[], float],
    pre_state_unsatisfied: bool = False,
) -> dict[str, Any]:
    hard_timeout = max(5.0, (definition.max_elapsed_seconds or 15.0) + 5.0)
    probe = _ZeroActionProbe(controller)
    with probe:
        outcome, elapsed, retries = _run_runtime_instruction(
            settings,
            controller,
            instruction,
            hard_timeout_seconds=hard_timeout,
            monotonic=monotonic,
        )
    success = bool(getattr(outcome, "success", False))
    stage = None if success else str(getattr(outcome, "stage", None) or "controller")
    message = _safe_result_message(outcome)
    metric_actions = _controller_counter(
        controller,
        ("last_action_count", "action_count", "actions_executed", "actions"),
    )
    observed_actions = max(probe.action_count, metric_actions or 0)
    executed = bool(
        definition.expect_zero_actions
        or pre_state_unsatisfied
        or observed_actions > 0
    )
    record_result = "passed" if success else "failed"

    if definition.max_elapsed_seconds is not None and elapsed >= definition.max_elapsed_seconds:
        controller_stage = str(getattr(outcome, "stage", None) or "none")
        controller_error = str(getattr(outcome, "error_code", None) or "none")
        success = False
        stage = "elapsed_threshold"
        message = (
            f"elapsed {elapsed:.3f}s did not meet the strict "
            f"<{definition.max_elapsed_seconds:.3f}s threshold; "
            f"controller_success={bool(getattr(outcome, 'success', False))}, "
            f"controller_stage={controller_stage}, controller_error={controller_error}; "
            f"controller_message={message}"
        )
    if definition.min_elapsed_seconds is not None and elapsed < definition.min_elapsed_seconds:
        success = False
        stage = "timing_fixture"
        message = (
            f"elapsed {elapsed:.3f}s did not demonstrate the prepared "
            f">={definition.min_elapsed_seconds:.3f}s delayed transition"
        )
    inspected_result = " ".join(
        str(value or "")
        for value in (
            getattr(outcome, "message", ""),
            getattr(outcome, "safe_message", ""),
            getattr(outcome, "error_code", ""),
            getattr(outcome, "stage", ""),
        )
    )
    for forbidden in definition.forbidden_result_text:
        if forbidden in inspected_result:
            success = False
            stage = "legacy_gate"
            message = f"forbidden legacy gate appeared in the result: {forbidden}"
            break
    if definition.expect_zero_actions:
        if not probe.instrumented and metric_actions is None:
            success = False
            stage = "instrumentation"
            message = "zero-action completion could not be independently observed"
        elif observed_actions != 0:
            success = False
            stage = "zero_action"
            message = f"expected zero actions but observed {observed_actions}"
    elif success and observed_actions == 0:
        success = False
        if pre_state_unsatisfied:
            stage = "execution_evidence"
            message = (
                "controller reported success from a verified-unsatisfied pre-state, "
                "but no desktop action seam was exercised"
            )
            record_result = "failed"
        else:
            stage = "already_satisfied"
            message = (
                "controller returned zero-action success; this repeat is recorded as "
                "already satisfied, not as an executed acceptance attempt"
            )
            record_result = "already_satisfied"

    if not success and record_result == "passed":
        record_result = "failed"

    record = _result_record(
        definition,
        result=record_result,
        success=success,
        elapsed_seconds=elapsed,
        retry_count=retries,
        failure_stage=stage,
        message=message,
        attempt=attempt,
        executed=executed,
    )
    record["validation_boundary"] = _VOICE_RUNTIME_BOUNDARY
    return record


def _evaluate_native_fallback(
    definition: ScenarioDefinition,
    *,
    settings: object,
    controller: Controller,
    attempt: int,
    monotonic: Callable[[], float],
) -> dict[str, Any]:
    instruction = _unique_google_instruction()
    injection = _NativeRetryableFailure(controller)
    planner_probe = _PlannerDecisionProbe(controller)
    with injection, planner_probe:
        record = _evaluate_single(
            definition,
            settings=settings,
            controller=controller,
            instruction=instruction,
            attempt=attempt,
            monotonic=monotonic,
            pre_state_unsatisfied=True,
        )
    record["planner_decide_calls"] = planner_probe.call_count
    # After the injected native failure the generic assistive path may finish
    # through its own deterministic skills before any planner call; the
    # controller reports that hand-off in its native_fallbacks metric.
    skill_fallbacks = _controller_counter(controller, ("native_fallbacks",)) or 0
    record["native_fallbacks"] = skill_fallbacks
    generic_reached = planner_probe.call_count > 0 or (
        skill_fallbacks > 0 and bool(record["success"])
    )
    if not injection.used:
        record.update(
            result="failed",
            success=False,
            failure_stage="native_injection",
            message="native retryable-failure seam was not exercised",
        )
    elif not planner_probe.instrumented and skill_fallbacks == 0:
        record.update(
            result="failed",
            success=False,
            failure_stage="instrumentation",
            message="generic planner decide seam could not be instrumented",
        )
    elif not generic_reached:
        record.update(
            result="failed",
            success=False,
            failure_stage="generic_fallback",
            message="native failure did not reach the generic assistive path",
        )
    return record


def _evaluate_queue_continuation(
    definition: ScenarioDefinition,
    *,
    settings: object,
    controller: Controller,
    attempt: int,
    monotonic: Callable[[], float],
) -> dict[str, Any]:
    runtime: object | None = None
    outcomes: list[JobOutcome] = []
    before_retries = _controller_counter(
        controller,
        ("retry_count", "retries", "total_retries"),
    )
    started = monotonic()
    accepted = False
    drained = False
    callback_delivered = False
    runtime_error: str | None = None
    try:
        runtime = _new_scenario_runtime(settings, controller)
        session_started = runtime.start_continuous_session()
        worker = getattr(runtime, "command_worker", None)
        worker_paused = bool(worker is not None and worker.pause())
        first_turn = inject_runtime_text(
            runtime,
            _missing_path_instruction(settings),
            continuous_control=True,
        )
        second_turn = inject_runtime_text(
            runtime,
            definition.instruction,
            continuous_control=True,
        )
        queued_together = bool(worker is not None and worker.unfinished_count == 2)
        worker_resumed = bool(worker is not None and worker.resume())
        accepted = bool(
            getattr(session_started, "success", False)
            and worker_paused
            and getattr(first_turn, "success", False)
            and getattr(second_turn, "success", False)
            and queued_together
            and worker_resumed
        )
        drained = bool(accepted and worker.drain(timeout=22.0))
        callback_delivered = bool(
            drained and runtime.wait_for_scenario_outcomes(2, timeout=1.0)
        )
        outcomes = list(getattr(runtime, "scenario_outcomes", ()))
    except Exception as exc:
        runtime_error = type(exc).__name__
    finally:
        elapsed = max(0.0, monotonic() - started)
        if runtime is not None:
            _stop_scenario_runtime(runtime)

    ordered = sorted(outcomes, key=lambda item: item.command.sequence)
    first = ordered[0] if len(ordered) >= 1 else None
    second = ordered[1] if len(ordered) >= 2 else None
    first_failed = first is not None and not first.success and not first.cancelled
    second_succeeded = second is not None and second.success
    fifo_order = [item.command.sequence for item in outcomes[:2]] == [1, 2]
    success = bool(
        accepted
        and drained
        and callback_delivered
        and fifo_order
        and first_failed
        and second_succeeded
    )
    if runtime_error is not None:
        stage = "voice_runtime"
        message = f"VoiceRuntime text injection raised {runtime_error}"
    elif not accepted:
        stage = "runtime_ingress"
        message = "VoiceRuntime did not accept both delimited text commands"
    elif not drained or not callback_delivered or not fifo_order:
        stage = "queue_worker"
        message = (
            "VoiceRuntime's CommandWorker did not deliver two terminal callbacks "
            "in FIFO order"
        )
    elif not first_failed:
        stage = "failure_fixture"
        message = "the deliberately missing-path task did not fail, so continuation was not tested"
    elif not second_succeeded:
        stage = str(getattr(second, "stage", None) or "next_task")
        message = (
            "first task failed, but next task did not complete: "
            f"{second.message if second is not None else 'no second outcome'}"
        )
    else:
        stage = None
        message = (
            "VoiceRuntime accepted both delimited text commands and its CommandWorker "
            "delivered both callbacks in FIFO order: the first task failed as intended "
            "and the next completed"
        )
    record = _result_record(
        definition,
        result="passed" if success else "failed",
        success=success,
        elapsed_seconds=elapsed,
        retry_count=_retry_count(controller, second or first, before_retries),
        failure_stage=stage,
        message=message,
        attempt=attempt,
        executed=bool(accepted and len(ordered) >= 1),
    )
    record["validation_boundary"] = _VOICE_RUNTIME_BOUNDARY
    return record


def _summary(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for definition in SCENARIOS:
        matching = [item for item in records if item["scenario"] == definition.scenario]
        if not matching:
            continue
        executed = sum(1 for item in matching if item.get("executed") is True)
        passed = sum(
            1
            for item in matching
            if item.get("executed") is True and item["success"]
        )
        already_satisfied = sum(
            1 for item in matching if item["result"] == "already_satisfied"
        )
        skipped = sum(1 for item in matching if item["result"] == "skipped")
        success_rate = passed / executed if executed else 0.0
        reached_requested_repeat = executed == len(matching)
        summaries.append(
            {
                "scenario": definition.scenario,
                "runs": len(matching),
                "requested_attempts": len(matching),
                "attempted": executed,
                "executed_attempts": executed,
                "already_satisfied": already_satisfied,
                "skipped": skipped,
                "passed": passed,
                "success_rate": round(success_rate, 4),
                "deterministic": definition.deterministic,
                "reached_requested_repeat": reached_requested_repeat,
                "meets_95_percent": (
                    reached_requested_repeat and success_rate >= 0.95
                    if definition.deterministic
                    else None
                ),
                "validation_boundary": _VOICE_RUNTIME_BOUNDARY,
            }
        )
    return summaries


def unavailable_report(
    *,
    scenario_names: Sequence[str] | None,
    repeat: int,
    reason: str,
    failure_stage: str = "precondition",
) -> dict[str, Any]:
    definitions = _selected_definitions(scenario_names)
    records = [
        _result_record(
            definition,
            result="skipped",
            success=False,
            elapsed_seconds=0.0,
            retry_count=0,
            failure_stage=failure_stage,
            message=reason,
            attempt=attempt,
        )
        for definition in definitions
        for attempt in range(1, repeat + 1)
    ]
    return {
        "schema_version": 2,
        "engine": None,
        "repeat": repeat,
        "all_success": False,
        "records": records,
        "summary": _summary(records),
    }


def run_scenarios(
    settings: object,
    controller: Controller,
    *,
    scenario_names: Sequence[str] | None = None,
    repeat: int = 1,
    native: object | None = None,
    environ: Mapping[str, str] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Execute live scenarios through VoiceRuntime's no-audio text/queue ingress."""

    if repeat < 1 or repeat > 20:
        raise ValueError("repeat must be between 1 and 20")
    definitions = _selected_definitions(scenario_names)
    environment = os.environ if environ is None else environ
    records: list[dict[str, Any]] = []

    for definition in definitions:
        for attempt in range(1, repeat + 1):
            try:
                setup = _prepare_attempt(definition, native=native)
            except Exception as exc:
                setup = _AttemptSetup(
                    status="skipped",
                    message=f"repeat pre-state preparation failed: {type(exc).__name__}",
                    strategy="pre_state_error",
                )
            if setup.status != "ready":
                result = (
                    "already_satisfied"
                    if setup.status == "already_satisfied"
                    else "skipped"
                )
                record = _result_record(
                    definition,
                    result=result,
                    success=False,
                    elapsed_seconds=0.0,
                    retry_count=0,
                    failure_stage=(
                        "already_satisfied"
                        if result == "already_satisfied"
                        else "precondition"
                    ),
                    message=setup.message,
                    attempt=attempt,
                    executed=False,
                )
                record["pre_state_strategy"] = setup.strategy
                records.append(record)
                continue
            try:
                missing = _check_precondition(
                    definition,
                    settings=settings,
                    controller=controller,
                    native=native,
                    environ=environment,
                )
            except Exception as exc:
                missing = f"precondition inspection failed: {type(exc).__name__}"
            if missing is not None:
                records.append(
                    _result_record(
                        definition,
                        result="skipped",
                        success=False,
                        elapsed_seconds=0.0,
                        retry_count=0,
                        failure_stage="precondition",
                        message=missing,
                        attempt=attempt,
                        executed=False,
                    )
                )
                continue

            instruction = setup.instruction or definition.instruction
            if definition.scenario == "wechat_delayed_transition":
                instruction = environment.get(_WECHAT_DELAY_COMMAND, instruction).strip()
            elif definition.scenario == "open_workmap_folder":
                instruction = environment.get(_FOLDER_COMMAND, "").strip() or instruction
            elif definition.scenario == "wechat_send_file_self":
                instruction = environment.get(_WECHAT_SEND_COMMAND, "").strip() or instruction
            if definition.execution_mode == "native_fallback":
                record = _evaluate_native_fallback(
                    definition,
                    settings=settings,
                    controller=controller,
                    attempt=attempt,
                    monotonic=monotonic,
                )
            elif definition.execution_mode == "failure_then_next":
                record = _evaluate_queue_continuation(
                    definition,
                    settings=settings,
                    controller=controller,
                    attempt=attempt,
                    monotonic=monotonic,
                )
            else:
                record = _evaluate_single(
                    definition,
                    settings=settings,
                    controller=controller,
                    instruction=instruction,
                    attempt=attempt,
                    monotonic=monotonic,
                    pre_state_unsatisfied=setup.pre_state_unsatisfied,
                )
            record["pre_state_strategy"] = setup.strategy
            restored, restore_message = _restore_attempt_foreground(
                setup,
                native=native,
            )
            if restored is not None:
                record["state_restored"] = restored
                if not restored:
                    record["message"] = (
                        f"{record['message']}; repeat pre-state not safely restored: "
                        f"{restore_message}"
                    )[:500]
            records.append(record)

    all_success = bool(records) and all(
        bool(item["success"]) and item.get("executed") is True
        for item in records
    )
    return {
        "schema_version": 2,
        "engine": str(getattr(getattr(settings, "computer_control", None), "engine", "")),
        "repeat": repeat,
        "all_success": all_success,
        "records": records,
        "summary": _summary(records),
    }


def live_configuration_blocker(settings: object) -> str | None:
    if platform.system() != "Windows":
        return "live scenarios require Windows"
    control = getattr(settings, "computer_control", None)
    if not bool(getattr(control, "enabled", False)):
        return "computer_control.enabled must be true"
    if str(getattr(control, "engine", "")).casefold() not in {"assistive_v1", "kimi_agent"}:
        return "computer_control.engine must be assistive_v1 or kimi_agent"
    if str(getattr(control, "backend", "")).casefold() != "local_agent":
        return "computer_control.backend must be local_agent"
    if str(getattr(control, "driver", "")).casefold() != "windows_uia":
        return "computer_control.driver must be windows_uia for these Windows scenarios"
    if bool(getattr(getattr(settings, "execution", None), "dry_run", True)):
        return "execution.dry_run must be false; scenarios never override it"
    return None


def write_report(report: Mapping[str, Any], path: str | Path | None) -> None:
    if path is None:
        return
    import json

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
