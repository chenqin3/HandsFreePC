from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

import pytest
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
    assert set(report["models"]) == {"wake", "delimiter", "command", "vad"}


def test_doctor_requires_a_complete_delimiter_model(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
speech:
  wake:
    model_path: models/wake
  delimiter:
    model_path: models/delimiter
  command:
    model_path: models/command
  vad:
    model_path: models/vad.onnx
""",
        encoding="utf-8",
    )
    for directory in ("wake/am", "wake/conf", "delimiter/am", "command"):
        (tmp_path / "models" / directory).mkdir(parents=True, exist_ok=True)
    (tmp_path / "models/wake/am/final.mdl").touch()
    (tmp_path / "models/wake/conf/model.conf").touch()
    (tmp_path / "models/delimiter/am/final.mdl").touch()
    (tmp_path / "models/command/tokens.txt").touch()
    (tmp_path / "models/command/model.int8.onnx").touch()
    (tmp_path / "models/vad.onnx").touch()
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli.platform, "platform", lambda: "Windows-test")
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(cli, "list_audio_devices", lambda: [{"index": 0}])
    monkeypatch.setattr(cli, "_check_command", lambda *_args, **_kwargs: {"found": False})

    cli.command_doctor(
        SimpleNamespace(config=str(config), check_planner_auth=False, strict=False)
    )
    incomplete = json.loads(capsys.readouterr().out)

    assert incomplete["models"]["delimiter"]["ready"] is False
    assert incomplete["ready_for_run"] is False

    (tmp_path / "models/delimiter/conf").mkdir()
    (tmp_path / "models/delimiter/conf/model.conf").touch()
    cli.command_doctor(
        SimpleNamespace(config=str(config), check_planner_auth=False, strict=False)
    )
    complete = json.loads(capsys.readouterr().out)

    assert complete["models"]["delimiter"]["ready"] is True
    assert complete["ready_for_run"] is True


def test_doctor_reports_faster_whisper_command_backend_without_sensevoice_weights(
    tmp_path, monkeypatch, capsys
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
speech:
  wake:
    model_path: models/wake
  delimiter:
    model_path: models/delimiter
  command:
    backend: faster-whisper
    model: large-v3-turbo
  vad:
    model_path: models/vad.onnx
""",
        encoding="utf-8",
    )
    for directory in ("wake/am", "wake/conf", "delimiter/am", "delimiter/conf"):
        (tmp_path / "models" / directory).mkdir(parents=True, exist_ok=True)
    for relative in (
        "wake/am/final.mdl",
        "wake/conf/model.conf",
        "delimiter/am/final.mdl",
        "delimiter/conf/model.conf",
        "vad.onnx",
    ):
        (tmp_path / "models" / relative).touch()
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli.platform, "platform", lambda: "Windows-test")
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(cli, "list_audio_devices", lambda: [{"index": 0}])
    monkeypatch.setattr(cli, "_check_command", lambda *_args, **_kwargs: {"found": False})

    exit_code = cli.command_doctor(
        SimpleNamespace(config=str(config), check_planner_auth=False, strict=True)
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["models"]["command"] == {
        "backend": "faster-whisper",
        "model": "large-v3-turbo",
        "ready": True,
        "weights_may_download_on_first_run": True,
    }
    assert report["modules"]["faster_whisper"] is True
    assert report["ready_for_run"] is True


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


def test_command_check_uses_shared_executable_resolver(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "resolve_executable",
        lambda name: "C:/resolved/codex.exe" if name == "codex" else None,
    )

    assert cli._check_command("codex") == {
        "found": True,
        "path": "C:/resolved/codex.exe",
    }
    assert cli._check_command("missing") == {"found": False, "path": None}


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


def test_logs_tail_outputs_only_bounded_diagnostic_events(tmp_path, capsys) -> None:
    path = tmp_path / "handsfreepc.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-31T00:00:00.000Z",
                "level": "error",
                "stage": "observe_driver",
                "error_code": "UIA_READ_FAILED",
                "safe_message": "UI Automation could not read the target app",
                "prompt": "private prompt",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli.command_logs(SimpleNamespace(path=str(path), tail=10))

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["log_file"] == str(path.resolve())
    assert report["event_count"] == 1
    assert "prompt" not in report["events"][0]


def test_diagnose_last_reports_newest_failure_without_raw_fields(tmp_path, capsys) -> None:
    path = tmp_path / "handsfreepc.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-08-31T00:00:00.000Z",
                        "level": "error",
                        "stage": "plan",
                        "error_code": "PLANNER_FAILED",
                        "safe_message": "Planner returned no safe next step",
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-08-31T00:00:01.000Z",
                        "level": "info",
                        "stage": "runtime",
                        "error_code": "QUEUE_PAUSED",
                        "safe_message": "Queue paused",
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli.command_diagnose_last(SimpleNamespace(path=str(path)))

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["found"] is True
    assert report["event"]["stage"] == "plan"
    assert report["event"]["error_code"] == "PLANNER_FAILED"


def test_diagnostic_subcommands_accept_tail_and_empty_log(tmp_path, capsys) -> None:
    parser = cli.build_parser()
    logs_args = parser.parse_args(["logs", "--tail", "7", "--path", str(tmp_path / "none")])
    diagnose_args = parser.parse_args(["diagnose-last", "--path", str(tmp_path / "none")])

    assert logs_args.func(logs_args) == 0
    logs_report = json.loads(capsys.readouterr().out)
    assert logs_report["events"] == []
    assert diagnose_args.func(diagnose_args) == 0
    diagnose_report = json.loads(capsys.readouterr().out)
    assert diagnose_report == {
        "found": False,
        "log_file": str((tmp_path / "none").resolve()),
        "event": None,
    }


def test_transcripts_tail_outputs_raw_text_and_absolute_path(tmp_path, capsys) -> None:
    path = (tmp_path / "asr-transcripts.jsonl").resolve()
    raw = "  切换到 Claude，打开 Chat and Cowork  "
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-09-01T00:00:00.000Z",
                "source": "command_utterance",
                "text": raw,
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli.command_transcripts(
        SimpleNamespace(config=None, path=str(path), tail=10)
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["transcript_file"] == str(path)
    assert report["entry_count"] == 1
    assert report["entries"][0]["text"] == raw


def test_run_prints_diagnostic_and_transcript_locations(
    settings,
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    diagnostic_path = (tmp_path / "diagnostics.jsonl").resolve()
    transcript_path = (tmp_path / "transcripts.jsonl").resolve()

    class FakeDiagnostics:
        path = diagnostic_path

        @staticmethod
        def event(**_kwargs):
            pass

    class FakeRuntime:
        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def run_microphone() -> None:
            pass

        @staticmethod
        def stop() -> None:
            pass

    monkeypatch.setattr(cli, "configure_diagnostics", lambda: FakeDiagnostics())
    monkeypatch.setattr(cli, "load_settings", lambda _config: settings)
    monkeypatch.setattr(cli, "_build_executor", lambda _settings: object())
    monkeypatch.setattr(cli, "default_transcript_path", lambda: transcript_path)
    monkeypatch.setattr("handsfree_pc.runtime.VoiceRuntime", FakeRuntime)

    assert cli.command_run(SimpleNamespace(config=None)) == 0

    report = json.loads(capsys.readouterr().out)
    assert report == {
        "diagnostics_file": str(diagnostic_path),
        "transcripts_enabled": False,
        "transcript_file": str(transcript_path),
        "audio_saved": False,
    }


def test_run_records_runtime_lifecycle_without_transcript_content(monkeypatch) -> None:
    events: list[dict[str, object]] = []

    class FakeDiagnostics:
        def event(self, **kwargs):
            events.append(kwargs)

    class FakeRuntime:
        stopped = False

        def __init__(self, *_args, **_kwargs):
            pass

        @staticmethod
        def run_microphone() -> None:
            pass

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr(cli, "configure_diagnostics", lambda: FakeDiagnostics())
    monkeypatch.setattr(cli, "load_settings", lambda _config: object())
    monkeypatch.setattr(cli, "_build_executor", lambda _settings: object())
    monkeypatch.setattr("handsfree_pc.runtime.VoiceRuntime", FakeRuntime)

    assert cli.command_run(SimpleNamespace(config=None)) == 0
    assert [event["error_code"] for event in events] == ["RUNTIME_STARTED", "RUNTIME_STOPPED"]
    assert all("prompt" not in event and "uia_text" not in event for event in events)


def test_run_records_initialization_failure_by_exception_type_only(monkeypatch) -> None:
    events: list[dict[str, object]] = []

    class FakeDiagnostics:
        def event(self, **kwargs):
            events.append(kwargs)

    monkeypatch.setattr(cli, "configure_diagnostics", lambda: FakeDiagnostics())
    monkeypatch.setattr(
        cli,
        "load_settings",
        lambda _config: (_ for _ in ()).throw(RuntimeError("private configuration detail")),
    )

    with pytest.raises(RuntimeError, match="private configuration detail"):
        cli.command_run(SimpleNamespace(config=None))

    assert len(events) == 1
    assert events[0]["stage"] == "runtime"
    assert events[0]["error_code"] == "RUNTIME_INITIALIZATION_FAILED"
    assert events[0]["safe_message"] == "HandsFreePC voice runtime could not initialize"
    assert isinstance(events[0]["exception_type"], RuntimeError)
    assert "private configuration detail" not in str(events[0]["safe_message"])
