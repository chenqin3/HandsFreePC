from __future__ import annotations

import ctypes
import hashlib
import os
import re
import time
from collections.abc import Callable, Iterable, Sequence
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


class NativeWindowsError(RuntimeError):
    """Base error for the small, allow-listed Win32 surface used by HandsFreePC."""


class WindowNotFoundError(NativeWindowsError):
    pass


class WindowActivationError(NativeWindowsError):
    pass


class ForegroundIntegrityBoundary(WindowActivationError):
    """The current process cannot join a higher-integrity foreground input queue."""

    reason_code = "foreground_integrity_boundary"


class DesktopUnavailableError(NativeWindowsError):
    pass


class AmbiguousWindowError(NativeWindowsError):
    def __init__(self, candidates: Sequence[WindowInfo]) -> None:
        super().__init__(f"Multiple configured windows matched ({len(candidates)})")
        self.candidates = tuple(candidates)
        self.evidence = {
            "candidate_count": len(candidates),
            "candidate_hwnds": [item.hwnd for item in candidates],
        }


class InputInjectionError(NativeWindowsError):
    pass


class InvalidHotkeyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WindowInfo:
    hwnd: int
    title: str
    process_id: int
    process_name: str | None = None

    def to_evidence(self) -> dict[str, object]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "process_id": self.process_id,
            "process_name": self.process_name,
        }


ULONG_PTR = ctypes.c_size_t


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [("type", wintypes.DWORD), ("data", _INPUTUNION)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
SW_RESTORE = 9
SW_SHOWNORMAL = 1
DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2
PM_NOREMOVE = 0x0000
ERROR_ACCESS_DENIED = 5
_CALLBACK_FACTORY = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
WNDENUMPROC = _CALLBACK_FACTORY(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


_MODIFIER_KEYS: dict[str, int] = {
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "win": 0x5B,
    "windows": 0x5B,
}
_NAMED_KEYS: dict[str, int] = {
    "backspace": 0x08,
    "tab": 0x09,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "space": 0x20,
    "pageup": 0x21,
    "pagedown": 0x22,
    "end": 0x23,
    "home": 0x24,
    "left": 0x25,
    "up": 0x26,
    "right": 0x27,
    "down": 0x28,
    "delete": 0x2E,
}
_MODIFIER_VKS = set(_MODIFIER_KEYS.values())


def _virtual_key(token: str) -> int:
    normalized = token.strip().casefold()
    if normalized in _MODIFIER_KEYS:
        return _MODIFIER_KEYS[normalized]
    if normalized in _NAMED_KEYS:
        return _NAMED_KEYS[normalized]
    if len(normalized) == 1 and normalized.isascii() and normalized.isalnum():
        return ord(normalized.upper())
    function_key = re.fullmatch(r"f([1-9]|1[0-9]|2[0-4])", normalized)
    if function_key:
        return 0x70 + int(function_key.group(1)) - 1
    raise InvalidHotkeyError(f"Hotkey token is not allow-listed: {token!r}")


def parse_hotkey(specification: str) -> tuple[int, ...]:
    """Parse one shortcut without accepting arbitrary key or command syntax."""

    if not isinstance(specification, str) or not specification.strip():
        raise InvalidHotkeyError("Hotkey must be a non-empty string")
    if any(character in specification for character in (",", ";", "|", "&", "\n", "\r")):
        raise InvalidHotkeyError("Only plus-separated hotkeys are accepted")
    tokens = [item.strip() for item in specification.split("+")]
    if any(not item for item in tokens) or len(tokens) > 4:
        raise InvalidHotkeyError("Hotkey must contain one to four non-empty keys")
    keys = tuple(_virtual_key(token) for token in tokens)
    if len(set(keys)) != len(keys):
        raise InvalidHotkeyError("Hotkey cannot contain duplicate keys")
    primary_keys = [key for key in keys if key not in _MODIFIER_VKS]
    if len(primary_keys) != 1:
        raise InvalidHotkeyError("Hotkey must contain exactly one non-modifier key")
    return keys


def _default_process_name(process_id: int) -> str | None:
    try:
        import psutil

        return psutil.Process(process_id).name()
    except (ImportError, OSError):
        return None
    except Exception:  # psutil has platform-specific process lookup exceptions.
        return None


def _configure_user32(user32: object) -> None:
    """Declare pointer-width-safe signatures for the real Win32 DLL."""

    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.IsIconic.argtypes = [wintypes.HWND]
    user32.IsIconic.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindow.restype = wintypes.BOOL
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.BringWindowToTop.restype = wintypes.BOOL
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.restype = wintypes.BOOL
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.AttachThreadInput.restype = wintypes.BOOL
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.UINT,
    ]
    user32.PeekMessageW.restype = wintypes.BOOL
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
    user32.SendInput.restype = wintypes.UINT
    user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user32.OpenInputDesktop.restype = wintypes.HANDLE
    user32.GetUserObjectInformationW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetUserObjectInformationW.restype = wintypes.BOOL
    user32.CloseDesktop.argtypes = [wintypes.HANDLE]
    user32.CloseDesktop.restype = wintypes.BOOL


def _configure_kernel32(kernel32: object) -> None:
    kernel32.GetCurrentThreadId.argtypes = []
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def _configure_shell32(shell32: object) -> None:
    shell32.ShellExecuteW.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = wintypes.HINSTANCE


class NativeWindows:
    """Minimal Win32 adapter; it deliberately exposes no shell-command primitive."""

    def __init__(
        self,
        *,
        user32: object | None = None,
        shell32: object | None = None,
        kernel32: object | None = None,
        path_opener: Callable[[str], object] | None = None,
        process_name_resolver: Callable[[int], str | None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        owns_user32 = user32 is None
        if user32 is None:
            if os.name != "nt":
                raise OSError("NativeWindows is available only on Windows")
            user32 = ctypes.WinDLL("user32", use_last_error=True)
        owns_shell32 = shell32 is None and os.name == "nt"
        if shell32 is None and os.name == "nt":
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        owns_kernel32 = kernel32 is None and os.name == "nt"
        if kernel32 is None and os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if owns_user32:
            _configure_user32(user32)
        if owns_shell32 and shell32 is not None:
            _configure_shell32(shell32)
        if owns_kernel32 and kernel32 is not None:
            _configure_kernel32(kernel32)
        self.user32 = user32
        self.shell32 = shell32
        self.kernel32 = kernel32
        self._path_opener = path_opener
        self._process_name_resolver = process_name_resolver or _default_process_name
        self._monotonic = monotonic
        self._sleep = sleeper

    def enumerate_windows(self) -> list[WindowInfo]:
        windows: list[WindowInfo] = []

        @WNDENUMPROC
        def visit(hwnd: int, _lparam: int) -> bool:
            if not self.user32.IsWindowVisible(hwnd):
                return True
            length = int(self.user32.GetWindowTextLengthW(hwnd))
            if length <= 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            self.user32.GetWindowTextW(hwnd, buffer, len(buffer))
            title = buffer.value.strip()
            if not title:
                return True
            process_id = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            pid = int(process_id.value)
            windows.append(
                WindowInfo(
                    hwnd=int(hwnd),
                    title=title,
                    process_id=pid,
                    process_name=self._process_name_resolver(pid),
                )
            )
            return True

        if not self.user32.EnumWindows(visit, 0):
            error = ctypes.get_last_error()
            if error:
                raise NativeWindowsError(f"EnumWindows failed with Win32 error {error}")
        return windows

    def assert_interactive_desktop(self) -> str:
        """Reject lock-screen and secure-desktop execution before any OS action."""

        handle = self.user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
        if not handle:
            error = ctypes.get_last_error()
            raise DesktopUnavailableError(
                f"The interactive input desktop is unavailable (Win32 error {error})"
            )
        try:
            needed = wintypes.DWORD()
            self.user32.GetUserObjectInformationW(handle, UOI_NAME, None, 0, ctypes.byref(needed))
            if not needed.value:
                raise DesktopUnavailableError("Could not identify the active input desktop")
            characters = max(
                1,
                (int(needed.value) + ctypes.sizeof(ctypes.c_wchar) - 1)
                // ctypes.sizeof(ctypes.c_wchar),
            )
            buffer = ctypes.create_unicode_buffer(characters)
            if not self.user32.GetUserObjectInformationW(
                handle,
                UOI_NAME,
                buffer,
                ctypes.sizeof(buffer),
                ctypes.byref(needed),
            ):
                error = ctypes.get_last_error()
                raise DesktopUnavailableError(
                    f"Could not read the active input desktop (Win32 error {error})"
                )
            name = buffer.value.strip()
            if name.casefold() != "default":
                raise DesktopUnavailableError(
                    f"Refusing OS input on the non-default desktop: {name or 'unknown'}"
                )
            return name
        finally:
            self.user32.CloseDesktop(handle)

    def find_windows(
        self,
        *,
        title_patterns: Iterable[str] = (),
        process_names: Iterable[str] = (),
    ) -> list[WindowInfo]:
        patterns = tuple(item.casefold() for item in title_patterns if item.strip())
        processes = {item.casefold() for item in process_names if item.strip()}
        if not patterns and not processes:
            raise ValueError("At least one title pattern or process name is required")
        result: list[WindowInfo] = []
        for window in self.enumerate_windows():
            title_matches = not patterns or any(
                pattern in window.title.casefold() for pattern in patterns
            )
            process_matches = not processes or (
                window.process_name is not None and window.process_name.casefold() in processes
            )
            if title_matches and process_matches:
                result.append(window)
        return result

    def get_foreground_window_info(self) -> WindowInfo | None:
        hwnd = int(self.user32.GetForegroundWindow())
        if not hwnd:
            return None
        for window in self.enumerate_windows():
            if window.hwnd == hwnd:
                return window
        return WindowInfo(hwnd=hwnd, title="", process_id=0, process_name=None)

    def is_foreground(self, hwnd: int) -> bool:
        return bool(hwnd) and int(self.user32.GetForegroundWindow()) == int(hwnd)

    def assert_foreground(self, hwnd: int) -> None:
        actual = int(self.user32.GetForegroundWindow())
        if not hwnd or actual != int(hwnd):
            raise WindowActivationError(
                f"Refusing input because foreground window is {actual}, expected {int(hwnd)}"
            )

    def activate_window(self, hwnd: int, *, timeout: float = 2.0) -> WindowInfo:
        candidates = {window.hwnd: window for window in self.enumerate_windows()}
        if int(hwnd) not in candidates:
            raise WindowNotFoundError(f"Window no longer exists or is not visible: {int(hwnd)}")
        if self.user32.IsIconic(hwnd):
            self.user32.ShowWindow(hwnd, SW_RESTORE)
        self.user32.BringWindowToTop(hwnd)
        self.user32.SetForegroundWindow(hwnd)
        if not self.is_foreground(hwnd):
            self._attach_and_activate(hwnd)
        deadline = self._monotonic() + max(0.0, timeout)
        while not self.is_foreground(hwnd) and self._monotonic() < deadline:
            self._sleep(0.05)
        self.assert_foreground(hwnd)
        return candidates[int(hwnd)]

    def _attach_and_activate(self, hwnd: int) -> None:
        """Retry foreground activation with temporarily attached input queues."""

        if self.kernel32 is None or not all(
            hasattr(self.user32, name)
            for name in (
                "AttachThreadInput",
                "GetWindowThreadProcessId",
                "PeekMessageW",
                "SetFocus",
            )
        ):
            return
        # AttachThreadInput fails when either thread has no message queue.  A
        # command-line worker normally has none, so explicitly create ours
        # before trying to join the foreground input queue.
        message = wintypes.MSG()
        self.user32.PeekMessageW(ctypes.byref(message), None, 0, 0, PM_NOREMOVE)
        foreground_hwnd = int(self.user32.GetForegroundWindow())
        current_thread = int(self.kernel32.GetCurrentThreadId())
        foreground_thread = (
            int(self.user32.GetWindowThreadProcessId(foreground_hwnd, None))
            if foreground_hwnd
            else 0
        )
        target_thread = int(self.user32.GetWindowThreadProcessId(hwnd, None))
        thread_ids: list[int] = []
        # Join the actual foreground queue first.  A set made this order
        # nondeterministic and could join the target before discovering that
        # Windows had denied access to the foreground integrity level.
        for thread_id in (foreground_thread, target_thread):
            if thread_id and thread_id != current_thread and thread_id not in thread_ids:
                thread_ids.append(thread_id)
        attached: list[int] = []
        try:
            for thread_id in thread_ids:
                ctypes.set_last_error(0)
                if self.user32.AttachThreadInput(current_thread, thread_id, True):
                    attached.append(thread_id)
                    continue
                error = ctypes.get_last_error()
                foreground_is_unchanged = (
                    not foreground_hwnd or int(self.user32.GetForegroundWindow()) == foreground_hwnd
                )
                if error == ERROR_ACCESS_DENIED and foreground_is_unchanged:
                    raise ForegroundIntegrityBoundary(
                        "Windows denied access to the foreground input queue across an "
                        "integrity boundary. Run HandsFreePC at the same integrity level "
                        "as the foreground desktop application or focus the target manually."
                    )
            self.user32.BringWindowToTop(hwnd)
            self.user32.SetForegroundWindow(hwnd)
            self.user32.SetFocus(hwnd)
        finally:
            for thread_id in reversed(attached):
                self.user32.AttachThreadInput(current_thread, thread_id, False)

    def wait_for_windows(
        self,
        *,
        title_patterns: Iterable[str],
        process_names: Iterable[str],
        timeout: float = 8.0,
    ) -> list[WindowInfo]:
        deadline = self._monotonic() + max(0.0, timeout)
        while True:
            matches = self.find_windows(
                title_patterns=title_patterns,
                process_names=process_names,
            )
            if matches or self._monotonic() >= deadline:
                return matches
            self._sleep(0.1)

    def open_path(self, path: str | Path) -> str:
        """Open a trusted path through the Windows shell, never through a command string."""

        target = str(path)
        opener = self._path_opener or getattr(os, "startfile", None)
        if opener is not None:
            opener(target)
            return "os.startfile"
        if self.shell32 is None:
            raise NativeWindowsError("No Windows path opener is available")
        result = int(self.shell32.ShellExecuteW(None, "open", target, None, None, SW_SHOWNORMAL))
        if result <= 32:
            raise NativeWindowsError(f"ShellExecuteW failed with result {result}")
        return "ShellExecuteW"

    @staticmethod
    def _canonical_path(path: str | Path) -> str:
        return os.path.normcase(str(Path(path).resolve()))

    def _explorer_directory_hwnds(self, target: Path) -> tuple[int, ...]:
        """Return Explorer windows whose COM folder identity exactly matches target."""

        try:
            import pythoncom
            import win32com.client
        except ImportError as exc:
            raise NativeWindowsError(
                "Exact Explorer path verification requires the Windows pywin32 extra"
            ) from exc
        expected = self._canonical_path(target)
        matches: list[int] = []
        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("Shell.Application")
            for window in shell.Windows():
                try:
                    current = str(window.Document.Folder.Self.Path)
                    hwnd = int(window.HWND)
                except Exception:
                    continue
                if self._canonical_path(current) == expected:
                    matches.append(hwnd)
        finally:
            pythoncom.CoUninitialize()
        return tuple(matches)

    def path_open_state(self, path: str | Path) -> dict[str, object]:
        """Inspect an exact local postcondition without opening the target."""

        target = Path(path).resolve()
        foreground = self.get_foreground_window_info()
        foreground_hwnd = foreground.hwnd if foreground is not None else None
        if target.is_dir():
            matches = self._explorer_directory_hwnds(target)
            return {
                "kind": "explorer_directory",
                "verified": foreground_hwnd in matches,
                "foreground_hwnd": foreground_hwnd,
                "matching_hwnds": matches,
            }
        expected_name = target.name.casefold()
        title = (foreground.title if foreground is not None else "").casefold()
        return {
            "kind": "foreground_document_title",
            "verified": bool(expected_name and expected_name in title),
            "foreground_hwnd": foreground_hwnd,
            "title_digest": (hashlib.sha256(title.encode("utf-8")).hexdigest() if title else None),
        }

    def wait_for_path_open(
        self,
        path: str | Path,
        *,
        before: dict[str, object],
        timeout: float = 8.0,
    ) -> dict[str, object]:
        """Wait until a false-before exact path condition becomes true locally."""

        if before.get("verified") is True:
            raise NativeWindowsError("The requested path was already open in the foreground")
        deadline = self._monotonic() + max(0.0, timeout)
        while True:
            current = self.path_open_state(path)
            foreground_changed = current.get("foreground_hwnd") != before.get("foreground_hwnd")
            if current.get("verified") is True and foreground_changed:
                return {
                    "postcondition_verified": True,
                    "verification_kind": current.get("kind"),
                    "foreground_changed": True,
                }
            if self._monotonic() >= deadline:
                raise NativeWindowsError(
                    "The opened path did not reach a verified foreground state"
                )
            self._sleep(0.1)

    @staticmethod
    def _keyboard_input(*, virtual_key: int = 0, scan_code: int = 0, flags: int = 0) -> INPUT:
        item = INPUT()
        item.type = INPUT_KEYBOARD
        item.ki = KEYBDINPUT(virtual_key, scan_code, flags, 0, 0)
        return item

    def _send_inputs(self, inputs: Sequence[INPUT]) -> int:
        if not inputs:
            return 0
        array_type = INPUT * len(inputs)
        inserted = int(
            self.user32.SendInput(len(inputs), array_type(*inputs), ctypes.sizeof(INPUT))
        )
        if inserted != len(inputs):
            error = ctypes.get_last_error()
            raise InputInjectionError(
                f"SendInput inserted {inserted} of {len(inputs)} events (Win32 error {error})"
            )
        return inserted

    def send_text(self, text: str) -> int:
        """Inject Unicode UTF-16 code units without using the clipboard."""

        if "\x00" in text:
            raise ValueError("NUL characters are not allowed")
        encoded = text.encode("utf-16-le")
        units = [
            int.from_bytes(encoded[index : index + 2], "little")
            for index in range(0, len(encoded), 2)
        ]
        inputs: list[INPUT] = []
        for unit in units:
            inputs.append(self._keyboard_input(scan_code=unit, flags=KEYEVENTF_UNICODE))
            inputs.append(
                self._keyboard_input(
                    scan_code=unit,
                    flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                )
            )
        self._send_inputs(inputs)
        return len(units)

    def send_hotkey(self, specification: str) -> tuple[int, ...]:
        keys = parse_hotkey(specification)
        inputs = [self._keyboard_input(virtual_key=key) for key in keys]
        inputs.extend(
            self._keyboard_input(virtual_key=key, flags=KEYEVENTF_KEYUP) for key in reversed(keys)
        )
        self._send_inputs(inputs)
        return keys


# A descriptive alias for callers that prefer a backend-oriented name.
NativeWindowsBackend = NativeWindows
