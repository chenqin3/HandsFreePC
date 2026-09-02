from __future__ import annotations

from ..computer_control import CodexComputerController, Controller
from ..config import Settings
from ..windows.executor import WindowsExecutor
from .agent_loop import DesktopAgentLoopController
from .native_skills import NativeSkillRouter
from .open_computer_use import PersistentOpenComputerUseDriver
from .safety import DesktopSafetyPolicy
from .step_planner import ClaudeDesktopStepPlanner, CodexDesktopStepPlanner, DesktopStepPlanner
from .visual_ocr import VisualOcrClient
from .windows_uia import WindowsUiaDriver
from .workmap_selector import ClaudeWorkMapSelector, CodexWorkMapSelector


def build_computer_controller(
    settings: Settings,
    executor: WindowsExecutor,
    *,
    diagnostics: object | None = None,
) -> Controller:
    """Build the explicit controller backend; never silently fall back to legacy Codex."""

    control = settings.computer_control
    if control.backend == "legacy_codex_cli":
        control.working_directory.mkdir(parents=True, exist_ok=True)
        return CodexComputerController(
            executable=control.codex_executable,
            model=control.model,
            timeout_seconds=control.timeout_seconds,
            working_directory=control.working_directory,
        )

    assistive = control.engine == "assistive_v1"
    driver = None
    if control.driver == "windows_uia":
        unrestricted = control.safety_profile == "local_unrestricted"
        visual_ocr_client = None
        if settings.visual_ocr.enabled and settings.visual_ocr.ocr_regions_enabled:
            visual_ocr_client = VisualOcrClient(
                settings.visual_ocr.endpoint,
                allow_remote_screen_ocr=settings.visual_ocr.allow_remote_screen_ocr,
                timeout_seconds=settings.visual_ocr.timeout_seconds,
                max_image_bytes=settings.visual_ocr.max_image_bytes,
                max_response_bytes=settings.visual_ocr.max_response_bytes,
                max_items=settings.visual_ocr.max_items,
                max_text_chars=settings.visual_ocr.max_text_chars,
            )
        driver = WindowsUiaDriver(
            settings.apps,
            discover_all_windows=unrestricted,
            activate_on_observe=unrestricted and not assistive,
            capture_screenshots=unrestricted and not assistive,
            strict_visual_postcondition=not assistive,
            activate_before_execute=not assistive,
            same_window_browser_navigation_fast_path=assistive,
            # Assistive observation is passive. Its controller explicitly
            # escalates to a screenshot only when UIA is insufficient.
            visual_screenshot_enabled=settings.visual_ocr.enabled,
            automatic_visual_screenshots=not assistive,
            visual_ocr_client=visual_ocr_client,
            visual_ocr_apps=tuple(settings.visual_ocr.apps),
            visual_ocr_bbox_tolerance_pixels=(
                settings.visual_ocr.bbox_tolerance_pixels
            ),
        )
    elif control.driver == "open_computer_use":
        driver = PersistentOpenComputerUseDriver(
            executable=control.open_computer_use_executable,
            args=control.open_computer_use_args,
            allow_coordinate_actions=control.allow_coordinate_actions,
            allowed_apps=tuple(settings.apps),
        )

    assistive_planner: object | None = None
    proof_planner: DesktopStepPlanner | None = None
    planner_timeout = (
        control.planner_step_timeout_seconds
        if assistive
        else min(control.timeout_seconds, settings.planner.timeout_seconds)
    )
    if assistive:
        # Keep the proof planner's expectation-bearing DesktopDecision out of
        # the assistive engine. The delayed import also leaves proof_v1 usable
        # in installations that do not opt into the new package.
        from .assistive.planner import build_assistive_planner

        assistive_planner = build_assistive_planner(settings)
    else:
        planner_kwargs = {
            "model": control.model,
            "timeout_seconds": planner_timeout,
            "max_observation_chars": control.max_observation_chars,
            "safety_profile": control.safety_profile,
        }
        if control.planner_backend == "codex_cli_best_effort":
            proof_planner = CodexDesktopStepPlanner(
                executable=control.codex_executable,
                **planner_kwargs,
            )
        elif control.planner_backend == "claude":
            proof_planner = ClaudeDesktopStepPlanner(
                executable=control.claude_executable,
                **planner_kwargs,
            )

    workmap_selector = None
    permits_bounded_path_free_cloud_selection = (
        control.enabled
        and settings.privacy.allow_cloud_planner
        and control.allow_screen_context_to_cloud
    )
    if (
        permits_bounded_path_free_cloud_selection
        and control.planner_backend == "codex_cli_best_effort"
        and control.allow_codex_cli_host_read
    ):
        workmap_selector = CodexWorkMapSelector(
            executable=control.codex_executable,
            model=control.model,
            timeout_seconds=planner_timeout,
        )
    elif permits_bounded_path_free_cloud_selection and control.planner_backend == "claude":
        workmap_selector = ClaudeWorkMapSelector(
            executable=control.claude_executable,
            model=control.model,
            timeout_seconds=planner_timeout,
        )

    native_router = NativeSkillRouter(
        settings,
        executor=executor,
        workmap_selector=workmap_selector,
    )
    if assistive:
        from .assistive.controller import AssistiveController

        return AssistiveController(
            native_router=native_router,
            driver=driver,
            planner=assistive_planner,
            timeout_seconds=control.timeout_seconds,
            max_steps=control.max_steps,
            diagnostics=diagnostics,
            send_policy=dict(control.send_policy),
            confirmation_timeout_seconds=settings.execution.confirmation_timeout_seconds,
        )

    return DesktopAgentLoopController(
        native_router=native_router,
        driver=driver,
        planner=proof_planner,
        safety=DesktopSafetyPolicy(profile=control.safety_profile),
        timeout_seconds=control.timeout_seconds,
        confirmation_timeout_seconds=settings.execution.confirmation_timeout_seconds,
        max_steps=control.max_steps,
        control_prefixes=settings.app.control_prefixes,
        diagnostics=diagnostics,
    )
