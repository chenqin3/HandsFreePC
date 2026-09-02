from __future__ import annotations

import json

import pytest

from handsfree_pc.desktop.assistive.models import ActionOutcome, Goal, GoalKind, TaskSpec
from handsfree_pc.desktop.assistive.retry import (
    ProgressDetector,
    ProgressProbe,
    wait_for_outcome,
)
from handsfree_pc.desktop.assistive.verifier import GoalVerifier
from handsfree_pc.desktop.protocol import DesktopElement, DesktopObservation, ElementPlane
from handsfree_pc.windows.native import DesktopUnavailableError


class Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def _observation(
    *,
    generation: int = 1,
    value: str = "",
    focused: bool = True,
) -> DesktopObservation:
    element = DesktopElement(
        "0",
        "消息输入框",
        "Edit",
        value=value,
        value_observed=True,
        focused=focused,
        plane=ElementPlane.INPUT,
        local_identity="a" * 64,
    )
    return DesktopObservation(
        app="claude",
        generation=generation,
        accessibility_text='0 name="消息输入框" control_type="Edit"',
        window_title="Claude",
        local_window_id="window-1",
        elements=(element,),
    )


def test_progress_detector_does_not_count_observation_generation_as_progress() -> None:
    inventory = json.dumps(
        [{"app": "claude", "window_title": "Claude", "foreground": True}],
        sort_keys=True,
    )
    before = ProgressProbe(
        goals_complete=False,
        satisfied_goal_count=0,
        observation=_observation(generation=1),
        inventory=inventory,
    )
    after = ProgressProbe(
        goals_complete=False,
        satisfied_goal_count=0,
        observation=_observation(generation=99),
        inventory=inventory,
    )

    assert ProgressDetector.signature(before) == ProgressDetector.signature(after)
    assert not ProgressDetector.changed(
        ProgressDetector.signature(before),
        ProgressDetector.signature(after),
    )


def test_wait_for_outcome_uses_a_bounded_absolute_poll_schedule() -> None:
    clock = Clock()
    before = ProgressProbe(False, 0, observation=_observation(), inventory="[]")
    samples = 0

    def probe() -> ProgressProbe:
        nonlocal samples
        samples += 1
        return ProgressProbe(
            False,
            0,
            observation=_observation(generation=samples + 1),
            inventory="[]",
        )

    result = wait_for_outcome(
        probe,
        before=before,
        timeout_seconds=0.3,
        schedule=(0.0, 0.1, 0.2),
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result.outcome == ActionOutcome.NO_EFFECT
    assert result.polls == 4
    assert result.elapsed_seconds == 0.3
    assert clock.value == 0.3
    assert sum(clock.sleeps) == 0.3


def test_wait_for_outcome_returns_immediately_on_task_goal_completion() -> None:
    clock = Clock()
    before = ProgressProbe(False, 0, observation=_observation(), inventory="[]")
    calls = 0

    def probe() -> ProgressProbe:
        nonlocal calls
        calls += 1
        return ProgressProbe(
            goals_complete=calls == 2,
            satisfied_goal_count=int(calls == 2),
            observation=_observation(generation=calls + 1),
            inventory="[]",
        )

    result = wait_for_outcome(
        probe,
        before=before,
        timeout_seconds=1.0,
        schedule=(0.0, 0.1, 0.5),
        sleeper=clock.sleep,
        monotonic=clock.monotonic,
    )

    assert result.outcome == ActionOutcome.COMPLETED
    assert result.polls == 2
    assert clock.value == 0.1


class VerifierDriver:
    def __init__(self, inventory: list[dict[str, object]]) -> None:
        self.inventory = inventory

    def list_apps(self, *, cancel_event=None) -> str:
        return json.dumps(self.inventory, ensure_ascii=False)


def test_goal_verifier_requires_the_requested_app_to_be_foreground() -> None:
    task = TaskSpec(
        goals=(Goal(GoalKind.APP_FOREGROUND, "claude"),),
        forbid_submit=False,
        side_effect=False,
        raw_text="切换到 Claude",
    )
    background = VerifierDriver(
        [{"app": "claude", "window_title": "Claude", "foreground": False}]
    )
    foreground = VerifierDriver(
        [{"app": "claude", "window_title": "Claude", "foreground": True}]
    )

    assert not GoalVerifier().evaluate(task, background).completed
    assert GoalVerifier().evaluate(task, foreground).completed


def test_foreground_app_does_not_trust_document_title_as_process_identity() -> None:
    task = TaskSpec(
        goals=(Goal(GoalKind.APP_FOREGROUND, "claude"),),
        forbid_submit=False,
        side_effect=False,
        raw_text="切换到 Claude",
    )
    chrome_with_misleading_title = VerifierDriver(
        [
            {
                "app": "chrome",
                "display_name": "Google Chrome",
                "process_name": "chrome.exe",
                "window_title": "Claude documentation - Google Chrome",
                "foreground": True,
            }
        ]
    )

    assert not GoalVerifier().evaluate(task, chrome_with_misleading_title).completed

    scoped_text_task = TaskSpec(
        goals=(Goal(GoalKind.INPUT_CONTAINS, "完整原文", app="claude"),),
        forbid_submit=True,
        side_effect=False,
        raw_text="在 Claude 输入完整原文，不要发送",
    )
    misleading_observation = DesktopObservation(
        app="chrome",
        generation=1,
        accessibility_text="完整原文",
        window_title="Claude documentation - Google Chrome",
        process_name="chrome.exe",
        local_window_id="chrome-1",
        elements=(DesktopElement("0", "完整原文", "Text"),),
    )
    assert not GoalVerifier().evaluate(
        scoped_text_task,
        chrome_with_misleading_title,
        observation=misleading_observation,
    ).completed


def test_goal_verifier_checks_input_value_in_the_bound_app() -> None:
    task = TaskSpec(
        goals=(Goal(GoalKind.INPUT_CONTAINS, "完整原文", app="claude"),),
        forbid_submit=True,
        side_effect=False,
        raw_text="输入完整原文，不要发送",
    )
    driver = VerifierDriver(
        [{"app": "claude", "window_title": "Claude", "foreground": True}]
    )

    missing = GoalVerifier().evaluate(task, driver, observation=_observation(value=""))
    present = GoalVerifier().evaluate(
        task,
        driver,
        observation=_observation(value="前缀 完整原文 后缀"),
    )

    assert not missing.completed
    assert present.completed


def test_url_goal_ignores_body_and_bookmark_text_but_accepts_trusted_surfaces() -> None:
    task = TaskSpec(
        goals=(Goal(GoalKind.URL_LOADED, "google.com", app="chrome"),),
        forbid_submit=False,
        side_effect=False,
        raw_text="在 Chrome 打开 Google 网页",
    )
    driver = VerifierDriver(
        [{"app": "chrome", "process_name": "chrome.exe", "foreground": True}]
    )
    untrusted_body = DesktopObservation(
        app="chrome",
        generation=1,
        accessibility_text="正文提到 https://google.com，也有一个 Google 书签",
        window_title="本地新闻",
        local_window_id="chrome-1",
        elements=(
            DesktopElement("0", "正文中的 Google", "Text"),
            DesktopElement("1", "Google 书签", "Hyperlink"),
        ),
    )
    chrome_brand_title = DesktopObservation(
        app="chrome",
        generation=2,
        accessibility_text="",
        window_title="Google Chrome",
        local_window_id="chrome-1",
    )
    document_title = DesktopObservation(
        app="chrome",
        generation=3,
        accessibility_text="",
        window_title="Chrome",
        local_window_id="chrome-1",
        elements=(DesktopElement("0", "Google.com Search", "Document"),),
    )
    spoofed_page_title = DesktopObservation(
        app="chrome",
        generation=3,
        accessibility_text="",
        window_title="google.com - unrelated page - Google Chrome",
        process_name="chrome.exe",
        local_window_id="chrome-1",
        elements=(DesktopElement("0", "google.com - unrelated page", "Document"),),
    )
    focused_address_bar = DesktopObservation(
        app="chrome",
        generation=4,
        accessibility_text="",
        window_title="Chrome",
        process_name="chrome.exe",
        local_window_id="chrome-1",
        elements=(
            DesktopElement(
                "0",
                "地址栏",
                "Edit",
                automation_id="address-bar",
                value="https://www.google.com/search?q=test",
                focused=True,
                plane=ElementPlane.INPUT,
                local_identity="a" * 64,
                browser_chrome=True,
            ),
        ),
    )
    committed_address_bar = DesktopObservation(
        app="chrome",
        generation=5,
        accessibility_text="",
        window_title="Chrome",
        process_name="chrome.exe",
        local_window_id="chrome-1",
        elements=(
            DesktopElement(
                "0",
                "地址栏",
                "Edit",
                automation_id="address-bar",
                value="https://www.google.com/search?q=test",
                focused=False,
                plane=ElementPlane.INPUT,
                local_identity="b" * 64,
                browser_chrome=True,
            ),
            DesktopElement(
                "1",
                "Google 搜索",
                "Document",
                automation_id="RootWebArea",
                value="https://www.google.com/search?q=test",
                value_observed=True,
                plane=ElementPlane.CONTENT,
                local_identity="c" * 64,
            ),
        ),
    )
    chromium_unlabeled_address_bar = DesktopObservation(
        app="chrome",
        generation=6,
        accessibility_text="",
        window_title="Google - Google Chrome",
        process_name="chrome.exe",
        local_window_id="chrome-1",
        elements=(
            DesktopElement(
                "0",
                "https://www.google.com/search?q=test",
                "Edit",
                automation_id="view_1012",
                value="https://www.google.com/search?q=test",
                value_observed=True,
                focused=False,
                editable=True,
                local_identity="d" * 64,
                browser_chrome=True,
            ),
            DesktopElement(
                "1",
                "Google 搜索",
                "Document",
                automation_id="RootWebArea",
                value="https://www.google.com/search?q=test",
                value_observed=True,
                plane=ElementPlane.CONTENT,
                local_identity="f" * 64,
            ),
        ),
    )
    unknown_focus_address_bar = DesktopObservation(
        app="chrome",
        generation=7,
        accessibility_text="",
        window_title="Chrome",
        process_name="chrome.exe",
        local_window_id="chrome-1",
        elements=(
            DesktopElement(
                "0",
                "https://www.google.com/search?q=test",
                "Edit",
                automation_id="view_1012",
                value="https://www.google.com/search?q=test",
                value_observed=True,
                focused=None,
                editable=True,
                local_identity="e" * 64,
                browser_chrome=True,
            ),
        ),
    )

    def misleading_address(value: str) -> DesktopObservation:
        return DesktopObservation(
            app="chrome",
                generation=6,
                accessibility_text="",
                window_title="Chrome",
                process_name="chrome.exe",
                local_window_id="chrome-1",
            elements=(
                DesktopElement(
                    "0",
                    "地址栏",
                    "Edit",
                    automation_id="address-bar",
                        value=value,
                        focused=False,
                        plane=ElementPlane.INPUT,
                        local_identity="c" * 64,
                        browser_chrome=True,
                ),
            ),
        )

    verifier = GoalVerifier()

    assert not verifier.evaluate(task, driver, observation=untrusted_body).completed
    assert not verifier.evaluate(task, driver, observation=chrome_brand_title).completed
    assert not verifier.evaluate(task, driver, observation=document_title).completed
    assert not verifier.evaluate(task, driver, observation=spoofed_page_title).completed
    assert not verifier.evaluate(task, driver, observation=focused_address_bar).completed
    assert verifier.evaluate(task, driver, observation=committed_address_bar).completed
    assert verifier.evaluate(
        task,
        driver,
        observation=chromium_unlabeled_address_bar,
    ).completed
    assert not verifier.evaluate(
        task,
        driver,
        observation=unknown_focus_address_bar,
    ).completed
    for value in (
        "https://evilgoogle.com",
        "https://google.com.evil.example",
        "https://example.com/?next=google.com",
    ):
        assert not verifier.evaluate(
            task,
            driver,
            observation=misleading_address(value),
        ).completed

    specific_task = TaskSpec(
        goals=(
            Goal(
                GoalKind.URL_LOADED,
                "https://google.com/?handsfreepc-scenario=NEW",
                app="chrome",
            ),
        ),
        forbid_submit=False,
        side_effect=False,
        raw_text="打开指定 Google 测试网页",
    )
    assert not verifier.evaluate(
        specific_task,
        driver,
        observation=document_title,
    ).completed


def test_path_verifier_re_raises_secure_desktop_boundary() -> None:
    class SecureNative:
        @staticmethod
        def path_open_state(_path):
            raise DesktopUnavailableError("secure desktop")

    task = TaskSpec(
        goals=(Goal(GoalKind.PATH_OPEN, r"D:\\"),),
        forbid_submit=False,
        side_effect=False,
        raw_text="打开 D 盘",
    )
    driver = VerifierDriver([])

    with pytest.raises(DesktopUnavailableError, match="secure desktop"):
        GoalVerifier(native=SecureNative()).evaluate(task, driver)


def test_conversation_requires_selected_state_or_an_exact_trusted_title() -> None:
    task = TaskSpec(
        goals=(
            Goal(
                GoalKind.CONVERSATION_SELECTED,
                "Voice Design",
                app="claude",
            ),
        ),
        forbid_submit=False,
        side_effect=False,
        raw_text="在 Claude 里打开会话 Voice Design",
    )
    driver = VerifierDriver(
        [{"app": "claude", "process_name": "Claude.exe", "foreground": True}]
    )
    focused_only = DesktopObservation(
        app="claude",
        generation=1,
        accessibility_text="",
        window_title="Claude",
        local_window_id="claude-1",
        elements=(
            DesktopElement(
                "0",
                "Voice Design",
                "ListItem",
                focused=True,
                selected=False,
            ),
        ),
    )
    trusted_brand_suffix = DesktopObservation(
        app="claude",
        generation=2,
        accessibility_text="",
        window_title="Voice Design - Claude",
        local_window_id="claude-1",
    )
    arbitrary_title_substring = DesktopObservation(
        app="claude",
        generation=5,
        accessibility_text="",
        window_title="Voice Design notes - Claude",
        local_window_id="claude-1",
    )
    selected = DesktopObservation(
        app="claude",
        generation=3,
        accessibility_text="",
        window_title="Claude",
        local_window_id="claude-1",
        elements=(
            DesktopElement(
                "0",
                "Voice Design",
                "ListItem",
                selected=True,
            ),
        ),
    )
    exact_title = DesktopObservation(
        app="claude",
        generation=4,
        accessibility_text="",
        window_title="Voice Design",
        local_window_id="claude-1",
    )

    verifier = GoalVerifier()

    assert not verifier.evaluate(task, driver, observation=focused_only).completed
    assert verifier.evaluate(task, driver, observation=trusted_brand_suffix).completed
    assert not verifier.evaluate(
        task,
        driver,
        observation=arbitrary_title_substring,
    ).completed
    assert verifier.evaluate(task, driver, observation=selected).completed
    assert verifier.evaluate(task, driver, observation=exact_title).completed


class _InventoryDriver:
    def __init__(self, inventory: str = "[]") -> None:
        self.inventory = inventory

    def list_apps(self, *, cancel_event=None) -> str:
        return self.inventory


def _task_with(goal: Goal, raw_text: str) -> TaskSpec:
    return TaskSpec(goals=(goal,), forbid_submit=False, side_effect=False, raw_text=raw_text)


def test_opaque_window_makes_unmet_textual_goals_planner_judged() -> None:
    observation = DesktopObservation(
        app="weixin-1",
        generation=1,
        accessibility_text='0 name="Weixin" control_type="Pane"',
        window_title="微信",
        process_name="Weixin.exe",
        local_window_id="hwnd:1",
        elements=(
            DesktopElement("0", "Weixin", "Pane"),
            DesktopElement("1", "MMUIRenderSubWindowHW", "Pane"),
        ),
    )
    task = _task_with(
        Goal(GoalKind.CONVERSATION_SELECTED, "文件传输助手", app="wechat"),
        "切换到微信 打开文件传输助手",
    )

    result = GoalVerifier().evaluate(task, _InventoryDriver(), observation=observation)

    assert not result.completed
    assert result.checks[0].verifiable is False
    assert result.pending_unverifiable


def test_positive_evidence_counts_even_in_a_thin_window() -> None:
    task = _task_with(Goal(GoalKind.INPUT_CONTAINS, "这是测试", app="claude"), "输入这是测试")

    result = GoalVerifier().evaluate(
        task, _InventoryDriver(), observation=_observation(value="这是测试")
    )

    assert result.completed
    assert result.checks[0].verifiable is True
    assert not result.pending_unverifiable


def test_rich_window_keeps_unmet_textual_goals_verifiable() -> None:
    buttons = tuple(
        DesktopElement(
            str(index),
            f"按钮{index}",
            "Button",
            local_identity=chr(97 + (index % 6)) * 64,
        )
        for index in range(1, 7)
    )
    base = _observation(value="")
    observation = DesktopObservation(
        app=base.app,
        generation=base.generation,
        accessibility_text=base.accessibility_text,
        window_title=base.window_title,
        local_window_id=base.local_window_id,
        elements=base.elements + buttons,
    )
    task = _task_with(Goal(GoalKind.INPUT_CONTAINS, "这是测试", app="claude"), "输入这是测试")

    result = GoalVerifier().evaluate(task, _InventoryDriver(), observation=observation)

    assert not result.completed
    assert result.checks[0].verifiable is True
    assert not result.pending_unverifiable


def test_free_form_goal_is_always_planner_judged() -> None:
    task = _task_with(Goal(GoalKind.FREE_FORM, "把左侧第一项展开"), "把左侧第一项展开")

    result = GoalVerifier().evaluate(task, _InventoryDriver(), observation=_observation())

    assert result.pending_unverifiable


def test_spoken_window_title_matches_only_outside_alias_families() -> None:
    design = json.dumps(
        [
            {
                "app": "claude-1",
                "process_name": "claude.exe",
                "window_title": "Design",
                "foreground": True,
            }
        ]
    )
    helper = json.dumps(
        [
            {
                "app": "other-1",
                "process_name": "other.exe",
                "window_title": "Google Chrome Helper",
                "foreground": True,
            }
        ]
    )

    by_title = GoalVerifier().evaluate(
        _task_with(Goal(GoalKind.APP_FOREGROUND, "Design"), "切换到 Design"),
        _InventoryDriver(design),
    )
    family = GoalVerifier().evaluate(
        _task_with(Goal(GoalKind.APP_FOREGROUND, "chrome"), "切换到 Chrome"),
        _InventoryDriver(helper),
    )

    assert by_title.completed
    assert not family.completed
