from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from handsfree_pc.config import load_settings
from handsfree_pc.models import Action, ActionType, Plan
from handsfree_pc.windows import (
    AmbiguousElementError,
    AmbiguousWindowError,
    DesktopUnavailableError,
    ElementMatch,
    InvalidHotkeyError,
    NativeWindows,
    PasswordFieldError,
    UIABackend,
    WindowActivationError,
    WindowInfo,
    WindowsExecutor,
    parse_hotkey,
)
from handsfree_pc.windows.native import KEYEVENTF_KEYUP, KEYEVENTF_UNICODE


@pytest.fixture
def settings(tmp_path):
    value = load_settings(tmp_path / "missing.yaml", allow_missing=True)
    value.execution.dry_run = False
    return value


class FakeNative:
    def __init__(self) -> None:
        self.window = WindowInfo(101, "Codex - HandsFreePC", 5001, "ChatGPT.exe")
        self.claude_window = WindowInfo(202, "Claude", 5002, "claude.exe")
        self.foreground = self.window.hwnd
        self.calls: list[tuple] = []

    def assert_interactive_desktop(self):
        self.calls.append(("assert_interactive_desktop",))
        return "Default"

    def find_windows(self, *, title_patterns, process_names):
        self.calls.append(("find_windows", tuple(title_patterns), tuple(process_names)))
        if "Claude" in title_patterns:
            return [self.claude_window]
        return [self.window]

    def get_foreground_window_info(self):
        if self.foreground == self.claude_window.hwnd:
            return self.claude_window
        if self.foreground == self.window.hwnd:
            return self.window
        return None

    def activate_window(self, hwnd):
        self.calls.append(("activate_window", hwnd))
        self.foreground = hwnd
        return self.claude_window if hwnd == self.claude_window.hwnd else self.window

    def assert_foreground(self, hwnd):
        self.calls.append(("assert_foreground", hwnd))
        if self.foreground != hwnd:
            raise WindowActivationError("focus changed")

    def open_path(self, path):
        self.calls.append(("open_path", str(path)))
        return "os.startfile"

    def wait_for_windows(self, *, title_patterns, process_names):
        self.calls.append(("wait_for_windows", tuple(title_patterns), tuple(process_names)))
        return self.find_windows(title_patterns=title_patterns, process_names=process_names)

    def send_hotkey(self, specification):
        self.calls.append(("send_hotkey", specification))
        return parse_hotkey(specification)

    def send_text(self, text):
        self.calls.append(("send_text", text))
        return len(text.encode("utf-16-le")) // 2


class FakeUIA:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.entry_runtime_id = (42, 7)

    def click_named(self, hwnd, names, *, control_types):
        normalized = [names] if isinstance(names, str) else list(names)
        self.calls.append(("click_named", hwnd, tuple(normalized), tuple(control_types)))
        return {
            "name": normalized[0],
            "control_type": tuple(control_types)[0],
            "score": 1.0,
            "exact": True,
            "operation": "click",
        }

    def focus_text_entry(self, hwnd):
        self.calls.append(("focus_text_entry", hwnd))
        return {
            "name": "Message Codex",
            "control_type": "Edit",
            "score": 1.0,
            "exact": True,
            "operation": "focus",
            "focus_verified": True,
            "runtime_id": self.entry_runtime_id,
        }

    def verify_focused_text_entry(self, hwnd):
        self.calls.append(("verify_focused_text_entry", hwnd))
        return {
            "name": "Message Codex",
            "control_type": "Edit",
            "operation": "verify_focus",
            "focus_verified": True,
            "password_field": False,
            "runtime_id": self.entry_runtime_id,
        }

    def verify_named_selected(self, hwnd, names, *, control_types):
        normalized = [names] if isinstance(names, str) else list(names)
        self.calls.append(("verify_named_selected", hwnd, tuple(normalized), tuple(control_types)))
        return {
            "name": normalized[0],
            "control_type": tuple(control_types)[0],
            "operation": "verify_selection",
            "selection_verified": True,
        }


def test_hotkey_parser_accepts_only_allow_listed_shortcuts():
    assert parse_hotkey("ctrl+shift+k") == (0x11, 0x10, ord("K"))
    assert parse_hotkey("F12") == (0x7B,)
    assert parse_hotkey("enter") == (0x0D,)

    for invalid in ("ctrl", "ctrl+k+p", "ctrl++k", "ctrl+k;calc", "volume_up"):
        with pytest.raises(InvalidHotkeyError):
            parse_hotkey(invalid)


class RecordingUser32:
    def __init__(self) -> None:
        self.events: list[tuple[int, int, int]] = []

    def SendInput(self, count, inputs, _size):
        for index in range(count):
            item = inputs[index]
            self.events.append((item.ki.wVk, item.ki.wScan, item.ki.dwFlags))
        return count


def test_unicode_text_uses_sendinput_without_clipboard():
    user32 = RecordingUser32()
    native = NativeWindows(user32=user32, shell32=object())

    units = native.send_text("娃A")

    assert units == 2
    assert user32.events == [
        (0, ord("娃"), KEYEVENTF_UNICODE),
        (0, ord("娃"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        (0, ord("A"), KEYEVENTF_UNICODE),
        (0, ord("A"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
    ]


def test_dry_run_never_constructs_or_calls_native_backend(settings):
    settings.execution.dry_run = True
    executor = WindowsExecutor(settings)
    plan = Plan(
        "dry",
        [
            Action(ActionType.ACTIVATE_APP, app="codex"),
            Action(
                ActionType.OPEN_CONVERSATION,
                app="codex",
                project="Demo",
                conversation="Design",
            ),
            Action(ActionType.ENTER_DICTATION, app="codex"),
            Action(ActionType.TYPE_TEXT, text="你好"),
        ],
    )

    results = executor.execute_plan(plan)

    assert all(result.success for result in results)
    assert all(result.evidence["dry_run"] for result in results)
    assert executor._native is None


def test_dry_run_still_rejects_non_allow_listed_configured_hotkey(settings):
    settings.execution.dry_run = True
    settings.apps["codex"].native_voice_hotkey = "ctrl+volume_up"
    executor = WindowsExecutor(settings)

    result = executor.execute(Action(ActionType.START_NATIVE_VOICE, app="codex"))

    assert not result.success
    assert result.evidence["error_type"] == InvalidHotkeyError.__name__
    assert executor._native is None


def test_open_path_resolves_then_uses_native_path_api(settings, tmp_path):
    target = tmp_path / "说明.txt"
    target.write_text("safe", encoding="utf-8")
    native = FakeNative()
    executor = WindowsExecutor(settings, native=native, uia=FakeUIA())

    result = executor.execute(Action(ActionType.OPEN_PATH, path=str(target)))

    assert result.success
    assert result.evidence["opened_via"] == "os.startfile"
    assert ("open_path", str(target.resolve())) in native.calls


def test_secure_desktop_blocks_path_dispatch(settings, tmp_path):
    class SecureDesktopNative(FakeNative):
        def assert_interactive_desktop(self):
            raise DesktopUnavailableError("secure desktop")

    target = tmp_path / "safe.txt"
    target.write_text("safe", encoding="utf-8")
    native = SecureDesktopNative()
    executor = WindowsExecutor(settings, native=native, uia=FakeUIA())

    result = executor.execute(Action(ActionType.OPEN_PATH, path=str(target)))

    assert not result.success
    assert result.evidence["error_type"] == "DesktopUnavailableError"
    assert "open_path" not in {call[0] for call in native.calls}


def test_prepare_plan_exposes_resolved_executable_suffix_to_safety(settings, tmp_path):
    target = tmp_path / "installer.exe"
    target.write_bytes(b"MZ")
    settings.execution.search_roots = [tmp_path]
    executor = WindowsExecutor(settings, native=FakeNative(), uia=FakeUIA())

    prepared = executor.prepare_plan(Plan("open", [Action(ActionType.OPEN_PATH, path="installer")]))

    assert prepared.actions[0].path == str(target.resolve())


def test_activate_app_uses_configured_identity_and_verifies_foreground(settings):
    native = FakeNative()
    executor = WindowsExecutor(settings, native=native, uia=FakeUIA())

    result = executor.execute(Action(ActionType.ACTIVATE_APP, app="codex"))

    assert result.success
    assert result.evidence["foreground_verified"] is True
    assert ("activate_window", 101) in native.calls
    assert ("assert_foreground", 101) in native.calls


def test_activate_app_fails_closed_for_multiple_nonforeground_windows(settings):
    class MultipleWindowNative(FakeNative):
        def find_windows(self, *, title_patterns, process_names):
            del title_patterns, process_names
            return [
                self.window,
                WindowInfo(303, "Codex - Other", 5003, "ChatGPT.exe"),
            ]

    native = MultipleWindowNative()
    native.foreground = 999
    executor = WindowsExecutor(settings, native=native, uia=FakeUIA())

    result = executor.execute(Action(ActionType.ACTIVATE_APP, app="codex"))

    assert not result.success
    assert result.evidence["error_type"] == AmbiguousWindowError.__name__
    assert result.evidence["candidate_count"] == 2
    assert "activate_window" not in {call[0] for call in native.calls}


def test_open_conversation_uses_exact_uia_targets_when_no_search_hotkey(settings):
    native = FakeNative()
    uia = FakeUIA()
    executor = WindowsExecutor(settings, native=native, uia=uia)
    action = Action(
        ActionType.OPEN_CONVERSATION,
        app="codex",
        project="Demo project",
        conversation="Voice design",
    )

    result = executor.execute(action)

    assert result.success
    assert result.evidence["method"] == "uia"
    assert [call[2] for call in uia.calls] == [("Demo project",), ("Voice design",)]
    assert native.calls.count(("assert_foreground", 101)) >= 3


def test_configured_search_hotkey_is_parsed_and_window_checked_before_input(settings):
    settings.apps["codex"].search_hotkey = "ctrl+k"
    native = FakeNative()
    uia = FakeUIA()
    executor = WindowsExecutor(settings, native=native, uia=uia)

    result = executor.execute(
        Action(
            ActionType.OPEN_CONVERSATION,
            app="codex",
            project="Demo",
            conversation="Design",
        )
    )

    assert result.success
    assert result.evidence["method"] == "configured_search_hotkey"
    assert ("send_hotkey", "ctrl+k") in native.calls
    assert ("send_text", "Demo / Design") in native.calls
    assert ("send_hotkey", "enter") in native.calls
    assert [call[0] for call in uia.calls] == ["verify_named_selected"]


def test_search_hotkey_rejects_control_characters_before_input(settings):
    settings.apps["codex"].search_hotkey = "ctrl+k"
    native = FakeNative()
    executor = WindowsExecutor(settings, native=native, uia=FakeUIA())

    result = executor.execute(
        Action(
            ActionType.OPEN_CONVERSATION,
            app="codex",
            project="Demo\r",
            conversation="Design",
        )
    )

    assert not result.success
    assert result.evidence["error_type"] == "ValueError"
    assert "send_text" not in {call[0] for call in native.calls}


def test_open_mode_selects_tab_then_named_mode(settings):
    native = FakeNative()
    uia = FakeUIA()
    executor = WindowsExecutor(settings, native=native, uia=uia)

    result = executor.execute(Action(ActionType.OPEN_MODE, app="claude", tab="Chat", mode="Design"))

    assert result.success
    assert [call[2] for call in uia.calls] == [("Chat",), ("Design",)]
    assert all(call[1] == 202 for call in uia.calls)


def test_own_dictation_focuses_entry_then_sends_unicode_to_same_window(settings):
    native = FakeNative()
    uia = FakeUIA()
    executor = WindowsExecutor(settings, native=native, uia=uia)

    ready = executor.execute(Action(ActionType.ENTER_DICTATION, app="codex"))
    typed = executor.execute(Action(ActionType.TYPE_TEXT, text="请继续设计"))
    sent = executor.execute(Action(ActionType.SEND_PROMPT))

    assert ready.success and typed.success and sent.success
    assert ready.evidence["uses_application_native_voice"] is False
    assert ("focus_text_entry", 101) in uia.calls
    assert uia.calls.count(("verify_focused_text_entry", 101)) == 2
    assert ("send_text", "请继续设计") in native.calls
    assert ("send_hotkey", "enter") in native.calls


def test_text_injection_fails_closed_if_focus_changes_after_dictation(settings):
    native = FakeNative()
    executor = WindowsExecutor(settings, native=native, uia=FakeUIA())
    assert executor.execute(Action(ActionType.ENTER_DICTATION, app="codex")).success
    native.foreground = 999

    result = executor.execute(Action(ActionType.TYPE_TEXT, text="不要输入到错误窗口"))

    assert not result.success
    assert result.evidence["error_type"] == "WindowActivationError"
    assert ("send_text", "不要输入到错误窗口") not in native.calls


def test_text_injection_fails_if_focus_moves_within_same_window(settings):
    native = FakeNative()
    uia = FakeUIA()
    executor = WindowsExecutor(settings, native=native, uia=uia)
    assert executor.execute(Action(ActionType.ENTER_DICTATION, app="codex")).success
    uia.entry_runtime_id = (99, 3)

    result = executor.execute(Action(ActionType.TYPE_TEXT, text="不要输入到搜索框"))

    assert not result.success
    assert result.evidence["error_type"] == "WindowsExecutionError"
    assert ("send_text", "不要输入到搜索框") not in native.calls


def test_text_entry_requires_explicit_own_dictation_state(settings):
    executor = WindowsExecutor(settings, native=FakeNative(), uia=FakeUIA())

    result = executor.execute(Action(ActionType.TYPE_TEXT, text="hello"))

    assert not result.success
    assert result.evidence["error_type"] == "WindowsExecutionError"


def test_dictation_rejects_unconfigured_current_window_even_in_dry_run(settings):
    settings.execution.dry_run = True
    executor = WindowsExecutor(settings)

    result = executor.execute(Action(ActionType.ENTER_DICTATION, app="current"))

    assert not result.success
    assert result.evidence["error_type"] == "WindowsExecutionError"
    assert executor._native is None


def test_text_injection_rejects_enter_control_character(settings):
    native = FakeNative()
    executor = WindowsExecutor(settings, native=native, uia=FakeUIA())
    assert executor.execute(Action(ActionType.ENTER_DICTATION, app="codex")).success

    result = executor.execute(Action(ActionType.TYPE_TEXT, text="do not submit\r"))

    assert not result.success
    assert result.evidence["error_type"] == "ValueError"
    assert ("send_text", "do not submit\r") not in native.calls


def test_native_voice_prefers_only_explicitly_configured_hotkey(settings):
    settings.apps["codex"].native_voice_hotkey = "ctrl+shift+v"
    native = FakeNative()
    uia = FakeUIA()
    executor = WindowsExecutor(settings, native=native, uia=uia)

    result = executor.execute(Action(ActionType.START_NATIVE_VOICE, app="codex"))

    assert result.success
    assert result.evidence["method"] == "configured_hotkey"
    assert ("send_hotkey", "ctrl+shift+v") in native.calls
    assert not uia.calls


def test_native_voice_uses_only_allow_listed_named_uia_button(settings):
    settings.apps["codex"].voice_button_names = ["Calibrated voice button"]
    native = FakeNative()
    uia = FakeUIA()
    executor = WindowsExecutor(settings, native=native, uia=uia)

    result = executor.execute(Action(ActionType.START_NATIVE_VOICE, app="codex"))

    assert result.success
    assert result.evidence["method"] == "named_uia_button"
    assert uia.calls[0][2] == tuple(settings.apps["codex"].voice_button_names)


def test_native_voice_fails_when_profile_has_no_authorized_mechanism(settings):
    settings.apps["codex"].native_voice_hotkey = None
    settings.apps["codex"].voice_button_names = []
    executor = WindowsExecutor(settings, native=FakeNative(), uia=FakeUIA())

    result = executor.execute(Action(ActionType.START_NATIVE_VOICE, app="codex"))

    assert not result.success
    assert result.evidence["error_type"] == "WindowsExecutionError"


@dataclass
class FakeElement:
    name: str
    control_type: str = "Button"
    automation_id: str = ""
    invoked: bool = False
    focused: bool = False
    is_password: bool = False
    selected: bool = False
    runtime_id: tuple[int, ...] = (1, 2, 3)

    def __post_init__(self):
        self.element_info = SimpleNamespace(
            name=self.name,
            control_type=self.control_type,
            automation_id=self.automation_id,
            runtime_id=self.runtime_id,
        )

    def window_text(self):
        return self.name

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def invoke(self):
        self.invoked = True
        self.selected = True

    def is_selected(self):
        return self.selected

    def set_focus(self):
        self.focused = True

    def has_keyboard_focus(self):
        return self.focused


class FakeRoot:
    def __init__(self, elements):
        self.elements = elements

    def wait(self, _condition, timeout):
        assert timeout > 0

    def descendants(self):
        return self.elements


class FakeDesktop:
    def __init__(self, elements):
        self.root = FakeRoot(elements)

    def window(self, *, handle):
        assert handle == 77
        return self.root


def desktop_factory(elements):
    def build(*, backend):
        assert backend == "uia"
        return FakeDesktop(elements)

    return build


def test_uia_prefers_exact_match_and_reports_evidence():
    wanted = FakeElement("Design")
    backend = UIABackend(
        desktop_factory=desktop_factory([FakeElement("Designer tools"), wanted]),
        threshold=0.7,
    )

    evidence = backend.click_named(77, "Design")

    assert wanted.invoked
    assert evidence["name"] == "Design"
    assert evidence["exact"] is True
    assert evidence["method"] == "invoke"
    assert evidence["postcondition_verified"] is True


def test_uia_fuzzy_match_fails_closed_when_top_candidates_are_ambiguous():
    backend = UIABackend(
        desktop_factory=desktop_factory([FakeElement("Design one"), FakeElement("Design two")]),
        threshold=0.6,
        ambiguity_margin=0.1,
    )

    with pytest.raises(AmbiguousElementError) as caught:
        backend.click_named(77, "Design")

    assert len(caught.value.candidates) == 2
    assert all(isinstance(item, ElementMatch) for item in caught.value.candidates)


def test_uia_refuses_password_field_before_focus_or_typing():
    password = FakeElement(
        "Message",
        control_type="Edit",
        focused=True,
        is_password=True,
    )
    backend = UIABackend(desktop_factory=desktop_factory([password]))

    with pytest.raises(PasswordFieldError):
        backend.focus_text_entry(77)
    with pytest.raises(PasswordFieldError):
        backend.verify_focused_text_entry(77)

    assert password.focused is True
