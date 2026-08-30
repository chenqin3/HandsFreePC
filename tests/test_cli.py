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
