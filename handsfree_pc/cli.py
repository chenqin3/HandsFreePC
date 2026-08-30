from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import yaml

from .audio import build_transcriber, list_audio_devices
from .config import DEFAULT_CONFIG, discover_config_path, load_settings
from .downloads import download_models
from .feedback import FeedbackController
from .intents import DeterministicIntentParser
from .models import FeedbackMode, RuntimeState
from .planner import _sanitized_env, build_planner


class _NoopSpeaker:
    def __init__(self) -> None:
        self.speaking = threading.Event()


class _NoopFeedback:
    """Keep parse/simulation commands free of visible and audible side effects."""

    def __init__(self) -> None:
        self.mode = FeedbackMode.SILENT
        self.speaker = _NoopSpeaker()

    def emit(self, text: str, **kwargs: Any) -> None:
        del text, kwargs

    def set_mode(self, mode: FeedbackMode, *, allow_voice: bool = True) -> None:
        del allow_voice
        self.mode = mode

    def close(self) -> None:
        pass


def _json(value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, indent=2)
    try:
        print(payload)
    except UnicodeEncodeError:
        # ``backslashreplace`` can emit Python-only ``\xNN`` escapes, which
        # are invalid JSON. Re-serialize as ASCII JSON so downstream parsers
        # receive valid ``\uNNNN`` escapes on legacy Windows code pages.
        ascii_payload = json.dumps(value, ensure_ascii=True, indent=2)
        sys.stdout.buffer.write(f"{ascii_payload}\n".encode("ascii"))
        sys.stdout.buffer.flush()


def _build_executor(settings: Any) -> Any:
    from .windows.executor import WindowsExecutor

    return WindowsExecutor(settings)


def command_init(args: argparse.Namespace) -> int:
    destination = Path(args.output or discover_config_path()).resolve()
    source = Path(__file__).resolve().parents[1] / "config.example.yaml"
    if destination.exists() and not args.force:
        print(f"配置已存在，未覆盖: {destination}")
        return 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        shutil.copyfile(source, destination)
    else:
        destination.write_text(
            yaml.safe_dump(DEFAULT_CONFIG, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
    print(f"已创建配置: {destination}")
    return 0


def command_parse(args: argparse.Namespace) -> int:
    settings = load_settings(args.config, allow_missing=True)
    parser = DeterministicIntentParser()
    plan = parser.parse(args.text)
    if plan is None and args.use_planner:
        planner = build_planner(settings.planner)
        if planner is None:
            raise RuntimeError("Planner is disabled in configuration")
        plan = planner.plan(args.text, context={"configured_apps": sorted(settings.apps)})
    _json(plan.to_dict() if plan else None)
    return 0 if plan else 2


def command_simulate(args: argparse.Namespace) -> int:
    from .runtime import VoiceRuntime

    settings = load_settings(args.config, allow_missing=True)
    settings.execution.dry_run = True
    runtime = VoiceRuntime(settings, _build_executor(settings), feedback=_NoopFeedback())
    commands = args.text or []
    if args.file:
        commands.extend(
            line.strip()
            for line in Path(args.file).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    outcomes = []
    for command in commands:
        if args.independent:
            runtime.state = RuntimeState.ARMED
            runtime.pending_plan = None
        outcome = runtime.handle_text(command, require_wake=args.require_wake)
        outcomes.append(
            {
                "input": command,
                "handled": outcome.handled,
                "state": outcome.state.value,
                "message": outcome.message,
                "success": outcome.success,
                "plan": outcome.plan.to_dict() if outcome.plan else None,
            }
        )
    _json(outcomes)
    runtime.stop()
    return 0 if outcomes and all(item["success"] for item in outcomes) else 2


def _check_command(
    name: str,
    status_args: list[str] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    executable = shutil.which(name)
    result: dict[str, Any] = {"found": bool(executable), "path": executable}
    if executable and status_args:
        completed = subprocess.run(
            [executable, *status_args],
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            env=env,
        )
        result["status_ok"] = completed.returncode == 0
        raw_status = (completed.stdout or completed.stderr).strip()
        try:
            status_payload = json.loads(raw_status)
        except json.JSONDecodeError:
            first_line = raw_status.splitlines()
            result["status"] = first_line[0] if first_line else ""
        else:
            allowed_status_fields = {"loggedIn", "authMethod", "apiProvider", "apiKeySource"}
            result["status"] = {
                key: value for key, value in status_payload.items() if key in allowed_status_fields
            }
    return result


def command_doctor(args: argparse.Namespace) -> int:
    settings = load_settings(args.config, allow_missing=True)
    base_dir = settings.config_path.parent
    modules = [
        "yaml",
        "psutil",
        "numpy",
        "sounddevice",
        "vosk",
        "sherpa_onnx",
        "win32api",
        "pywinauto",
    ]
    report: dict[str, Any] = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "config": str(settings.config_path),
        "config_exists": settings.config_path.exists(),
        "privacy": {
            "save_audio": settings.privacy.save_audio,
            "save_transcripts": settings.privacy.save_transcripts,
            "cloud_planner": settings.privacy.allow_cloud_planner and settings.planner.enabled,
        },
        "modules": {name: importlib.util.find_spec(name) is not None for name in modules},
        "commands": {
            "codex": _check_command(
                settings.planner.codex_executable,
                ["login", "status"] if args.check_planner_auth else None,
                env=_sanitized_env(),
            ),
            "claude": _check_command(
                settings.planner.claude_executable,
                ["auth", "status"] if args.check_planner_auth else None,
                env=_sanitized_env(),
            ),
        },
    }

    def resolve_model_path(value: object) -> Path:
        path = Path(str(value))
        return path if path.is_absolute() else (base_dir / path).resolve()

    wake_path = resolve_model_path(settings.speech.wake["model_path"])
    command_path = resolve_model_path(settings.speech.command["model_path"])
    vad_path = resolve_model_path(settings.speech.vad["model_path"])
    wake_ready = (wake_path / "am" / "final.mdl").is_file()
    command_ready = (command_path / "tokens.txt").is_file() and any(
        (command_path / name).is_file() for name in ("model.int8.onnx", "model.onnx")
    )
    vad_ready = vad_path.is_file()
    report["models"] = {
        "wake": {"path": str(wake_path), "ready": wake_ready},
        "command": {"path": str(command_path), "ready": command_ready},
        "vad": {"path": str(vad_path), "ready": vad_ready},
    }
    try:
        report["audio_inputs"] = list_audio_devices()
    except Exception as exc:
        report["audio_inputs"] = []
        report["audio_error"] = str(exc)
    core_ok = platform.system() == "Windows" and all(
        report["modules"].get(name, False) for name in ("yaml", "psutil")
    )
    runtime_modules = ("yaml", "psutil", "numpy", "sounddevice", "vosk", "sherpa_onnx")
    if platform.system() == "Windows":
        runtime_modules += ("win32api", "pywinauto")
    ready_for_run = (
        platform.system() == "Windows"
        and report["config_exists"]
        and all(report["modules"].get(name, False) for name in runtime_modules)
        and all(model["ready"] for model in report["models"].values())
        and bool(report["audio_inputs"])
    )
    report["ready_for_run"] = ready_for_run
    _json(report)
    if getattr(args, "strict", False):
        return 0 if ready_for_run else 1
    return 0 if core_ok else 1


def command_download_models(args: argparse.Namespace) -> int:
    models_dir = Path(args.directory).resolve()
    download_models(models_dir, force=args.force)
    return 0


def command_devices(args: argparse.Namespace) -> int:
    del args
    _json(list_audio_devices())
    return 0


def command_transcribe(args: argparse.Namespace) -> int:
    import wave

    import numpy as np

    settings = load_settings(args.config, allow_missing=True)
    path = Path(args.audio).resolve()
    with wave.open(str(path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise RuntimeError("test-asr expects mono 16-bit PCM WAV")
        sample_rate = wav.getframerate()
        samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype=np.int16).astype(np.float32)
        samples /= 32768.0
    transcriber = build_transcriber(settings.speech, base_dir=settings.config_path.parent)
    text = transcriber.transcribe(samples, sample_rate)
    _json({"audio": str(path), "sample_rate": sample_rate, "text": text})
    return 0


def command_overlay(args: argparse.Namespace) -> int:
    import time

    controller = FeedbackController(FeedbackMode(args.mode))
    controller.emit(args.text, kind=args.kind, duration=args.duration)
    time.sleep(args.duration + 0.5)
    controller.close()
    return 0


def command_run(args: argparse.Namespace) -> int:
    from .runtime import VoiceRuntime

    settings = load_settings(args.config)
    runtime = VoiceRuntime(settings, _build_executor(settings))
    try:
        runtime.run_microphone()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handsfreepc", description="HandsFreePC voice controller")
    parser.add_argument("--config", help="Path to local YAML configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a local configuration")
    init_parser.add_argument("--output")
    init_parser.add_argument("--force", action="store_true")
    init_parser.set_defaults(func=command_init)

    parse_parser = subparsers.add_parser("parse", help="Parse one command without executing it")
    parse_parser.add_argument("text")
    parse_parser.add_argument("--use-planner", action="store_true")
    parse_parser.set_defaults(func=command_parse)

    simulate = subparsers.add_parser("simulate", help="Dry-run commands")
    simulate.add_argument("text", nargs="*")
    simulate.add_argument("--file")
    simulate.add_argument("--require-wake", action="store_true")
    simulate.add_argument(
        "--independent",
        action="store_true",
        help="Reset runtime state before every input command",
    )
    simulate.set_defaults(func=command_simulate)

    doctor = subparsers.add_parser("doctor", help="Check configuration and dependencies")
    doctor.add_argument(
        "--check-planner-auth",
        action="store_true",
        help="Explicitly run Codex/Claude authentication status commands",
    )
    doctor.add_argument(
        "--strict",
        action="store_true",
        help="Fail unless default runtime dependencies, models, and an input device are ready",
    )
    doctor.set_defaults(func=command_doctor)

    downloader = subparsers.add_parser(
        "download-models", help="Download official local speech models"
    )
    downloader.add_argument("--directory", default="models")
    downloader.add_argument("--force", action="store_true")
    downloader.set_defaults(func=command_download_models)

    devices = subparsers.add_parser("list-audio-devices")
    devices.set_defaults(func=command_devices)

    transcribe = subparsers.add_parser("test-asr", help="Transcribe a mono PCM WAV file")
    transcribe.add_argument("audio")
    transcribe.set_defaults(func=command_transcribe)

    overlay = subparsers.add_parser("overlay-demo", help="Show the non-activating feedback overlay")
    overlay.add_argument("--text", default="HandsFreePC 正在听")
    overlay.add_argument(
        "--kind",
        default="listening",
        choices=["armed", "listening", "recognized", "executing", "confirm", "success", "error"],
    )
    overlay.add_argument("--duration", type=float, default=3.0)
    overlay.add_argument("--mode", default="overlay", choices=[item.value for item in FeedbackMode])
    overlay.set_defaults(func=command_overlay)

    run_parser = subparsers.add_parser("run", help="Start the always-on local voice controller")
    run_parser.set_defaults(func=command_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"HandsFreePC error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
