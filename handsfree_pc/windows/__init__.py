"""Verified, allow-listed Windows execution primitives."""

from .executor import WindowsExecutionError, WindowsExecutor
from .native import (
    AmbiguousWindowError,
    DesktopUnavailableError,
    InputInjectionError,
    InvalidHotkeyError,
    NativeWindows,
    NativeWindowsBackend,
    NativeWindowsError,
    WindowActivationError,
    WindowInfo,
    WindowNotFoundError,
    parse_hotkey,
)
from .uia import (
    UIA,
    AmbiguousElementError,
    ElementMatch,
    ElementNotFoundError,
    PasswordFieldError,
    UIABackend,
    UIAError,
    UIAPostconditionError,
    UIAUnavailableError,
)

__all__ = [
    "AmbiguousElementError",
    "AmbiguousWindowError",
    "DesktopUnavailableError",
    "ElementMatch",
    "ElementNotFoundError",
    "InputInjectionError",
    "InvalidHotkeyError",
    "NativeWindows",
    "NativeWindowsBackend",
    "NativeWindowsError",
    "PasswordFieldError",
    "UIA",
    "UIABackend",
    "UIAError",
    "UIAPostconditionError",
    "UIAUnavailableError",
    "WindowActivationError",
    "WindowInfo",
    "WindowNotFoundError",
    "WindowsExecutionError",
    "WindowsExecutor",
    "parse_hotkey",
]
