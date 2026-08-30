from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

import yaml

import handsfree_pc.cli as cli
from handsfree_pc.config import DEFAULT_CONFIG
from handsfree_pc.models import RuntimeState
from handsfree_pc.runtime import TurnOutcome
from handsfree_pc.windows.native import ForegroundIntegrityBoundary


def test_init_falls_back_to_packaged_defaults_when_repo_template_is_absent(
    tmp_path, monkeypatch
) -> None:
    fake_module = tmp_path / "site-packages" / "handsfree_pc" / "cli.py"
    destination = tmp_path / "config.yaml"
    monkeypatch.setattr(cli, "__file__", str(fake_module))

    exit_code = cli.command_init(SimpleNamespace(output=str(destination), force=False))

    assert exit_code == 0
    assert yaml.safe_load(destination.read_text(encoding="utf-8")) == DEFAULT_CONFIG


def test_simulate_returns_nonzero_when_a_command_fails(tmp_path, monkeypatch) -> None:
    class FakeRuntime:
        def __init__(self, *_args, **_kwargs):
            self.state = RuntimeState.ARMED
            self.pending_plan = None

        def handle_text(self, _command, *, require_wake):
            del require_wake
            return TurnOutcome(True, RuntimeState.ARMED, "目标不存在", success=False)

        def stop(self):
            pass

    monkeypatch.setattr("handsfree_pc.runtime.VoiceRuntime", FakeRuntime)
    monkeypatch.setattr(cli, "_build_executor", lambda _settings: object())
    args = SimpleNamespace(
        config=str(tmp_path / "missing.yaml"),
        text=["打开不存在的文件"],
        file=None,
        independent=False,
        require_wake=False,
    )

    assert cli.command_simulate(args) == 2


def test_doctor_strict_requires_complete_runtime(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli.platform, "platform", lambda: "Windows-test")
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(cli, "list_audio_devices", lambda: [{"index": 0}])
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")

    exit_code = cli.command_doctor(
        SimpleNamespace(config=str(config), check_planner_auth=False, strict=True)
    )

    report = yaml.safe_load(capsys.readouterr().out)
    assert exit_code == 1
    assert report["ready_for_run"] is False
    assert set(report["models"]) == {"wake", "command", "vad"}


def test_json_fallback_remains_valid_on_legacy_console(monkeypatch) -> None:
    class LegacyStdout:
        encoding = "ascii"

        def __init__(self) -> None:
            self.buffer = io.BytesIO()

        @staticmethod
        def write(value: str) -> int:
            if any(ord(char) > 127 for char in value):
                raise UnicodeEncodeError("ascii", value, 0, 1, "ordinal not in range")
            return len(value)

        @staticmethod
        def flush() -> None:
            pass

    output = LegacyStdout()
    monkeypatch.setattr(sys, "stdout", output)

    cli._json({"device": "麦克风 👶"})

    parsed = json.loads(output.buffer.getvalue().decode("ascii"))
    assert parsed == {"device": "麦克风 👶"}


def test_doctor_never_claims_live_control_from_static_checks(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli.platform, "platform", lambda: "Windows-test")
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(cli, "list_audio_devices", lambda: [{"index": 0}])
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")

    cli.command_doctor(SimpleNamespace(config=str(config), check_planner_auth=False, strict=False))

    report = json.loads(capsys.readouterr().out)
    assert report["live_control_verified"] is False
    assert report["ready_for_live_control"] is False
    assert report["computer_control"]["preflight_is_static_only"] is True


def test_doctor_uses_desktop_planner_executables_and_accepts_codex_best_effort(
    tmp_path, monkeypatch, capsys
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
privacy:
  allow_cloud_planner: true
computer_control:
  enabled: true
  backend: local_agent
  driver: windows_uia
  planner_backend: codex_cli_best_effort
  allow_screen_context_to_cloud: true
  allow_codex_cli_host_read: true
  codex_executable: desktop-codex
  claude_executable: desktop-claude
execution:
  dry_run: false
""",
        encoding="utf-8",
    )
    checked: list[str] = []

    def fake_check(name, *_args, **_kwargs):
        checked.append(name)
        return {"found": True, "path": f"C:/tools/{name}.exe"}

    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli.platform, "platform", lambda: "Windows-test")
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(cli, "list_audio_devices", lambda: [{"index": 0}])
    monkeypatch.setattr(cli, "_check_command", fake_check)

    exit_code = cli.command_doctor(
        SimpleNamespace(config=str(config), check_planner_auth=False, strict=False)
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["computer_control"]["planner_backend"] == "codex_cli_best_effort"
    assert "desktop-codex" in checked
    assert "desktop-claude" in checked


def test_computer_doctor_live_uses_owned_fixture_result(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
privacy:
  allow_cloud_planner: false
computer_control:
  enabled: true
  backend: local_agent
  driver: windows_uia
  planner_backend: none
execution:
  dry_run: false
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        cli,
        "_run_windows_uia_live_smoke",
        lambda _settings: {
            "driver": "windows_uia",
            "fresh_observation": True,
            "text_round_trip_verified": True,
            "unicode_round_trip_verified": True,
            "live_control_verified": True,
        },
    )

    exit_code = cli.command_computer_doctor(SimpleNamespace(config=str(config), live=True))

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["live_control_verified"] is True
    assert report["unicode_round_trip_verified"] is True


def test_computer_doctor_live_refuses_disabled_control(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")

    exit_code = cli.command_computer_doctor(SimpleNamespace(config=str(config), live=True))

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert report["error_type"] == "ComputerControlDisabled"


def test_computer_doctor_reports_foreground_integrity_boundary_without_details(
    tmp_path, monkeypatch, capsys
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
privacy:
  allow_cloud_planner: false
computer_control:
  enabled: true
  backend: local_agent
  driver: windows_uia
  planner_backend: none
execution:
  dry_run: false
""",
        encoding="utf-8",
    )

    def fail_closed(_settings):
        raise ForegroundIntegrityBoundary("private diagnostic details")

    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli, "_run_windows_uia_live_smoke", fail_closed)

    exit_code = cli.command_computer_doctor(SimpleNamespace(config=str(config), live=True))

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert report["live_control_verified"] is False
    assert report["error_type"] == "ForegroundIntegrityBoundary"
    assert "private diagnostic details" not in json.dumps(report)
