from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

import handsfree_pc.cli as cli
from handsfree_pc.config import DEFAULT_CONFIG
from handsfree_pc.control import ControlResult


def _prepare_windows_doctor(monkeypatch, *, kimi_path=None, skill=None) -> None:
    monkeypatch.setattr(cli.platform, "system", lambda: "Windows")
    monkeypatch.setattr(cli.platform, "platform", lambda: "Windows-test")
    monkeypatch.setattr(cli.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(cli, "list_audio_devices", lambda: [{"index": 0}])
    monkeypatch.setattr(cli, "resolve_kimi", lambda _executable: kimi_path)
    monkeypatch.setattr(cli, "gui_control_skill_path", lambda _kimi: skill)
    monkeypatch.setattr(cli, "kimi_version", lambda _path: "kimi 9.9.9")


def _write_complete_models(tmp_path: Path) -> None:
    for directory in ("wake/am", "wake/conf", "delimiter/am", "delimiter/conf", "command"):
        (tmp_path / "models" / directory).mkdir(parents=True, exist_ok=True)
    for relative in (
        "wake/am/final.mdl",
        "wake/conf/model.conf",
        "delimiter/am/final.mdl",
        "delimiter/conf/model.conf",
        "command/tokens.txt",
        "command/model.int8.onnx",
        "vad.onnx",
    ):
        (tmp_path / "models" / relative).touch()


_MODEL_CONFIG = """
speech:
  wake:
    model_path: models/wake
  delimiter:
    model_path: models/delimiter
  command:
    model_path: models/command
  vad:
    model_path: models/vad.onnx
"""


def test_init_falls_back_to_packaged_defaults_when_repo_template_is_absent(
    tmp_path, monkeypatch
) -> None:
    fake_module = tmp_path / "site-packages" / "handsfree_pc" / "cli.py"
    destination = tmp_path / "config.yaml"
    monkeypatch.setattr(cli, "__file__", str(fake_module))

    exit_code = cli.command_init(SimpleNamespace(output=str(destination), force=False))

    assert exit_code == 0
    assert yaml.safe_load(destination.read_text(encoding="utf-8")) == DEFAULT_CONFIG


def test_doctor_strict_fails_without_models_or_kimi(tmp_path, monkeypatch, capsys) -> None:
    _prepare_windows_doctor(monkeypatch)
    config = tmp_path / "config.yaml"
    config.write_text("{}\n", encoding="utf-8")

    exit_code = cli.command_doctor(
        SimpleNamespace(config=str(config), check_kimi=False, strict=True)
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert report["ready_for_run"] is False
    assert report["ready_for_control"] is False
    assert set(report["models"]) == {"wake", "delimiter", "command", "vad"}
    assert report["kimi"]["found"] is False
    assert report["kimi"]["gui_control_skill"] is None


def test_doctor_requires_a_complete_delimiter_model(tmp_path, monkeypatch, capsys) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(_MODEL_CONFIG, encoding="utf-8")
    _write_complete_models(tmp_path)
    (tmp_path / "models/delimiter/conf/model.conf").unlink()
    _prepare_windows_doctor(monkeypatch)

    cli.command_doctor(SimpleNamespace(config=str(config), check_kimi=False, strict=False))
    incomplete = json.loads(capsys.readouterr().out)
    assert incomplete["models"]["delimiter"]["ready"] is False
    assert incomplete["ready_for_run"] is False

    (tmp_path / "models/delimiter/conf/model.conf").touch()
    cli.command_doctor(SimpleNamespace(config=str(config), check_kimi=False, strict=False))
    complete = json.loads(capsys.readouterr().out)
    assert complete["models"]["delimiter"]["ready"] is True
    assert complete["ready_for_run"] is True


def test_doctor_strict_passes_when_models_kimi_and_skill_are_ready(
    tmp_path, monkeypatch, capsys
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(_MODEL_CONFIG + "kimi:\n  executable: kimi\n", encoding="utf-8")
    _write_complete_models(tmp_path)
    skill = tmp_path / "skills" / "gui-control" / "SKILL.md"
    _prepare_windows_doctor(monkeypatch, kimi_path="C:/kimi/kimi.exe", skill=skill)

    exit_code = cli.command_doctor(
        SimpleNamespace(config=str(config), check_kimi=True, strict=True)
    )

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["ready_for_control"] is True
    assert report["kimi"] == {
        "executable": "kimi",
        "path": "C:/kimi/kimi.exe",
        "found": True,
        "version": "kimi 9.9.9",
        "gui_control_skill": str(skill),
        "working_directory": str(Path.home()),
        "timeout_seconds": 600.0,
    }


def test_doctor_reports_faster_whisper_backend_without_sensevoice_weights(
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
    _write_complete_models(tmp_path)
    _prepare_windows_doctor(monkeypatch, kimi_path="C:/kimi/kimi.exe", skill=tmp_path / "s")

    exit_code = cli.command_doctor(
        SimpleNamespace(config=str(config), check_kimi=False, strict=True)
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
    assert report["kimi"]["version"] is None


def test_gui_control_skill_is_looked_up_in_configured_then_default_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: tmp_path / "home"))
    kimi = SimpleNamespace(skills_dir=tmp_path / "custom")
    assert cli.gui_control_skill_path(kimi) is None

    default_skill = tmp_path / "home" / ".kimi-code" / "skills" / "gui-control" / "SKILL.md"
    default_skill.parent.mkdir(parents=True)
    default_skill.write_text("---\nname: gui-control\n---\n", encoding="utf-8")
    assert cli.gui_control_skill_path(kimi) == default_skill

    custom_skill = tmp_path / "custom" / "gui-control" / "SKILL.md"
    custom_skill.parent.mkdir(parents=True)
    custom_skill.write_text("---\nname: gui-control\n---\n", encoding="utf-8")
    assert cli.gui_control_skill_path(kimi) == custom_skill


def test_resolve_kimi_accepts_a_full_path_or_a_name_on_path(tmp_path, monkeypatch) -> None:
    exe = tmp_path / "kimi.exe"
    exe.write_bytes(b"")
    assert cli.resolve_kimi(str(exe)) == str(exe)
    monkeypatch.setattr(
        cli.shutil, "which", lambda name: "C:/on/path/kimi.exe" if name == "kimi" else None
    )
    assert cli.resolve_kimi("kimi") == "C:/on/path/kimi.exe"
    assert cli.resolve_kimi("missing") is None


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


def test_exec_hands_one_instruction_to_kimi(tmp_path, monkeypatch, capsys) -> None:
    import handsfree_pc.kimi_agent as kimi_module

    class FakeController:
        def __init__(self) -> None:
            self.calls = []
            self.closed = False
            self.last_run = SimpleNamespace(
                tool_calls=3,
                tool_names=["Skill", "Bash", "Bash"],
                screenshot="C:/shots/final.png",
                session_id="session_x",
                returncode=0,
            )

        def run(self, instruction, *, cancel_event=None):
            del cancel_event
            self.calls.append(instruction)
            return ControlResult(True, "已打开记事本", error_code="KIMI_COMPLETED")

        def close(self):
            self.closed = True

    controller = FakeController()
    built = []

    def fake_build(kimi_settings, *, diagnostics=None, on_progress=None):
        built.append((kimi_settings.executable, diagnostics is not None, callable(on_progress)))
        return controller

    class FakeDiagnostics:
        path = tmp_path / "events.jsonl"

        def event(self, **_kwargs):
            pass

    monkeypatch.setattr(kimi_module, "build_kimi_controller", fake_build)
    monkeypatch.setattr(cli, "configure_diagnostics", lambda: FakeDiagnostics())
    config = tmp_path / "config.yaml"
    config.write_text("kimi:\n  executable: kimi-test\n", encoding="utf-8")

    exit_code = cli.command_exec(SimpleNamespace(config=str(config), text="打开记事本"))

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert controller.calls == ["打开记事本"]
    assert controller.closed
    assert built == [("kimi-test", True, True)]
    assert report["success"] is True
    assert report["tool_calls"] == 3
    assert report["screenshot"] == "C:/shots/final.png"


def test_logs_tail_outputs_only_bounded_diagnostic_events(tmp_path, capsys) -> None:
    path = tmp_path / "handsfreepc.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-31T00:00:00.000Z",
                "level": "error",
                "stage": "kimi_agent",
                "error_code": "KIMI_TIMEOUT",
                "safe_message": "Kimi timed out",
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


def test_diagnose_last_reports_newest_failure(tmp_path, capsys) -> None:
    path = tmp_path / "handsfreepc.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-08-31T00:00:00.000Z",
                        "level": "error",
                        "stage": "kimi_agent",
                        "error_code": "KIMI_REPORTED_FAILURE",
                        "safe_message": "Kimi reported the task failed",
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
    assert report["event"]["stage"] == "kimi_agent"
    assert report["event"]["error_code"] == "KIMI_REPORTED_FAILURE"


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


def test_parser_exposes_only_the_current_commands() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["exec", "打开记事本"])
    assert args.func is cli.command_exec
    assert args.text == "打开记事本"
    with pytest.raises(SystemExit):
        parser.parse_args(["simulate", "x"])
    with pytest.raises(SystemExit):
        parser.parse_args(["computer-doctor"])


def test_transcripts_tail_outputs_raw_text_and_absolute_path(tmp_path, capsys) -> None:
    path = (tmp_path / "asr-transcripts.jsonl").resolve()
    raw = "  切换到 Claude，打开 Chat and Cowork  "
    path.write_text(
        json.dumps(
            {"timestamp": "2026-09-01T00:00:00.000Z", "source": "command_utterance", "text": raw},
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli.command_transcripts(SimpleNamespace(config=None, path=str(path), tail=10))

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["transcript_file"] == str(path)
    assert report["entry_count"] == 1
    assert report["entries"][0]["text"] == raw


class _FakeDiagnostics:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.events = []

    def event(self, **kwargs) -> None:
        self.events.append(kwargs)


def test_run_prints_diagnostic_and_transcript_locations(
    settings, tmp_path, monkeypatch, capsys
) -> None:
    diagnostic_path = (tmp_path / "diagnostics.jsonl").resolve()
    transcript_path = (tmp_path / "transcripts.jsonl").resolve()
    constructed = []

    class FakeRuntime:
        def __init__(self, passed_settings, *, diagnostics, transcript_journal):
            constructed.append((passed_settings, diagnostics, transcript_journal))

        @staticmethod
        def run_microphone() -> None:
            pass

        @staticmethod
        def stop() -> None:
            pass

    diagnostics = _FakeDiagnostics(diagnostic_path)
    monkeypatch.setattr(cli, "configure_diagnostics", lambda: diagnostics)
    monkeypatch.setattr(cli, "_acquire_single_instance_lock", lambda: tmp_path / "run.lock")
    monkeypatch.setattr(cli, "load_settings", lambda _config: settings)
    monkeypatch.setattr(cli, "default_transcript_path", lambda: transcript_path)
    monkeypatch.setattr("handsfree_pc.runtime.VoiceRuntime", FakeRuntime)

    exit_code = cli.command_run(SimpleNamespace(config=None))

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report == {
        "diagnostics_file": str(diagnostic_path),
        "transcripts_enabled": False,
        "transcript_file": str(transcript_path),
    }
    assert constructed == [(settings, diagnostics, None)]
    assert [event["error_code"] for event in diagnostics.events] == [
        "RUNTIME_STARTED",
        "RUNTIME_STOPPED",
    ]


def test_run_refuses_a_second_listener(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_acquire_single_instance_lock", lambda: None)

    exit_code = cli.command_run(SimpleNamespace(config=None))

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 3
    assert "already running" in report["error"]


def test_run_records_initialization_failure_by_exception_type_only(
    tmp_path, monkeypatch, capsys
) -> None:
    diagnostics = _FakeDiagnostics(tmp_path / "diagnostics.jsonl")
    monkeypatch.setattr(cli, "configure_diagnostics", lambda: diagnostics)
    monkeypatch.setattr(cli, "_acquire_single_instance_lock", lambda: tmp_path / "run.lock")

    def broken_settings(_config):
        raise RuntimeError("private detail C:\\Users\\someone\\secret.yaml")

    monkeypatch.setattr(cli, "load_settings", broken_settings)

    with pytest.raises(RuntimeError):
        cli.command_run(SimpleNamespace(config=None))

    capsys.readouterr()
    assert diagnostics.events[-1]["error_code"] == "RUNTIME_INITIALIZATION_FAILED"
    assert isinstance(diagnostics.events[-1]["exception_type"], RuntimeError)
    assert "secret" not in diagnostics.events[-1]["safe_message"]
