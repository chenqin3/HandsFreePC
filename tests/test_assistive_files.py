from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from handsfree_pc.desktop.assistive.controller import AssistiveController
from handsfree_pc.desktop.assistive.models import Goal, GoalKind
from handsfree_pc.desktop.assistive.policy import AssistivePolicy
from handsfree_pc.desktop.assistive.skills.explorer import ExplorerSkill
from handsfree_pc.desktop.assistive.skills.wechat_files import (
    WeChatSendFileSkill,
    is_self_transfer,
)
from handsfree_pc.desktop.assistive.spoken_paths import SpokenPathResolver
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
from handsfree_pc.windows.native import NativeWindows

# --- parser ------------------------------------------------------------------


def test_parser_handles_the_chrome_chatgpt_draft_sentence() -> None:
    task = parse_task("去chrome打开chatgpt网页然后开一个新对话，问一下测试问题（但是不要发送）")

    assert task.goals == (
        Goal(GoalKind.APP_FOREGROUND, "chrome"),
        Goal(GoalKind.URL_LOADED, "chatgpt.com", app="chrome"),
        Goal(GoalKind.INPUT_CONTAINS, "测试问题", app="chrome"),
    )
    assert task.forbid_submit is True


def test_parser_handles_send_file_to_wechat_conversation() -> None:
    task = parse_task("把下载文件夹里面那个季度总结的网页发送到微信的文件传输助手")

    assert task.goals == (
        Goal(GoalKind.APP_FOREGROUND, "wechat"),
        Goal(GoalKind.CONVERSATION_SELECTED, "文件传输助手", app="wechat"),
        Goal(GoalKind.FILE_SENT, "下载文件夹里面那个季度总结的网页", app="wechat"),
    )
    assert task.side_effect is True


def test_parser_keeps_a_spoken_folder_as_an_app_goal_for_runtime_resolution() -> None:
    task = parse_task("打开d盘研究数据库那个文件夹")

    assert task.goals == (Goal(GoalKind.APP_FOREGROUND, "d盘研究数据库那个文件夹"),)


# --- spoken path resolver ----------------------------------------------------


class FakeWorkMap:
    def __init__(self, entries: dict[str, tuple[str, Path]]) -> None:
        self.entries = entries

    def search_candidates(self, query: str, *, limit: int = 5, minimum_score: float = 0.0):
        from handsfree_pc.workmap import _score

        candidates = []
        for target_id, (title, _path) in self.entries.items():
            candidates.append(
                SimpleNamespace(
                    target_id=target_id,
                    title=title,
                    score=_score(query, title),
                    target_available=True,
                )
            )
        return tuple(sorted(candidates, key=lambda item: -item.score)[:limit])

    def resolve_candidate_id(self, target_id: str) -> Path | None:
        return self.entries[target_id][1]


def test_resolver_maps_an_abbreviation_with_a_drive_hint_to_the_project_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "研究数据仓库"
    root.mkdir()
    other = tmp_path / "数字经济指数"
    other.mkdir()
    index = FakeWorkMap(
        {"wm-1": ("研究数据仓库", root), "wm-2": ("数字经济指数", other)}
    )
    resolver = SpokenPathResolver(workmap_index=index, home=tmp_path)

    match = resolver.resolve(f"{root.drive[0].lower()}盘研究数据库那个文件夹")

    assert match is not None
    assert match.path == root
    assert match.source == "workmap"


def test_resolver_rejects_a_drive_hint_that_contradicts_every_project(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "研究数据仓库"
    root.mkdir()
    index = FakeWorkMap({"wm-1": ("研究数据仓库", root)})
    resolver = SpokenPathResolver(workmap_index=index, home=tmp_path)
    wrong_drive = "z" if root.drive[0].lower() != "z" else "y"
    # The drive-root scan is the last resort; keep the machine's real drives out.
    monkeypatch.setattr(resolver, "_scored_folder", lambda base, description: [])

    assert resolver.resolve(f"{wrong_drive}盘研究数据库") is None


def test_resolver_picks_the_file_by_description_and_type_hint(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    for name in (
        "季度总结·离线版.html",
        "会议纪要·离线版.html",
        "培训录像目录-离线版.html",
        "季度总结.py",
    ):
        (downloads / name).write_text("x", encoding="utf-8")
    resolver = SpokenPathResolver(home=tmp_path)

    match = resolver.resolve("下载文件夹里面那个季度总结的网页")

    assert match is not None
    assert match.path == downloads / "季度总结·离线版.html"


def test_resolver_refuses_a_tie_between_two_equally_good_files(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "报告 v1.docx").write_text("x", encoding="utf-8")
    (downloads / "报告 v2.docx").write_text("x", encoding="utf-8")
    resolver = SpokenPathResolver(home=tmp_path)

    assert resolver.resolve("下载里的报告文档") is None


# --- clipboard ---------------------------------------------------------------


class FakeKernel32:
    """ctypes-style stand-in: plain functions accept the argtypes/restype writes."""

    def __init__(self) -> None:
        self.freed: list[int] = []

        def global_alloc(flags, size):
            return 4242

        def global_free(handle):
            self.freed.append(handle)
            return 0

        self.GlobalAlloc = global_alloc
        self.GlobalLock = lambda handle: 1
        self.GlobalUnlock = lambda handle: True
        self.GlobalFree = global_free
        self.GetCurrentThreadId = lambda: 1


class FakeUser32:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

        def open_clipboard(hwnd):
            self.calls.append(("open", hwnd))
            return True

        def empty_clipboard():
            self.calls.append(("empty", None))
            return True

        def set_clipboard_data(fmt, handle):
            self.calls.append(("set", (fmt, handle)))
            return handle

        def close_clipboard():
            self.calls.append(("close", None))
            return True

        self.OpenClipboard = open_clipboard
        self.EmptyClipboard = empty_clipboard
        self.SetClipboardData = set_clipboard_data
        self.CloseClipboard = close_clipboard

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return SimpleNamespace(argtypes=None, restype=None)


def test_copy_files_to_clipboard_places_a_cf_hdrop_list(tmp_path: Path, monkeypatch) -> None:
    target = tmp_path / "季度总结·离线版.html"
    target.write_text("x", encoding="utf-8")
    kernel32 = FakeKernel32()
    user32 = FakeUser32()
    captured: dict[str, bytes] = {}

    def fake_memmove(pointer, payload, size):
        captured["payload"] = bytes(payload[:size])

    monkeypatch.setattr("handsfree_pc.windows.native.ctypes.memmove", fake_memmove)
    native = NativeWindows(user32=user32, shell32=object(), kernel32=kernel32)

    count = native.copy_files_to_clipboard([target])

    assert count == 1
    assert ("set", (15, 4242)) in user32.calls
    assert user32.calls[-1] == ("close", None)
    payload = captured["payload"]
    assert payload[:4] == (20).to_bytes(4, "little")
    assert str(target.resolve()).encode("utf-16-le") in payload
    assert payload.endswith(b"\x00\x00\x00\x00")
    assert kernel32.freed == []


# --- explorer open_directory ---------------------------------------------------


class FolderNative:
    def __init__(self, folder: str) -> None:
        self.folder = folder
        self.opened: list[str] = []
        self.foreground = SimpleNamespace(
            hwnd=7, process_name="explorer.exe", class_name="CabinetWClass"
        )
        self.shown = False

    def assert_interactive_desktop(self) -> None:
        return None

    def path_open_state(self, path):
        return {
            "kind": "explorer_directory",
            "verified": self.shown,
            "foreground_hwnd": 7,
            "matching_hwnds": (7,) if self.shown else (),
        }

    def shell_execute_path(self, path):
        self.opened.append(str(path))
        self.shown = True
        return "ShellExecuteW"

    def get_foreground_window_info(self):
        return self.foreground

    def activate_window(self, hwnd, timeout=2.0):
        return self.foreground


def test_explorer_skill_opens_any_existing_folder(tmp_path: Path) -> None:
    native = FolderNative(str(tmp_path))

    result = ExplorerSkill(native, sleeper=lambda _s: None).open_directory(tmp_path)

    assert result.status == "succeeded"
    assert native.opened == [str(tmp_path)]


# --- wechat send file skill -----------------------------------------------------


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


def _frame(generation: int, rows: list[tuple[str, tuple[int, int, int, int]]]):
    elements = [_visual_text(str(index), name) for index, (name, _bbox) in enumerate(rows)]
    elements.append(
        DesktopElement(
            str(len(rows)),
            "Visual screenshot viewport",
            "VisualViewport",
            plane=ElementPlane.CONTROL,
            editable=False,
            visual_ocr=True,
            local_identity="f" * 64,
            supported_actions=(DesktopElementAction.CLICK, DesktopElementAction.SCROLL),
        )
    )
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


class SendNative:
    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def assert_interactive_desktop(self) -> None:
        return None

    def assert_foreground(self, hwnd: int) -> None:
        self.events.append(("foreground", hwnd))

    def send_hotkey(self, specification: str):
        self.events.append(("hotkey", specification))
        return (1,)

    def copy_files_to_clipboard(self, paths):
        self.events.append(("clipboard", tuple(str(p) for p in paths)))
        return len(paths)

    def keystrokes(self):
        return [e for e in self.events if e[0] in {"hotkey", "clipboard"}]


class FrameDriver:
    def __init__(self, frames) -> None:
        self.frames = list(frames)
        self.boxes = {}
        for observation, boxes in frames:
            for index, bbox in boxes.items():
                self.boxes[(observation.generation, index)] = bbox
        self.actions = []

    def observe(self, app, *, cancel_event=None, capture_screenshot=False):
        observation, _boxes = self.frames[0]
        if len(self.frames) > 1:
            self.frames.pop(0)
        return observation

    def visual_region_bbox(self, before, element):
        return self.boxes.get((before.generation, element.index))

    def execute(self, action, before, *, cancel_event=None):
        self.actions.append(action)
        return ActionReceipt(action, True, before.generation, "accepted")


def test_wechat_send_file_pastes_into_the_composer_and_verifies(tmp_path: Path) -> None:
    target = tmp_path / "季度总结·离线版.html"
    target.write_text("x", encoding="utf-8")
    before = _frame(
        1,
        [("文件传输助手", (1200, 100, 1500, 150)), ("发送", (2780, 1750, 2860, 1790))],
    )
    after = _frame(
        2,
        [
            ("文件传输助手", (1200, 100, 1500, 150)),
            ("季度总结·离线版.html", (2100, 1200, 2600, 1240)),
            ("发送", (2780, 1750, 2860, 1790)),
        ],
    )
    native = SendNative()
    driver = FrameDriver([before, after])
    skill = WeChatSendFileSkill(native, driver, AssistivePolicy(), sleeper=lambda _s: None)
    skill._helper._frame_size = lambda observation, regions: (2952, 1866)  # type: ignore[method-assign]

    result = skill.send(
        parse_task("把下载文件夹里面那个季度总结的网页发送到微信的文件传输助手"),
        app="weixin-1",
        hwnd=777,
        path=target,
        conversation="文件传输助手",
    )

    assert result.status == "succeeded"
    assert result.details["verified"] is True
    assert result.details["via"] == "button"
    # Focus the composer, then click 发送 (never Enter when the button is visible).
    assert [action.type for action in driver.actions] == [
        DesktopActionType.CLICK,
        DesktopActionType.CLICK,
    ]
    assert driver.actions[0].y < 1750
    assert (driver.actions[1].x, driver.actions[1].y) == ((2780 + 2860) // 2, (1750 + 1790) // 2)
    assert native.keystrokes() == [
        ("clipboard", (str(target),)),
        ("hotkey", "ctrl+v"),
    ]


def test_wechat_send_file_stops_before_enter_for_confirm_policy_contacts(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    frame = _frame(
        1, [("某个同事", (1200, 100, 1500, 150)), ("发送", (2780, 1750, 2860, 1790))]
    )
    native = SendNative()
    driver = FrameDriver([frame])
    skill = WeChatSendFileSkill(native, driver, AssistivePolicy(), sleeper=lambda _s: None)
    skill._helper._frame_size = lambda observation, regions: (2952, 1866)  # type: ignore[method-assign]

    result = skill.send(
        parse_task("把a发送到微信的某个同事"),
        app="weixin-1",
        hwnd=777,
        path=target,
        conversation="某个同事",
    )

    assert result.status == "policy_rejected"
    assert native.keystrokes() == [("clipboard", (str(target),)), ("hotkey", "ctrl+v")]
    assert len(driver.actions) == 1
    assert is_self_transfer("文件传输助手") and not is_self_transfer("某个同事")


# --- controller wiring -----------------------------------------------------------


class RouterStub:
    def __init__(self, workmap_index=None) -> None:
        self.executor = object()
        self.settings = None
        self.workmap_index = workmap_index

    def route(self, instruction, *, explicit_submission=False, cancel_event=None):
        return NativeSkillResult(NativeRouteStatus.MISS, "miss")


class PathDriver:
    def __init__(self, native, inventory: str) -> None:
        self.native = native
        self.inventory = inventory
        self.calls: list[str] = []

    def start(self) -> None:
        return None

    def list_apps(self, *, cancel_event=None) -> str:
        return self.inventory

    def observe(self, app, *, cancel_event=None, capture_screenshot=False):
        raise AssertionError("no observation needed")

    def _native_backend(self):
        return self.native

    def close(self) -> None:
        return None


def test_controller_resolves_a_spoken_folder_and_opens_it_in_explorer(tmp_path: Path) -> None:
    root = tmp_path / "研究数据仓库"
    root.mkdir()
    index = FakeWorkMap({"wm-1": ("研究数据仓库", root)})
    native = FolderNative(str(root))
    native.path_open_state = lambda path: {  # type: ignore[method-assign]
        "kind": "explorer_directory",
        "verified": native.shown and str(path) == str(root),
        "foreground_hwnd": 7,
        "matching_hwnds": (7,) if native.shown else (),
    }
    inventory = json.dumps(
        [
            {
                "app": "claude",
                "process_name": "claude.exe",
                "window_title": "Claude",
                "foreground": True,
            }
        ]
    )
    driver = PathDriver(native, inventory)

    class NoPlanner:
        def decide(self, *args, **kwargs):
            raise AssertionError("planner must not run")

    controller = AssistiveController(
        native_router=RouterStub(index),
        driver=driver,
        planner=NoPlanner(),
        timeout_seconds=30,
    )

    result = controller.run(f"打开{root.drive[0].lower()}盘研究数据库那个文件夹")

    assert result.success
    assert native.opened == [str(root)]
