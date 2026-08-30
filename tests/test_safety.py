import pytest

from handsfree_pc.models import Action, ActionType, FeedbackMode, Plan, RiskLevel
from handsfree_pc.safety import SafetyPolicy


def test_executable_open_requires_confirmation(settings) -> None:
    policy = SafetyPolicy(settings.execution)
    plan = Plan("打开安装程序", [Action(ActionType.OPEN_PATH, path="D:\\setup.exe")])
    assert policy.evaluate(plan, user_text="打开安装程序").risk == RiskLevel.CONFIRM


def test_delete_request_is_blocked(settings) -> None:
    policy = SafetyPolicy(settings.execution)
    plan = Plan("删除文件", [Action(ActionType.OPEN_PATH, path="D:\\file.txt")])
    assert policy.evaluate(plan, user_text="删除这个文件").risk == RiskLevel.BLOCKED


def test_send_prompt_phrase_counts_as_explicit_submission(settings) -> None:
    policy = SafetyPolicy(settings.execution)
    plan = Plan("发送", [Action(ActionType.SEND_PROMPT)])
    assert policy.evaluate(plan, explicit_submission=True).risk == RiskLevel.SAFE
    assert policy.evaluate(plan, explicit_submission=False).risk == RiskLevel.CONFIRM


def test_too_many_actions_are_blocked(settings) -> None:
    policy = SafetyPolicy(settings.execution, max_actions=2)
    plan = Plan("too many", [Action(ActionType.PAUSE)] * 3)
    assert policy.evaluate(plan).risk == RiskLevel.BLOCKED


def test_existing_confirmation_risk_cannot_be_lowered(settings) -> None:
    policy = SafetyPolicy(settings.execution)
    plan = Plan("planner requires confirmation", [Action(ActionType.PAUSE)], RiskLevel.CONFIRM)
    assert policy.evaluate(plan).risk == RiskLevel.CONFIRM


def test_existing_blocked_risk_cannot_be_lowered(settings) -> None:
    policy = SafetyPolicy(settings.execution)
    plan = Plan("planner blocked", [Action(ActionType.PAUSE)], RiskLevel.BLOCKED)
    assert policy.evaluate(plan).risk == RiskLevel.BLOCKED


@pytest.mark.parametrize(
    "suffix",
    [".exe", ".hta", ".wsf", ".wsh", ".jse", ".vbe", ".cpl", ".pif", ".msc", ".jar"],
)
def test_active_or_unknown_file_types_require_confirmation(settings, suffix) -> None:
    policy = SafetyPolicy(settings.execution)
    plan = Plan("打开文件", [Action(ActionType.OPEN_PATH, path=f"D:\\demo{suffix}")])
    assert policy.evaluate(plan).risk == RiskLevel.CONFIRM


def test_allow_listed_document_type_is_safe(settings) -> None:
    policy = SafetyPolicy(settings.execution)
    plan = Plan("打开文档", [Action(ActionType.OPEN_PATH, path="D:\\demo.txt")])
    assert policy.evaluate(plan).risk == RiskLevel.SAFE


@pytest.mark.parametrize("source", ["codex", "claude", "llm"])
@pytest.mark.parametrize("action_type", [ActionType.TYPE_TEXT, ActionType.SEND_PROMPT])
def test_cloud_planner_cannot_generate_or_submit_text(settings, source, action_type) -> None:
    policy = SafetyPolicy(settings.execution)
    action = (
        Action(action_type, text="untrusted text")
        if action_type == ActionType.TYPE_TEXT
        else Action(action_type)
    )
    plan = Plan("harmless", [action], source=source)

    assert policy.evaluate(plan, explicit_submission=True).risk == RiskLevel.BLOCKED


def test_cloud_planner_may_only_enter_dictation_without_text(settings) -> None:
    policy = SafetyPolicy(settings.execution)
    plan = Plan(
        "focus composer",
        [Action(ActionType.ENTER_DICTATION, app="codex")],
        source="codex",
    )
    assert policy.evaluate(plan).risk == RiskLevel.SAFE


def test_native_voice_must_be_last_and_cannot_switch_feedback(settings) -> None:
    policy = SafetyPolicy(settings.execution)
    native_then_wait = Plan(
        "bad order",
        [
            Action(ActionType.START_NATIVE_VOICE, app="codex"),
            Action(ActionType.WAIT, seconds=0.1),
        ],
    )
    feedback_then_native = Plan(
        "mixed modes",
        [
            Action(ActionType.SET_FEEDBACK_MODE, feedback_mode=FeedbackMode.BOTH),
            Action(ActionType.START_NATIVE_VOICE, app="codex"),
        ],
    )

    assert policy.evaluate(native_then_wait).risk == RiskLevel.BLOCKED
    assert policy.evaluate(feedback_then_native).risk == RiskLevel.BLOCKED


def test_dictation_cannot_target_arbitrary_current_window(settings) -> None:
    policy = SafetyPolicy(settings.execution)
    plan = Plan("dictate anywhere", [Action(ActionType.ENTER_DICTATION, app="current")])
    assert policy.evaluate(plan).risk == RiskLevel.BLOCKED


def test_direct_plan_with_control_summary_is_blocked(settings) -> None:
    policy = SafetyPolicy(settings.execution)
    plan = Plan("unsafe\nsummary", [Action(ActionType.PAUSE)])
    checked = policy.evaluate(plan)
    assert checked.risk == RiskLevel.BLOCKED
    assert checked.summary == "计划字段未通过本地校验"


@pytest.mark.parametrize(
    "path",
    [
        r"\\server\share\file.txt",
        "//server/share/file.txt",
        r"\\?\C:\Windows\file.txt",
        r"\\.\PhysicalDrive0",
        "https://example.test/file.txt",
        "file:///C:/Windows/file.txt",
    ],
)
def test_remote_uri_and_device_paths_are_blocked(settings, path) -> None:
    policy = SafetyPolicy(settings.execution)
    plan = Plan("unsafe path", [Action(ActionType.OPEN_PATH, path=path)])
    assert policy.evaluate(plan).risk == RiskLevel.BLOCKED
