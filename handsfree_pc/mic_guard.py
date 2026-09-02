"""Pause listening while another application is using the microphone.

Windows records every app's microphone use under
``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\microphone``
(the same data behind the privacy indicator). An entry whose ``LastUsedTimeStart``
is set and whose ``LastUsedTimeStop`` is zero is capturing right now. Meeting and
call apps (Zoom, Teams, 腾讯会议, WeLink, WeChat calls, browser meetings) all show
up there, so the voice runtime can release the microphone for the duration of a
meeting and pick it up again afterwards without any app-specific integration.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

_CONSENT_STORE = (
    r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
)
_FILETIME_EPOCH_OFFSET = 116444736000000000
_FILETIME_TICKS_PER_SECOND = 10_000_000


@dataclass(frozen=True, slots=True)
class MicrophoneUse:
    """One consent-store entry: who used the microphone and when."""

    label: str
    started_at: float
    stopped_at: float | None

    @property
    def in_use(self) -> bool:
        return self.started_at > 0 and self.stopped_at is None


def _filetime_to_unix(value: object) -> float | None:
    if not isinstance(value, int) or value <= 0:
        return None
    return (value - _FILETIME_EPOCH_OFFSET) / _FILETIME_TICKS_PER_SECOND


def _friendly_label(key_name: str, *, packaged: bool) -> str:
    if packaged:
        # "MicrosoftTeams_8wekyb3d8bbwe" -> "MicrosoftTeams"
        return key_name.split("_", 1)[0]
    return key_name.replace("#", "\\")


def read_registry_microphone_uses() -> list[MicrophoneUse]:
    """Read the consent store; empty outside Windows or when unreadable."""

    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:  # pragma: no cover - non-Windows interpreters
        return []

    uses: list[MicrophoneUse] = []

    def collect(path: str, *, packaged: bool) -> None:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            return
        with key:
            index = 0
            while True:
                try:
                    name = winreg.EnumKey(key, index)
                except OSError:
                    break
                index += 1
                if packaged and name == "NonPackaged":
                    continue
                try:
                    sub = winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{path}\\{name}")
                except OSError:
                    continue
                with sub:
                    values: dict[str, object] = {}
                    value_index = 0
                    while True:
                        try:
                            value_name, value, _type = winreg.EnumValue(sub, value_index)
                        except OSError:
                            break
                        value_index += 1
                        values[value_name] = value
                started = _filetime_to_unix(values.get("LastUsedTimeStart"))
                if started is None:
                    continue
                stopped = _filetime_to_unix(values.get("LastUsedTimeStop"))
                uses.append(
                    MicrophoneUse(
                        _friendly_label(name, packaged=packaged),
                        started,
                        stopped,
                    )
                )

    collect(_CONSENT_STORE, packaged=True)
    collect(_CONSENT_STORE + r"\NonPackaged", packaged=False)
    return uses


def _normalize(value: str) -> str:
    return value.replace("/", "\\").strip().casefold()


class MicrophoneGuard:
    """Answer "is someone else using the microphone right now?" with a poll cache."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        poll_seconds: float = 3.0,
        ignore: Iterable[str] = (),
        recent_hours: float = 12.0,
        reader: Callable[[], list[MicrophoneUse]] = read_registry_microphone_uses,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], float] = time.time,
        own_executables: Iterable[str] | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.poll_seconds = max(0.5, float(poll_seconds))
        self.recent_hours = max(0.0, float(recent_hours))
        self._reader = reader
        self._monotonic = monotonic
        self._clock = clock
        own = list(own_executables) if own_executables is not None else self._default_own()
        self._ignore = {_normalize(item) for item in (*ignore, *own) if item.strip()}
        self._last_poll = float("-inf")
        self._last_busy: str | None = None

    @staticmethod
    def _default_own() -> list[str]:
        """Every path Windows might attribute this process's capture to.

        A venv's python.exe is a launcher that runs the base interpreter as a
        child, so the consent store may record either path; list both, plus
        their pythonw siblings.
        """

        candidates: list[str] = []
        for executable in (
            sys.executable or "",
            getattr(sys, "_base_executable", "") or "",
            os.path.realpath(sys.executable) if sys.executable else "",
        ):
            if not executable:
                continue
            _stem, ext = os.path.splitext(executable)
            directory = os.path.dirname(executable)
            candidates.append(executable)
            for name in ("python", "pythonw"):
                candidates.append(os.path.join(directory, name + (ext or ".exe")))
        return candidates

    def _ignored(self, label: str) -> bool:
        normalized = _normalize(label)
        base = normalized.rsplit("\\", 1)[-1]
        return normalized in self._ignore or base in self._ignore

    def busy_apps(self) -> list[str]:
        """Fresh scan: labels of other apps capturing right now (no cache)."""

        if not self.enabled:
            return []
        try:
            uses = self._reader()
        except Exception:
            return []
        now = self._clock()
        horizon = self.recent_hours * 3600 if self.recent_hours > 0 else None
        busy: list[str] = []
        for use in uses:
            if not use.in_use or self._ignored(use.label):
                continue
            if horizon is not None and now - use.started_at > horizon:
                # A crashed app can leave "in use" behind forever; ignore
                # ancient starts so listening cannot be blocked indefinitely.
                continue
            busy.append(use.label.rsplit("\\", 1)[-1])
        return busy

    def busy_app(self) -> str | None:
        """Cached view used by the listen loop; None when the microphone is free."""

        if not self.enabled:
            return None
        now = self._monotonic()
        if now - self._last_poll < self.poll_seconds:
            return self._last_busy
        self._last_poll = now
        busy = self.busy_apps()
        self._last_busy = busy[0] if busy else None
        return self._last_busy


__all__ = ["MicrophoneGuard", "MicrophoneUse", "read_registry_microphone_uses"]
