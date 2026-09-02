import json

import pytest

from handsfree_pc.desktop.assistive.models import (
    ActionOutcome,
    AssistiveDecision,
    AssistiveDecisionKind,
    Goal,
    GoalKind,
    SkillStatus,
    TaskSpec,
)
from handsfree_pc.desktop.assistive.task_parser import DeterministicTaskParser, parse_task
from handsfree_pc.desktop.protocol import DesktopAction, DesktopActionType


@pytest.mark.parametrize("verb", ["切换到", "切到", "激活", "打开"])
def test_parses_app_activation_phrases(verb: str) -> None:
    task = parse_task(f"{verb} Claude")

    assert task.goals == (Goal(GoalKind.APP_FOREGROUND, "claude"),)
    assert task.forbid_submit is False
    assert task.side_effect is False


def test_open_explorer_is_an_explicit_app_goal() -> None:
    task = parse_task("打开资源管理器")

    assert task.goals == (Goal(GoalKind.APP_FOREGROUND, "explorer"),)


@pytest.mark.parametrize(
    ("command", "expected"),
    [("打开D盘", "D:\\"), ("打开 d 盘", "D:\\"), ("进入A盘", "A:\\"), ("查看z盘", "Z:\\")],
)
def test_parses_spoken_drive_roots(command: str, expected: str) -> None:
    assert parse_task(command).goals == (Goal(GoalKind.PATH_OPEN, expected),)


@pytest.mark.parametrize("alias", ["桌面", "文档", "下载"])
def test_parses_default_path_aliases(alias: str) -> None:
    assert parse_task(f"打开{alias}").goals == (Goal(GoalKind.PATH_OPEN, alias),)


def test_parses_direct_and_configured_paths() -> None:
    direct = parse_task(r"打开 D:\研究数据\调查数据库")
    configured = DeterministicTaskParser(
        path_aliases={"调查数据库": r"D:\研究数据\调查数据库"}
    ).parse("打开调查数据库")

    expected = Goal(GoalKind.PATH_OPEN, r"D:\研究数据\调查数据库")
    assert direct.goals == (expected,)
    assert configured.goals == (expected,)


def test_unpunctuated_compound_command_becomes_two_goals() -> None:
    task = parse_task("切换到 Chrome 打开 Google 网页")

    assert task.goals == (
        Goal(GoalKind.APP_FOREGROUND, "chrome"),
        Goal(GoalKind.URL_LOADED, "google.com", app="chrome"),
    )


def test_explicit_url_query_and_fragment_survive_surface_normalization() -> None:
    url = "https://www.google.com/search?q=hands+free&lang=zh-CN#result"

    task = parse_task(f"切换到 Chrome 打开 {url}")

    assert task.goals == (
        Goal(GoalKind.APP_FOREGROUND, "chrome"),
        Goal(GoalKind.URL_LOADED, url, app="chrome"),
    )


def test_compound_app_switch_and_draft_input_preserves_both_goals_and_app() -> None:
    task = parse_task("切换到 Claude 然后输入你好（不要发送）")

    assert task.goals == (
        Goal(GoalKind.APP_FOREGROUND, "claude"),
        Goal(GoalKind.INPUT_CONTAINS, "你好", app="claude"),
    )
    assert task.forbid_submit is True
    assert task.side_effect is False


def test_unknown_compound_second_clause_is_preserved_for_the_agent() -> None:
    task = parse_task("切换到记事本 打开最近的文件")

    assert task.goals == (
        Goal(GoalKind.APP_FOREGROUND, "记事本"),
        Goal(GoalKind.FREE_FORM, "打开 最近的文件", app="记事本"),
    )


@pytest.mark.parametrize("verb", ["找到", "打开"])
def test_parses_app_conversation_request(verb: str) -> None:
    task = parse_task(f"在 Claude 里{verb}会话 Voice Design")

    assert task.goals == (
        Goal(GoalKind.APP_FOREGROUND, "claude"),
        Goal(GoalKind.CONVERSATION_SELECTED, "Voice Design", app="claude"),
    )


@pytest.mark.parametrize("negation", ["不要发送", "别发送", "先不发"])
def test_input_with_no_send_phrase_preserves_draft_and_forbids_submit(negation: str) -> None:
    task = parse_task(f"在 Claude 的输入框输入 这是草稿，{negation}")

    assert task.goals == (Goal(GoalKind.INPUT_CONTAINS, "这是草稿", app="claude"),)
    assert task.forbid_submit is True
    assert task.side_effect is False


def test_positive_action_after_input_is_never_swallowed_into_the_draft() -> None:
    raw = "在 Claude 的输入框输入 你好，然后发送"

    task = parse_task(raw)

    assert task.goals == (Goal(GoalKind.FREE_FORM, raw),)
    assert task.side_effect is True
    assert task.forbid_submit is False


def test_unknown_full_command_falls_back_without_losing_raw_text() -> None:
    raw = "帮我判断当前页面下一步应该做什么"

    task = parse_task(raw)

    assert task.goals == (Goal(GoalKind.FREE_FORM, raw),)
    assert task.raw_text == raw


def test_negated_open_is_not_executed_as_a_deterministic_prefix() -> None:
    task = parse_task("不要打开微信")

    assert task.goals == (Goal(GoalKind.FREE_FORM, "不要打开微信"),)


def test_task_spec_has_a_json_round_trip_and_tuple_goals() -> None:
    original = parse_task("打开 D 盘")
    payload = json.loads(json.dumps(original.to_dict(), ensure_ascii=False))

    restored = TaskSpec.from_dict(payload)

    assert restored == original
    assert isinstance(restored.goals, tuple)


def test_assistive_model_enums_match_pr1_contract() -> None:
    assert {item.value for item in GoalKind} == {
        "app_foreground",
        "path_open",
        "url_loaded",
        "conversation_selected",
        "input_contains",
        "text_visible",
        "element_state",
        "message_sent",
        "file_sent",
        "free_form",
    }
    assert {item.value for item in SkillStatus} == {
        "completed",
        "progress",
        "retryable_failure",
        "miss",
        "needs_confirmation",
        "blocked",
    }
    assert {item.value for item in ActionOutcome} == {
        "completed",
        "progress",
        "no_effect",
        "unobservable",
    }


def test_assistive_decision_has_no_expectation_and_binds_action_app() -> None:
    action = DesktopAction(
        DesktopActionType.CLICK,
        app="claude",
        generation=1,
        element_index="1",
    )
    decision = AssistiveDecision(
        AssistiveDecisionKind.ACTION,
        "select the visible target",
        app="claude",
        action=action,
    )

    assert not hasattr(decision, "expectation")
    with pytest.raises(ValueError, match="must match"):
        AssistiveDecision(
            AssistiveDecisionKind.ACTION,
            "wrong window",
            app="codex",
            action=action,
        )


def test_activation_prefix_drops_the_input_location_from_the_app_name() -> None:
    task = parse_task("切换到 Claude 然后在输入框输入 你好 不要发送")

    assert task.goals == (
        Goal(GoalKind.APP_FOREGROUND, "claude"),
        Goal(GoalKind.INPUT_CONTAINS, "你好", app="claude"),
    )
    assert task.forbid_submit is True


def test_search_after_an_app_name_is_free_form_not_an_app_name() -> None:
    task = parse_task("切换到 Chrome 搜索 北京天气")

    assert task.goals == (Goal(GoalKind.FREE_FORM, "切换到 Chrome 搜索 北京天气"),)
