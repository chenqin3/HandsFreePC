"""Launch an installed application by spoken name when none of its windows exist.

Only three sources can be launched, all of which the user already installed:
a configured app profile's executable, a small alias table of Windows
accessories resolved through ``PATH``, and Start Menu shortcuts matched by
name. Terminals, security surfaces, and uninstallers are never launched.
"""

from __future__ import annotations

import os
import shutil
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ....windows.native import DesktopUnavailableError, ForegroundIntegrityBoundary
from ..policy_terms import TERMINAL_APPS
from ..spoken_paths import _containment_score, _dice_score

AppLaunchStatus = Literal["succeeded", "retryable_failure", "hard_failure"]

_KNOWN_EXECUTABLES: dict[str, str] = {
    "记事本": "notepad.exe",
    "notepad": "notepad.exe",
    "计算器": "calc.exe",
    "calculator": "calc.exe",
    "画图": "mspaint.exe",
    "paint": "mspaint.exe",
    "写字板": "wordpad.exe",
    "wordpad": "wordpad.exe",
    "截图工具": "snippingtool.exe",
    "chrome": "chrome.exe",
    "谷歌浏览器": "chrome.exe",
    "浏览器": "chrome.exe",
    "edge": "msedge.exe",
    "资源管理器": "explorer.exe",
    "explorer": "explorer.exe",
}
_BLOCKED_MARKERS = (
    "终端",
    "命令提示符",
    "命令行",
    "powershell",
    "terminal",
    "cmd",
    "wsl",
    "ubuntu",
    "regedit",
    "注册表",
    "uninstall",
    "卸载",
    "settings",
    "设置",
    "控制面板",
    "control panel",
    "security",
    "安全中心",
)
_SHORTCUT_NOISE = (
    "uninstall",
    "卸载",
    "readme",
    "help",
    "帮助",
    "documentation",
    "website",
    "官网",
)


def _normalize(value: object) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _blocked(name: str) -> bool:
    key = _normalize(name)
    if key in {_normalize(item) for item in TERMINAL_APPS}:
        return True
    return any(_normalize(marker) in key for marker in _BLOCKED_MARKERS)


def default_start_menu_dirs(environ: Mapping[str, str] | None = None) -> list[Path]:
    env = os.environ if environ is None else environ
    roots: list[Path] = []
    for variable in ("ProgramData", "APPDATA"):
        base = env.get(variable)
        if base:
            roots.append(Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return roots


@dataclass(frozen=True, slots=True)
class LaunchTarget:
    path: Path
    source: str
    process_names: tuple[str, ...] = ()
    label: str = ""


@dataclass(frozen=True, slots=True)
class AppLaunchResult:
    status: AppLaunchStatus
    message: str
    details: Mapping[str, object] = field(default_factory=dict)


class AppLaunchSkill:
    def __init__(
        self,
        native: Any,
        profiles: Mapping[str, Any],
        *,
        start_menu_dirs: Iterable[Path] | None = None,
        wait_timeout: float = 6.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.native = native
        self.profiles = profiles
        self.start_menu_dirs = (
            list(start_menu_dirs) if start_menu_dirs is not None else default_start_menu_dirs()
        )
        self.wait_timeout = max(0.0, float(wait_timeout))
        self._monotonic = monotonic
        self._sleep = sleeper

    # -- resolution -----------------------------------------------------------

    def _profile_target(self, name: str) -> LaunchTarget | None:
        key = _normalize(name)
        for app_name, profile in self.profiles.items():
            executable = getattr(profile, "executable", None)
            if not executable:
                continue
            aliases = {_normalize(app_name), _normalize(getattr(profile, "name", ""))}
            if key not in aliases:
                continue
            resolved = shutil.which(str(executable)) or str(executable)
            processes = tuple(
                _normalize(item) for item in (getattr(profile, "process_names", ()) or ())
            )
            return LaunchTarget(Path(resolved), "profile", processes, app_name)
        return None

    @staticmethod
    def _alias_target(name: str) -> LaunchTarget | None:
        executable = _KNOWN_EXECUTABLES.get(_normalize(name))
        if executable is None:
            return None
        resolved = shutil.which(executable)
        if resolved is None:
            return None
        return LaunchTarget(Path(resolved), "alias", (_normalize(executable),), executable)

    def _shortcut_target(self, name: str) -> LaunchTarget | None:
        query = _normalize(name)
        if len(query) < 2:
            return None
        scored: list[tuple[float, float, str, Path]] = []
        for root in self.start_menu_dirs:
            try:
                shortcuts = list(root.rglob("*.lnk"))
            except OSError:
                continue
            for shortcut in shortcuts:
                stem = shortcut.stem
                if any(noise in _normalize(stem) for noise in _SHORTCUT_NOISE):
                    continue
                if _blocked(stem):
                    continue
                score = _containment_score(name, stem)
                if score <= 0:
                    continue
                scored.append((score, _dice_score(name, stem), stem, shortcut))
        if not scored:
            return None
        scored.sort(key=lambda item: (-item[0], -item[1], item[2].casefold()))
        best = scored[0]
        exact = _normalize(best[2]) == query
        if not exact and best[0] < 0.6:
            return None
        if len(scored) > 1 and not exact and best[0] - scored[1][0] < 0.2:
            return None
        return LaunchTarget(best[3], "start_menu", (), best[2])

    def resolve(self, name: str) -> LaunchTarget | None:
        if not isinstance(name, str) or not name.strip() or _blocked(name):
            return None
        return self._profile_target(name) or self._alias_target(name) or self._shortcut_target(name)

    # -- launching ------------------------------------------------------------

    def _matches(self, window: Any, target: LaunchTarget, name: str) -> bool:
        process = _normalize(getattr(window, "process_name", "")).removesuffix(".exe")
        title = _normalize(getattr(window, "title", ""))
        if target.process_names:
            return any(process == item.removesuffix(".exe") for item in target.process_names)
        label = _normalize(target.label or name)
        return bool(label) and (label in title or label in process or process in label)

    def launch(self, name: str) -> AppLaunchResult:
        if not callable(getattr(self.native, "shell_execute_path", None)):
            return AppLaunchResult("retryable_failure", "The native backend cannot launch apps")
        target = self.resolve(name)
        if target is None:
            return AppLaunchResult(
                "retryable_failure",
                "No installed application answers to that name",
                {"name": name},
            )
        try:
            before = {getattr(window, "hwnd", None) for window in self.native.enumerate_windows()}
            self.native.shell_execute_path(target.path)
            deadline = self._monotonic() + self.wait_timeout
            while True:
                fresh = [
                    window
                    for window in self.native.enumerate_windows()
                    if getattr(window, "hwnd", None) not in before
                    and self._matches(window, target, name)
                ]
                if fresh:
                    window = fresh[0]
                    self.native.activate_window(window.hwnd)
                    return AppLaunchResult(
                        "succeeded",
                        f"Launched {target.label or name}",
                        {
                            "hwnd": window.hwnd,
                            "path": str(target.path),
                            "source": target.source,
                        },
                    )
                if self._monotonic() >= deadline:
                    return AppLaunchResult(
                        "retryable_failure",
                        "The application was started but showed no window in time",
                        {"path": str(target.path), "source": target.source},
                    )
                self._sleep(0.25)
        except (DesktopUnavailableError, ForegroundIntegrityBoundary) as exc:
            return AppLaunchResult(
                "hard_failure",
                "Secure desktop or a foreground integrity boundary blocked the launch",
                {"error_type": type(exc).__name__},
            )
        except Exception as exc:
            return AppLaunchResult(
                "retryable_failure",
                "Launching the application failed through the native Windows adapter",
                {"error_type": type(exc).__name__},
            )


__all__ = ["AppLaunchResult", "AppLaunchSkill", "LaunchTarget", "default_start_menu_dirs"]
