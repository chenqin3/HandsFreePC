from __future__ import annotations

from ..computer_control import CodexComputerController, Controller
from ..config import Settings
from ..windows.executor import WindowsExecutor
from .agent_loop import DesktopAgentLoopController
from .native_skills import NativeSkillRouter
from .open_computer_use import PersistentOpenComputerUseDriver
from .safety import DesktopSafetyPolicy
from .step_planner import ClaudeDesktopStepPlanner, CodexDesktopStepPlanner, DesktopStepPlanner
from .windows_uia import WindowsUiaDriver


def build_computer_controller(settings: Settings, executor: WindowsExecutor) -> Controller:
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

    driver = None
    if control.driver == "windows_uia":
        driver = WindowsUiaDriver(settings.apps)
    elif control.driver == "open_computer_use":
        driver = PersistentOpenComputerUseDriver(
            executable=control.open_computer_use_executable,
            args=control.open_computer_use_args,
            allow_coordinate_actions=control.allow_coordinate_actions,
            allowed_apps=tuple(settings.apps),
        )

    planner: DesktopStepPlanner | None = None
    planner_kwargs = {
        "model": control.model,
        "timeout_seconds": min(control.timeout_seconds, settings.planner.timeout_seconds),
        "max_observation_chars": control.max_observation_chars,
        "safety_profile": control.safety_profile,
    }
    if control.planner_backend == "codex_cli_best_effort":
        planner = CodexDesktopStepPlanner(
            executable=control.codex_executable,
            **planner_kwargs,
        )
    elif control.planner_backend == "claude":
        planner = ClaudeDesktopStepPlanner(
            executable=control.claude_executable,
            **planner_kwargs,
        )

    return DesktopAgentLoopController(
        native_router=NativeSkillRouter(settings, executor=executor),
        driver=driver,
        planner=planner,
        safety=DesktopSafetyPolicy(profile=control.safety_profile),
        timeout_seconds=control.timeout_seconds,
        confirmation_timeout_seconds=settings.execution.confirmation_timeout_seconds,
        max_steps=control.max_steps,
    )
