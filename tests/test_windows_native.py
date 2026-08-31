from __future__ import annotations

import ctypes

import pytest

from handsfree_pc.windows.native import (
    ERROR_ACCESS_DENIED,
    ForegroundIntegrityBoundary,
    NativeWindows,
    WindowActivationError,
    WindowInfo,
)


class FakeKernel32:
    def GetCurrentThreadId(self):
        return 10


class ActivationUser32:
    def __init__(
        self,
        *,
        promote_after_attach: bool = False,
        attach_failures: dict[int, int] | None = None,
    ) -> None:
        self.target = 101
        self.foreground = 202
        self.promote_after_attach = promote_after_attach
        self.attach_failures = attach_failures or {}
        self.attached: list[int] = []
        self.calls: list[tuple[object, ...]] = []

    def IsIconic(self, hwnd):
        self.calls.append(("is_iconic", int(hwnd)))
        return False

    def ShowWindow(self, hwnd, command):
        self.calls.append(("show", int(hwnd), int(command)))
        return True

    def BringWindowToTop(self, hwnd):
        self.calls.append(("bring", int(hwnd)))
        return True

    def SetForegroundWindow(self, hwnd):
        self.calls.append(("set_foreground", int(hwnd)))
        if self.promote_after_attach and self.attached == [20, 30]:
            self.foreground = int(hwnd)
            return True
        return False

    def GetForegroundWindow(self):
        self.calls.append(("get_foreground",))
        return self.foreground

    def PeekMessageW(self, _message, _hwnd, _minimum, _maximum, _remove):
        self.calls.append(("peek_message",))
        return False

    def GetWindowThreadProcessId(self, hwnd, _process_id):
        return {self.foreground: 20, self.target: 30}.get(int(hwnd), 0)

    def AttachThreadInput(self, current_thread, other_thread, attach):
        current = int(current_thread)
        other = int(other_thread)
        enabled = bool(attach)
        self.calls.append(("attach", current, other, enabled))
        if enabled and other in self.attach_failures:
            ctypes.set_last_error(self.attach_failures[other])
            return False
        if enabled:
            self.attached.append(other)
        elif other in self.attached:
            self.attached.remove(other)
        return True

    def SetFocus(self, hwnd):
        self.calls.append(("set_focus", int(hwnd)))
        return 0


def build_native(user32: ActivationUser32) -> NativeWindows:
    clock = [0.0]

    def sleep(duration: float) -> None:
        clock[0] += duration

    native = NativeWindows(
        user32=user32,
        shell32=object(),
        kernel32=FakeKernel32(),
        monotonic=lambda: clock[0],
        sleeper=sleep,
    )
    native.enumerate_windows = lambda: [
        WindowInfo(user32.target, "Owned fixture", 5001, "python.exe")
    ]
    return native


def test_activation_builds_queue_and_attaches_in_deterministic_order() -> None:
    user32 = ActivationUser32(promote_after_attach=True)
    native = build_native(user32)

    activated = native.activate_window(user32.target)

    assert activated.hwnd == user32.target
    assert user32.foreground == user32.target
    assert user32.calls.index(("peek_message",)) < user32.calls.index(("attach", 10, 20, True))
    assert ("attach", 10, 20, True) in user32.calls
    assert ("attach", 10, 30, True) in user32.calls
    assert user32.calls.index(("attach", 10, 20, True)) < user32.calls.index(
        ("attach", 10, 30, True)
    )
    assert user32.calls.index(("attach", 10, 30, False)) < user32.calls.index(
        ("attach", 10, 20, False)
    )
    assert user32.attached == []


def test_integrity_denial_is_a_stable_fail_closed_activation_error() -> None:
    user32 = ActivationUser32(attach_failures={20: ERROR_ACCESS_DENIED})
    native = build_native(user32)

    with pytest.raises(ForegroundIntegrityBoundary) as captured:
        native.activate_window(user32.target)

    assert isinstance(captured.value, WindowActivationError)
    assert captured.value.reason_code == "foreground_integrity_boundary"
    assert "101" not in str(captured.value)
    assert "202" not in str(captured.value)
    assert "python.exe" not in str(captured.value)
    assert ("set_focus", user32.target) not in user32.calls
    assert user32.foreground == 202


def test_integrity_denial_detaches_any_queue_already_joined() -> None:
    user32 = ActivationUser32(attach_failures={30: ERROR_ACCESS_DENIED})
    native = build_native(user32)

    with pytest.raises(ForegroundIntegrityBoundary):
        native.activate_window(user32.target)

    assert ("attach", 10, 20, True) in user32.calls
    assert ("attach", 10, 20, False) in user32.calls
    assert user32.attached == []


def test_activation_never_succeeds_without_exact_foreground_hwnd() -> None:
    user32 = ActivationUser32(promote_after_attach=False)
    native = build_native(user32)

    with pytest.raises(WindowActivationError, match="expected 101"):
        native.activate_window(user32.target, timeout=0.1)

    assert user32.foreground == 202
    assert user32.attached == []


def test_null_foreground_handle_is_treated_as_no_foreground_window() -> None:
    user32 = ActivationUser32(promote_after_attach=False)
    user32.foreground = None
    native = build_native(user32)

    assert native.get_foreground_window_info() is None
    assert native.is_foreground(user32.target) is False
    with pytest.raises(WindowActivationError, match="foreground window is 0"):
        native.assert_foreground(user32.target)
    with pytest.raises(WindowActivationError):
        native.activate_window(user32.target, timeout=0.1)
