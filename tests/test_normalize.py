import pytest

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


def test_wake_suffix_supports_custom_configured_phrase_at_utterance_start() -> None:
    phrase, suffix = wake_suffix("芝麻开门，打开 Claude over", ["芝麻开门"])

    assert phrase == "芝麻开门"
    assert suffix == "打开 Claude over"


def test_custom_configured_wake_rejects_leading_context() -> None:
    assert wake_suffix("不要芝麻开门", ["芝麻开门"]) == (None, "")
    assert wake_suffix("她说芝麻开门", ["芝麻开门"]) == (None, "")


def test_wake_suffix_does_not_match_inside_a_longer_ascii_word() -> None:
    assert wake_suffix("hey pcx open Claude", ["hey pc"]) == (None, "")
    assert wake_suffix("hey pc open Claude", ["hey pc"]) == (
        "hey pc",
        "open Claude",
    )


@pytest.mark.parametrize(
    "utterance",
    [
        "不要开始语音操作",
        "我说的是开始语音操作",
        "他说开始语音操作",
        "“开始语音操作”",
        "开始语音操作”这句话",
    ],
)
def test_wake_suffix_rejects_negation_quotation_and_reported_speech(
    utterance: str,
) -> None:
    assert wake_suffix(utterance, ["开始语音操作"]) == (None, "")


def test_parse_ordinal() -> None:
    assert parse_ordinal("打开第二个") == 2
    assert parse_ordinal("选第3个") == 3
