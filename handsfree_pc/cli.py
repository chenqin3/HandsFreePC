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
import time
import uuid
from pathlib import Path
from typing import Any

import yaml

from .audio import build_transcriber, list_audio_devices
from .config import DEFAULT_CONFIG, discover_config_path, load_settings
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
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    computer_use_skill_discovered = any(
        codex_home.glob("plugins/**/computer-use/**/skills/computer-use/SKILL.md")
    )
    codex_config = codex_home / "config.toml"
    try:
        node_repl_config_mentioned = "node_repl" in codex_config.read_text(
            encoding="utf-8", errors="ignore"
        )
    except OSError:
        node_repl_config_mentioned = False
    control = settings.computer_control
    uses_cloud_desktop_planner = control.backend == "legacy_codex_cli" or (
        control.backend == "local_agent" and control.planner_backend != "none"
    )
    report: dict[str, Any] = {
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "config": str(settings.config_path),
        "config_exists": settings.config_path.exists(),
        "privacy": {
            "save_audio": settings.privacy.save_audio,
            "save_transcripts": settings.privacy.save_transcripts,
            "cloud_transcript_permission": settings.privacy.allow_cloud_planner,
            "planner_transcripts_to_cloud": (
                settings.privacy.allow_cloud_planner and settings.planner.enabled
            ),
            "computer_control_transcripts_to_cloud": (
                settings.privacy.allow_cloud_planner
                and settings.computer_control.enabled
                and uses_cloud_desktop_planner
            ),
        },
        "effective_mode": "live" if not settings.execution.dry_run else "dry-run",
        "computer_control": {
            "enabled": settings.computer_control.enabled,
            "backend": settings.computer_control.backend,
            "driver": settings.computer_control.driver,
            "planner_backend": settings.computer_control.planner_backend,
            "screen_context_to_cloud": (
                settings.computer_control.enabled
                and settings.computer_control.allow_screen_context_to_cloud
            ),
            "max_queue_size": settings.computer_control.max_queue_size,
            "computer_use_skill_discovered": computer_use_skill_discovered,
            "node_repl_config_mentioned": node_repl_config_mentioned,
            "preflight_is_static_only": True,
            "experimental_driver_enabled": (
                settings.computer_control.driver == "open_computer_use"
            ),
        },
        "modules": {name: importlib.util.find_spec(name) is not None for name in modules},
        "commands": {
            "codex_computer_control": _check_command(
                settings.computer_control.codex_executable,
                ["login", "status"] if args.check_planner_auth else None,
                env=_sanitized_env(),
            ),
            "codex_planner": _check_command(
                settings.planner.codex_executable,
                ["login", "status"] if args.check_planner_auth else None,
                env=_sanitized_env(),
            ),
            "claude_planner": _check_command(
                settings.planner.claude_executable,
                ["auth", "status"] if args.check_planner_auth else None,
                env=_sanitized_env(),
            ),
            "claude_computer_control": _check_command(
                control.claude_executable,
                ["auth", "status"] if args.check_planner_auth else None,
                env=_sanitized_env(),
            ),
            "open_computer_use": _check_command(
                settings.computer_control.open_computer_use_executable,
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
    codex_ready = bool(report["commands"]["codex_computer_control"]["found"])
    if control.driver == "windows_uia":
        driver_ready = all(report["modules"].get(name, False) for name in ("win32api", "pywinauto"))
    elif control.driver == "open_computer_use":
        driver_ready = bool(report["commands"]["open_computer_use"]["found"])
    else:
        driver_ready = True
    planner_ready = {
        "none": True,
        "codex_cli_best_effort": bool(report["commands"]["codex_computer_control"]["found"]),
        "claude": bool(report["commands"]["claude_computer_control"]["found"]),
    }[control.planner_backend]
    consent_ready = not uses_cloud_desktop_planner or (
        settings.privacy.allow_cloud_planner and control.allow_screen_context_to_cloud
    )
    if control.backend == "legacy_codex_cli":
        controller_ready = (
            codex_ready and computer_use_skill_discovered and node_repl_config_mentioned
        )
    else:
        controller_ready = driver_ready and planner_ready
    static_control_preflight_passed = (
        ready_for_run
        and not settings.execution.dry_run
        and settings.computer_control.enabled
        and consent_ready
        and controller_ready
    )
    report["static_control_preflight_passed"] = static_control_preflight_passed
    report["live_control_verified"] = False
    report["ready_for_live_control"] = False
    _json(report)
    if getattr(args, "strict", False):
        strict_ready = (
            static_control_preflight_passed if settings.computer_control.enabled else ready_for_run
        )
        return 0 if strict_ready else 1
    return 0 if core_ok else 1


def _run_windows_uia_live_smoke(settings: Any) -> dict[str, Any]:
    """Exercise a harmless owned fixture and verify text through fresh UIA state."""

    from .config import AppProfile
    from .desktop.protocol import (
        DesktopAction,
        DesktopActionType,
        DesktopExpectation,
        DesktopExpectationKind,
    )
    from .desktop.safety import DesktopSafetyDisposition, DesktopSafetyPolicy
    from .desktop.verifier import DesktopVerifier
    from .desktop.windows_uia import WindowsUiaDriver

    title = f"HandsFreePC Live Fixture {uuid.uuid4().hex[:10]}"
    verification_text = f"HandsFreePC-中文验收-{uuid.uuid4().hex[:10]}"
    executable_name = Path(sys.executable).name
    profile = AppProfile(
        name="handsfreepc_live_fixture",
        process_names=[executable_name],
        executable=None,
        title_patterns=[title],
        search_hotkey=None,
        native_voice_hotkey=None,
        voice_button_names=[],
    )
    process: subprocess.Popen[str] | None = None
    driver = WindowsUiaDriver({profile.name: profile})
    try:
        process = subprocess.Popen(
            [sys.executable, "-m", "handsfree_pc.live_fixture", "--title", title],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            cwd=str(Path(__file__).resolve().parents[1]),
            env=_sanitized_env(),
            creationflags=(getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0),
        )
        deadline = time.monotonic() + 12
        observation = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("live UIA fixture exited before observation")
            try:
                observation = driver.observe(profile.name)
                break
            except Exception:
                time.sleep(0.1)
        if observation is None:
            raise RuntimeError("live UIA fixture did not become observable")
        candidates = [
            item
            for item in observation.elements
            if item.control_type.casefold() in {"edit", "document"}
            and item.enabled
            and not item.password
        ]
        if len(candidates) != 1:
            raise RuntimeError("live UIA fixture did not expose one unambiguous text field")
        action = DesktopAction(
            DesktopActionType.SET_VALUE,
            app=observation.app,
            generation=observation.generation,
            element_index=candidates[0].index,
            value=verification_text,
        )
        policy = DesktopSafetyPolicy()
        inspection = policy.inspect_observation(observation)
        user_task = f"fill {verification_text} into {candidates[0].name}"
        safety = policy.evaluate(
            action,
            observation,
            user_text=user_task,
            expectation=DesktopExpectation(
                DesktopExpectationKind.TEXT_PRESENT,
                text=verification_text,
            ),
        )
        if (
            inspection.disposition != DesktopSafetyDisposition.ALLOW
            or safety.disposition != DesktopSafetyDisposition.CONFIRM
            or safety.confirmation is None
        ):
            raise RuntimeError("live UIA fixture was rejected by local safety policy")
        # `computer-doctor --live` is itself an explicit, foreground opt-in to
        # mutate this process-owned fixture.  Production controller actions go
        # through VoiceRuntime's separate random one-time challenge.
        receipt = driver.execute(action, observation)
        after = driver.observe(observation.app)
        verification = DesktopVerifier().verify_action(action, receipt, observation, after)
        return {
            "driver": "windows_uia",
            "fixture_started": True,
            "fresh_observation": after.generation > observation.generation,
            "text_round_trip_verified": verification.verified,
            "unicode_round_trip_verified": verification.verified,
            "live_control_verified": verification.verified,
            "verification_reason": verification.reason,
        }
    finally:
        driver.close()
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def command_computer_doctor(args: argparse.Namespace) -> int:
    settings = load_settings(args.config, allow_missing=False)
    control = settings.computer_control
    report: dict[str, Any] = {
        "backend": control.backend,
        "driver": control.driver,
        "planner_backend": control.planner_backend,
        "live_requested": bool(args.live),
        "live_control_verified": False,
    }
    if not args.live:
        report["next_step"] = "Run computer-doctor --live for the owned UIA fixture test"
        _json(report)
        return 0
    if platform.system() != "Windows":
        report["error_type"] = "UnsupportedPlatform"
        _json(report)
        return 2
    if control.backend != "local_agent" or control.driver != "windows_uia":
        report["error_type"] = "LiveDoctorBackendUnsupported"
        report["supported_backend"] = "local_agent/windows_uia"
        _json(report)
        return 2
    if not control.enabled:
        report["error_type"] = "ComputerControlDisabled"
        _json(report)
        return 2
    if settings.execution.dry_run:
        report["error_type"] = "DryRunEnabled"
        _json(report)
        return 2
    try:
        report.update(_run_windows_uia_live_smoke(settings))
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        _json(report)
        return 2
    _json(report)
    return 0 if report["live_control_verified"] else 2


def command_app_doctor(args: argparse.Namespace) -> int:
    """Observe or draft-test a configured AI app without exposing chat content."""

    from .app_doctor import AppDoctorFailure, run_app_doctor

    report: dict[str, Any] = {
        "app": args.app,
        "mode": "draft-smoke" if args.draft_smoke else "observe-only",
        "observe_succeeded": False,
    }
    if platform.system() != "Windows":
        report.update(
            error_code="UNSUPPORTED_PLATFORM",
            safe_message="app-doctor requires an interactive Windows desktop",
        )
        _json(report)
        return 2
    try:
        settings = load_settings(args.config, allow_missing=False)
        control = settings.computer_control
        if not control.enabled:
            raise AppDoctorFailure(
                "COMPUTER_CONTROL_DISABLED",
                "Computer control is disabled in the selected configuration",
            )
        if control.backend != "local_agent" or control.driver != "windows_uia":
            raise AppDoctorFailure(
                "APP_DOCTOR_BACKEND_UNSUPPORTED",
                "app-doctor requires the local_agent/windows_uia backend",
            )
        if settings.execution.dry_run:
            raise AppDoctorFailure(
                "DRY_RUN_ENABLED",
                "app-doctor requires execution.dry_run=false",
            )
        report = run_app_doctor(
            settings,
            app=args.app,
            draft_smoke=bool(args.draft_smoke),
        )
    except AppDoctorFailure as exc:
        report.update(
            error_code=exc.error_code,
            safe_message=exc.safe_message,
        )
        _json(report)
        return 2
    except Exception as exc:
        # Exception text may contain a title, path, UI label, or driver detail.
        # Keep terminal output diagnosable without copying that private text.
        report.update(
            error_code="APP_DOCTOR_FAILED",
            exception_type=type(exc).__name__,
            safe_message="The local application diagnostic failed",
        )
        _json(report)
        return 2
    _json(report)
    if args.draft_smoke:
        return 0 if report.get("draft_smoke", {}).get("verified") is True else 2
    return 0 if report.get("observe_succeeded") is True else 2


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
    _json(
        {
            "log_file": path.name,
            "event_count": len(events),
            "events": events,
        }
    )
    return 0


def command_diagnose_last(args: argparse.Namespace) -> int:
    path = Path(args.path).expanduser().resolve() if args.path else default_log_path().resolve()
    event = diagnose_last_event(path)
    _json(
        {
            "found": event is not None,
            "log_file": path.name,
            "event": event,
        }
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    from .runtime import VoiceRuntime

    diagnostics = configure_diagnostics()
    runtime: VoiceRuntime | None = None
    try:
        settings = load_settings(args.config)
        runtime = VoiceRuntime(settings, _build_executor(settings), diagnostics=diagnostics)
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
                "RUNTIME_INITIALIZATION_FAILED"
                if runtime is None
                else "RUNTIME_UNEXPECTED_FAILURE"
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
        if runtime is not None:
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

    computer_doctor = subparsers.add_parser(
        "computer-doctor",
        help="Run static or opt-in live checks for the owned desktop driver",
    )
    computer_doctor.add_argument(
        "--live",
        action="store_true",
        help="Open a harmless owned fixture, type a random Unicode token, and read it back",
    )
    computer_doctor.set_defaults(func=command_computer_doctor)

    app_doctor = subparsers.add_parser(
        "app-doctor",
        help="Observe Claude/Codex or write one locally verified unsent draft",
    )
    app_doctor.add_argument("--app", required=True, choices=["claude", "codex"])
    app_doctor_mode = app_doctor.add_mutually_exclusive_group()
    app_doctor_mode.add_argument(
        "--observe-only",
        action="store_true",
        help="Inspect safe UIA statistics without performing an action (default)",
    )
    app_doctor_mode.add_argument(
        "--draft-smoke",
        action="store_true",
        help="Type one random draft into the verified composer without sending it",
    )
    app_doctor.set_defaults(func=command_app_doctor)

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

    logs = subparsers.add_parser("logs", help="Read privacy-bounded local diagnostic events")
    logs.add_argument(
        "--tail",
        type=int,
        nargs="?",
        const=DEFAULT_TAIL_COUNT,
        default=DEFAULT_TAIL_COUNT,
        choices=range(1, MAX_TAIL_COUNT + 1),
        metavar="N",
        help=f"Show the newest N events (default {DEFAULT_TAIL_COUNT})",
    )
    logs.add_argument("--path", help=argparse.SUPPRESS)
    logs.set_defaults(func=command_logs)

    diagnose_last = subparsers.add_parser(
        "diagnose-last",
        help="Show the newest privacy-bounded failure event",
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
