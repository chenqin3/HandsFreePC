import pytest

from handsfree_pc.intents import DeterministicIntentParser
from handsfree_pc.models import ActionType, FeedbackMode


def test_parse_spoken_drive_path() -> None:
    plan = DeterministicIntentParser().parse("打开D盘的项目文件夹里的说明.txt")
    assert plan is not None
    assert plan.actions[0].type == ActionType.OPEN_PATH
    assert plan.actions[0].path == "D:\\项目\\说明.txt"


def test_parse_deep_spoken_path_and_spoken_extension() -> None:
    parser = DeterministicIntentParser()
    command = "打开G盘下面的年度资料文件夹下面的已处理数据文件夹里面的最终报告点txt"

    plan = parser.parse(command)

    assert plan is not None
    assert plan.actions[0].path == "G:\\年度资料\\已处理数据\\最终报告.txt"
    assert parser.covers_full_text(command, plan) is True


@pytest.mark.parametrize("误识别", ["锯盘", "居盘", "句盘", "鸡盘", "据盘", "局盘"])
def test_scoped_mandarin_asr_drive_aliases_resolve_to_g_drive(误识别: str) -> None:
    parser = DeterministicIntentParser()
    command = f"打开{误识别}下面的研究资料库文件夹"

    plan = parser.parse(command)

    assert plan is not None
    assert plan.actions[0].path == "G:\\研究资料库"
    assert parser.covers_full_text(command, plan) is True


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


@pytest.mark.parametrize("alias", ["ChatGPT", "chat gpt", "聊天GPT"])
def test_chatgpt_voice_alias_uses_existing_codex_profile(alias: str) -> None:
    parser = DeterministicIntentParser()
    command = f"打开 {alias}"

    plan = parser.parse(command)

    assert plan is not None
    assert [action.type for action in plan.actions] == [ActionType.ACTIVATE_APP]
    assert plan.actions[0].app == "codex"
    assert parser.covers_full_text(command, plan) is True


def test_chatgpt_alias_is_scoped_to_an_app_request_not_dictated_content() -> None:
    parser = DeterministicIntentParser()

    assert parser.parse("输入 ChatGPT 到普通字段") is None


def test_parse_claude_mode_and_dictation() -> None:
    plan = DeterministicIntentParser().parse(
        "打开claude app，到chat选项卡里面，开启一个design，接下来我会语音输入"
    )
    assert plan is not None
    mode = next(action for action in plan.actions if action.type == ActionType.OPEN_MODE)
    assert mode.tab == "chat"
    assert mode.mode == "design"
    assert plan.actions[-1].type == ActionType.ENTER_DICTATION


@pytest.mark.parametrize(
    ("command", "app", "surface"),
    [
        ("打开 Claude 到 Chat 选项卡", "claude", "chat"),
        ("切换 Codex 到 Code 选项卡", "codex", "code"),
        ("打开 Claude 的 Cowork 标签页", "claude", "cowork"),
    ],
)
def test_parse_explicit_app_surface_without_created_mode(
    command: str, app: str, surface: str
) -> None:
    plan = DeterministicIntentParser().parse(command)

    assert plan is not None
    assert [action.type for action in plan.actions] == [
        ActionType.ACTIVATE_APP,
        ActionType.OPEN_MODE,
    ]
    assert plan.actions[1].app == app
    assert plan.actions[1].tab is None
    assert plan.actions[1].mode == surface


def test_chat_and_cowork_product_label_is_a_complete_native_chat_surface() -> None:
    parser = DeterministicIntentParser()
    command = "切换到 Claude，打开 Chat and Cowork"

    plan = parser.parse(command)

    assert plan is not None
    assert [action.type for action in plan.actions] == [
        ActionType.ACTIVATE_APP,
        ActionType.OPEN_MODE,
    ]
    assert plan.actions[1].mode == "chat"
    assert parser.covers_full_text(command, plan)


@pytest.mark.parametrize(
    "command",
    [
        "切换到cloud，打开chat and cowork。",
        "切换到cloud，打开chat in cowork。",
        "切换道克劳德，打开聊天和协作。",
    ],
)
def test_sensevoice_claude_aliases_and_surfaces_remain_complete_app_commands(
    command: str,
) -> None:
    parser = DeterministicIntentParser()

    plan = parser.parse(command)

    assert plan is not None
    assert [(action.type, action.app) for action in plan.actions] == [
        (ActionType.ACTIVATE_APP, "claude"),
        (ActionType.OPEN_MODE, "claude"),
    ]
    assert plan.actions[1].mode == "chat"
    assert parser.covers_full_text(command, plan)


def test_sensevoice_claude_alias_can_bind_a_non_deterministic_typed_text_command() -> None:
    parser = DeterministicIntentParser()
    command = "在cloloud的输入框输入，这是语音控制测试，不要发送。"

    plan = parser.parse(command)

    assert plan is not None
    assert plan.actions[0].type == ActionType.ACTIVATE_APP
    assert plan.actions[0].app == "claude"
    assert parser.covers_full_text(command, plan) is False


def test_sensevoice_cloud_alias_is_not_a_global_text_rewrite() -> None:
    parser = DeterministicIntentParser()

    assert parser.parse("cloud computing 很重要") is None
    assert parser.parse("输入 cloud 到输入框") is None


def test_codex_name_is_not_mistaken_for_code_surface() -> None:
    plan = DeterministicIntentParser().parse("打开 Codex")

    assert plan is not None
    assert [action.type for action in plan.actions] == [ActionType.ACTIVATE_APP]


@pytest.mark.parametrize(
    ("command", "app"),
    [
        ("切换到 Chrome", "chrome"),
        ("打开资源管理器", "explorer"),
        ("切换到微信", "wechat"),
    ],
)
def test_common_windows_apps_have_complete_deterministic_activation_commands(
    command: str,
    app: str,
) -> None:
    parser = DeterministicIntentParser()

    plan = parser.parse(command)

    assert plan is not None
    assert [(action.type, action.app) for action in plan.actions] == [
        (ActionType.ACTIVATE_APP, app)
    ]
    assert parser.covers_full_text(command, plan) is True


def test_native_voice_requires_explicit_phrase() -> None:
    plan = DeterministicIntentParser().parse("打开codex，使用应用内语音")
    assert plan is not None
    assert plan.actions[-1].type == ActionType.START_NATIVE_VOICE


@pytest.mark.parametrize(
    "command",
    [
        "打开 Codex，然后最大化窗口",
        "打开 Codex，然后保存当前文件",
        "打开 Codex，然后把当前聊天归档",
        "打开 Codex，然后退出账户",
        "打开 Codex，然后创建新对话",
    ],
)
def test_app_prefix_match_does_not_claim_full_text_coverage(command: str) -> None:
    parser = DeterministicIntentParser()
    plan = parser.parse(command)

    assert plan is not None
    assert [action.type for action in plan.actions] == [ActionType.ACTIVATE_APP]
    assert parser.covers_full_text(command, plan) is False


@pytest.mark.parametrize(
    "command",
    [
        "打开 Codex",
        "打开 Claude 到 Chat 选项卡",
        "切换到 Claude，打开 Chat and Cowork",
        "切换到桌面上的codex app，打开其中的演示项目下的语音设计对话，打开语音输入",
        "打开claude app，到chat选项卡里面，开启一个design，接下来我会语音输入",
        "打开D盘的项目文件夹里的说明.txt",
        "切换到屏幕反馈",
    ],
)
def test_supported_deterministic_grammar_covers_complete_text(command: str) -> None:
    parser = DeterministicIntentParser()
    plan = parser.parse(command)

    assert plan is not None
    assert parser.covers_full_text(command, plan) is True


def test_direct_path_prefix_does_not_consume_a_second_open_request() -> None:
    parser = DeterministicIntentParser()
    command = r"打开 D:\资料，打开这个文件"
    plan = parser.parse(command)

    assert plan is not None
    assert [action.type for action in plan.actions] == [ActionType.OPEN_PATH]
    assert parser.covers_full_text(command, plan) is False


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
