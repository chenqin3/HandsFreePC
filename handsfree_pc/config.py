from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import FeedbackMode

DEFAULT_CONFIG: dict[str, Any] = {
    "app": {
        "language": "zh-CN",
        "wake_phrases": ["现在开始语音操作", "电脑开始操作"],
        "stop_phrases": ["停止所有操作", "电脑停止"],
        "control_prefixes": ["电脑"],
        "awake_timeout_seconds": 20,
        "feedback_mode": "overlay",
    },
    "privacy": {
        "save_audio": False,
        "save_transcripts": False,
        "redact_paths_in_logs": True,
        "allow_cloud_planner": False,
    },
    "speech": {
        "sample_rate": 16000,
        "input_device": None,
        "pre_roll_seconds": 1.2,
        "trailing_silence_seconds": 0.8,
        "max_utterance_seconds": 25,
        "min_speech_seconds": 0.25,
        "energy_threshold": 0.012,
        "noise_multiplier": 3.0,
        "vad": {
            "backend": "silero",
            "model_path": "models/silero-vad-v6.2.1/silero_vad.onnx",
            "threshold": 0.5,
            "min_silence_duration": 0.5,
            "min_speech_duration": 0.15,
            "max_speech_duration": 30.0,
            "window_size": 512,
        },
        "wake": {
            "backend": "vosk",
            "model_path": "models/vosk-model-small-cn-0.22",
            "grammar": [
                "现在 开始 语音 操作",
                "电脑 开始 操作",
                "停止 所有 操作",
                "电脑 停止",
                "恢复 语音 操作",
                "恢复 监听",
            ],
        },
        "command": {
            "backend": "sensevoice",
            "model_path": "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
            "language": "zh",
            "use_itn": True,
            "num_threads": 4,
            "provider": "cpu",
        },
        "fallback": {
            "backend": "none",
            "model": "large-v3-turbo",
            "device": "auto",
            "compute_type": "auto",
        },
    },
    "planner": {
        "enabled": False,
        "backend": "codex",
        "timeout_seconds": 90,
        "model": None,
        "codex_executable": "codex",
        "claude_executable": "claude",
    },
    "execution": {
        "dry_run": True,
        "ambiguity_threshold": 0.78,
        "max_search_results": 5,
        "confirmation_timeout_seconds": 15,
        "confirmation_phrases": ["确认执行"],
        "cancellation_phrases": ["取消操作", "不要执行"],
        "blocked_keywords": ["删除", "格式化", "付款", "转账", "输入密码"],
        "path_aliases": {
            "桌面": "%USERPROFILE%\\Desktop",
            "文档": "%USERPROFILE%\\Documents",
            "下载": "%USERPROFILE%\\Downloads",
        },
        "search_roots": [],
    },
    "apps": {
        "codex": {
            "process_names": ["ChatGPT.exe", "codex.exe"],
            "executable": None,
            "title_patterns": ["ChatGPT", "Codex"],
            "search_hotkey": None,
            "native_voice_hotkey": None,
            "voice_button_names": [],
        },
        "claude": {
            "process_names": ["claude.exe"],
            "executable": None,
            "title_patterns": ["Claude"],
            "search_hotkey": None,
            "native_voice_hotkey": None,
            "voice_button_names": [],
        },
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _expand_path(value: str, *, base_dir: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    path = Path(expanded)
    return path if path.is_absolute() else (base_dir / path).resolve()


def _require_bool(mapping: dict[str, Any], key: str, *, section: str) -> bool:
    value = mapping.get(key)
    if type(value) is not bool:
        raise ValueError(f"{section}.{key} must be a YAML boolean, not a quoted string")
    return value


@dataclass(slots=True)
class AppSettings:
    language: str
    wake_phrases: list[str]
    stop_phrases: list[str]
    control_prefixes: list[str]
    awake_timeout_seconds: float
    feedback_mode: FeedbackMode


@dataclass(slots=True)
class PrivacySettings:
    save_audio: bool
    save_transcripts: bool
    redact_paths_in_logs: bool
    allow_cloud_planner: bool


@dataclass(slots=True)
class SpeechSettings:
    sample_rate: int
    input_device: int | str | None
    pre_roll_seconds: float
    trailing_silence_seconds: float
    max_utterance_seconds: float
    min_speech_seconds: float
    energy_threshold: float
    noise_multiplier: float
    vad: dict[str, Any]
    wake: dict[str, Any]
    command: dict[str, Any]
    fallback: dict[str, Any]


@dataclass(slots=True)
class PlannerSettings:
    enabled: bool
    backend: str
    timeout_seconds: float
    model: str | None
    codex_executable: str
    claude_executable: str


@dataclass(slots=True)
class ExecutionSettings:
    dry_run: bool
    ambiguity_threshold: float
    max_search_results: int
    confirmation_timeout_seconds: float
    confirmation_phrases: list[str]
    cancellation_phrases: list[str]
    blocked_keywords: list[str]
    path_aliases: dict[str, Path]
    search_roots: list[Path]


@dataclass(slots=True)
class AppProfile:
    name: str
    process_names: list[str]
    executable: Path | None
    title_patterns: list[str]
    search_hotkey: str | None
    native_voice_hotkey: str | None
    voice_button_names: list[str]


@dataclass(slots=True)
class Settings:
    config_path: Path
    app: AppSettings
    privacy: PrivacySettings
    speech: SpeechSettings
    planner: PlannerSettings
    execution: ExecutionSettings
    apps: dict[str, AppProfile] = field(default_factory=dict)


def discover_config_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if env_path := os.environ.get("HANDSFREEPC_CONFIG"):
        return Path(env_path).expanduser().resolve()
    local = Path.cwd() / "config.local.yaml"
    if local.exists():
        return local.resolve()
    user = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "HandsFreePC" / "config.yaml"
    return user.resolve()


def load_settings(path: str | Path | None = None, *, allow_missing: bool = False) -> Settings:
    config_path = discover_config_path(path)
    if config_path.exists():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("Configuration root must be a mapping")
    elif allow_missing:
        loaded = {}
    else:
        raise FileNotFoundError(
            f"Configuration not found: {config_path}. Copy config.example.yaml first."
        )
    raw = _deep_merge(DEFAULT_CONFIG, loaded)
    base_dir = config_path.parent

    app_raw = raw["app"]
    privacy_raw = raw["privacy"]
    speech_raw = raw["speech"]
    planner_raw = raw["planner"]
    execution_raw = raw["execution"]

    privacy_values = {
        key: _require_bool(privacy_raw, key, section="privacy")
        for key in (
            "save_audio",
            "save_transcripts",
            "redact_paths_in_logs",
            "allow_cloud_planner",
        )
    }
    planner_enabled = _require_bool(planner_raw, "enabled", section="planner")
    execution_dry_run = _require_bool(execution_raw, "dry_run", section="execution")
    _require_bool(speech_raw["command"], "use_itn", section="speech.command")

    aliases = {
        str(name): _expand_path(str(value), base_dir=base_dir)
        for name, value in execution_raw["path_aliases"].items()
    }
    roots = [_expand_path(str(value), base_dir=base_dir) for value in execution_raw["search_roots"]]
    profiles: dict[str, AppProfile] = {}
    for name, value in raw["apps"].items():
        executable = value.get("executable")
        profiles[name.lower()] = AppProfile(
            name=name.lower(),
            process_names=[str(item) for item in value.get("process_names", [])],
            executable=_expand_path(str(executable), base_dir=base_dir) if executable else None,
            title_patterns=[str(item) for item in value.get("title_patterns", [])],
            search_hotkey=value.get("search_hotkey"),
            native_voice_hotkey=value.get("native_voice_hotkey"),
            voice_button_names=[str(item) for item in value.get("voice_button_names", [])],
        )

    settings = Settings(
        config_path=config_path,
        app=AppSettings(
            language=str(app_raw["language"]),
            wake_phrases=[str(item) for item in app_raw["wake_phrases"]],
            stop_phrases=[str(item) for item in app_raw["stop_phrases"]],
            control_prefixes=[str(item) for item in app_raw["control_prefixes"]],
            awake_timeout_seconds=float(app_raw["awake_timeout_seconds"]),
            feedback_mode=FeedbackMode(app_raw["feedback_mode"]),
        ),
        privacy=PrivacySettings(**privacy_values),
        speech=SpeechSettings(
            sample_rate=int(speech_raw["sample_rate"]),
            input_device=speech_raw["input_device"],
            pre_roll_seconds=float(speech_raw["pre_roll_seconds"]),
            trailing_silence_seconds=float(speech_raw["trailing_silence_seconds"]),
            max_utterance_seconds=float(speech_raw["max_utterance_seconds"]),
            min_speech_seconds=float(speech_raw["min_speech_seconds"]),
            energy_threshold=float(speech_raw["energy_threshold"]),
            noise_multiplier=float(speech_raw["noise_multiplier"]),
            vad=copy.deepcopy(speech_raw["vad"]),
            wake=copy.deepcopy(speech_raw["wake"]),
            command=copy.deepcopy(speech_raw["command"]),
            fallback=copy.deepcopy(speech_raw["fallback"]),
        ),
        planner=PlannerSettings(
            enabled=planner_enabled,
            backend=str(planner_raw["backend"]).lower(),
            timeout_seconds=float(planner_raw["timeout_seconds"]),
            model=planner_raw.get("model"),
            codex_executable=str(planner_raw["codex_executable"]),
            claude_executable=str(planner_raw["claude_executable"]),
        ),
        execution=ExecutionSettings(
            dry_run=execution_dry_run,
            ambiguity_threshold=float(execution_raw["ambiguity_threshold"]),
            max_search_results=int(execution_raw["max_search_results"]),
            confirmation_timeout_seconds=float(execution_raw["confirmation_timeout_seconds"]),
            confirmation_phrases=[str(item) for item in execution_raw["confirmation_phrases"]],
            cancellation_phrases=[str(item) for item in execution_raw["cancellation_phrases"]],
            blocked_keywords=[str(item) for item in execution_raw["blocked_keywords"]],
            path_aliases=aliases,
            search_roots=roots,
        ),
        apps=profiles,
    )
    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    if not settings.app.wake_phrases:
        raise ValueError("At least one wake phrase is required")
    if settings.speech.sample_rate not in {8000, 16000, 24000, 32000, 44100, 48000}:
        raise ValueError("Unsupported sample rate")
    if not 0 < settings.execution.ambiguity_threshold <= 1:
        raise ValueError("ambiguity_threshold must be in (0, 1]")
    if settings.planner.backend not in {"codex", "claude", "none"}:
        raise ValueError("planner.backend must be codex, claude, or none")
    if settings.planner.enabled and not settings.privacy.allow_cloud_planner:
        raise ValueError(
            "planner.enabled requires privacy.allow_cloud_planner=true because "
            "transcripts leave the machine"
        )
