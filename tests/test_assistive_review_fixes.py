"""Regression tests for the review of the file/folder, WeChat, and web flows."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from types import SimpleNamespace

from test_assistive_files import (
    FakeWorkMap,
    FolderNative,
    FrameDriver,
    PathDriver,
    RouterStub,
    SendNative,
    _frame,
)

from handsfree_pc.desktop.assistive.controller import AssistiveController
from handsfree_pc.desktop.assistive.models import Goal, GoalKind
from handsfree_pc.desktop.assistive.policy import AssistivePolicy
from handsfree_pc.desktop.assistive.skills.app_launch import AppLaunchSkill
from handsfree_pc.desktop.assistive.skills.wechat_files import WeChatSendFileSkill
from handsfree_pc.desktop.assistive.spoken_paths import SpokenPathResolver, has_location_cue
from handsfree_pc.desktop.assistive.task_parser import parse_task

# --- parser ----------------------------------------------------------------------


def test_go_is_an_activation_verb_only_before_a_known_app() -> None:
    assert parse_task("去年的报表在哪里").goals[0].kind == GoalKind.FREE_FORM
    assert parse_task("去掉这个文件").goals[0].kind == GoalKind.FREE_FORM
    assert parse_task("去chrome").goals == (Goal(GoalKind.APP_FOREGROUND, "chrome"),)


def test_clause_split_never_cuts_inside_a_name() -> None:
    task = parse_task("切换到资源管理器 打开并购数据文件夹")
    assert task.goals[1].target == "打开 并购数据文件夹"
    chained = parse_task("切换到 Chrome 打开 google.com 再打开 github.com")
    assert [goal.target for goal in chained.goals] == ["chrome", "google.com", "打开 github.com"]


def test_only_an_explicit_new_chat_clause_is_implied_by_the_front_page() -> None:
    kept = parse_task("切换到 Chrome 打开 claude.ai 然后打开聊天")
    assert kept.goals[-1] == Goal(GoalKind.FREE_FORM, "打开聊天", app="chrome")
    dropped = parse_task("切换到 Chrome 打开 chatgpt.com 然后开一个新对话")
    assert [goal.kind for goal in dropped.goals] == [GoalKind.APP_FOREGROUND, GoalKind.URL_LOADED]


def test_send_file_parse_honours_forbid_and_defaults_self_transfer_to_wechat() -> None:
    attach = parse_task("把下载里的a发到微信文件传输助手但是不要发送")
    assert attach.forbid_submit is True
    assert attach.goals[1] == Goal(GoalKind.CONVERSATION_SELECTED, "文件传输助手", app="wechat")
    implicit = parse_task("把报告发给文件传输助手")
    assert implicit.goals == (
        Goal(GoalKind.APP_FOREGROUND, "wechat"),
        Goal(GoalKind.CONVERSATION_SELECTED, "文件传输助手", app="wechat"),
        Goal(GoalKind.FILE_SENT, "报告", app="wechat"),
    )


# --- resolver --------------------------------------------------------------------


def test_resolver_never_offers_executables_but_can_suggest(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "安装程序.exe").write_bytes(b"MZ")
    (downloads / "安装说明.txt").write_text("x", encoding="utf-8")
    resolver = SpokenPathResolver(home=tmp_path)

    assert resolver.resolve("下载里的安装程序") is None
    assert resolver.suggest("下载里的安装说明") == ["安装说明.txt"]


def test_strict_resolution_rejects_a_project_that_merely_contains_the_word(
    tmp_path: Path,
) -> None:
    root = tmp_path / "记事本工具"
    root.mkdir()
    index = FakeWorkMap({"wm-1": ("记事本工具", root)})
    resolver = SpokenPathResolver(workmap_index=index, home=tmp_path)

    assert resolver.resolve("记事本", strict=False) is not None
    assert resolver.resolve("记事本", strict=True) is None
    assert has_location_cue("研究数据库") and not has_location_cue("记事本")


# --- send skill ------------------------------------------------------------------


def _send_skill(native, driver):
    skill = WeChatSendFileSkill(native, driver, AssistivePolicy(), sleeper=lambda _s: None)
    skill._helper._frame_size = lambda observation, regions: (2952, 1866)  # type: ignore[method-assign]
    return skill


def test_send_file_refuses_when_another_chat_is_open(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    frame = _frame(1, [("别的群", (1200, 100, 1500, 150)), ("发送", (2780, 1750, 2860, 1790))])
    native = SendNative()
    driver = FrameDriver([frame])

    result = _send_skill(native, driver).send(
        parse_task("把a发送到微信的文件传输助手"),
        app="weixin-1",
        hwnd=777,
        path=target,
        conversation="文件传输助手",
    )

    assert result.status == "retryable_failure"
    assert result.details["wrong_conversation"] is True
    assert native.keystrokes() == []
    assert driver.actions == []


def test_send_file_only_attaches_when_the_user_said_not_to_send(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    frame = _frame(
        1, [("文件传输助手", (1200, 100, 1500, 150)), ("发送", (2780, 1750, 2860, 1790))]
    )
    native = SendNative()
    driver = FrameDriver([frame])

    result = _send_skill(native, driver).send(
        parse_task("把a发到微信文件传输助手但是不要发送"),
        app="weixin-1",
        hwnd=777,
        path=target,
        conversation="文件传输助手",
    )

    assert result.status == "succeeded"
    assert result.details["sent"] is False
    assert native.keystrokes() == [("clipboard", (str(target),)), ("hotkey", "ctrl+v")]
    assert len(driver.actions) == 1


def test_send_file_reports_unverified_when_the_chat_does_not_show_it(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    frame = _frame(
        1, [("文件传输助手", (1200, 100, 1500, 150)), ("发送", (2780, 1750, 2860, 1790))]
    )
    native = SendNative()
    driver = FrameDriver([frame])

    result = _send_skill(native, driver).send(
        parse_task("把a发送到微信的文件传输助手"),
        app="weixin-1",
        hwnd=777,
        path=target,
        conversation="文件传输助手",
    )

    assert result.status == "unverified"
    assert result.details["via"] == "button"
    assert ("hotkey", "enter") not in native.keystrokes()


def test_send_file_restores_the_clipboard_text(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("x", encoding="utf-8")
    frame = _frame(
        1, [("文件传输助手", (1200, 100, 1500, 150)), ("发送", (2780, 1750, 2860, 1790))]
    )

    class ClipboardNative(SendNative):
        def read_clipboard_text(self):
            return "之前复制的文字"

        def set_clipboard_text(self, text):
            self.events.append(("restore", text))

    native = ClipboardNative()
    _send_skill(native, FrameDriver([frame])).send(
        parse_task("把a发到微信文件传输助手但是不要发送"),
        app="weixin-1",
        hwnd=777,
        path=target,
        conversation="文件传输助手",
    )

    kinds = [event[0] for event in native.events if event[0] in {"clipboard", "hotkey", "restore"}]
    assert kinds == ["clipboard", "hotkey", "restore"]
    assert ("restore", "之前复制的文字") in native.events


# --- app launch --------------------------------------------------------------------


class LaunchNative:
    def __init__(self) -> None:
        self.windows: list[SimpleNamespace] = []
        self.launched: list[str] = []
        self.foreground: SimpleNamespace | None = None

    def enumerate_windows(self):
        return list(self.windows)

    def get_foreground_window_info(self):
        return self.foreground

    def shell_execute_path(self, path):
        self.launched.append(str(path))
        self.windows.append(
            SimpleNamespace(
                hwnd=55, title="无标题 - 记事本", process_name="notepad.exe", class_name="Notepad"
            )
        )
        return "ShellExecuteW"

    def activate_window(self, hwnd, timeout=2.0):
        self.foreground = next(window for window in self.windows if window.hwnd == hwnd)
        return self.foreground

    def assert_foreground(self, hwnd):
        return None

    def assert_interactive_desktop(self):
        return None


def test_app_launch_resolves_aliases_and_start_menu_shortcuts(tmp_path: Path, monkeypatch) -> None:
    programs = tmp_path / "Programs"
    programs.mkdir()
    (programs / "Obsidian.lnk").write_bytes(b"x")
    (programs / "Uninstall Obsidian.lnk").write_bytes(b"x")
    monkeypatch.setattr(
        "handsfree_pc.desktop.assistive.skills.app_launch.shutil.which",
        lambda name: f"C:/Windows/{name}",
    )
    skill = AppLaunchSkill(LaunchNative(), {}, start_menu_dirs=[programs])

    assert skill.resolve("记事本").path == Path("C:/Windows/notepad.exe")
    assert skill.resolve("obsidian").path == programs / "Obsidian.lnk"
    assert skill.resolve("powershell") is None
    assert skill.resolve("卸载 obsidian") is None
    assert skill.resolve("不存在的软件") is None


def test_app_launch_waits_for_the_new_window_and_activates_it(monkeypatch) -> None:
    monkeypatch.setattr(
        "handsfree_pc.desktop.assistive.skills.app_launch.shutil.which",
        lambda name: f"C:/Windows/{name}",
    )
    native = LaunchNative()
    clock = itertools.count()
    skill = AppLaunchSkill(
        native, {}, start_menu_dirs=[], sleeper=lambda _s: None, monotonic=lambda: next(clock)
    )

    result = skill.launch("记事本")

    assert result.status == "succeeded"
    assert result.details["hwnd"] == 55
    assert [Path(item) for item in native.launched] == [Path("C:/Windows/notepad.exe")]
    assert native.foreground is not None and native.foreground.hwnd == 55


# --- controller ----------------------------------------------------------------------


class NoPlanner:
    def decide(self, *args, **kwargs):
        raise AssertionError("planner must not run")


def _inventory(app: str, process: str) -> str:
    return json.dumps(
        [{"app": app, "process_name": process, "window_title": app, "foreground": True}]
    )


def test_controller_refuses_to_run_an_executable_path(tmp_path: Path) -> None:
    exe = tmp_path / "setup.exe"
    exe.write_bytes(b"MZ")
    native = FolderNative(str(tmp_path))
    driver = PathDriver(native, _inventory("claude", "claude.exe"))
    controller = AssistiveController(
        native_router=RouterStub(), driver=driver, planner=NoPlanner(), timeout_seconds=30
    )

    result = controller.run(f"打开路径 {exe}")

    assert not result.success
    assert result.error_code == "ASSISTIVE_POLICY_REJECTED"
    assert native.opened == []


def test_controller_fails_fast_when_the_file_to_send_is_missing(
    tmp_path: Path, monkeypatch
) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "季度报告.xlsx").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        AssistiveController,
        "_spoken_path_resolver",
        lambda self: SpokenPathResolver(home=tmp_path),
    )
    driver = PathDriver(FolderNative(str(tmp_path)), _inventory("weixin-1", "Weixin.exe"))
    controller = AssistiveController(
        native_router=RouterStub(), driver=driver, planner=NoPlanner(), timeout_seconds=30
    )

    result = controller.run("把下载文件夹里的年度报告表格发送到微信的文件传输助手")

    assert not result.success
    assert result.error_code == "ASSISTIVE_FILE_NOT_FOUND"
    assert "季度报告.xlsx" in result.message


def test_controller_rejects_file_sending_to_unsupported_apps(tmp_path: Path, monkeypatch) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "季度总结·离线版.html").write_text("x", encoding="utf-8")
    monkeypatch.setattr(
        AssistiveController,
        "_spoken_path_resolver",
        lambda self: SpokenPathResolver(home=tmp_path),
    )
    driver = PathDriver(FolderNative(str(tmp_path)), _inventory("claude", "claude.exe"))
    controller = AssistiveController(
        native_router=RouterStub(), driver=driver, planner=NoPlanner(), timeout_seconds=30
    )

    result = controller.run("把下载文件夹里的季度总结网页发送到claude的项目周报")

    assert not result.success
    assert result.error_code == "ASSISTIVE_UNSUPPORTED_TARGET"


def test_controller_does_not_turn_switch_requests_into_folders(tmp_path: Path) -> None:
    root = tmp_path / "记事本工具"
    root.mkdir()
    native = FolderNative(str(root))
    driver = PathDriver(native, _inventory("claude", "claude.exe"))
    controller = AssistiveController(
        native_router=RouterStub(FakeWorkMap({"wm-1": ("记事本工具", root)})),
        driver=driver,
        planner=None,
        timeout_seconds=30,
    )

    result = controller.run("切换到记事本")

    assert not result.success
    assert native.opened == []


def test_chat_window_prefers_the_main_wechat_window_over_viewers() -> None:
    inventory = json.dumps(
        [
            {
                "app": "weixin-viewer",
                "process_name": "Weixin.exe",
                "window_title": "图片和视频",
                "foreground": True,
            },
            {
                "app": "weixin-main",
                "process_name": "Weixin.exe",
                "window_title": "微信",
                "foreground": False,
            },
        ]
    )

    assert AssistiveController._chat_window(inventory, "wechat") == "weixin-main"
    assert AssistiveController._chat_window(inventory, "claude") is None
