"""The small interface between the voice queue and the desktop executor."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(slots=True)
class ControlResult:
    """Outcome of one spoken instruction handed to the executor."""

    success: bool
    message: str
    session_id: str | None = None
    cancelled: bool = False
    timed_out: bool = False
    returncode: int | None = None
    stage: str | None = None
    error_code: str | None = None
    safe_message: str | None = None
    exception_type: str | None = None


@runtime_checkable
class Controller(Protocol):
    """What the voice runtime needs from an executor."""

    @property
    def session_id(self) -> str | None: ...

    def run(
        self,
        instruction: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ControlResult: ...

    def cancel(self) -> bool: ...

    def close(self) -> None: ...
