"""Command-line entry points: run the listener, check the machine, inspect local logs."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from .audio import build_transcriber, list_audio_devices
from .config import DEFAULT_CONFIG, KimiSettings, discover_config_path, load_settings
from .diagnostics import (
    DEFAULT_TAIL_COUNT,
    MAX_TAIL_COUNT,
    configure_diagnostics,
    default_log_path,
    diagnose_last_event,
    tail_events,
)
from .downloads import download_models
from .feedback import FeedbackController
from .models import FeedbackMode
from .transcripts import TranscriptJournal, default_transcript_path, tail_transcripts


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


# -- doctor -------------------------------------------------------------------------


def resolve_kimi(executable: str) -> str | None:
    """A full path to kimi.exe, or a name looked up on PATH."""

    candidate = Path(os.path.expandvars(executable)).expanduser()
    if candidate.is_file():
        return str(candidate)
    return shutil.which(executable)


def kimi_version(path: str) -> str | None:
    try:
        completed = subprocess.run(
            [path, "--version"],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (completed.stdout or completed.stderr).strip().splitlines()
    return output[0][:80] if output else None


def gui_control_skill_path(kimi: KimiSettings) -> Path | None:
    """Where Kimi will find the gui-control skill this runtime relies on."""

    roots = []
    if kimi.skills_dir is not None:
        roots.append(Path(kimi.skills_dir))
    roots.append(Path.home() / ".kimi-code" / "skills")
    for root in roots:
        candidate = root / "gui-control" / "SKILL.md"
        if candidate.is_file():
            return candidate
    return None


def command_doctor(args: argparse.Namespace) -> int:
    settings = load_settings(args.config, allow_missing=True)
    base_dir = settings.config_path.parent
    command_asr_backend = str(settings.speech.command.get("backend", "sensevoice")).lower()
    fallback_asr_backend = str(settings.speech.fallback.get("backend", "none")).lower()
    modules = ["yaml", "psutil", "numpy", "sounddevice", "vosk", "sherpa_onnx", "win32api"]
    if "faster-whisper" in {command_asr_backend, fallback_asr_backend}:
        modules.append("faster_whisper")
    report: dict[str, Any] = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "config": str(settings.config_path),
        "config_exists": settings.config_path.exists(),
        "privacy": {"save_transcripts": settings.privacy.save_transcripts},
        "modules": {name: importlib.util.find_spec(name) is not None for name in modules},
    }

    def resolve_model_path(value: object) -> Path:
        path = Path(str(value))
        return path if path.is_absolute() else (base_dir / path).resolve()

    def vosk_model_ready(path: Path) -> bool:
        return all((path / name).is_file() for name in ("am/final.mdl", "conf/model.conf"))

    wake_path = resolve_model_path(settings.speech.wake["model_path"])
    delimiter_path = resolve_model_path(settings.speech.delimiter["model_path"])
    vad_path = resolve_model_path(settings.speech.vad["model_path"])
    if command_asr_backend == "sensevoice":
        command_path = resolve_model_path(settings.speech.command["model_path"])
        command_ready = (command_path / "tokens.txt").is_file() and any(
            (command_path / name).is_file() for name in ("model.int8.onnx", "model.onnx")
        )
        command_model: dict[str, Any] = {
            "backend": command_asr_backend,
            "path": str(command_path),
            "ready": command_ready,
        }
    elif command_asr_backend == "faster-whisper":
        model_name = str(settings.speech.command.get("model", "large-v3-turbo")).strip()
        command_model = {
            "backend": command_asr_backend,
            "model": model_name,
            "ready": bool(model_name) and report["modules"].get("faster_whisper", False),
            "weights_may_download_on_first_run": True,
        }
    else:
        command_model = {"backend": command_asr_backend, "ready": False}
    report["models"] = {
        "wake": {"path": str(wake_path), "ready": vosk_model_ready(wake_path)},
        "delimiter": {"path": str(delimiter_path), "ready": vosk_model_ready(delimiter_path)},
        "command": command_model,
        "vad": {"path": str(vad_path), "ready": vad_path.is_file()},
    }
    try:
        report["audio_inputs"] = list_audio_devices()
    except Exception as exc:
        report["audio_inputs"] = []
        report["audio_error"] = str(exc)

    kimi_path = resolve_kimi(settings.kimi.executable)
    skill = gui_control_skill_path(settings.kimi)
    report["kimi"] = {
        "executable": settings.kimi.executable,
        "path": kimi_path,
        "found": kimi_path is not None,
        "version": kimi_version(kimi_path) if kimi_path and args.check_kimi else None,
        "gui_control_skill": str(skill) if skill else None,
        "working_directory": str(settings.kimi.working_directory or Path.home()),
        "timeout_seconds": settings.kimi.timeout_seconds,
    }

    runtime_modules = ["yaml", "psutil", "numpy", "sounddevice", "vosk", "sherpa_onnx"]
    if "faster-whisper" in {command_asr_backend, fallback_asr_backend}:
        runtime_modules.append("faster_whisper")
    if platform.system() == "Windows":
        runtime_modules.append("win32api")
    ready_for_run = (
        platform.system() == "Windows"
        and report["config_exists"]
        and all(report["modules"].get(name, False) for name in runtime_modules)
        and all(model["ready"] for model in report["models"].values())
        and bool(report["audio_inputs"])
    )
    report["ready_for_run"] = ready_for_run
    report["ready_for_control"] = ready_for_run and kimi_path is not None and skill is not None
    _json(report)
    if getattr(args, "strict", False):
        return 0 if report["ready_for_control"] else 1
    core_ok = platform.system() == "Windows" and all(
        report["modules"].get(name, False) for name in ("yaml", "psutil")
    )
    return 0 if core_ok else 1


# -- one instruction without the microphone --------------------------------------------


def command_exec(args: argparse.Namespace) -> int:
    """Hand one typed instruction to Kimi exactly as a spoken command would be."""

    from .kimi_agent import build_kimi_controller

    settings = load_settings(args.config)
    diagnostics = configure_diagnostics()

    def progress(text: str) -> None:
        print(text, file=sys.stderr, flush=True)

    controller = build_kimi_controller(settings.kimi, diagnostics=diagnostics, on_progress=progress)
    try:
        result = controller.run(args.text)
    finally:
        controller.close()
    run = controller.last_run
    _json(
        {
            "success": result.success,
            "message": result.message,
            "error_code": result.error_code,
            "tool_calls": run.tool_calls if run else 0,
            "tools": run.tool_names if run else [],
            "screenshot": run.screenshot if run else None,
            "session_id": run.session_id if run else None,
            "returncode": run.returncode if run else None,
        }
    )
    return 0 if result.success else 1


# -- models, audio, logs ------------------------------------------------------------


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


def command_logs(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser().resolve() if args.path else default_log_path().resolve()
    events = tail_events(path, limit=args.tail)
    _json({"log_file": str(path), "event_count": len(events), "events": events})
    return 0


def command_transcripts(args: argparse.Namespace) -> int:
    settings = load_settings(args.config, allow_missing=True)
    path = (
        Path(args.path).expanduser().resolve() if args.path else default_transcript_path().resolve()
    )
    entries = tail_transcripts(path, limit=args.tail)
    _json(
        {
            "enabled": settings.privacy.save_transcripts,
            "transcript_file": str(path),
            "entry_count": len(entries),
            "entries": entries,
        }
    )
    return 0


def command_diagnose_last(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser().resolve() if args.path else default_log_path().resolve()
    event = diagnose_last_event(path)
    _json({"found": event is not None, "log_file": str(path), "event": event})
    return 0


# -- run --------------------------------------------------------------------------


def _acquire_single_instance_lock() -> Path | None:
    """Refuse to start a second listener; two runtimes would fight for the microphone.

    Returns the lock path when acquired, or None when another live runtime
    already holds it. A stale lock (its process is gone) is taken over.
    """

    lock_path = default_log_path().resolve().parent / "run.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        if lock_path.exists():
            try:
                other_pid = int(lock_path.read_text(encoding="utf-8").strip() or "0")
            except ValueError:
                other_pid = 0
            if other_pid and other_pid != os.getpid():
                try:
                    import psutil

                    alive = psutil.pid_exists(other_pid)
                except Exception:
                    alive = False
                if alive:
                    return None
        lock_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        return lock_path
    return lock_path


def _attach_console_log_when_headless() -> None:
    """Under pythonw there is no console: send stdout/stderr to logs\\run.log."""

    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        log_path = default_log_path().resolve().parent / "run.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(log_path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


def command_run(args: argparse.Namespace) -> int:
    from .runtime import VoiceRuntime

    _attach_console_log_when_headless()
    lock_path = _acquire_single_instance_lock()
    if lock_path is None:
        _json(
            {
                "error": "HandsFreePC is already running in this session",
                "hint": "stop the other listener first (it holds the microphone)",
            }
        )
        return 3
    diagnostics = configure_diagnostics()
    runtime: VoiceRuntime | None = None
    transcript_journal: TranscriptJournal | None = None
    try:
        settings = load_settings(args.config)
        transcript_path = default_transcript_path().resolve()
        transcripts_enabled = bool(settings.privacy.save_transcripts)
        _json(
            {
                "diagnostics_file": str(getattr(diagnostics, "path", default_log_path().resolve())),
                "transcripts_enabled": transcripts_enabled,
                "transcript_file": str(transcript_path),
            }
        )
        if transcripts_enabled:
            transcript_journal = TranscriptJournal(transcript_path)
        runtime = VoiceRuntime(
            settings,
            diagnostics=diagnostics,
            transcript_journal=transcript_journal,
        )
        diagnostics.event(
            stage="runtime",
            error_code="RUNTIME_STARTED",
            safe_message="HandsFreePC voice runtime started",
            level="info",
        )
        runtime.run_microphone()
        diagnostics.event(
            stage="runtime",
            error_code="RUNTIME_STOPPED",
            safe_message="HandsFreePC voice runtime stopped normally",
            level="info",
        )
    except KeyboardInterrupt:
        diagnostics.event(
            stage="runtime",
            error_code="RUNTIME_STOPPED_BY_USER",
            safe_message="HandsFreePC voice runtime was stopped by the user",
            level="info",
        )
    except Exception as exc:
        diagnostics.event(
            stage="runtime",
            error_code=(
                "RUNTIME_INITIALIZATION_FAILED" if runtime is None else "RUNTIME_UNEXPECTED_FAILURE"
            ),
            safe_message=(
                "HandsFreePC voice runtime could not initialize"
                if runtime is None
                else "HandsFreePC voice runtime stopped after an unexpected failure"
            ),
            exception_type=exc,
        )
        raise
    finally:
        try:
            if runtime is not None:
                runtime.stop()
        finally:
            if transcript_journal is not None:
                transcript_journal.close()
    return 0


# -- parser -------------------------------------------------------------------------


def _add_tail_argument(parser: argparse.ArgumentParser, what: str) -> None:
    parser.add_argument(
        "--tail",
        type=int,
        nargs="?",
        const=DEFAULT_TAIL_COUNT,
        default=DEFAULT_TAIL_COUNT,
        choices=range(1, MAX_TAIL_COUNT + 1),
        metavar="N",
        help=f"Show the newest N {what} (default {DEFAULT_TAIL_COUNT})",
    )
    parser.add_argument("--path", help=argparse.SUPPRESS)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="handsfreepc", description="HandsFreePC voice controller")
    parser.add_argument("--config", help="Path to local YAML configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init", help="Create a local configuration")
    init_parser.add_argument("--output")
    init_parser.add_argument("--force", action="store_true")
    init_parser.set_defaults(func=command_init)

    doctor = subparsers.add_parser("doctor", help="Check configuration, models, audio, and Kimi")
    doctor.add_argument(
        "--check-kimi", action="store_true", help="Also run `kimi --version` (a few seconds)"
    )
    doctor.add_argument(
        "--strict",
        action="store_true",
        help="Exit non-zero unless models, an input device, Kimi, and its skill are all ready",
    )
    doctor.set_defaults(func=command_doctor)

    execute = subparsers.add_parser(
        "exec", help="Hand one typed instruction to Kimi (no microphone)"
    )
    execute.add_argument("text")
    execute.set_defaults(func=command_exec)

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
        choices=["armed", "listening", "recognized", "executing", "success", "error"],
    )
    overlay.add_argument("--duration", type=float, default=3.0)
    overlay.add_argument("--mode", default="overlay", choices=[item.value for item in FeedbackMode])
    overlay.set_defaults(func=command_overlay)

    logs = subparsers.add_parser("logs", help="Read privacy-bounded local diagnostic events")
    _add_tail_argument(logs, "events")
    logs.set_defaults(func=command_logs)

    transcripts = subparsers.add_parser(
        "transcripts", help="Read explicitly saved raw local ASR transcripts"
    )
    _add_tail_argument(transcripts, "transcript entries")
    transcripts.set_defaults(func=command_transcripts)

    diagnose_last = subparsers.add_parser(
        "diagnose-last", help="Show the newest privacy-bounded failure event"
    )
    diagnose_last.add_argument("--path", help=argparse.SUPPRESS)
    diagnose_last.set_defaults(func=command_diagnose_last)

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
