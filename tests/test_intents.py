import pytest

from handsfree_pc.intents import DeterministicIntentParser
from handsfree_pc.models import ActionType, FeedbackMode


def test_parse_spoken_drive_path() -> None:
    plan = DeterministicIntentParser().parse("打开D盘的项目文件夹里的说明.txt")
    assert plan is not None
    assert plan.actions[0].type == ActionType.OPEN_PATH
    assert plan.actions[0].path == "D:\\项目\\说明.txt"


@pytest.mark.parametrize("alias", ["桌面", "文档", "下载"])
def test_parse_nested_alias_path(alias: str) -> None:
    plan = DeterministicIntentParser().parse(f"打开{alias}上的项目文件夹里的说明.txt")
    assert plan is not None
    assert plan.actions[0].path == f"{alias}\\项目\\说明.txt"


def test_parse_feedback_modes() -> None:
    parser = DeterministicIntentParser()
    assert parser.parse("切换到屏幕反馈").actions[0].feedback_mode == FeedbackMode.OVERLAY
    assert parser.parse("大字和语音两种都开").actions[0].feedback_mode == FeedbackMode.BOTH
    assert parser.parse("切换到语音反馈").actions[0].feedback_mode == FeedbackMode.VOICE
    assert parser.parse("安静模式").actions[0].feedback_mode == FeedbackMode.SILENT


def test_parse_codex_project_conversation_and_dictation() -> None:
    plan = DeterministicIntentParser().parse(
        "切换到桌面上的codex app，打开其中的演示项目下的语音设计对话，打开语音输入"
    )
    assert plan is not None
    assert [action.type for action in plan.actions] == [
        ActionType.ACTIVATE_APP,
        ActionType.OPEN_CONVERSATION,
        ActionType.ENTER_DICTATION,
    ]
    assert plan.actions[1].project == "演示"
    assert plan.actions[1].conversation == "语音设计"


def test_parse_claude_mode_and_dictation() -> None:
    plan = DeterministicIntentParser().parse(
        "打开claude app，到chat选项卡里面，开启一个design，接下来我会语音输入"
    )
    assert plan is not None
    mode = next(action for action in plan.actions if action.type == ActionType.OPEN_MODE)
    assert mode.tab == "chat"
    assert mode.mode == "design"
    assert plan.actions[-1].type == ActionType.ENTER_DICTATION


def test_native_voice_requires_explicit_phrase() -> None:
    plan = DeterministicIntentParser().parse("打开codex，使用应用内语音")
    assert plan is not None
    assert plan.actions[-1].type == ActionType.START_NATIVE_VOICE


def test_send_prompt_requires_exact_control_command() -> None:
    parser = DeterministicIntentParser()
    assert parser.parse("发送提示").actions[0].type == ActionType.SEND_PROMPT
    assert parser.parse("电脑发送提示").actions[0].type == ActionType.SEND_PROMPT
    assert parser.parse("电脑不要发送提示") is None
    assert parser.parse("这段文字提到了发送提示") is None


def test_bare_dictation_requires_a_configured_application() -> None:
    parser = DeterministicIntentParser()
    for command in ("开始听写", "打开语音输入"):
        plan = parser.parse(command)
        assert plan is not None
        assert plan.risk.value == "blocked"
        assert plan.actions == []
