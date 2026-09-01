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
    ElementNotFoundError,
    InvalidHotkeyError,
    NativeWindows,
    NativeWindowsError,
    PasswordFieldError,
    UIABackend,
    UIAError,
    UIAPostconditionError,
    WindowActivationError,
    WindowInfo,
    WindowNotFoundError,
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

    def path_open_state(self, path):
        self.calls.append(("path_open_state", str(path)))
        return {"verified": False, "foreground_hwnd": self.foreground}

    def wait_for_path_open(self, path, *, before):
        self.calls.append(("wait_for_path_open", str(path), before["foreground_hwnd"]))
        return {
            "postcondition_verified": True,
            "verification_kind": "test_fixture",
            "foreground_changed": True,
        }

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

    def click_named_exact(self, hwnd, names, *, control_types):
        return self.click_named(hwnd, names, control_types=control_types)

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

    def verify_focused_text_contains(self, hwnd, expected):
        self.calls.append(("verify_focused_text_contains", hwnd, expected))
        return {
            "name": "Message Codex",
            "control_type": "Edit",
            "operation": "verify_text_value",
            "focus_verified": True,
            "input_text_verified": True,
            "character_count": len(expected),
            "runtime_id": self.entry_runtime_id,
        }

    def verify_named_selected(self, hwnd, names, *, control_types):
        normalized = [names] if isinstance(names, str) else list(names)
        self.calls.append(("verify_named_selected", hwnd, tuple(normalized), tuple(control_types)))
        return {
            "name": normalized[0],
            "control_type": tuple(control_types)[0],
            "exact": True,
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
    assert result.evidence["postcondition_verified"] is True
    assert ("open_path", str(target.resolve())) in native.calls


def test_open_path_refuses_a_postcondition_that_was_already_true(settings, tmp_path):
    class AlreadyOpenNative(FakeNative):
        def path_open_state(self, path):
            self.calls.append(("path_open_state", str(path)))
            return {"verified": True, "foreground_hwnd": self.foreground}

    target = tmp_path / "already-open.txt"
    target.write_text("safe", encoding="utf-8")
    native = AlreadyOpenNative()
    executor = WindowsExecutor(settings, native=native, uia=FakeUIA())

    result = executor.execute(Action(ActionType.OPEN_PATH, path=str(target)))

    assert not result.success
    assert "already open" in result.message
    assert "open_path" not in {call[0] for call in native.calls}


def test_file_title_match_without_new_foreground_window_is_not_verified(monkeypatch):
    clock = [0.0]

    def sleeper(duration):
        clock[0] += duration

    native = NativeWindows(
        user32=object(),
        shell32=object(),
        monotonic=lambda: clock[0],
        sleeper=sleeper,
    )
    monkeypatch.setattr(
        native,
        "path_open_state",
        lambda _path: {
            "kind": "foreground_document_title",
            "verified": True,
            "foreground_hwnd": 101,
        },
    )

    with pytest.raises(NativeWindowsError, match="verified foreground"):
        native.wait_for_path_open(
            "report.txt",
            before={"verified": False, "foreground_hwnd": 101},
            timeout=0.2,
        )


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


class InitiallyHiddenNative(FakeNative):
    def __init__(self, *, restore_after_hotkey: bool) -> None:
        super().__init__()
        self.restore_after_hotkey = restore_after_hotkey
        self.visible = False
        self.foreground = 999

    def find_windows(self, *, title_patterns, process_names):
        self.calls.append(("find_windows", tuple(title_patterns), tuple(process_names)))
        return [self.window] if self.visible else []

    def wait_for_windows(self, *, title_patterns, process_names):
        self.calls.append(("wait_for_windows", tuple(title_patterns), tuple(process_names)))
        return self.find_windows(
            title_patterns=title_patterns,
            process_names=process_names,
        )

    def send_hotkey(self, specification):
        self.calls.append(("send_hotkey", specification))
        keys = parse_hotkey(specification)
        if self.restore_after_hotkey:
            self.visible = True
        return keys


def test_activate_app_restores_hidden_window_with_configured_global_hotkey(settings):
    settings.apps["codex"].activation_hotkey = "ctrl+alt+w"
    native = InitiallyHiddenNative(restore_after_hotkey=True)
    executor = WindowsExecutor(settings, native=native, uia=FakeUIA())

    result = executor.execute(Action(ActionType.ACTIVATE_APP, app="codex"))

    assert result.success
    assert result.evidence["activation_hotkey_attempted"] is True
    assert result.evidence["activation_hotkey_restored"] is True
    assert result.evidence["foreground_verified"] is True
    assert ("send_hotkey", "ctrl+alt+w") in native.calls
    assert ("activate_window", 101) in native.calls
    assert native.calls.index(("send_hotkey", "ctrl+alt+w")) < native.calls.index(
        ("activate_window", 101)
    )


def test_activate_app_fails_closed_when_hotkey_does_not_restore_window(settings):
    settings.apps["codex"].activation_hotkey = "ctrl+alt+w"
    native = InitiallyHiddenNative(restore_after_hotkey=False)
    executor = WindowsExecutor(settings, native=native, uia=FakeUIA())

    result = executor.execute(Action(ActionType.ACTIVATE_APP, app="codex"))

    assert not result.success
    assert result.evidence["error_type"] == WindowNotFoundError.__name__
    assert ("send_hotkey", "ctrl+alt+w") in native.calls
    assert "activate_window" not in {call[0] for call in native.calls}


def test_activate_app_without_hotkey_keeps_existing_no_window_behavior(settings):
    settings.apps["codex"].activation_hotkey = None
    native = InitiallyHiddenNative(restore_after_hotkey=True)
    executor = WindowsExecutor(settings, native=native, uia=FakeUIA())

    result = executor.execute(Action(ActionType.ACTIVATE_APP, app="codex"))

    assert not result.success
    assert result.evidence["error_type"] == WindowNotFoundError.__name__
    assert "send_hotkey" not in {call[0] for call in native.calls}
    assert "wait_for_windows" not in {call[0] for call in native.calls}


def test_activate_app_rejects_invalid_activation_hotkey_before_os_input(settings):
    settings.apps["codex"].activation_hotkey = "ctrl+w;calc"
    native = InitiallyHiddenNative(restore_after_hotkey=True)
    executor = WindowsExecutor(settings, native=native, uia=FakeUIA())

    result = executor.execute(Action(ActionType.ACTIVATE_APP, app="codex"))

    assert not result.success
    assert result.evidence["error_type"] == InvalidHotkeyError.__name__
    assert "send_hotkey" not in {call[0] for call in native.calls}
    assert "activate_window" not in {call[0] for call in native.calls}


def test_activation_hotkey_failure_can_continue_to_executable_fallback(settings, tmp_path):
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture")
    settings.apps["codex"].activation_hotkey = "ctrl+alt+w"
    settings.apps["codex"].executable = executable

    class LaunchableHiddenNative(InitiallyHiddenNative):
        def open_path(self, path):
            result = super().open_path(path)
            self.visible = True
            return result

    native = LaunchableHiddenNative(restore_after_hotkey=False)
    executor = WindowsExecutor(settings, native=native, uia=FakeUIA())

    result = executor.execute(Action(ActionType.ACTIVATE_APP, app="codex"))

    assert result.success
    assert result.evidence["activation_hotkey_attempted"] is True
    assert result.evidence["activation_hotkey_restored"] is False
    assert result.evidence["launched"] is True
    assert ("open_path", str(executable)) in native.calls


def test_dry_activation_reports_hotkey_attempt_without_exposing_shortcut(settings):
    settings.execution.dry_run = True
    executor = WindowsExecutor(settings)

    result = executor.execute(Action(ActionType.ACTIVATE_APP, app="wechat"))

    assert result.success
    assert result.evidence["would_try_activation_hotkey_if_no_visible_window"] is True
    assert "ctrl+alt+w" not in repr(result.evidence)
    assert executor._native is None


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
    assert [call[2] for call in uia.calls if call[0] == "click_named"] == [
        ("Demo project",),
        ("Voice design",),
    ]
    assert result.evidence["postcondition_verified"] is True
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
    assert [call[2] for call in uia.calls if call[0] == "click_named"] == [
        ("Chat and Cowork",),
        ("Design",),
    ]
    assert all(call[1] == 202 for call in uia.calls)
    assert result.evidence["postcondition_verified"] is True


def test_open_mode_retries_a_transient_uia_snapshot_failure(settings):
    class TransientSnapshotUIA(FakeUIA):
        def __init__(self) -> None:
            super().__init__()
            self.click_attempts = 0

        def click_named_exact(self, hwnd, names, *, control_types):
            self.click_attempts += 1
            if self.click_attempts == 1:
                raise UIAError("Could not snapshot UI elements")
            return super().click_named_exact(hwnd, names, control_types=control_types)

    delays: list[float] = []
    uia = TransientSnapshotUIA()
    executor = WindowsExecutor(
        settings,
        native=FakeNative(),
        uia=uia,
        sleeper=delays.append,
    )

    result = executor.execute(Action(ActionType.OPEN_MODE, app="claude", mode="Code"))

    assert result.success
    assert uia.click_attempts == 2
    assert delays == [0.15]


def test_open_mode_does_not_treat_focus_only_as_selected(settings):
    class FocusOnlyUIA(FakeUIA):
        def click_named(self, hwnd, names, *, control_types):
            evidence = super().click_named(hwnd, names, control_types=control_types)
            evidence.update({"postcondition": "focused", "postcondition_verified": True})
            return evidence

        def verify_named_selected(self, hwnd, names, *, control_types):
            self.calls.append(("verify_named_selected", hwnd, (names,), tuple(control_types)))
            raise UIAPostconditionError("not selected")

    executor = WindowsExecutor(settings, native=FakeNative(), uia=FocusOnlyUIA())

    result = executor.execute(Action(ActionType.OPEN_MODE, app="claude", mode="Code"))

    assert not result.success
    assert result.evidence["error_type"] == "WindowsExecutionError"


def test_open_mode_rejects_unmapped_label_before_activation_or_click(settings):
    native = FakeNative()
    uia = FakeUIA()
    executor = WindowsExecutor(settings, native=native, uia=uia)

    result = executor.execute(Action(ActionType.OPEN_MODE, app="claude", mode="Settings"))

    assert not result.success
    assert result.evidence["error_type"] == "WindowsExecutionError"
    assert "activate_window" not in {call[0] for call in native.calls}
    assert not uia.calls


def test_own_dictation_types_unicode_but_unverified_submission_fails_closed(settings):
    native = FakeNative()
    uia = FakeUIA()
    executor = WindowsExecutor(settings, native=native, uia=uia)

    ready = executor.execute(Action(ActionType.ENTER_DICTATION, app="codex"))
    typed = executor.execute(Action(ActionType.TYPE_TEXT, text="请继续设计"))
    sent = executor.execute(Action(ActionType.SEND_PROMPT))

    assert ready.success and typed.success and not sent.success
    assert ready.evidence["uses_application_native_voice"] is False
    assert ("focus_text_entry", 101) in uia.calls
    assert uia.calls.count(("verify_focused_text_entry", 101)) == 1
    assert ("verify_focused_text_contains", 101, "请继续设计") in uia.calls
    assert ("send_text", "请继续设计") in native.calls
    assert ("send_hotkey", "enter") not in native.calls
    assert "postcondition" in sent.message


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


def test_native_voice_hotkey_fails_closed_without_active_state_postcondition(settings):
    settings.apps["codex"].native_voice_hotkey = "ctrl+shift+v"
    native = FakeNative()
    uia = FakeUIA()
    executor = WindowsExecutor(settings, native=native, uia=uia)

    result = executor.execute(Action(ActionType.START_NATIVE_VOICE, app="codex"))

    assert not result.success
    assert result.evidence["error_type"] == "WindowsExecutionError"
    assert ("send_hotkey", "ctrl+shift+v") not in native.calls
    assert not uia.calls


def test_native_voice_button_fails_closed_without_active_state_postcondition(settings):
    settings.apps["codex"].voice_button_names = ["Calibrated voice button"]
    native = FakeNative()
    uia = FakeUIA()
    executor = WindowsExecutor(settings, native=native, uia=uia)

    result = executor.execute(Action(ActionType.START_NATIVE_VOICE, app="codex"))

    assert not result.success
    assert result.evidence["error_type"] == "WindowsExecutionError"
    assert not uia.calls
    assert ("activate_window", 101) not in native.calls


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


def test_uia_falls_back_to_verified_click_when_element_has_no_invoke_capability():
    class ClickOnlyElement(FakeElement):
        invoke = None

        def click_input(self):
            self.selected = True

    wanted = ClickOnlyElement("Chat")
    backend = UIABackend(
        desktop_factory=desktop_factory([wanted]),
        foreground_guard=lambda _hwnd: None,
    )

    evidence = backend.click_named(77, "Chat")

    assert evidence["method"] == "click_input"
    assert evidence["postcondition"] == "selected"
    assert evidence["postcondition_verified"] is True


def test_uia_never_retries_failed_invoke_through_physical_click() -> None:
    class UnknownResultElement(FakeElement):
        physical_clicks = 0

        def invoke(self):
            self.selected = True
            raise RuntimeError("provider result unknown")

        def click_input(self):
            self.physical_clicks += 1

    wanted = UnknownResultElement("Chat")
    backend = UIABackend(
        desktop_factory=desktop_factory([wanted]),
        foreground_guard=lambda _hwnd: None,
    )

    with pytest.raises(UIAError, match="Could not activate"):
        backend.click_named_exact(77, "Chat")

    assert wanted.physical_clicks == 0


def test_uia_checks_foreground_immediately_before_physical_click() -> None:
    class ClickOnlyElement(FakeElement):
        invoke = None
        physical_clicks = 0

        def click_input(self):
            self.physical_clicks += 1

    wanted = ClickOnlyElement("Chat")

    def stolen_focus(_hwnd):
        raise WindowActivationError("focus changed")

    backend = UIABackend(
        desktop_factory=desktop_factory([wanted]),
        foreground_guard=stolen_focus,
    )

    with pytest.raises(UIAError, match="Could not activate"):
        backend.click_named_exact(77, "Chat")

    assert wanted.physical_clicks == 0


def test_uia_can_re_resolve_exact_target_and_verify_focus_after_tree_change():
    wanted = FakeElement("Chat", focused=True)
    backend = UIABackend(desktop_factory=desktop_factory([wanted]))

    evidence = backend.verify_named_focused(77, "Chat")

    assert evidence["exact"] is True
    assert evidence["focus_verified"] is True


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


def test_uia_single_fuzzy_match_is_rejected_before_any_click() -> None:
    fuzzy = FakeElement("Designer tools")
    backend = UIABackend(
        desktop_factory=desktop_factory([fuzzy]),
        threshold=0.6,
    )

    with pytest.raises(ElementNotFoundError, match="No exact"):
        backend.click_named_exact(77, "Design")

    assert not fuzzy.invoked


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


def test_uia_rechecks_exact_identity_immediately_before_invoke() -> None:
    class RenamingElement(FakeElement):
        reads = 0

        def window_text(self):
            self.reads += 1
            return "Chat" if self.reads <= 2 else "Delete"

    target = RenamingElement("Chat")
    backend = UIABackend(desktop_factory=desktop_factory([target]))

    with pytest.raises(UIAError, match="identity changed"):
        backend.click_named_exact(77, "Chat")

    assert not target.invoked
