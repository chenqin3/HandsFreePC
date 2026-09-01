from __future__ import annotations

import ctypes
from ctypes import wintypes

import pytest

from handsfree_pc.windows.native import (
    ERROR_ACCESS_DENIED,
    GUI_CARETBLINKING,
    GUI_INMENUMODE,
    GUITHREADINFO,
    ForegroundIntegrityBoundary,
    NativeFocusSnapshot,
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
        foreground_visible: bool = True,
    ) -> None:
        self.target = 101
        self.foreground = 202
        self.promote_after_attach = promote_after_attach
        self.foreground_visible = foreground_visible
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
        expected_attachments = [20, 30] if self.foreground_visible else [30]
        if self.promote_after_attach and self.attached == expected_attachments:
            self.foreground = int(hwnd)
            return True
        return False

    def IsWindowVisible(self, hwnd):
        if int(hwnd) == self.target:
            return True
        return self.foreground_visible

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


class FocusUser32:
    def __init__(self) -> None:
        # Handles above DWORD_MAX prove that the structure and fake boundary do
        # not truncate HWNDs on 64-bit Python.
        self.target = 0x1_0000_0101
        self.focus = 0x1_0000_0202
        self.caret = 0x1_0000_0303
        self.foreground = self.target
        self.target_thread = 71
        self.target_process = 7001
        self.owners: dict[int, tuple[int, int]] = {
            self.target: (self.target_thread, self.target_process),
            self.focus: (72, self.target_process),
            self.caret: (73, self.target_process),
        }
        self.windows = set(self.owners)
        self.children = {(self.target, self.focus), (self.target, self.caret)}
        self.active = self.target
        self.gui_focus = self.focus
        self.gui_caret = self.caret
        self.gui_flags = GUI_CARETBLINKING
        self.caret_rect = (11, 13, 13, 31)
        self.physical_origin = (1000, 2000)
        self.gui_success = True
        self.client_to_screen_success = True
        self.foreground_race = False
        self.foreground_reads = 0
        self.gui_cb_size = 0
        self.gui_thread_id = 0
        self.coordinate_calls: list[tuple[str, int]] = []

    @staticmethod
    def _write_process_id(pointer, process_id: int) -> None:
        if pointer is None:
            return
        ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD)).contents.value = process_id

    def GetWindowThreadProcessId(self, hwnd, process_id):
        owner = self.owners.get(int(hwnd))
        if owner is None:
            return 0
        thread_id, pid = owner
        self._write_process_id(process_id, pid)
        return thread_id

    def GetForegroundWindow(self):
        self.foreground_reads += 1
        if self.foreground_race and self.foreground_reads > 1:
            return self.focus
        return self.foreground

    def GetGUIThreadInfo(self, thread_id, pointer):
        self.gui_thread_id = int(thread_id)
        info = ctypes.cast(pointer, ctypes.POINTER(GUITHREADINFO)).contents
        self.gui_cb_size = int(info.cbSize)
        if not self.gui_success:
            return False
        info.flags = self.gui_flags
        info.hwndActive = self.active
        info.hwndFocus = self.gui_focus
        info.hwndCaret = self.gui_caret
        info.rcCaret.left = self.caret_rect[0]
        info.rcCaret.top = self.caret_rect[1]
        info.rcCaret.right = self.caret_rect[2]
        info.rcCaret.bottom = self.caret_rect[3]
        return True

    def IsWindow(self, hwnd):
        return int(hwnd) in self.windows

    def IsChild(self, parent, child):
        return (int(parent), int(child)) in self.children

    def ClientToScreen(self, hwnd, pointer):
        self.coordinate_calls.append(("client_to_screen", int(hwnd)))
        if not self.client_to_screen_success or int(hwnd) not in self.windows:
            return False
        point = ctypes.cast(pointer, ctypes.POINTER(wintypes.POINT)).contents
        point.x = self.physical_origin[0] + int(point.x)
        point.y = self.physical_origin[1] + int(point.y)
        return True


def build_focus_native(user32: FocusUser32) -> NativeWindows:
    return NativeWindows(
        user32=user32,
        shell32=object(),
        kernel32=FakeKernel32(),
        monotonic=lambda: 123.5,
        sleeper=lambda _duration: None,
    )


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


def test_gui_thread_info_layout_is_pointer_width_safe() -> None:
    pointer_size = ctypes.sizeof(wintypes.HWND)
    expected_size = 72 if pointer_size == 8 else 48

    assert pointer_size in {4, 8}
    assert ctypes.sizeof(GUITHREADINFO) == expected_size
    assert GUITHREADINFO.hwndActive.offset == 8
    assert GUITHREADINFO.hwndCaret.offset == 8 + 5 * pointer_size
    assert GUITHREADINFO.rcCaret.offset == 8 + 6 * pointer_size


def test_focus_snapshot_preserves_64_bit_handles_and_screen_caret_geometry() -> None:
    user32 = FocusUser32()
    native = build_focus_native(user32)

    snapshot = native.get_focus_snapshot(user32.target)

    assert isinstance(snapshot, NativeFocusSnapshot)
    assert snapshot.target_hwnd == user32.target
    assert snapshot.foreground_hwnd == user32.target
    assert snapshot.target_thread_id == user32.target_thread
    assert snapshot.target_process_id == user32.target_process
    assert snapshot.active_hwnd == user32.target
    assert snapshot.focus_hwnd == user32.focus
    assert snapshot.focus_thread_id == 72
    assert snapshot.caret_hwnd == user32.caret
    assert snapshot.caret_thread_id == 73
    assert snapshot.caret_rect_client == (11, 13, 13, 31)
    assert snapshot.caret_rect_screen == (1011, 2013, 1013, 2031)
    assert snapshot.gui_thread_flags == GUI_CARETBLINKING
    assert snapshot.has_visible_system_caret is True
    assert snapshot.captured_at_monotonic == 123.5
    assert snapshot.is_bound_to(user32.target) is True
    assert snapshot.is_bound_to(user32.focus) is False
    assert user32.gui_thread_id == user32.target_thread
    assert user32.gui_cb_size == ctypes.sizeof(GUITHREADINFO)
    assert user32.coordinate_calls == [
        ("client_to_screen", user32.caret),
        ("client_to_screen", user32.caret),
    ]
    assert str(user32.target) not in repr(snapshot)
    assert str(user32.target_process) not in repr(snapshot)


def test_focus_snapshot_accepts_same_top_level_active_focus_and_caret() -> None:
    user32 = FocusUser32()
    user32.gui_focus = user32.target
    user32.gui_caret = user32.target
    user32.caret_rect = (266, 120, 268, 122)
    native = build_focus_native(user32)

    snapshot = native.get_focus_snapshot(user32.target)

    assert snapshot is not None
    assert snapshot.active_hwnd == user32.target
    assert snapshot.focus_hwnd == user32.target
    assert snapshot.caret_hwnd == user32.target
    assert snapshot.caret_rect_screen == (1266, 2120, 1268, 2122)
    assert snapshot.is_bound_to(user32.target) is True


def test_focus_snapshot_without_system_caret_is_only_application_focus_evidence() -> None:
    user32 = FocusUser32()
    user32.gui_caret = 0
    user32.gui_flags = 0
    native = build_focus_native(user32)

    snapshot = native.get_focus_snapshot(user32.target)

    assert snapshot is not None
    assert snapshot.is_bound_to(user32.target) is True
    assert snapshot.caret_hwnd == 0
    assert snapshot.caret_rect_client is None
    assert snapshot.caret_rect_screen is None
    assert snapshot.caret_is_target_or_child is None
    assert snapshot.has_visible_system_caret is False
    assert user32.coordinate_calls == []


@pytest.mark.parametrize(
    "failure",
    (
        "gui_info",
        "foreign_focus_process",
        "foreign_focus_tree",
        "foreign_caret_process",
        "caret_client_to_screen",
        "transient_menu",
        "foreground_race",
    ),
)
def test_focus_snapshot_failures_return_none(failure: str) -> None:
    user32 = FocusUser32()
    if failure == "gui_info":
        user32.gui_success = False
    elif failure == "foreign_focus_process":
        user32.owners[user32.focus] = (72, 9009)
    elif failure == "foreign_focus_tree":
        user32.children.remove((user32.target, user32.focus))
    elif failure == "foreign_caret_process":
        user32.owners[user32.caret] = (73, 9009)
    elif failure == "caret_client_to_screen":
        user32.client_to_screen_success = False
    elif failure == "transient_menu":
        user32.gui_flags |= GUI_INMENUMODE
    elif failure == "foreground_race":
        user32.foreground_race = True
    native = build_focus_native(user32)

    assert native.get_focus_snapshot(user32.target) is None


def test_focus_snapshot_rejects_null_focus_and_missing_capability() -> None:
    user32 = FocusUser32()
    user32.gui_focus = 0
    native = build_focus_native(user32)
    activation_only = build_native(ActivationUser32())

    assert native.get_focus_snapshot(user32.target) is None
    assert activation_only.get_focus_snapshot(101) is None


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


def test_invisible_foreground_helper_queue_is_not_joined() -> None:
    user32 = ActivationUser32(
        promote_after_attach=True,
        attach_failures={20: ERROR_ACCESS_DENIED},
        foreground_visible=False,
    )
    native = build_native(user32)

    activated = native.activate_window(user32.target)

    assert activated.hwnd == user32.target
    assert ("attach", 10, 20, True) not in user32.calls
    assert ("attach", 10, 30, True) in user32.calls
    assert user32.foreground == user32.target


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
