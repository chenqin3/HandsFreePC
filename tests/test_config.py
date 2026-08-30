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
