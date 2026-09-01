from __future__ import annotations

from handsfree_pc.computer_control import CodexComputerController
from handsfree_pc.config import load_settings
from handsfree_pc.desktop.agent_loop import DesktopAgentLoopController
from handsfree_pc.desktop.factory import build_computer_controller
from handsfree_pc.desktop.step_planner import ClaudeDesktopStepPlanner
from handsfree_pc.desktop.windows_uia import WindowsUiaDriver
from handsfree_pc.desktop.workmap_selector import ClaudeWorkMapSelector, CodexWorkMapSelector


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


def test_factory_passes_configured_control_prefixes_to_continuous_dictation(tmp_path):
    settings = _settings(tmp_path)
    settings.app.control_prefixes = ["电脑操作", "助手"]

    controller = build_computer_controller(settings, FakeExecutor())

    assert controller._control_prefixes == ("电脑操作", "助手")


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


def test_factory_wires_screenshot_visual_planning_without_requiring_ocr(tmp_path):
    settings = _settings(tmp_path)
    settings.computer_control.safety_profile = "local_unrestricted"
    settings.visual_ocr.enabled = True

    controller = build_computer_controller(settings, FakeExecutor())

    assert controller.driver._visual_screenshot_enabled is True
    assert controller.driver._visual_ocr_client is None
    assert controller.driver._visual_ocr_apps == {"codex", "wechat"}


def test_factory_wires_optional_ocr_regions_only_after_separate_opt_in(tmp_path):
    settings = _settings(tmp_path)
    settings.computer_control.safety_profile = "local_unrestricted"
    settings.visual_ocr.enabled = True
    settings.visual_ocr.ocr_regions_enabled = True

    controller = build_computer_controller(settings, FakeExecutor())

    assert controller.driver._visual_screenshot_enabled is True
    assert controller.driver._visual_ocr_client is not None


def test_legacy_codex_controller_is_only_selected_explicitly(tmp_path):
    settings = _settings(tmp_path)
    settings.computer_control.backend = "legacy_codex_cli"
    settings.computer_control.working_directory.mkdir(parents=True)

    controller = build_computer_controller(settings, FakeExecutor())

    assert isinstance(controller, CodexComputerController)


def test_factory_wires_matching_workmap_selector_only_after_explicit_cloud_consents(tmp_path):
    settings = _settings(tmp_path)
    settings.computer_control.enabled = True
    settings.computer_control.planner_backend = "codex_cli_best_effort"
    settings.computer_control.allow_screen_context_to_cloud = True
    settings.computer_control.allow_codex_cli_host_read = True
    settings.privacy.allow_cloud_planner = True
    settings.workmap.enabled = True
    settings.workmap.out_directory = tmp_path / "workmap-out"

    controller = build_computer_controller(settings, FakeExecutor())

    assert isinstance(controller.native_router.workmap_selector, CodexWorkMapSelector)


def test_factory_wires_bounded_path_selector_even_when_workmap_is_disabled(tmp_path):
    settings = _settings(tmp_path)
    settings.computer_control.enabled = True
    settings.computer_control.planner_backend = "codex_cli_best_effort"
    settings.computer_control.allow_screen_context_to_cloud = True
    settings.computer_control.allow_codex_cli_host_read = True
    settings.privacy.allow_cloud_planner = True
    settings.workmap.enabled = False

    controller = build_computer_controller(settings, FakeExecutor())

    assert isinstance(controller.native_router.workmap_selector, CodexWorkMapSelector)


def test_factory_does_not_wire_workmap_selector_without_cloud_consent(tmp_path):
    settings = _settings(tmp_path)
    settings.computer_control.enabled = True
    settings.computer_control.planner_backend = "codex_cli_best_effort"
    settings.computer_control.allow_codex_cli_host_read = True
    settings.workmap.enabled = True
    settings.workmap.out_directory = tmp_path / "workmap-out"

    controller = build_computer_controller(settings, FakeExecutor())

    assert controller.native_router.workmap_selector is None


def test_factory_can_reuse_claude_backend_for_bounded_workmap_selection(tmp_path):
    settings = _settings(tmp_path)
    settings.computer_control.enabled = True
    settings.computer_control.planner_backend = "claude"
    settings.computer_control.allow_screen_context_to_cloud = True
    settings.privacy.allow_cloud_planner = True
    settings.workmap.enabled = True
    settings.workmap.out_directory = tmp_path / "workmap-out"

    controller = build_computer_controller(settings, FakeExecutor())

    assert isinstance(controller.native_router.workmap_selector, ClaudeWorkMapSelector)
