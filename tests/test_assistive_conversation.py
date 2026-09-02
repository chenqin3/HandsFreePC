from __future__ import annotations

import json

from handsfree_pc.desktop.assistive.controller import AssistiveController
from handsfree_pc.desktop.assistive.models import Goal, GoalKind, TaskSpec
from handsfree_pc.desktop.assistive.policy import AssistivePolicy
from handsfree_pc.desktop.assistive.skills.conversation import (
    PaletteConversationSkill,
    WeChatConversationSkill,
)
from handsfree_pc.desktop.assistive.task_parser import parse_task
from handsfree_pc.desktop.native_skills import NativeRouteStatus, NativeSkillResult
from handsfree_pc.desktop.protocol import (
    ActionReceipt,
    DesktopActionType,
    DesktopElement,
    DesktopElementAction,
    DesktopObservation,
    ElementPlane,
)


def _task(raw: str) -> TaskSpec:
    return parse_task(raw)


class FakeNative:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def assert_interactive_desktop(self) -> None:
        self.events.append(("interactive", None))

    def assert_foreground(self, hwnd: int) -> None:
        self.events.append(("foreground", hwnd))

    def send_hotkey(self, specification: str) -> tuple[int, ...]:
        self.events.append(("hotkey", specification))
        return (1,)

    def send_text(self, text: str) -> int:
        self.events.append(("text", text))
        return len(text)

    def keystrokes(self) -> list[tuple[str, object]]:
        return [event for event in self.events if event[0] in {"hotkey", "text"}]


def _list_item(index: str, name: str, *, selected: bool = False) -> DesktopElement:
    return DesktopElement(
        index,
        name,
        "ListItem",
        selected=selected,
        local_identity=chr(97 + int(index) % 6) * 64,
        supported_actions=(DesktopElementAction.CLICK, DesktopElementAction.INVOKE),
    )


def _palette_observation(app: str, items: list[DesktopElement]) -> DesktopObservation:
    return DesktopObservation(
        app=app,
        generation=3,
        accessibility_text="palette",
        window_title="ChatGPT",
        process_name="ChatGPT.exe",
        local_window_id="hwnd:501",
        elements=tuple(items),
    )


class PaletteDriver:
    """The palette rows exist only after Ctrl+K; before that the sidebar is bare."""

    def __init__(self, observation: DesktopObservation, native: FakeNative | None = None) -> None:
        self.observation = observation
        self.native = native
        self.actions = []

    def observe(self, app: str, *, cancel_event=None, capture_screenshot=False):
        native = getattr(self, "native", None)
        if native is not None and ("hotkey", "ctrl+k") in native.events:
            return self.observation
        return DesktopObservation(
            app=self.observation.app,
            generation=1,
            accessibility_text="sidebar",
            window_title=self.observation.window_title,
            process_name=self.observation.process_name,
            local_window_id=self.observation.local_window_id,
        )

    def execute(self, action, before, *, cancel_event=None) -> ActionReceipt:
        self.actions.append(action)
        return ActionReceipt(action, True, before.generation, "accepted")


def test_palette_skill_types_the_name_and_clicks_the_first_matching_entry() -> None:
    native = FakeNative()
    observation = _palette_observation(
        "codex",
        [
            _list_item("0", "测试 健康召回 示例项目", selected=True),
            _list_item("1", "报表生成 研究数据仓库"),
            _list_item("2", "月度汇总 研究数据仓库"),
        ],
    )
    driver = PaletteDriver(observation, native)

    result = PaletteConversationSkill(native, driver, AssistivePolicy()).open(
        _task("切换到 Codex 打开报表生成"),
        app="codex",
        name="报表生成",
        hwnd=501,
    )

    assert result.status == "succeeded"
    assert native.keystrokes() == [("hotkey", "ctrl+k"), ("text", "报表生成")]
    assert [action.type for action in driver.actions] == [DesktopActionType.CLICK]
    assert driver.actions[0].element_index == "1"
    assert result.details["item"] == "报表生成 研究数据仓库"


def test_palette_skill_prefers_an_exact_name_over_a_longer_match() -> None:
    native = FakeNative()
    observation = _palette_observation(
        "claude",
        [_list_item("0", "项目周报提纲 Today"), _list_item("1", "项目周报 Yesterday")],
    )
    driver = PaletteDriver(observation, native)

    result = PaletteConversationSkill(native, driver, AssistivePolicy()).open(
        _task("在 Claude 里打开会话 项目周报"),
        app="claude",
        name="项目周报 Yesterday",
        hwnd=501,
    )

    assert result.status == "succeeded"
    assert driver.actions[0].element_index == "1"


def test_palette_skill_never_clicks_a_quick_action_that_echoes_the_query() -> None:
    native = FakeNative()
    observation = _palette_observation(
        "claude",
        [
            _list_item("0", "New chat“项目周报”", selected=True),
            _list_item("1", "New task“项目周报”"),
            _list_item("2", "项目周报 Yesterday"),
        ],
    )
    driver = PaletteDriver(observation, native)

    result = PaletteConversationSkill(native, driver, AssistivePolicy()).open(
        _task("在 Claude 里打开会话 项目周报"),
        app="claude",
        name="项目周报",
        hwnd=501,
    )

    assert result.status == "succeeded"
    assert driver.actions[0].element_index == "2"


def test_palette_skill_fails_instead_of_creating_a_chat_when_only_actions_match() -> None:
    native = FakeNative()
    observation = _palette_observation(
        "claude",
        [
            _list_item("0", "New chat“不存在的会话”", selected=True),
            _list_item("1", "新聊天 Ctrl+N"),
        ],
    )
    driver = PaletteDriver(observation, native)

    result = PaletteConversationSkill(native, driver, AssistivePolicy()).open(
        _task("在 Claude 里打开会话 不存在的会话"),
        app="claude",
        name="不存在的会话",
        hwnd=501,
    )

    assert result.status == "retryable_failure"
    assert driver.actions == []


def test_palette_skill_dismisses_the_palette_when_nothing_matches() -> None:
    native = FakeNative()
    driver = PaletteDriver(
        _palette_observation("codex", [_list_item("0", "新聊天 Ctrl+N")]),
        native,
    )

    result = PaletteConversationSkill(native, driver, AssistivePolicy()).open(
        _task("切换到 Codex 打开报表生成"),
        app="codex",
        name="报表生成",
        hwnd=501,
    )

    assert result.status == "retryable_failure"
    assert driver.actions == []
    assert native.keystrokes()[-1] == ("hotkey", "escape")


def _visual_text(index: str, name: str) -> DesktopElement:
    return DesktopElement(
        index,
        name,
        "VisualText",
        plane=ElementPlane.CONTROL,
        editable=False,
        visual_ocr=True,
        local_identity=chr(97 + int(index) % 6) * 64,
        supported_actions=(DesktopElementAction.CLICK,),
    )


def _viewport(index: str) -> DesktopElement:
    return DesktopElement(
        index,
        "Visual screenshot viewport",
        "VisualViewport",
        plane=ElementPlane.CONTROL,
        editable=False,
        visual_ocr=True,
        local_identity="f" * 64,
        supported_actions=(DesktopElementAction.CLICK, DesktopElementAction.SCROLL),
    )


_WIDTH, _HEIGHT = 2952, 1866


def _search_regions() -> dict[str, tuple[int, int, int, int]]:
    return {
        "搜索网络结果": (286, 201, 473, 239),
        "文件传输助手": (277, 283, 462, 318),
        "文件传输助手打开": (277, 368, 522, 402),
        "功能": (240, 930, 320, 960),
        "文件传输助手 ": (405, 1090, 700, 1130),
        "聊天记录": (240, 1260, 350, 1290),
        "读书会群": (405, 1420, 600, 1460),
        "野球拳是不是金庸群侠传里的?": (970, 1470, 1400, 1500),
    }


def test_wechat_pick_result_skips_web_suggestions_and_takes_the_section_entry() -> None:
    regions = [
        (_visual_text(str(index), name.strip()), bbox)
        for index, (name, bbox) in enumerate(_search_regions().items())
    ]

    chosen = WeChatConversationSkill.pick_result(regions, name="文件传输助手", width=_WIDTH)

    assert chosen is not None
    assert chosen[1] == (405, 1090, 700, 1130)


def test_wechat_pick_result_uses_the_first_entry_under_the_topmost_section() -> None:
    regions = [
        (_visual_text("0", "最常使用"), (240, 300, 360, 330)),
        (_visual_text("1", "家庭群"), (405, 360, 800, 400)),
        (_visual_text("2", "聊天记录"), (240, 520, 350, 550)),
        (_visual_text("3", "【另一个】同事群"), (405, 600, 800, 640)),
    ]

    chosen = WeChatConversationSkill.pick_result(regions, name="全家", width=_WIDTH)

    assert chosen is not None
    assert chosen[0].name == "家庭群"


def test_wechat_pick_result_needs_a_real_section_header() -> None:
    regions = [
        (_visual_text("0", "搜索网络结果"), (286, 201, 473, 239)),
        (_visual_text("1", "文件传输助手"), (277, 283, 462, 318)),
    ]

    assert WeChatConversationSkill.pick_result(regions, name="文件传输助手", width=_WIDTH) is None


class WeChatDriver:
    """Serve OCR-backed observations; boxes are keyed by (generation, index)."""

    def __init__(self, frames: list[tuple[DesktopObservation, dict[str, tuple]]]) -> None:
        self.frames = list(frames)
        self.boxes: dict[tuple[int, str], tuple] = {}
        for observation, boxes in frames:
            for index, bbox in boxes.items():
                self.boxes[(observation.generation, index)] = bbox
        self.actions = []

    def observe(self, app: str, *, cancel_event=None, capture_screenshot=False):
        assert capture_screenshot is True
        observation, _boxes = self.frames[0]
        if len(self.frames) > 1:
            self.frames.pop(0)
        return observation

    def visual_region_bbox(self, before, element):
        return self.boxes.get((before.generation, element.index))

    def execute(self, action, before, *, cancel_event=None) -> ActionReceipt:
        self.actions.append(action)
        return ActionReceipt(action, True, before.generation, "accepted")


def _wechat_frame(
    generation: int,
    rows: list[tuple[str, tuple[int, int, int, int]]],
) -> tuple[DesktopObservation, dict[str, tuple]]:
    elements = [_visual_text(str(index), name) for index, (name, _bbox) in enumerate(rows)]
    elements.append(_viewport(str(len(rows))))
    observation = DesktopObservation(
        app="weixin-1",
        generation=generation,
        accessibility_text="wechat",
        window_title="微信",
        process_name="Weixin.exe",
        local_window_id="hwnd:777",
        elements=tuple(elements),
        screenshot_png=b"\x89PNG\r\n\x1a\n" + b"\x00" * 32,
    )
    return observation, {str(index): bbox for index, (_name, bbox) in enumerate(rows)}


def test_wechat_skill_clicks_the_section_entry_through_the_viewport_and_verifies() -> None:
    native = FakeNative()
    # The skill first screenshots to see whether the wanted chat is already
    # open; a different chat (no matching header) sends it to the search path.
    idle = _wechat_frame(9, [("别的会话", (1200, 100, 1500, 150))])
    search = _wechat_frame(
        1,
        [
            ("搜索网络结果", (286, 201, 473, 239)),
            ("文件传输助手", (277, 283, 462, 318)),
            ("文件传输助手打开", (277, 368, 522, 402)),
            ("功能", (240, 930, 320, 960)),
            ("文件传输助手", (405, 1090, 700, 1130)),
            ("聊天记录", (240, 1260, 350, 1290)),
        ],
    )
    opened = _wechat_frame(
        2,
        [("文件传输助手", (1200, 100, 1500, 150)), ("读书会群", (405, 1420, 600, 1460))],
    )
    driver = WeChatDriver([idle, search, opened])
    skill = WeChatConversationSkill(native, driver, AssistivePolicy(), sleeper=lambda _s: None)
    skill._frame_size = lambda observation, regions: (_WIDTH, _HEIGHT)  # type: ignore[method-assign]

    result = skill.open(
        _task("切换到微信 打开文件传输助手"),
        app="weixin-1",
        name="文件传输助手",
        hwnd=777,
    )

    assert result.status == "succeeded"
    assert native.keystrokes() == [("hotkey", "ctrl+f"), ("text", "文件传输助手")]
    action = driver.actions[0]
    assert action.type == DesktopActionType.CLICK
    assert action.element_index == "6"
    assert (action.x, action.y) == ((405 + 700) // 2, (1090 + 1130) // 2)
    assert result.details["verified"] is True


def test_parser_maps_chat_app_open_targets_to_conversations() -> None:
    wechat = parse_task("切换到微信 打开文件传输助手")
    palette = parse_task("切换到周报生成聊天框")
    app_only = parse_task("切换到微信聊天")
    other = parse_task("切换到记事本 打开最近的文件")

    assert wechat.goals == (
        Goal(GoalKind.APP_FOREGROUND, "wechat"),
        Goal(GoalKind.CONVERSATION_SELECTED, "文件传输助手", app="wechat"),
    )
    assert palette.goals == (Goal(GoalKind.CONVERSATION_SELECTED, "周报生成"),)
    assert app_only.goals == (Goal(GoalKind.APP_FOREGROUND, "wechat"),)
    assert other.goals[1].kind == GoalKind.FREE_FORM


class RouterStub:
    def __init__(self) -> None:
        self.executor = object()
        self.settings = None

    def route(self, instruction, *, explicit_submission=False, cancel_event=None):
        return NativeSkillResult(NativeRouteStatus.MISS, "miss")


class ControllerDriver(PaletteDriver):
    def __init__(self, observation: DesktopObservation, inventory: str) -> None:
        super().__init__(observation)
        self.inventory = inventory
        self.activations: list[str] = []

    def start(self) -> None:
        return None

    def list_apps(self, *, cancel_event=None) -> str:
        return self.inventory

    def activate_app(self, app: str, *, cancel_event=None) -> int:
        self.activations.append(app)
        return 501

    def _native_backend(self):
        return self.native

    def close(self) -> None:
        return None


def test_controller_routes_a_foreground_chat_conversation_goal_to_the_palette_skill() -> None:
    inventory = json.dumps(
        [
            {
                "app": "chatgpt-1",
                "display_name": "codex",
                "process_name": "ChatGPT.exe",
                "window_title": "ChatGPT",
                "foreground": True,
            }
        ],
        ensure_ascii=False,
    )
    observation = _palette_observation(
        "chatgpt-1",
        [
            _list_item("0", "测试 健康召回 示例项目"),
            _list_item("1", "周报生成 研究数据仓库"),
        ],
    )
    driver = ControllerDriver(observation, inventory)
    driver.native = FakeNative()

    class NoPlanner:
        def decide(self, *args, **kwargs):
            raise AssertionError("planner must not run")

    controller = AssistiveController(
        native_router=RouterStub(),
        driver=driver,
        planner=NoPlanner(),
        timeout_seconds=30,
    )

    result = controller.run("切换到周报生成聊天框")

    assert result.success
    assert "周报生成" in result.message
    assert driver.activations == ["chatgpt-1"]
    assert driver.native.keystrokes() == [("hotkey", "ctrl+k"), ("text", "周报生成")]
    assert driver.actions[0].element_index == "1"


def _sidebar_button(index: str, name: str, *, selected: bool | None = None) -> DesktopElement:
    return DesktopElement(
        index,
        name,
        "Button",
        selected=selected,
        local_identity=chr(97 + int(index) % 6) * 64,
        supported_actions=(DesktopElementAction.CLICK, DesktopElementAction.INVOKE),
    )


def _group_button(index: str, name: str) -> DesktopElement:
    return DesktopElement(
        index,
        name,
        "Button",
        local_identity=chr(97 + int(index) % 6) * 64,
        supported_actions=(DesktopElementAction.COLLAPSE,),
    )


def _claude_observation(generation: int, *, chat_selected: bool, rows: list[DesktopElement]):
    return DesktopObservation(
        app="claude",
        generation=generation,
        accessibility_text="claude",
        window_title="Claude",
        process_name="claude.exe",
        local_window_id="hwnd:501",
        elements=(
            _sidebar_button("0", "Chat and Cowork", selected=chat_selected),
            _sidebar_button("1", "Code, working", selected=not chat_selected),
            _sidebar_button("2", "New"),
            _sidebar_button("3", "Search"),
            *rows,
        ),
    )


class SequenceDriver(PaletteDriver):
    def __init__(self, observations: list[DesktopObservation]) -> None:
        super().__init__(observations[0])
        self.observations = list(observations)

    def observe(self, app: str, *, cancel_event=None, capture_screenshot=False):
        if len(self.observations) > 1:
            return self.observations.pop(0)
        return self.observations[0]


def test_goal_mode_round_trips_and_normalizes() -> None:
    goal = Goal(GoalKind.CONVERSATION_SELECTED, "项目周报", app="claude", mode="Chat")

    assert goal.mode == "chat"
    assert Goal.from_dict(goal.to_dict()) == goal
    assert "mode" not in Goal(GoalKind.APP_FOREGROUND, "claude").to_dict()


def test_parser_extracts_claude_surface_qualifiers() -> None:
    chat = parse_task("在 Claude 的 Chat and Cowork 里打开会话 写作指导")
    bare_chat = parse_task("在 chat and cowork 里打开会话 写作指导")
    code = parse_task("在 Claude Code 里打开会话 项目周报")
    default = parse_task("在 Claude 里打开会话 项目周报")
    codex = parse_task("切换到 Codex 打开报表生成")

    assert chat.goals[1] == Goal(
        GoalKind.CONVERSATION_SELECTED, "写作指导", app="claude", mode="chat"
    )
    assert bare_chat.goals[0] == Goal(GoalKind.APP_FOREGROUND, "claude")
    assert bare_chat.goals[1].mode == "chat"
    assert code.goals[1].mode == "code"
    assert default.goals[1].mode is None
    assert codex.goals[1].mode is None


def test_palette_skill_switches_claude_surface_then_clicks_the_sidebar_row() -> None:
    native = FakeNative()
    before = _claude_observation(
        1,
        chat_selected=False,
        rows=[_group_button("4", "示例项目"), _sidebar_button("5", "Idle 项目周报")],
    )
    after = _claude_observation(
        2,
        chat_selected=True,
        rows=[
            _sidebar_button("4", "Idle 写作指导"),
            _sidebar_button("5", "More options for 写作指导"),
            _sidebar_button("6", "Mark as unread"),
        ],
    )
    driver = SequenceDriver([before, after])

    result = PaletteConversationSkill(
        native,
        driver,
        AssistivePolicy(),
        mode="chat",
        mode_buttons={"code": ("Code",), "chat": ("Chat and Cowork",)},
        sleeper=lambda _s: None,
    ).open(
        _task("在 Claude 的 Chat and Cowork 里打开会话 写作指导"),
        app="claude",
        name="写作指导",
        hwnd=501,
    )

    assert result.status == "succeeded"
    assert result.details["via"] == "sidebar"
    assert [action.element_index for action in driver.actions] == ["0", "4"]
    assert native.keystrokes() == []


def test_palette_skill_keeps_the_code_surface_and_never_clicks_group_headers() -> None:
    native = FakeNative()
    observation = _claude_observation(
        1,
        chat_selected=False,
        rows=[
            _group_button("4", "项目周报"),
            _sidebar_button("5", "New session in 项目周报"),
            _sidebar_button("6", "Running 项目周报"),
            _sidebar_button("7", "More options for 项目周报"),
        ],
    )
    driver = SequenceDriver([observation])

    result = PaletteConversationSkill(
        native,
        driver,
        AssistivePolicy(),
        mode="code",
        mode_buttons={"code": ("Code",), "chat": ("Chat and Cowork",)},
        sleeper=lambda _s: None,
    ).open(_task("在 Claude 里打开会话 项目周报"), app="claude", name="项目周报", hwnd=501)

    assert result.status == "succeeded"
    assert [action.element_index for action in driver.actions] == ["6"]


def test_palette_skill_falls_back_to_the_palette_when_the_sidebar_lacks_the_row() -> None:
    native = FakeNative()
    sidebar = _claude_observation(1, chat_selected=False, rows=[_sidebar_button("4", "Idle 其他")])
    palette = _palette_observation(
        "claude",
        [_list_item("0", "New chat“项目周报”"), _list_item("1", "项目周报 Yesterday")],
    )
    driver = SequenceDriver([sidebar, palette])

    result = PaletteConversationSkill(
        native,
        driver,
        AssistivePolicy(),
        mode="code",
        mode_buttons={"code": ("Code",), "chat": ("Chat and Cowork",)},
        sleeper=lambda _s: None,
    ).open(_task("在 Claude 里打开会话 项目周报"), app="claude", name="项目周报", hwnd=501)

    assert result.status == "succeeded"
    assert result.details["via"] == "palette"
    assert native.keystrokes() == [("hotkey", "ctrl+k"), ("text", "项目周报")]
    assert driver.actions[-1].element_index == "1"


def test_wechat_skill_short_circuits_when_the_chat_is_already_open() -> None:
    native = FakeNative()
    already_open, _boxes = _wechat_frame(
        1, [("文件传输助手", (1200, 100, 1500, 150)), ("读书会群", (405, 1420, 600, 1460))]
    )
    driver = WeChatDriver([(already_open, _boxes)])
    skill = WeChatConversationSkill(native, driver, AssistivePolicy(), sleeper=lambda _s: None)
    skill._frame_size = lambda observation, regions: (_WIDTH, _HEIGHT)  # type: ignore[method-assign]

    result = skill.open(
        _task("切换到微信 打开文件传输助手"),
        app="weixin-1",
        name="文件传输助手",
        hwnd=777,
    )

    assert result.status == "succeeded"
    assert result.details["already_open"] is True
    assert native.keystrokes() == []
    assert driver.actions == []


def test_wechat_skill_polls_until_the_search_sections_appear() -> None:
    native = FakeNative()
    idle = _wechat_frame(9, [("别的会话", (1200, 100, 1500, 150))])
    loading = _wechat_frame(
        8,
        [("搜索网络结果", (286, 201, 473, 239)), ("文件传输助手", (277, 283, 462, 318))],
    )
    search = _wechat_frame(
        1,
        [
            ("搜索网络结果", (286, 201, 473, 239)),
            ("文件传输助手", (277, 283, 462, 318)),
            ("功能", (240, 930, 320, 960)),
            ("文件传输助手", (405, 1090, 700, 1130)),
        ],
    )
    opened = _wechat_frame(
        2,
        [("文件传输助手", (1200, 100, 1500, 150)), ("读书会群", (405, 1420, 600, 1460))],
    )
    driver = WeChatDriver([idle, loading, search, opened])
    skill = WeChatConversationSkill(native, driver, AssistivePolicy(), sleeper=lambda _s: None)
    skill._frame_size = lambda observation, regions: (_WIDTH, _HEIGHT)  # type: ignore[method-assign]

    result = skill.open(
        _task("切换到微信 打开文件传输助手"),
        app="weixin-1",
        name="文件传输助手",
        hwnd=777,
    )

    assert result.status == "succeeded"
    assert len(driver.actions) == 1
    assert (driver.actions[0].x, driver.actions[0].y) == ((405 + 700) // 2, (1090 + 1130) // 2)
