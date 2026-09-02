from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from handsfree_pc.config import DEFAULT_CONFIG, FeedbackMode, load_settings


def test_defaults(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.yaml", allow_missing=True)

    assert settings.privacy.save_transcripts is False
    assert settings.app.feedback_mode == FeedbackMode.OVERLAY
    assert settings.app.wake_phrases == ["开始语音操作"]
    assert settings.app.end_session_phrases == ["结束语音操作"]
    assert "电脑停止" in settings.app.stop_phrases
    assert "继续队列" in settings.app.resume_phrases
    assert settings.app.prompt_delimiters == ["over"]
    assert settings.app.failure_policy == "continue"
    assert settings.app.max_queue_size == 8
    assert settings.app.max_prompt_chars == 4000
    assert settings.app.auto_pause_when_microphone_busy is True
    assert settings.app.microphone_guard_poll_seconds == 3.0
    assert settings.app.microphone_guard_ignore == []
    assert settings.kimi.executable == "kimi"
    assert settings.kimi.working_directory is None
    assert settings.kimi.model is None
    assert settings.kimi.skills_dir is None
    assert settings.kimi.preamble_file is None
    assert settings.kimi.resume_session is False
    assert settings.kimi.timeout_seconds == 600
    assert settings.speech.command["backend"] == "sensevoice"
    assert settings.speech.fallback["backend"] == "none"
    assert settings.speech.vad["backend"] == "silero"
    assert settings.speech.delimiter["grammar"] == ["over"]
    assert settings.speech.delimiter["model_path"].endswith("vosk-model-small-en-us-0.15")
    assert "Claude" in settings.speech.command["hotwords"]
    # Every locally recognised control phrase must be in the wake detector grammar.
    grammar = {item.replace(" ", "") for item in settings.speech.wake["grammar"]}
    for phrase in (
        *settings.app.wake_phrases,
        *settings.app.end_session_phrases,
        *settings.app.stop_phrases,
        *settings.app.resume_phrases,
    ):
        assert phrase in grammar


def test_example_configuration_matches_packaged_defaults() -> None:
    example_path = Path(__file__).resolve().parents[1] / "config.example.yaml"
    example = yaml.safe_load(example_path.read_text(encoding="utf-8"))

    assert example == DEFAULT_CONFIG


def test_sections_from_older_configurations_are_ignored(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "computer_control:\n  enabled: true\n  engine: kimi_agent\n"
        "planner:\n  enabled: false\n"
        "apps:\n  codex:\n    process_names: [codex.exe]\n"
        "kimi:\n  executable: kimi-test\n",
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.kimi.executable == "kimi-test"
    assert not hasattr(settings, "computer_control")


def test_kimi_paths_resolve_from_config_directory(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "kimi:\n  working_directory: work\n  skills_dir: skills\n  preamble_file: preamble.txt\n",
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.kimi.working_directory == (tmp_path / "work").resolve()
    assert settings.kimi.skills_dir == (tmp_path / "skills").resolve()
    assert settings.kimi.preamble_file == (tmp_path / "preamble.txt").resolve()


def test_missing_delimiter_section_uses_defaults(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("speech:\n  wake:\n    phrase_window_seconds: 4.0\n", encoding="utf-8")
    settings = load_settings(config)
    assert settings.speech.wake["phrase_window_seconds"] == 4.0
    assert settings.speech.delimiter["backend"] == "vosk"
    assert settings.speech.delimiter["grammar"] == ["over"]


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("privacy", "save_transcripts"),
        ("app", "auto_pause_when_microphone_busy"),
        ("kimi", "resume_session"),
        ("speech.command", "use_itn"),
    ],
)
@pytest.mark.parametrize("quoted_value", ["true", "false"])
def test_quoted_boolean_values_are_rejected(
    tmp_path: Path, section: str, field: str, quoted_value: str
) -> None:
    config = tmp_path / "config.yaml"
    keys = section.split(".")
    if len(keys) == 1:
        content = f'{keys[0]}:\n  {field}: "{quoted_value}"\n'
    else:
        content = f'{keys[0]}:\n  {keys[1]}:\n    {field}: "{quoted_value}"\n'
    config.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="must be a YAML boolean"):
        load_settings(config)


@pytest.mark.parametrize(
    "content",
    [
        "app:\n  wake_phrases: 开始语音操作\n",
        "app:\n  stop_phrases:\n    - 电脑停止\n    - 3\n",
        "speech:\n  delimiter:\n    grammar: over\n",
    ],
)
def test_phrase_fields_must_be_yaml_string_lists(tmp_path: Path, content: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="must be a YAML list of strings"):
        load_settings(config)


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("app:\n  wake_phrases: []\n", "wake phrase"),
        ("app:\n  prompt_delimiters: ['']\n", "prompt delimiter"),
        ("app:\n  failure_policy: retry\n", "failure_policy"),
        ("app:\n  max_prompt_chars: 9000\n", "max_prompt_chars"),
        ("app:\n  max_queue_size: 0\n", "max_queue_size"),
        ("app:\n  microphone_guard_poll_seconds: 0\n", "microphone_guard_poll_seconds"),
        ("app:\n  feedback_mode: loud\n", "loud"),
        ("kimi:\n  executable: ''\n", "kimi.executable"),
        ("kimi:\n  timeout_seconds: 0\n", "kimi.timeout_seconds"),
        ("kimi:\n  model: ''\n", "kimi.model"),
        ("speech:\n  wake:\n    backend: sherpa\n", "speech.wake.backend"),
        ("speech:\n  sample_rate: 12345\n", "sample rate"),
        ("app: 3\n", "app must be a mapping"),
    ],
)
def test_invalid_values_are_rejected(tmp_path: Path, content: str, match: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_settings(config)


def test_missing_configuration_is_an_error_unless_allowed(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "missing.yaml")
    assert load_settings(tmp_path / "missing.yaml", allow_missing=True).kimi.executable == "kimi"
