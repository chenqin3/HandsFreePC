from handsfree_pc.normalize import compact_text, parse_ordinal, wake_suffix


def test_compact_text_normalizes_punctuation_and_width() -> None:
    assert compact_text(" 现在，开始 语音 操作！ ") == "现在开始语音操作"


def test_wake_suffix_supports_command_in_same_utterance() -> None:
    phrase, suffix = wake_suffix("现在开始语音操作，打开 D 盘", ["现在开始语音操作"])
    assert phrase == "现在开始语音操作"
    assert suffix == "打开 D 盘"


def test_wake_suffix_preserves_path_case_spaces_and_punctuation() -> None:
    phrase, suffix = wake_suffix(
        r"开始 语音 操作，打开 C:\My Folder\Design Review.md",
        ["开始语音操作"],
    )

    assert phrase == "开始语音操作"
    assert suffix == r"打开 C:\My Folder\Design Review.md"


def test_parse_ordinal() -> None:
    assert parse_ordinal("打开第二个") == 2
    assert parse_ordinal("选第3个") == 3
