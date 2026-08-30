from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from handsfree_pc.config import DEFAULT_CONFIG, FeedbackMode, load_settings


def test_defaults_are_privacy_preserving(tmp_path: Path) -> None:
    settings = load_settings(tmp_path / "missing.yaml", allow_missing=True)
    assert settings.privacy.save_audio is False
    assert settings.privacy.save_transcripts is False
    assert settings.privacy.allow_cloud_planner is False
    assert settings.planner.enabled is False
    assert settings.computer_control.enabled is False
    assert settings.computer_control.allow_screen_context_to_cloud is False
    assert settings.execution.dry_run is True
    assert settings.speech.fallback["backend"] == "none"
    assert settings.app.feedback_mode == FeedbackMode.OVERLAY
    assert settings.speech.vad["backend"] == "silero"
    assert settings.apps["codex"].voice_button_names == []
    assert settings.apps["claude"].voice_button_names == []


def test_cloud_planner_requires_explicit_privacy_opt_in(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("planner:\n  enabled: true\n", encoding="utf-8")
    with pytest.raises(ValueError, match="allow_cloud_planner"):
        load_settings(config)


def test_computer_control_cannot_run_behind_dry_run_label(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "privacy:\n"
        "  allow_cloud_planner: true\n"
        "computer_control:\n"
        "  enabled: true\n"
        "  allow_screen_context_to_cloud: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="dry_run=false"):
        load_settings(config)


def test_computer_control_requires_cloud_transcript_opt_in(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "computer_control:\n"
        "  enabled: true\n"
        "  allow_screen_context_to_cloud: true\n"
        "execution:\n"
        "  dry_run: false\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="allow_cloud_planner"):
        load_settings(config)


def test_computer_control_requires_screen_context_opt_in(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "privacy:\n"
        "  allow_cloud_planner: true\n"
        "computer_control:\n"
        "  enabled: true\n"
        "execution:\n"
        "  dry_run: false\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="allow_screen_context_to_cloud=true"):
        load_settings(config)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("app:\n  stop_phrases: []\n", "stop phrase"),
        ("app:\n  stop_phrases: ['   ']\n", "stop phrase"),
        ("execution:\n  confirmation_phrases: []\n", "confirmation phrase"),
        ("execution:\n  confirmation_timeout_seconds: 0\n", "must be positive"),
        ("computer_control:\n  max_prompt_chars: 8001\n", "between 1 and 8000"),
        ("speech:\n  wake:\n    phrase_window_seconds: 0\n", "positive number"),
    ],
)
def test_local_control_phrases_and_window_cannot_be_disabled(
    tmp_path: Path, content: str, expected: str
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=expected):
        load_settings(config)


@pytest.mark.parametrize(
    "content",
    [
        "app:\n  wake_phrases: 开始语音操作\n",
        "app:\n  end_session_phrases: 结束语音操作\n",
        "app:\n  stop_phrases: 立即停止所有操作\n",
        "app:\n  prompt_delimiters: over\n",
        "app:\n  control_prefixes: 电脑\n",
        "execution:\n  confirmation_phrases: 确认执行\n",
        "execution:\n  cancellation_phrases: 取消所有操作\n",
        "speech:\n  wake:\n    grammar: 开始语音操作\n",
        "apps:\n  codex:\n    process_names: Codex.exe\n",
        "apps:\n  codex:\n    title_patterns: Codex\n",
        "apps:\n  codex:\n    voice_button_names: 语音\n",
    ],
)
def test_phrase_fields_must_be_yaml_string_lists(tmp_path: Path, content: str) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match="must be a YAML list of strings"):
        load_settings(config)


def test_relative_paths_resolve_from_config_directory(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "execution:\n  path_aliases:\n    测试: data\n  search_roots: [search]\n",
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.execution.path_aliases["测试"] == (tmp_path / "data").resolve()
    assert settings.execution.search_roots == [(tmp_path / "search").resolve()]


def test_example_configuration_matches_packaged_defaults() -> None:
    example_path = Path(__file__).resolve().parents[1] / "config.example.yaml"
    example = yaml.safe_load(example_path.read_text(encoding="utf-8"))

    assert example == DEFAULT_CONFIG


@pytest.mark.parametrize(
    ("section", "field"),
    [
        ("privacy", "allow_cloud_planner"),
        ("privacy", "save_audio"),
        ("planner", "enabled"),
        ("computer_control", "enabled"),
        ("computer_control", "allow_screen_context_to_cloud"),
        ("execution", "dry_run"),
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
