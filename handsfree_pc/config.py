from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .models import FeedbackMode
from .normalize import compact_text

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
        "prompt_delimiters": ["over"],
        "control_prefixes": ["电脑"],
        "awake_timeout_seconds": 0,
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
                "开始 语音 操作",
                "结束 语音 操作",
                "立即 停止 所有 操作",
                "取消 所有 操作",
                "停止 所有 操作",
                "电脑 停止",
                "恢复 语音 操作",
                "恢复 监听",
                "确认 执行",
                "取消 操作",
                "不要 执行",
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
            "initial_prompt": (
                "语音控制命令可能包含 "
                "Claude、Codex、Chat and Cowork、Design 和 over。"
            ),
            "hotwords": "Claude Codex Chat and Cowork Design over",
        },
        "fallback": {
            "backend": "none",
            "model": "large-v3-turbo",
            "device": "auto",
            "compute_type": "auto",
            "language": "zh",
            "beam_size": 5,
            "initial_prompt": (
                "语音控制命令可能包含 "
                "Claude、Codex、Chat and Cowork、Design 和 over。"
            ),
            "hotwords": "Claude Codex Chat and Cowork Design over",
        },
    },
    "planner": {
        "enabled": False,
        "backend": "claude",
        "timeout_seconds": 90,
        "model": None,
        "codex_executable": "codex",
        "claude_executable": "claude",
    },
    "computer_control": {
        "enabled": False,
        "backend": "local_agent",
        "driver": "windows_uia",
        "planner_backend": "claude",
        "safety_profile": "strict",
        "allow_screen_context_to_cloud": False,
        "allow_codex_cli_host_read": False,
        "allow_legacy_codex_computer_use": False,
        "timeout_seconds": 600,
        "max_steps": 20,
        "max_observation_chars": 24000,
        "max_queue_size": 8,
        "max_prompt_chars": 4000,
        "failure_policy": "pause",
        "end_policy": "drain",
        "working_directory": "runtime/computer-control",
        "codex_executable": "codex",
        "claude_executable": "claude",
        "model": None,
        "open_computer_use_executable": "open-computer-use",
        "open_computer_use_args": ["mcp"],
        "allow_experimental_driver": False,
        "allow_coordinate_actions": False,
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
            "mode_names": {"chat": ["Chat"], "code": ["Code"]},
            "include_control_types": [
                "Button",
                "TabItem",
                "MenuItem",
                "ListItem",
                "TreeItem",
                "Edit",
                "ComboBox",
                "CheckBox",
                "RadioButton",
                "Dialog",
                "Window",
            ],
            "content_control_types": ["Text", "Document", "Pane", "Group"],
            "drop_long_content": True,
            "max_control_name_chars": 500,
            "max_content_chars": 1000,
            "max_content_nodes": 80,
            "composer_names": [
                "Prompt",
                "Message",
                "Ask anything",
                "Type a message",
                "输入消息",
                "输入提示词",
            ],
        },
        "claude": {
            "process_names": ["claude.exe"],
            "executable": None,
            "title_patterns": ["Claude"],
            "search_hotkey": None,
            "native_voice_hotkey": None,
            "voice_button_names": [],
            "mode_names": {
                "chat": ["Chat and Cowork", "Chat"],
                "cowork": ["Chat and Cowork", "Cowork"],
                "code": ["Code"],
                "design": ["Design"],
            },
            "include_control_types": [
                "Button",
                "TabItem",
                "MenuItem",
                "ListItem",
                "TreeItem",
                "Edit",
                "ComboBox",
                "CheckBox",
                "RadioButton",
                "Dialog",
                "Window",
            ],
            "content_control_types": ["Text", "Document", "Pane", "Group"],
            "drop_long_content": True,
            "max_control_name_chars": 500,
            "max_content_chars": 1000,
            "max_content_nodes": 80,
            "composer_names": [
                "Prompt",
                "Message",
                "Ask Claude",
                "Type a message",
                "输入消息",
                "询问 Claude",
            ],
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


def _require_string_list(mapping: dict[str, Any], key: str, *, section: str) -> list[str]:
    value = mapping.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{section}.{key} must be a YAML list of strings")
    return list(value)


def _require_string_list_mapping(
    mapping: dict[str, Any], key: str, *, section: str
) -> dict[str, list[str]]:
    value = mapping.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{section}.{key} must be a YAML mapping of string lists")
    result: dict[str, list[str]] = {}
    for item_key, item_value in value.items():
        if (
            not isinstance(item_key, str)
            or not item_key.strip()
            or not isinstance(item_value, list)
            or not item_value
            or any(not isinstance(item, str) or not item.strip() for item in item_value)
        ):
            raise ValueError(f"{section}.{key} must map non-empty strings to string lists")
        result[item_key.strip().casefold()] = [item.strip() for item in item_value]
    return result


def _optional_string_list(
    mapping: dict[str, Any],
    key: str,
    *,
    section: str,
) -> list[str]:
    value = mapping.get(key, [])
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError(f"{section}.{key} must be a YAML list of non-empty strings")
    return [item.strip() for item in value]


def _optional_bool(
    mapping: dict[str, Any],
    key: str,
    *,
    section: str,
    default: bool,
) -> bool:
    value = mapping.get(key, default)
    if type(value) is not bool:
        raise ValueError(f"{section}.{key} must be a YAML boolean, not a quoted string")
    return value


def _optional_int_in_range(
    mapping: dict[str, Any],
    key: str,
    *,
    section: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = mapping.get(key, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{section}.{key} must be between {minimum} and {maximum}")
    return value


@dataclass(slots=True)
class AppSettings:
    language: str
    wake_phrases: list[str]
    end_session_phrases: list[str]
    stop_phrases: list[str]
    prompt_delimiters: list[str]
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
    delimiter: dict[str, Any]
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
class ComputerControlSettings:
    enabled: bool
    backend: str
    driver: str
    planner_backend: str
    safety_profile: str
    allow_screen_context_to_cloud: bool
    allow_codex_cli_host_read: bool
    allow_legacy_codex_computer_use: bool
    timeout_seconds: float
    max_steps: int
    max_observation_chars: int
    max_queue_size: int
    max_prompt_chars: int
    failure_policy: str
    end_policy: str
    working_directory: Path
    codex_executable: str
    claude_executable: str
    model: str | None
    open_computer_use_executable: str
    open_computer_use_args: list[str]
    allow_experimental_driver: bool
    allow_coordinate_actions: bool


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
    mode_names: dict[str, list[str]] = field(default_factory=dict)
    # Empty observation fields retain the pre-profile driver behavior. This is
    # intentional so existing direct AppProfile(...) construction remains
    # source- and behavior-compatible; packaged Claude/Codex profiles opt in.
    include_control_types: list[str] = field(default_factory=list)
    content_control_types: list[str] = field(default_factory=list)
    drop_long_content: bool = False
    max_control_name_chars: int = 500
    max_content_chars: int = 4000
    max_content_nodes: int = 500
    composer_names: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Settings:
    config_path: Path
    app: AppSettings
    privacy: PrivacySettings
    speech: SpeechSettings
    planner: PlannerSettings
    computer_control: ComputerControlSettings
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
    computer_control_raw = raw["computer_control"]
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
    computer_control_enabled = _require_bool(
        computer_control_raw, "enabled", section="computer_control"
    )
    allow_screen_context = _require_bool(
        computer_control_raw,
        "allow_screen_context_to_cloud",
        section="computer_control",
    )
    allow_codex_cli_host_read = _require_bool(
        computer_control_raw,
        "allow_codex_cli_host_read",
        section="computer_control",
    )
    allow_legacy_codex_computer_use = _require_bool(
        computer_control_raw,
        "allow_legacy_codex_computer_use",
        section="computer_control",
    )
    allow_experimental_driver = _require_bool(
        computer_control_raw,
        "allow_experimental_driver",
        section="computer_control",
    )
    allow_coordinate_actions = _require_bool(
        computer_control_raw,
        "allow_coordinate_actions",
        section="computer_control",
    )
    execution_dry_run = _require_bool(execution_raw, "dry_run", section="execution")
    _require_bool(speech_raw["command"], "use_itn", section="speech.command")

    wake_phrases = _require_string_list(app_raw, "wake_phrases", section="app")
    end_session_phrases = _require_string_list(app_raw, "end_session_phrases", section="app")
    stop_phrases = _require_string_list(app_raw, "stop_phrases", section="app")
    prompt_delimiters = _require_string_list(app_raw, "prompt_delimiters", section="app")
    control_prefixes = _require_string_list(app_raw, "control_prefixes", section="app")
    confirmation_phrases = _require_string_list(
        execution_raw, "confirmation_phrases", section="execution"
    )
    cancellation_phrases = _require_string_list(
        execution_raw, "cancellation_phrases", section="execution"
    )
    blocked_keywords = _require_string_list(execution_raw, "blocked_keywords", section="execution")
    wake_settings = copy.deepcopy(speech_raw["wake"])
    wake_settings["grammar"] = _require_string_list(
        speech_raw["wake"], "grammar", section="speech.wake"
    )
    delimiter_settings = copy.deepcopy(speech_raw["delimiter"])
    delimiter_settings["grammar"] = _require_string_list(
        speech_raw["delimiter"], "grammar", section="speech.delimiter"
    )

    aliases = {
        str(name): _expand_path(str(value), base_dir=base_dir)
        for name, value in execution_raw["path_aliases"].items()
    }
    roots = [_expand_path(str(value), base_dir=base_dir) for value in execution_raw["search_roots"]]
    profiles: dict[str, AppProfile] = {}
    for name, value in raw["apps"].items():
        if not isinstance(value, dict):
            raise ValueError(f"apps.{name} must be a mapping")
        executable = value.get("executable")
        profiles[name.lower()] = AppProfile(
            name=name.lower(),
            process_names=_require_string_list(value, "process_names", section=f"apps.{name}"),
            executable=_expand_path(str(executable), base_dir=base_dir) if executable else None,
            title_patterns=_require_string_list(value, "title_patterns", section=f"apps.{name}"),
            search_hotkey=value.get("search_hotkey"),
            native_voice_hotkey=value.get("native_voice_hotkey"),
            voice_button_names=_require_string_list(
                value, "voice_button_names", section=f"apps.{name}"
            ),
            mode_names=_require_string_list_mapping(
                value,
                "mode_names",
                section=f"apps.{name}",
            ),
            include_control_types=_optional_string_list(
                value,
                "include_control_types",
                section=f"apps.{name}",
            ),
            content_control_types=_optional_string_list(
                value,
                "content_control_types",
                section=f"apps.{name}",
            ),
            drop_long_content=_optional_bool(
                value,
                "drop_long_content",
                section=f"apps.{name}",
                default=False,
            ),
            max_control_name_chars=_optional_int_in_range(
                value,
                "max_control_name_chars",
                section=f"apps.{name}",
                default=500,
                minimum=1,
                maximum=1024,
            ),
            max_content_chars=_optional_int_in_range(
                value,
                "max_content_chars",
                section=f"apps.{name}",
                default=4000,
                minimum=1,
                maximum=16000,
            ),
            max_content_nodes=_optional_int_in_range(
                value,
                "max_content_nodes",
                section=f"apps.{name}",
                default=500,
                minimum=0,
                maximum=2000,
            ),
            composer_names=_optional_string_list(
                value,
                "composer_names",
                section=f"apps.{name}",
            ),
        )

    settings = Settings(
        config_path=config_path,
        app=AppSettings(
            language=str(app_raw["language"]),
            wake_phrases=wake_phrases,
            end_session_phrases=end_session_phrases,
            stop_phrases=stop_phrases,
            prompt_delimiters=prompt_delimiters,
            control_prefixes=control_prefixes,
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
            wake=wake_settings,
            delimiter=delimiter_settings,
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
        computer_control=ComputerControlSettings(
            enabled=computer_control_enabled,
            backend=str(computer_control_raw["backend"]).lower(),
            driver=str(computer_control_raw["driver"]).lower(),
            planner_backend=str(computer_control_raw["planner_backend"]).lower(),
            safety_profile=str(computer_control_raw["safety_profile"]).lower(),
            allow_screen_context_to_cloud=allow_screen_context,
            allow_codex_cli_host_read=allow_codex_cli_host_read,
            allow_legacy_codex_computer_use=allow_legacy_codex_computer_use,
            timeout_seconds=float(computer_control_raw["timeout_seconds"]),
            max_steps=int(computer_control_raw["max_steps"]),
            max_observation_chars=int(computer_control_raw["max_observation_chars"]),
            max_queue_size=int(computer_control_raw["max_queue_size"]),
            max_prompt_chars=int(computer_control_raw["max_prompt_chars"]),
            failure_policy=str(computer_control_raw["failure_policy"]).lower(),
            end_policy=str(computer_control_raw["end_policy"]).lower(),
            working_directory=_expand_path(
                str(computer_control_raw["working_directory"]), base_dir=base_dir
            ),
            codex_executable=str(computer_control_raw["codex_executable"]),
            claude_executable=str(computer_control_raw["claude_executable"]),
            model=computer_control_raw.get("model"),
            open_computer_use_executable=str(computer_control_raw["open_computer_use_executable"]),
            open_computer_use_args=_require_string_list(
                computer_control_raw,
                "open_computer_use_args",
                section="computer_control",
            ),
            allow_experimental_driver=allow_experimental_driver,
            allow_coordinate_actions=allow_coordinate_actions,
        ),
        execution=ExecutionSettings(
            dry_run=execution_dry_run,
            ambiguity_threshold=float(execution_raw["ambiguity_threshold"]),
            max_search_results=int(execution_raw["max_search_results"]),
            confirmation_timeout_seconds=float(execution_raw["confirmation_timeout_seconds"]),
            confirmation_phrases=confirmation_phrases,
            cancellation_phrases=cancellation_phrases,
            blocked_keywords=blocked_keywords,
            path_aliases=aliases,
            search_roots=roots,
        ),
        apps=profiles,
    )
    _validate(settings)
    return settings


def _validate(settings: Settings) -> None:
    phrase_groups = {
        "wake phrase": settings.app.wake_phrases,
        "end-session phrase": settings.app.end_session_phrases,
        "stop phrase": settings.app.stop_phrases,
        "prompt delimiter": settings.app.prompt_delimiters,
        "confirmation phrase": settings.execution.confirmation_phrases,
        "cancellation phrase": settings.execution.cancellation_phrases,
    }
    for label, values in phrase_groups.items():
        if not values or any(not compact_text(value) for value in values):
            raise ValueError(f"At least one non-empty {label} is required")
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
    if not 0 < settings.execution.ambiguity_threshold <= 1:
        raise ValueError("ambiguity_threshold must be in (0, 1]")
    if settings.execution.confirmation_timeout_seconds <= 0:
        raise ValueError("confirmation_timeout_seconds must be positive")
    if settings.planner.backend not in {"codex", "claude", "none"}:
        raise ValueError("planner.backend must be codex, claude, or none")
    if settings.planner.enabled and not settings.privacy.allow_cloud_planner:
        raise ValueError(
            "planner.enabled requires privacy.allow_cloud_planner=true because "
            "transcripts leave the machine"
        )
    if settings.computer_control.backend not in {"local_agent", "legacy_codex_cli"}:
        raise ValueError("computer_control.backend must be local_agent or legacy_codex_cli")
    if settings.computer_control.driver not in {
        "windows_uia",
        "open_computer_use",
        "none",
    }:
        raise ValueError("computer_control.driver must be windows_uia, open_computer_use, or none")
    if settings.computer_control.planner_backend not in {
        "codex_cli_best_effort",
        "claude",
        "none",
    }:
        raise ValueError(
            "computer_control.planner_backend must be codex_cli_best_effort, claude, or none"
        )
    if settings.computer_control.safety_profile not in {"strict", "personal_trusted"}:
        raise ValueError(
            "computer_control.safety_profile must be strict or personal_trusted"
        )
    if (
        settings.computer_control.driver == "open_computer_use"
        and not settings.computer_control.allow_experimental_driver
    ):
        raise ValueError(
            "open_computer_use is experimental on Chinese Windows; set "
            "computer_control.allow_experimental_driver=true to opt in"
        )
    if settings.computer_control.max_queue_size < 1:
        raise ValueError("computer_control.max_queue_size must be at least 1")
    if not 1 <= settings.computer_control.max_prompt_chars <= 8000:
        raise ValueError("computer_control.max_prompt_chars must be between 1 and 8000")
    if settings.computer_control.timeout_seconds <= 0:
        raise ValueError("computer_control.timeout_seconds must be positive")
    if not 1 <= settings.computer_control.max_steps <= 100:
        raise ValueError("computer_control.max_steps must be between 1 and 100")
    if not 1000 <= settings.computer_control.max_observation_chars <= 100000:
        raise ValueError("computer_control.max_observation_chars must be between 1000 and 100000")
    if settings.computer_control.failure_policy not in {"pause", "continue"}:
        raise ValueError("computer_control.failure_policy must be pause or continue")
    if settings.computer_control.end_policy != "drain":
        raise ValueError("computer_control.end_policy must be drain")
    uses_cloud_desktop_planner = settings.computer_control.backend == "legacy_codex_cli" or (
        settings.computer_control.backend == "local_agent"
        and settings.computer_control.planner_backend != "none"
    )
    if (
        settings.computer_control.enabled
        and uses_cloud_desktop_planner
        and not settings.privacy.allow_cloud_planner
    ):
        raise ValueError(
            "computer_control.enabled requires privacy.allow_cloud_planner=true because "
            "voice commands leave the machine"
        )
    if settings.computer_control.enabled and settings.execution.dry_run:
        raise ValueError(
            "computer_control.enabled requires execution.dry_run=false because Computer Use "
            "performs real desktop actions"
        )
    if (
        settings.computer_control.enabled
        and uses_cloud_desktop_planner
        and not settings.computer_control.allow_screen_context_to_cloud
    ):
        raise ValueError(
            "computer_control.enabled requires "
            "computer_control.allow_screen_context_to_cloud=true because the completed task, "
            "authorized app summary, and exactly named UI control labels may leave the machine"
        )
    uses_any_codex_cli = (settings.planner.enabled and settings.planner.backend == "codex") or (
        settings.computer_control.enabled
        and (
            settings.computer_control.backend == "legacy_codex_cli"
            or settings.computer_control.planner_backend == "codex_cli_best_effort"
        )
    )
    if uses_any_codex_cli and not settings.computer_control.allow_codex_cli_host_read:
        raise ValueError(
            "Codex CLI has no complete no-tools mode; set "
            "computer_control.allow_codex_cli_host_read=true only after accepting that the "
            "subscription CLI may read other files visible to the current Windows account"
        )
    if (
        settings.computer_control.enabled
        and settings.computer_control.backend == "legacy_codex_cli"
        and not settings.computer_control.allow_legacy_codex_computer_use
    ):
        raise ValueError(
            "legacy_codex_cli lets a Codex agent use broader computer tools without the 0.3 "
            "local verifier; set computer_control.allow_legacy_codex_computer_use=true only "
            "after separately accepting that compatibility boundary"
        )
