import pytest

from handsfree_pc.normalize import (
    compact_text,
    confirm_control_phrase,
    parse_ordinal,
    wake_suffix,
)


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


@pytest.mark.parametrize(
    ("transcript", "expected"),
    [
        ("开始语音操作", "开始语音操作"),
        ("  开始语音操作。  ", "开始语音操作"),
        ("开始语音操作 打开记事本 over", "开始语音操作 打开记事本 over"),
        ("开始语音操作，打开 C:\\My Folder over", "开始语音操作 打开 C:\\My Folder over"),
        ("开始语音操做 打开微信", "开始语音操作 打开微信"),  # one ASR slip
        ("开始语音操 打开微信", "开始语音操作 打开微信"),  # one dropped character
        ("嗯开始语音操作", "开始语音操作"),  # one stray leading character
    ],
)
def test_confirm_control_phrase_accepts_the_phrase_at_the_start(transcript, expected) -> None:
    assert confirm_control_phrase(transcript, "开始语音操作") == expected


@pytest.mark.parametrize(
    "transcript",
    [
        "",
        "包子。",
        "我们今天开始语音操作吧",
        "他说开始语音操作",
        "不开始语音操作",
        "“开始语音操作”",
        "开始语音操作”这句话",
        "开始语音",
        "操作 打开记事本",
        "接触语音操作",
    ],
)
def test_confirm_control_phrase_rejects_chatter_negation_quotation_and_hallucination(
    transcript,
) -> None:
    assert confirm_control_phrase(transcript, "开始语音操作") is None


def test_confirm_control_phrase_allows_one_edit_only() -> None:
    assert confirm_control_phrase("接触语音操作", "结束语音操作") is None  # two substitutions
    assert confirm_control_phrase("开始语音操作是什么意思", "开始语音操作") == (
        "开始语音操作 是什么意思"
    )
    assert confirm_control_phrase("电脑停止", "电脑停止") == "电脑停止"
    assert confirm_control_phrase("电脑停之", "电脑停止") == "电脑停止"
    assert confirm_control_phrase("电脑挺好", "电脑停止") is None
