from __future__ import annotations

from handsfree_pc.computer_control import CodexComputerController
from handsfree_pc.config import load_settings
from handsfree_pc.desktop.agent_loop import DesktopAgentLoopController
from handsfree_pc.desktop.factory import build_computer_controller
from handsfree_pc.desktop.step_planner import ClaudeDesktopStepPlanner
from handsfree_pc.desktop.windows_uia import WindowsUiaDriver


class FakeExecutor:
    pass


def _settings(tmp_path):
    value = load_settings(tmp_path / "missing.yaml", allow_missing=True)
    value.execution.dry_run = False
    return value


def test_factory_defaults_to_owned_local_agent_stack(tmp_path):
    settings = _settings(tmp_path)

    controller = build_computer_controller(settings, FakeExecutor())

    assert isinstance(controller, DesktopAgentLoopController)
    assert isinstance(controller.driver, WindowsUiaDriver)
    assert isinstance(controller.planner, ClaudeDesktopStepPlanner)
    assert controller.driver._discover_all_windows is False
    assert controller.driver._activate_on_observe is False
    assert controller.driver._capture_screenshots is False


def test_factory_can_choose_claude_as_step_planner(tmp_path):
    settings = _settings(tmp_path)
    settings.computer_control.planner_backend = "claude"

    controller = build_computer_controller(settings, FakeExecutor())

    assert isinstance(controller, DesktopAgentLoopController)
    assert isinstance(controller.planner, ClaudeDesktopStepPlanner)


def test_factory_propagates_personal_trusted_profile_to_step_planner(tmp_path):
    settings = _settings(tmp_path)
    settings.computer_control.safety_profile = "personal_trusted"

    controller = build_computer_controller(settings, FakeExecutor())

    assert isinstance(controller.planner, ClaudeDesktopStepPlanner)
    assert controller.planner.safety_profile == "personal_trusted"


def test_factory_enables_dynamic_visual_driver_only_for_local_unrestricted(tmp_path):
    settings = _settings(tmp_path)
    settings.computer_control.safety_profile = "local_unrestricted"

    controller = build_computer_controller(settings, FakeExecutor())

    assert isinstance(controller.driver, WindowsUiaDriver)
    assert controller.driver._discover_all_windows is True
    assert controller.driver._activate_on_observe is True
    assert controller.driver._capture_screenshots is True


def test_legacy_codex_controller_is_only_selected_explicitly(tmp_path):
    settings = _settings(tmp_path)
    settings.computer_control.backend = "legacy_codex_cli"
    settings.computer_control.working_directory.mkdir(parents=True)

    controller = build_computer_controller(settings, FakeExecutor())

    assert isinstance(controller, CodexComputerController)
