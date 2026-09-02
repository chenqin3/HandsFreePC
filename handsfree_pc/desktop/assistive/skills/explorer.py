from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ....windows.native import (
    DesktopUnavailableError,
    ForegroundIntegrityBoundary,
    NativeWindows,
    WindowInfo,
)

ExplorerSkillStatus = Literal["succeeded", "retryable_failure", "hard_failure"]

_EXPLORER_PROCESS_NAME = "explorer.exe"
_EXPLORER_WINDOW_CLASSES = frozenset({"CabinetWClass", "ExploreWClass"})
_DRIVE_ROOT_RE = re.compile(r"^\s*([a-z])(?::)?(?:[\\/])?\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ExplorerSkillResult:
    status: ExplorerSkillStatus
    message: str
    details: Mapping[str, object] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


class ExplorerSkill:
    """Deterministic Explorer activation and drive-root navigation."""

    def __init__(
        self,
        native: NativeWindows,
        *,
        windows_directory: str | Path | None = None,
        window_timeout: float = 3.0,
        path_timeout: float = 4.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.native = native
        self.windows_directory = (
            Path(windows_directory)
            if windows_directory is not None
            else (Path(value) if (value := os.environ.get("WINDIR")) else None)
        )
        self.window_timeout = max(0.0, float(window_timeout))
        self.path_timeout = max(0.0, float(path_timeout))
        self._monotonic = monotonic
        self._sleep = sleeper

    @staticmethod
    def is_explorer_window(window: WindowInfo) -> bool:
        return bool(
            window.process_name
            and window.process_name.casefold() == _EXPLORER_PROCESS_NAME
            and window.class_name in _EXPLORER_WINDOW_CLASSES
        )

    def _assert_interactive_desktop(self) -> None:
        check = getattr(self.native, "assert_interactive_desktop", None)
        if callable(check):
            check()

    def activate_or_open(self) -> ExplorerSkillResult:
        """Activate one Explorer window, or launch Explorer when none exists."""

        try:
            self._assert_interactive_desktop()
            candidates = self._explorer_windows()
            if candidates:
                selected = self._select_window(candidates)
                return self._activate_and_verify(selected, launched=False)

            if self.windows_directory is None:
                return ExplorerSkillResult(
                    "retryable_failure",
                    "WINDIR is unavailable; refusing to guess the Explorer executable",
                )
            executable = self.windows_directory / "explorer.exe"
            self.native.shell_execute_path(executable)
            candidates = self._wait_for_explorer_windows()
            if not candidates:
                return ExplorerSkillResult(
                    "retryable_failure",
                    "Explorer did not expose a verified window before the timeout",
                    {"launched": True},
                )
            return self._activate_and_verify(self._select_window(candidates), launched=True)
        except (DesktopUnavailableError, ForegroundIntegrityBoundary) as exc:
            return ExplorerSkillResult(
                "hard_failure",
                "Secure desktop or a foreground integrity boundary blocked Explorer",
                {"error_type": type(exc).__name__},
            )
        except Exception as exc:
            return ExplorerSkillResult(
                "retryable_failure",
                "Explorer activation failed through the native Windows adapter",
                {"error_type": type(exc).__name__},
            )

    def open_drive(self, drive_or_path: str | Path) -> ExplorerSkillResult:
        """Open one drive root with ShellExecuteW and verify its Explorer identity."""

        match = _DRIVE_ROOT_RE.fullmatch(str(drive_or_path))
        if match is None:
            return ExplorerSkillResult(
                "hard_failure",
                "Only an A-Z drive root can be opened by the Explorer drive skill",
            )
        drive = match.group(1).upper()
        target = f"{drive}:\\"
        try:
            self._assert_interactive_desktop()
            before = self.native.path_open_state(target)
            if before.get("verified") is True:
                return ExplorerSkillResult(
                    "succeeded",
                    f"Drive {drive}: is already open in the foreground Explorer window",
                    {"drive": drive, "already_open": True},
                )

            self.native.shell_execute_path(target)
            evidence = self._wait_for_drive_foreground(target)
            if evidence is None:
                return ExplorerSkillResult(
                    "retryable_failure",
                    "Explorer did not verify the requested drive path",
                    {"drive": drive},
                )
            if not self._foreground_is_explorer():
                return ExplorerSkillResult(
                    "retryable_failure",
                    "The requested drive opened without a verified Explorer foreground",
                    {"drive": drive},
                )
            return ExplorerSkillResult(
                "succeeded",
                f"Opened drive {drive}: in Explorer",
                {
                    "drive": drive,
                    "already_open": False,
                    "verification_kind": evidence.get("kind"),
                },
            )
        except (DesktopUnavailableError, ForegroundIntegrityBoundary) as exc:
            return ExplorerSkillResult(
                "hard_failure",
                "Secure desktop or a foreground integrity boundary blocked Explorer",
                {"drive": drive, "error_type": type(exc).__name__},
            )
        except Exception as exc:
            return ExplorerSkillResult(
                "retryable_failure",
                "Explorer could not open and verify the requested drive",
                {"drive": drive, "error_type": type(exc).__name__},
            )

    def open_directory(self, directory: str | Path) -> ExplorerSkillResult:
        """Open any existing local folder and bring its Explorer window forward."""

        target = str(Path(directory))
        if not Path(target).is_dir():
            return ExplorerSkillResult("retryable_failure", "The folder does not exist")
        try:
            self._assert_interactive_desktop()
            before = self.native.path_open_state(target)
            if before.get("verified") is True:
                return ExplorerSkillResult(
                    "succeeded",
                    "The folder is already open in the foreground Explorer window",
                    {"path": target, "already_open": True},
                )
            self.native.shell_execute_path(target)
            evidence = self._wait_for_drive_foreground(target)
            if evidence is None or not self._foreground_is_explorer():
                return ExplorerSkillResult(
                    "retryable_failure",
                    "Explorer did not verify the requested folder in the foreground",
                    {"path": target},
                )
            return ExplorerSkillResult(
                "succeeded",
                "Opened the folder in Explorer",
                {"path": target, "already_open": False, "verification_kind": evidence.get("kind")},
            )
        except (DesktopUnavailableError, ForegroundIntegrityBoundary) as exc:
            return ExplorerSkillResult(
                "hard_failure",
                "Secure desktop or a foreground integrity boundary blocked Explorer",
                {"path": target, "error_type": type(exc).__name__},
            )
        except Exception as exc:
            return ExplorerSkillResult(
                "retryable_failure",
                "Explorer could not open and verify the requested folder",
                {"path": target, "error_type": type(exc).__name__},
            )

    def _explorer_windows(self) -> list[WindowInfo]:
        return [
            window for window in self.native.enumerate_windows() if self.is_explorer_window(window)
        ]

    def _select_window(self, candidates: list[WindowInfo]) -> WindowInfo:
        foreground = self.native.get_foreground_window_info()
        if foreground is not None:
            for candidate in candidates:
                if candidate.hwnd == foreground.hwnd:
                    return candidate
        # EnumWindows preserves top-level Z order, so the first remaining
        # Explorer is the deterministic fallback. Multiple windows are normal.
        return candidates[0]

    def _activate_and_verify(
        self,
        selected: WindowInfo,
        *,
        launched: bool,
    ) -> ExplorerSkillResult:
        already_foreground = self.native.is_foreground(selected.hwnd)
        if not already_foreground:
            self.native.activate_window(selected.hwnd, timeout=self.window_timeout)
        if not self._foreground_is_explorer(expected_hwnd=selected.hwnd):
            return ExplorerSkillResult(
                "retryable_failure",
                "Explorer did not become the verified foreground window",
                {"launched": launched},
            )
        return ExplorerSkillResult(
            "succeeded",
            "Explorer is in the foreground",
            {
                "launched": launched,
                "activated": not already_foreground,
                "window_hwnd": selected.hwnd,
            },
        )

    def _foreground_is_explorer(self, *, expected_hwnd: int | None = None) -> bool:
        foreground = self.native.get_foreground_window_info()
        return bool(
            foreground is not None
            and (expected_hwnd is None or foreground.hwnd == expected_hwnd)
            and self.is_explorer_window(foreground)
        )

    def _wait_for_explorer_windows(self) -> list[WindowInfo]:
        deadline = self._monotonic() + self.window_timeout
        while True:
            candidates = self._explorer_windows()
            if candidates or self._monotonic() >= deadline:
                return candidates
            self._sleep(0.1)

    def _wait_for_drive_foreground(self, target: str) -> Mapping[str, object] | None:
        """Wait for ShellExecute, then explicitly foreground its exact COM HWND.

        Windows can legally reuse an existing background Explorer window and
        decline to transfer foreground ownership to the calling process.  COM
        path identity already gives us the exact matching HWND, so activating
        that HWND is deterministic and avoids falling into the slow planner.
        """

        deadline = self._monotonic() + self.path_timeout
        activated: set[int] = set()
        while True:
            state = self.native.path_open_state(target)
            if state.get("verified") is True and self._foreground_is_explorer():
                return state
            matches = tuple(
                int(hwnd)
                for hwnd in (state.get("matching_hwnds") or ())
                if isinstance(hwnd, int) and not isinstance(hwnd, bool) and hwnd > 0
            )
            for hwnd in matches:
                if hwnd in activated:
                    continue
                activated.add(hwnd)
                remaining = max(0.0, deadline - self._monotonic())
                if remaining <= 0:
                    break
                try:
                    self.native.activate_window(hwnd, timeout=min(2.0, remaining))
                except (DesktopUnavailableError, ForegroundIntegrityBoundary):
                    raise
                except Exception:
                    continue
                refreshed = self.native.path_open_state(target)
                if refreshed.get("verified") is True and self._foreground_is_explorer(
                    expected_hwnd=hwnd
                ):
                    return refreshed
            if self._monotonic() >= deadline:
                return None
            self._sleep(0.1)


__all__ = ["ExplorerSkill", "ExplorerSkillResult", "ExplorerSkillStatus"]
