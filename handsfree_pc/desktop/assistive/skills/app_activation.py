from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from ....windows.native import (
    DesktopUnavailableError,
    ForegroundIntegrityBoundary,
    NativeWindows,
    WindowInfo,
)
from ..policy_terms import TERMINAL_APPS

AppActivationStatus = Literal["succeeded", "retryable_failure", "hard_failure"]

_ALIASES: dict[str, tuple[str, ...]] = {
    "claude": ("claude", "克劳德"),
    "codex": ("codex", "chatgpt", "chat gpt", "代码助手"),
    "chrome": ("chrome", "google chrome", "谷歌浏览器"),
    "wechat": ("wechat", "weixin", "微信"),
}
_PROTECTED_PROCESSES = frozenset(
    {
        *TERMINAL_APPS,
        "credentialui.exe",
        "consent.exe",
        "logonui.exe",
        "sechealthui.exe",
    }
)
_NORMALIZED_PROTECTED_PROCESSES = frozenset(
    "".join(unicodedata.normalize("NFKC", item).casefold().split())
    for item in _PROTECTED_PROCESSES
)


def _normalize(value: object) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _family(value: object) -> str:
    normalized = _normalize(value)
    for family, aliases in _ALIASES.items():
        if normalized == _normalize(family) or any(
            normalized == _normalize(alias) for alias in aliases
        ):
            return family
    return normalized


@dataclass(frozen=True, slots=True)
class AppActivationResult:
    status: AppActivationStatus
    message: str
    details: Mapping[str, object] = field(default_factory=dict)


class AppActivationSkill:
    """Activate one exact visible top-level window without mutating on observe."""

    def __init__(self, native: NativeWindows, profiles: Mapping[str, Any]) -> None:
        self.native = native
        self.profiles = profiles

    @staticmethod
    def _profile_matches(window: WindowInfo, profile: Any) -> bool:
        processes = {
            _normalize(item)
            for item in (getattr(profile, "process_names", ()) or ())
            if str(item).strip()
        }
        patterns = tuple(
            _normalize(item)
            for item in (getattr(profile, "title_patterns", ()) or ())
            if str(item).strip()
        )
        process = _normalize(window.process_name)
        title = _normalize(window.title)
        return bool(
            (processes and process in processes)
            or (patterns and any(pattern in title for pattern in patterns))
        )

    def _matches(self, target: str, windows: list[WindowInfo]) -> list[WindowInfo]:
        family = _family(target)
        configured = next(
            (
                profile
                for key, profile in self.profiles.items()
                if _family(key) == family or _family(getattr(profile, "name", "")) == family
            ),
            None,
        )
        if configured is not None:
            return [window for window in windows if self._profile_matches(window, configured)]
        # WeChat 4.x runs as Weixin.exe with the title 微信: match every alias
        # of the family, not only the canonical English name.
        names = tuple(
            _normalize(item) for item in (family, *_ALIASES.get(family, ())) if _normalize(item)
        )
        return [
            window
            for window in windows
            if names
            and any(
                name in _normalize(window.process_name).removesuffix(".exe")
                or name in _normalize(window.title)
                for name in names
            )
        ]

    def activate(self, target: str) -> AppActivationResult:
        try:
            self.native.assert_interactive_desktop()
            windows = self.native.enumerate_windows()
            matches = self._matches(target, windows)
            if not matches:
                return AppActivationResult(
                    "retryable_failure",
                    "No visible top-level window matched the requested application",
                )
            foreground = self.native.get_foreground_window_info()
            selected = next(
                (
                    window
                    for window in matches
                    if foreground is not None and window.hwnd == foreground.hwnd
                ),
                matches[0],
            )
            if _normalize(selected.process_name) in _NORMALIZED_PROTECTED_PROCESSES:
                return AppActivationResult(
                    "hard_failure",
                    "Terminal, authentication, UAC, or Windows Security windows are blocked",
                    {"process_name": selected.process_name or ""},
                )
            if foreground is not None and foreground.hwnd == selected.hwnd:
                return AppActivationResult(
                    "succeeded",
                    "The requested exact application window is already foreground",
                    {"hwnd": selected.hwnd, "activated": False},
                )
            activated = self.native.activate_window(selected.hwnd)
            if activated.hwnd != selected.hwnd:
                return AppActivationResult(
                    "retryable_failure",
                    "The exact selected application window changed during activation",
                )
            self.native.assert_foreground(selected.hwnd)
            return AppActivationResult(
                "succeeded",
                "Activated the exact requested application window",
                {"hwnd": selected.hwnd, "activated": True},
            )
        except (DesktopUnavailableError, ForegroundIntegrityBoundary) as exc:
            return AppActivationResult(
                "hard_failure",
                "Secure desktop or a foreground integrity boundary blocked activation",
                {"error_type": type(exc).__name__},
            )
        except Exception as exc:
            return AppActivationResult(
                "retryable_failure",
                "Application activation failed through the native Windows adapter",
                {"error_type": type(exc).__name__},
            )


__all__ = ["AppActivationResult", "AppActivationSkill", "AppActivationStatus"]
