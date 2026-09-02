from __future__ import annotations

from pathlib import Path

import pytest

from handsfree_pc.desktop.assistive.skills.explorer import ExplorerSkill
from handsfree_pc.windows.native import NativeWindowsError, WindowInfo


def explorer_window(hwnd: int, *, class_name: str = "CabinetWClass") -> WindowInfo:
    return WindowInfo(hwnd, f"Folder {hwnd}", 5000 + hwnd, "explorer.exe", class_name)


class FakeNative:
    def __init__(self, windows: list[WindowInfo], *, foreground_hwnd: int | None = None) -> None:
        self.windows = list(windows)
        self.foreground_hwnd = foreground_hwnd
        self.activate_calls: list[tuple[int, float]] = []
        self.shell_calls: list[str] = []
        self.launch_window: WindowInfo | None = None
        self.drive_window: WindowInfo | None = None
        self.drive_reuses_background = False
        self.path_verified = False
        self.wait_error: Exception | None = None

    def enumerate_windows(self) -> list[WindowInfo]:
        return list(self.windows)

    def get_foreground_window_info(self) -> WindowInfo | None:
        return next(
            (window for window in self.windows if window.hwnd == self.foreground_hwnd),
            None,
        )

    def is_foreground(self, hwnd: int) -> bool:
        return self.foreground_hwnd == hwnd

    def activate_window(self, hwnd: int, *, timeout: float = 2.0) -> WindowInfo:
        self.activate_calls.append((hwnd, timeout))
        window = next((item for item in self.windows if item.hwnd == hwnd), None)
        if window is None:
            raise NativeWindowsError("window disappeared")
        self.foreground_hwnd = hwnd
        return window

    def shell_execute_path(self, path: str | Path) -> str:
        target = str(path)
        self.shell_calls.append(target)
        if target.casefold().endswith("explorer.exe") and self.launch_window is not None:
            self.windows.append(self.launch_window)
            self.foreground_hwnd = self.launch_window.hwnd
        elif len(target) == 3 and target[1:] == ":\\" and self.drive_window is not None:
            if all(window.hwnd != self.drive_window.hwnd for window in self.windows):
                self.windows.append(self.drive_window)
            self.path_verified = True
            if not self.drive_reuses_background:
                self.foreground_hwnd = self.drive_window.hwnd
        return "ShellExecuteW"

    def path_open_state(self, _path: str | Path) -> dict[str, object]:
        if self.wait_error is not None and self.shell_calls:
            raise self.wait_error
        return {
            "kind": "explorer_directory",
            "verified": self.path_verified,
            "foreground_hwnd": self.foreground_hwnd,
            "matching_hwnds": (
                (self.drive_window.hwnd,)
                if self.path_verified and self.drive_window is not None
                else ()
            ),
        }

    def wait_for_path_open(
        self,
        _path: str | Path,
        *,
        before: dict[str, object],
        timeout: float,
    ) -> dict[str, object]:
        assert before["verified"] is False
        assert timeout >= 0
        if self.wait_error is not None:
            raise self.wait_error
        self.path_verified = True
        return {
            "postcondition_verified": True,
            "verification_kind": "explorer_directory",
        }


@pytest.mark.parametrize(
    ("window", "expected"),
    [
        (explorer_window(101, class_name="CabinetWClass"), True),
        (explorer_window(102, class_name="ExploreWClass"), True),
        (explorer_window(103, class_name="Progman"), False),
        (WindowInfo(104, "Folder", 5104, "other.exe", "CabinetWClass"), False),
        (WindowInfo(105, "Folder", 5105, "explorer.exe", None), False),
    ],
)
def test_explorer_recognition_requires_process_and_class(window, expected) -> None:
    assert ExplorerSkill.is_explorer_window(window) is expected


def test_existing_foreground_explorer_wins_without_ambiguity_or_activation() -> None:
    first = explorer_window(101)
    foreground = explorer_window(202, class_name="ExploreWClass")
    native = FakeNative([first, foreground], foreground_hwnd=foreground.hwnd)

    result = ExplorerSkill(native).activate_or_open()

    assert result.status == "succeeded"
    assert result.details["window_hwnd"] == foreground.hwnd
    assert result.details["activated"] is False
    assert native.activate_calls == []
    assert native.shell_calls == []


def test_z_order_first_explorer_is_activated_when_none_is_foreground() -> None:
    foreground = WindowInfo(900, "Other", 5900, "other.exe", "OtherWindow")
    first = explorer_window(101)
    second = explorer_window(202)
    native = FakeNative([foreground, first, second], foreground_hwnd=foreground.hwnd)

    result = ExplorerSkill(native, window_timeout=1.25).activate_or_open()

    assert result.status == "succeeded"
    assert result.details["window_hwnd"] == first.hwnd
    assert native.activate_calls == [(first.hwnd, 1.25)]


def test_no_existing_explorer_launches_exact_windows_executable_and_verifies_window() -> None:
    native = FakeNative([])
    native.launch_window = explorer_window(303)

    result = ExplorerSkill(native, windows_directory=r"C:\Windows").activate_or_open()

    assert result.status == "succeeded"
    assert result.details["launched"] is True
    assert native.shell_calls == [r"C:\Windows\explorer.exe"]
    assert native.foreground_hwnd == 303


def test_no_existing_explorer_without_windir_is_retryable(monkeypatch) -> None:
    monkeypatch.delenv("WINDIR", raising=False)
    native = FakeNative([])

    result = ExplorerSkill(native, windows_directory=None).activate_or_open()

    assert result.status == "retryable_failure"
    assert native.shell_calls == []


def test_open_drive_uses_shell_execute_and_requires_exact_path_and_explorer_evidence() -> None:
    drive_window = explorer_window(404)
    native = FakeNative([], foreground_hwnd=None)
    native.drive_window = drive_window

    result = ExplorerSkill(native, path_timeout=2.5).open_drive("d:\\")

    assert result.status == "succeeded"
    assert result.details == {
        "drive": "D",
        "already_open": False,
        "verification_kind": "explorer_directory",
    }
    assert native.shell_calls == ["D:\\"]
    assert native.foreground_hwnd == drive_window.hwnd


def test_open_drive_activates_shell_reused_background_matching_hwnd_within_gate() -> None:
    drive_window = explorer_window(414)
    foreground = WindowInfo(900, "Other", 5900, "other.exe", "OtherWindow")
    native = FakeNative(
        [drive_window, foreground],
        foreground_hwnd=foreground.hwnd,
    )
    native.drive_window = drive_window
    native.drive_reuses_background = True
    now = 0.0

    result = ExplorerSkill(
        native,
        path_timeout=1.5,
        monotonic=lambda: now,
        sleeper=lambda _seconds: (_ for _ in ()).throw(
            AssertionError("matching_hwnd should avoid a polling sleep")
        ),
    ).open_drive("D")

    assert result.status == "succeeded"
    assert result.details["verification_kind"] == "explorer_directory"
    assert native.shell_calls == ["D:\\"]
    assert native.activate_calls == [(drive_window.hwnd, 1.5)]
    assert native.foreground_hwnd == drive_window.hwnd
    assert now < 1.5


def test_open_drive_returns_zero_action_success_when_already_verified() -> None:
    window = explorer_window(505)
    native = FakeNative([window], foreground_hwnd=window.hwnd)
    native.path_verified = True

    result = ExplorerSkill(native).open_drive("D")

    assert result.status == "succeeded"
    assert result.details["already_open"] is True
    assert native.shell_calls == []


@pytest.mark.parametrize("value", ["", "1", "DD", r"D:\private", "server/share"])
def test_open_drive_rejects_non_root_inputs(value: str) -> None:
    result = ExplorerSkill(FakeNative([])).open_drive(value)

    assert result.status == "hard_failure"


def test_open_drive_verification_failure_is_retryable() -> None:
    native = FakeNative([])
    native.drive_window = explorer_window(606)
    native.wait_error = NativeWindowsError("COM folder identity unavailable")

    result = ExplorerSkill(native).open_drive("E:")

    assert result.status == "retryable_failure"
    assert result.details["drive"] == "E"


def test_open_drive_unwrapped_com_failure_is_retryable() -> None:
    native = FakeNative([])
    native.drive_window = explorer_window(707)
    native.wait_error = RuntimeError("simulated pywin32 COM failure")

    result = ExplorerSkill(native).open_drive("F")

    assert result.status == "retryable_failure"
    assert result.details["error_type"] == "RuntimeError"
