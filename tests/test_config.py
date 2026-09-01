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
    assert settings.computer_control.backend == "local_agent"
    assert settings.computer_control.driver == "windows_uia"
    assert settings.computer_control.planner_backend == "claude"
    assert settings.computer_control.allow_screen_context_to_cloud is False
    assert settings.computer_control.allow_codex_cli_host_read is False
    assert settings.computer_control.allow_legacy_codex_computer_use is False
    assert settings.computer_control.failure_policy == "pause"
    assert settings.visual_ocr.enabled is False
    assert settings.visual_ocr.endpoint == "http://127.0.0.1:8766/layout-parsing"
    assert settings.visual_ocr.allow_remote_screen_ocr is False
    assert settings.visual_ocr.apps == ["codex", "wechat"]
    assert settings.workmap.enabled is False
    assert settings.workmap.out_directory is None
    assert settings.workmap.aliases == {}
    assert settings.execution.dry_run is True
    assert settings.speech.fallback["backend"] == "none"
    assert settings.speech.command["backend"] == "sensevoice"
    assert settings.speech.command["model"] == "large-v3-turbo"
    assert "Claude" in settings.speech.command["hotwords"]
    assert "ChatGPT" in settings.speech.command["hotwords"]
    assert settings.app.feedback_mode == FeedbackMode.OVERLAY
    assert settings.speech.vad["backend"] == "silero"
    assert settings.speech.delimiter["backend"] == "vosk"
    assert settings.speech.delimiter["grammar"] == ["over"]
    assert settings.speech.delimiter["model_path"].endswith("vosk-model-small-en-us-0.15")
    assert settings.apps["codex"].voice_button_names == []
    assert settings.apps["claude"].voice_button_names == []
    assert settings.apps["codex"].mode_names["chat"] == ["Chat"]
    assert settings.apps["claude"].mode_names["chat"] == ["Chat and Cowork", "Chat"]
    assert settings.apps["claude"].mode_names["design"] == ["Design"]
    assert settings.apps["wechat"].activation_hotkey == "ctrl+alt+w"
    assert settings.apps["codex"].activation_hotkey is None
    assert settings.apps["chrome"].activation_hotkey is None
    assert "Hyperlink" in settings.apps["chrome"].include_control_types
    assert settings.apps["codex"].include_control_types == [
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
    ]
    assert settings.apps["claude"].content_control_types == [
        "Text",
        "Document",
        "Pane",
        "Group",
    ]
    assert settings.apps["claude"].drop_long_content is True
    assert settings.apps["claude"].max_control_name_chars == 500
    assert settings.apps["claude"].max_content_chars == 1000
    assert settings.apps["claude"].max_content_nodes == 80
    assert "Prompt" in settings.apps["codex"].composer_names
    assert "Ask Claude" in settings.apps["claude"].composer_names


def test_visual_ocr_rejects_remote_screen_endpoint_without_separate_consent(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "visual_ocr:\n"
        "  endpoint: http://192.168.10.119:8089/layout-parsing\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="allow_remote_screen_ocr"):
        load_settings(config)


def test_enabled_visual_ocr_requires_local_unrestricted_codex_visual_planning(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "visual_ocr:\n"
        "  enabled: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="safety_profile=local_unrestricted"):
        load_settings(config)

    config.write_text(
        "privacy:\n"
        "  allow_cloud_planner: true\n"
        "computer_control:\n"
        "  enabled: true\n"
        "  safety_profile: local_unrestricted\n"
        "  allow_screen_context_to_cloud: true\n"
        "execution:\n"
        "  dry_run: false\n"
        "visual_ocr:\n"
        "  enabled: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="planner_backend=codex_cli_best_effort"):
        load_settings(config)


def test_ocr_regions_require_the_screenshot_visual_layer(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "visual_ocr:\n"
        "  ocr_regions_enabled: true\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="requires visual_ocr.enabled=true"):
        load_settings(config)


def test_personal_runtime_can_continue_fifo_after_an_ordinary_failure(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "computer_control:\n  failure_policy: continue\n",
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.computer_control.failure_policy == "continue"


def test_workmap_read_only_index_and_aliases_are_explicit_local_configuration(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "workmap" / "out"
    config = tmp_path / "config.yaml"
    config.write_text(
        "workmap:\n"
        "  enabled: true\n"
        f"  out_directory: {out_dir.as_posix()}\n"
        "  aliases:\n"
        "    示例数据库:\n"
        "      project: 示例数据项目-462365\n"
        "      relative_path: processed_data\n",
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.workmap.enabled is True
    assert settings.workmap.out_directory == out_dir
    assert settings.workmap.aliases["示例数据库"] == {
        "project": "示例数据项目-462365",
        "relative_path": "processed_data",
    }


def test_enabled_workmap_requires_an_out_directory(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("workmap:\n  enabled: true\n", encoding="utf-8")

    with pytest.raises(ValueError, match="workmap.out_directory"):
        load_settings(config)


def test_legacy_config_without_delimiter_section_uses_safe_defaults(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "speech:\n  wake:\n    phrase_window_seconds: 4\n",
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.speech.wake["phrase_window_seconds"] == 4
    assert settings.speech.delimiter["backend"] == "vosk"
    assert settings.speech.delimiter["grammar"] == ["over"]
    assert settings.speech.delimiter["model_path"].endswith("vosk-model-small-en-us-0.15")


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


def test_native_only_computer_control_does_not_require_cloud_consent(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
computer_control:
  enabled: true
  backend: local_agent
  driver: windows_uia
  planner_backend: none
execution:
  dry_run: false
""",
        encoding="utf-8",
    )

    settings = load_settings(config)

    assert settings.computer_control.enabled
    assert settings.computer_control.planner_backend == "none"
    assert not settings.privacy.allow_cloud_planner


def test_codex_cli_desktop_planner_requires_host_read_consent(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        """
privacy:
  allow_cloud_planner: true
computer_control:
  enabled: true
  planner_backend: codex_cli_best_effort
  allow_screen_context_to_cloud: true
execution:
  dry_run: false
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="allow_codex_cli_host_read=true"):
        load_settings(config)

    config.write_text(
        """
privacy:
  allow_cloud_planner: true
computer_control:
  enabled: true
  planner_backend: codex_cli_best_effort
  allow_screen_context_to_cloud: true
  allow_codex_cli_host_read: true
execution:
  dry_run: false
""",
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.computer_control.planner_backend == "codex_cli_best_effort"
    assert settings.computer_control.allow_codex_cli_host_read is True


def test_legacy_codex_backend_requires_both_independent_consents(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    base = """
privacy:
  allow_cloud_planner: true
computer_control:
  enabled: true
  backend: legacy_codex_cli
  allow_screen_context_to_cloud: true
  allow_codex_cli_host_read: {host_read}
  allow_legacy_codex_computer_use: {legacy_control}
execution:
  dry_run: false
"""

    config.write_text(
        base.format(host_read="false", legacy_control="true"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="allow_codex_cli_host_read=true"):
        load_settings(config)

    config.write_text(
        base.format(host_read="true", legacy_control="false"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="allow_legacy_codex_computer_use=true"):
        load_settings(config)

    config.write_text(
        base.format(host_read="true", legacy_control="true"),
        encoding="utf-8",
    )
    settings = load_settings(config)
    assert settings.computer_control.backend == "legacy_codex_cli"
    assert settings.computer_control.allow_codex_cli_host_read is True
    assert settings.computer_control.allow_legacy_codex_computer_use is True


def test_open_computer_use_requires_explicit_experimental_opt_in(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        "computer_control:\n  driver: open_computer_use\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="experimental"):
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
        ("speech:\n  delimiter:\n    phrase_window_seconds: 0\n", "positive number"),
        ("speech:\n  delimiter:\n    backend: none\n", "backend must be vosk"),
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
        "speech:\n  delimiter:\n    grammar: over\n",
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


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (
            "apps:\n  codex:\n    include_control_types: Button\n",
            "include_control_types must be a YAML list",
        ),
        (
            "apps:\n  wechat:\n    activation_hotkey: [ctrl, alt, w]\n",
            "activation_hotkey must be a non-empty string or null",
        ),
        (
            "apps:\n  claude:\n    composer_names: ['']\n",
            "composer_names must be a YAML list of non-empty strings",
        ),
        (
            'apps:\n  codex:\n    drop_long_content: "true"\n',
            "drop_long_content must be a YAML boolean",
        ),
        (
            "apps:\n  codex:\n    max_control_name_chars: 0\n",
            "max_control_name_chars must be between 1 and 1024",
        ),
        (
            "apps:\n  codex:\n    max_content_chars: 16001\n",
            "max_content_chars must be between 1 and 16000",
        ),
        (
            "apps:\n  codex:\n    max_content_nodes: -1\n",
            "max_content_nodes must be between 0 and 2000",
        ),
    ],
)
def test_app_observation_profiles_are_strictly_validated(
    tmp_path: Path,
    content: str,
    expected: str,
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=expected):
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
        ("computer_control", "allow_codex_cli_host_read"),
        ("computer_control", "allow_legacy_codex_computer_use"),
        ("computer_control", "allow_experimental_driver"),
        ("computer_control", "allow_coordinate_actions"),
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
