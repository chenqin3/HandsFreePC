from __future__ import annotations

from handsfree_pc.desktop.assistive.skills.app_activation import AppActivationSkill
from handsfree_pc.windows.native import NativeWindowsError, WindowInfo


def app_window(hwnd: int, process_name: str, title: str) -> WindowInfo:
    return WindowInfo(hwnd, title, 4000 + hwnd, process_name, "ApplicationFrameWindow")


class FakeNative:
    def __init__(
        self,
        windows: list[WindowInfo],
        *,
        foreground_hwnd: int | None,
        activation_result_hwnd: int | None = None,
    ) -> None:
        self.windows = list(windows)
        self.foreground_hwnd = foreground_hwnd
        self.activation_result_hwnd = activation_result_hwnd
        self.interactive_checks = 0
        self.activate_calls: list[int] = []
        self.foreground_checks: list[int] = []

    def assert_interactive_desktop(self) -> None:
        self.interactive_checks += 1

    def enumerate_windows(self) -> list[WindowInfo]:
        return list(self.windows)

    def get_foreground_window_info(self) -> WindowInfo | None:
        return next(
            (item for item in self.windows if item.hwnd == self.foreground_hwnd),
            None,
        )

    def activate_window(self, hwnd: int) -> WindowInfo:
        self.activate_calls.append(hwnd)
        selected = next(item for item in self.windows if item.hwnd == hwnd)
        returned = next(
            (
                item
                for item in self.windows
                if item.hwnd == self.activation_result_hwnd
            ),
            selected,
        )
        self.foreground_hwnd = returned.hwnd
        return returned

    def assert_foreground(self, hwnd: int) -> None:
        self.foreground_checks.append(hwnd)
        if self.foreground_hwnd != hwnd:
            raise NativeWindowsError("selected window is not foreground")


def test_multiple_matching_windows_keep_the_exact_foreground_match() -> None:
    first = app_window(101, "Claude.exe", "Claude - first")
    foreground = app_window(202, "Claude.exe", "Claude - current")
    native = FakeNative([first, foreground], foreground_hwnd=foreground.hwnd)

    result = AppActivationSkill(native, {}).activate("Claude")

    assert result.status == "succeeded"
    assert result.details == {"hwnd": foreground.hwnd, "activated": False}
    assert native.interactive_checks == 1
    assert native.activate_calls == []
    assert native.foreground_checks == []


def test_no_foreground_match_activates_z_order_first_and_verifies_exact_hwnd() -> None:
    unrelated = app_window(900, "other.exe", "Other")
    first = app_window(101, "Claude.exe", "Claude - first")
    second = app_window(202, "Claude.exe", "Claude - second")
    native = FakeNative(
        [unrelated, first, second],
        foreground_hwnd=unrelated.hwnd,
    )

    result = AppActivationSkill(native, {}).activate("Claude")

    assert result.status == "succeeded"
    assert result.details == {"hwnd": first.hwnd, "activated": True}
    assert native.activate_calls == [first.hwnd]
    assert native.foreground_checks == [first.hwnd]
    assert native.foreground_hwnd == first.hwnd


def test_activation_rejects_a_different_returned_window_before_foreground_check() -> None:
    unrelated = app_window(900, "other.exe", "Other")
    selected = app_window(101, "Claude.exe", "Claude")
    changed = app_window(202, "Claude.exe", "Claude - replaced")
    native = FakeNative(
        [unrelated, selected, changed],
        foreground_hwnd=unrelated.hwnd,
        activation_result_hwnd=changed.hwnd,
    )

    result = AppActivationSkill(native, {}).activate("Claude")

    assert result.status == "retryable_failure"
    assert native.activate_calls == [selected.hwnd]
    assert native.foreground_checks == []


def test_terminal_process_is_a_hard_failure_even_when_already_foreground() -> None:
    terminal = app_window(303, "powershell.exe", "Administrator: Windows PowerShell")
    native = FakeNative([terminal], foreground_hwnd=terminal.hwnd)

    result = AppActivationSkill(native, {}).activate("PowerShell")

    assert result.status == "hard_failure"
    assert result.details["process_name"] == "powershell.exe"
    assert native.activate_calls == []
    assert native.foreground_checks == []
