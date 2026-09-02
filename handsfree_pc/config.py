"""Configuration: the voice front-end (``app``, ``speech``), privacy, and the Kimi executor."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import FeedbackMode
from .normalize import compact_text

_ASR_CONTEXT = (
    "语音控制命令可能包含 Claude、Codex、ChatGPT、Chrome、资源管理器、微信、"
    "Chat and Cowork、Design 和 over。"
)
_ASR_HOTWORDS = (
    "Claude Codex ChatGPT 聊天GPT Chrome 资源管理器 文件资源管理器 微信 WeChat "
    "Chat and Cowork Design over"
)

DEFAULT_CONFIG: dict[str, Any] = {
    "app": {
        "language": "zh-CN",
        "wake_phrases": ["开始语音操作"],
        "end_session_phrases": ["结束语音操作"],
        "stop_phrases": [
            "立即停止所有操作",
            "取消所有操作",
            "停止所有操作",
            "电脑停止",
        ],
        "resume_phrases": ["恢复语音操作", "恢复监听", "继续队列", "恢复队列"],
        "prompt_delimiters": ["over"],
        "feedback_mode": "overlay",
        # How many spoken commands may wait for Kimi, and how long one may be.
        "max_queue_size": 8,
        "max_prompt_chars": 4000,
        # continue: later queued commands still run after one fails.
        # pause: the queue waits until you say a resume phrase.
        "failure_policy": "continue",
        # Release the microphone while another app (a meeting, a call) is
        # capturing, and resume automatically when it stops.
        "auto_pause_when_microphone_busy": True,
        "microphone_guard_poll_seconds": 3.0,
        # Executable names or full paths that never count as a meeting.
        "microphone_guard_ignore": [],
    },
    "privacy": {
        # Keep the exact local ASR text in %LOCALAPPDATA%\\HandsFreePC\\transcripts.
        "save_transcripts": False,
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
                "开始 语音 操作",
                "结束 语音 操作",
                "立即 停止 所有 操作",
                "取消 所有 操作",
                "停止 所有 操作",
                "电脑 停止",
                "恢复 语音 操作",
                "恢复 监听",
                "继续 队列",
                "恢复 队列",
            ],
            "phrase_window_seconds": 5.0,
        },
        "delimiter": {
            "backend": "vosk",
            "model_path": "models/vosk-model-small-en-us-0.15",
            "grammar": ["over"],
            "phrase_window_seconds": 2.0,
        },
        "command": {
            "backend": "sensevoice",
            "model_path": "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
            "model": "large-v3-turbo",
            "language": "auto",
            "use_itn": True,
            "num_threads": 4,
            "provider": "cpu",
            "device": "auto",
            "compute_type": "auto",
            "beam_size": 5,
            "initial_prompt": _ASR_CONTEXT,
            "hotwords": _ASR_HOTWORDS,
        },
        "fallback": {
            "backend": "none",
            "model": "large-v3-turbo",
            "device": "auto",
            "compute_type": "auto",
            "language": "zh",
            "beam_size": 5,
            "initial_prompt": _ASR_CONTEXT,
            "hotwords": _ASR_HOTWORDS,
        },
    },
    "kimi": {
        # Kimi Code CLI: a name on PATH or a full path to kimi.exe.
        "executable": "kimi",
        # Where Kimi runs; null means your home directory.
        "working_directory": None,
        # null keeps the CLI's default model (needs vision for screenshots).
        "model": None,
        # Extra skills directory passed as --skills-dir; null uses Kimi's own.
        "skills_dir": None,
        # Replace the built-in instructions Kimi receives before every command.
        "preamble_file": None,
        # Continue the same Kimi session across commands.
        "resume_session": False,
        # One command may take several minutes (screenshots plus model calls).
        "timeout_seconds": 600,
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


def _require_string_list(mapping: dict[str, Any], key: str, *, section: str) -> list[str]:
    value = mapping.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{section}.{key} must be a YAML list of strings")
    return list(value)


def _optional_string(mapping: dict[str, Any], key: str, *, section: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{section}.{key} must be a non-empty string or null")
    return value.strip()


def _optional_path(
    mapping: dict[str, Any], key: str, *, section: str, base_dir: Path
) -> Path | None:
    value = _optional_string(mapping, key, section=section)
    return _expand_path(value, base_dir=base_dir) if value else None


def _number(mapping: dict[str, Any], key: str, *, section: str) -> float:
    value = mapping.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{section}.{key} must be a number")
    return float(value)


@dataclass(slots=True)
class AppSettings:
    language: str
    wake_phrases: list[str]
    end_session_phrases: list[str]
    stop_phrases: list[str]
    resume_phrases: list[str]
    prompt_delimiters: list[str]
    feedback_mode: FeedbackMode
    max_queue_size: int = 8
    max_prompt_chars: int = 4000
    failure_policy: str = "continue"
    auto_pause_when_microphone_busy: bool = True
    microphone_guard_poll_seconds: float = 3.0
    microphone_guard_ignore: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PrivacySettings:
    save_transcripts: bool


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
    delimiter: dict[str, Any]
    command: dict[str, Any]
    fallback: dict[str, Any]


@dataclass(slots=True)
class KimiSettings:
    executable: str = "kimi"
    working_directory: Path | None = None
    model: str | None = None
    skills_dir: Path | None = None
    preamble_file: Path | None = None
    resume_session: bool = False
    timeout_seconds: float = 600.0


@dataclass(slots=True)
class Settings:
    config_path: Path
    app: AppSettings
    privacy: PrivacySettings
    speech: SpeechSettings
    kimi: KimiSettings


def discover_config_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    if env_path := os.environ.get("HANDSFREEPC_CONFIG"):
        return Path(env_path).expanduser().resolve()
    local = Path.cwd() / "config.local.yaml"
    if local.exists():
        return local.resolve()
    return (Path.cwd() / "config.yaml").resolve()


def load_settings(path: str | Path | None = None, *, allow_missing: bool = False) -> Settings:
    """Load YAML on top of the packaged defaults. Unknown sections are ignored."""

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
    for section in DEFAULT_CONFIG:
        if section in loaded and not isinstance(loaded[section], dict):
            raise ValueError(f"{section} must be a mapping")
    raw = _deep_merge(DEFAULT_CONFIG, {k: v for k, v in loaded.items() if k in DEFAULT_CONFIG})
    base_dir = config_path.parent

    app_raw = raw["app"]
    privacy_raw = raw["privacy"]
    speech_raw = raw["speech"]
    kimi_raw = raw["kimi"]

    _require_bool(speech_raw["command"], "use_itn", section="speech.command")
    wake_settings = copy.deepcopy(speech_raw["wake"])
    wake_settings["grammar"] = _require_string_list(
        speech_raw["wake"], "grammar", section="speech.wake"
    )
    delimiter_settings = copy.deepcopy(speech_raw["delimiter"])
    delimiter_settings["grammar"] = _require_string_list(
        speech_raw["delimiter"], "grammar", section="speech.delimiter"
    )

    settings = Settings(
        config_path=config_path,
        app=AppSettings(
            language=str(app_raw["language"]),
            wake_phrases=_require_string_list(app_raw, "wake_phrases", section="app"),
            end_session_phrases=_require_string_list(app_raw, "end_session_phrases", section="app"),
            stop_phrases=_require_string_list(app_raw, "stop_phrases", section="app"),
            resume_phrases=_require_string_list(app_raw, "resume_phrases", section="app"),
            prompt_delimiters=_require_string_list(app_raw, "prompt_delimiters", section="app"),
            feedback_mode=FeedbackMode(app_raw["feedback_mode"]),
            max_queue_size=int(_number(app_raw, "max_queue_size", section="app")),
            max_prompt_chars=int(_number(app_raw, "max_prompt_chars", section="app")),
            failure_policy=str(app_raw["failure_policy"]).lower(),
            auto_pause_when_microphone_busy=_require_bool(
                app_raw, "auto_pause_when_microphone_busy", section="app"
            ),
            microphone_guard_poll_seconds=_number(
                app_raw, "microphone_guard_poll_seconds", section="app"
            ),
            microphone_guard_ignore=_require_string_list(
                app_raw, "microphone_guard_ignore", section="app"
            ),
        ),
        privacy=PrivacySettings(
            save_transcripts=_require_bool(privacy_raw, "save_transcripts", section="privacy"),
        ),
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
            wake=wake_settings,
            delimiter=delimiter_settings,
            command=copy.deepcopy(speech_raw["command"]),
            fallback=copy.deepcopy(speech_raw["fallback"]),
        ),
        kimi=KimiSettings(
            executable=str(kimi_raw.get("executable") or "").strip(),
            working_directory=_optional_path(
                kimi_raw, "working_directory", section="kimi", base_dir=base_dir
            ),
            model=_optional_string(kimi_raw, "model", section="kimi"),
            skills_dir=_optional_path(kimi_raw, "skills_dir", section="kimi", base_dir=base_dir),
            preamble_file=_optional_path(
                kimi_raw, "preamble_file", section="kimi", base_dir=base_dir
            ),
            resume_session=_require_bool(kimi_raw, "resume_session", section="kimi"),
            timeout_seconds=_number(kimi_raw, "timeout_seconds", section="kimi"),
        ),
    )
    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    app = settings.app
    phrase_groups = {
        "wake phrase": app.wake_phrases,
        "end-session phrase": app.end_session_phrases,
        "stop phrase": app.stop_phrases,
        "resume phrase": app.resume_phrases,
        "prompt delimiter": app.prompt_delimiters,
    }
    for label, values in phrase_groups.items():
        if not values or any(not compact_text(value) for value in values):
            raise ValueError(f"At least one non-empty {label} is required")
    if app.max_queue_size < 1:
        raise ValueError("app.max_queue_size must be at least 1")
    if not 1 <= app.max_prompt_chars <= 8000:
        raise ValueError("app.max_prompt_chars must be between 1 and 8000")
    if app.failure_policy not in {"pause", "continue"}:
        raise ValueError("app.failure_policy must be pause or continue")
    if app.microphone_guard_poll_seconds <= 0:
        raise ValueError("app.microphone_guard_poll_seconds must be positive")
    for section, values in (
        ("speech.wake", settings.speech.wake),
        ("speech.delimiter", settings.speech.delimiter),
    ):
        try:
            phrase_window_seconds = float(values.get("phrase_window_seconds", 0))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{section}.phrase_window_seconds must be a positive number") from exc
        if phrase_window_seconds <= 0:
            raise ValueError(f"{section}.phrase_window_seconds must be a positive number")
        if str(values.get("backend", "")).lower() != "vosk":
            raise ValueError(f"{section}.backend must be vosk")
    if settings.speech.sample_rate not in {8000, 16000, 24000, 32000, 44100, 48000}:
        raise ValueError("Unsupported sample rate")
    if not settings.kimi.executable:
        raise ValueError("kimi.executable must be a non-empty string")
    if settings.kimi.timeout_seconds <= 0:
        raise ValueError("kimi.timeout_seconds must be positive")
