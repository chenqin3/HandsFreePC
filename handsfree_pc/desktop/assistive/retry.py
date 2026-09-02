from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from ..protocol import DesktopObservation
from .models import ActionOutcome

DEFAULT_POLL_SCHEDULE = (0.0, 0.1, 0.25, 0.5, 1.0, 1.75, 3.0)
SKILL_POLL_SCHEDULE = (0.0, 0.1, 0.25, 0.5, 1.0, 1.75, 3.0, 4.0, 5.0)


@dataclass(frozen=True, slots=True)
class ProgressProbe:
    """One non-mutating progress sample after an action."""

    goals_complete: bool
    satisfied_goal_count: int
    observation: DesktopObservation | None = None
    inventory: str = ""
    task_state_complete: bool = False
    task_state_progress: bool = False
    note: str = ""
    verification: Any | None = None


@dataclass(frozen=True, slots=True)
class ProgressSignature:
    satisfied_goal_count: int
    app: str | None
    local_window_id: str | None
    window_title: str | None
    observation_fingerprint: str | None
    screenshot_digest: str | None
    inventory_digest: str


@dataclass(frozen=True, slots=True)
class WaitResult:
    outcome: ActionOutcome
    probe: ProgressProbe
    polls: int
    elapsed_seconds: float
    meaningful_change: bool


class ProgressDetector:
    """Compare task-relevant state without treating observation generations as progress."""

    @staticmethod
    def signature(probe: ProgressProbe) -> ProgressSignature:
        observation = probe.observation
        screenshot_digest = (
            hashlib.sha256(observation.screenshot_png).hexdigest()
            if observation is not None and observation.screenshot_png is not None
            else None
        )
        try:
            inventory_value = json.loads(probe.inventory) if probe.inventory else []
            canonical_inventory = json.dumps(
                inventory_value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            canonical_inventory = probe.inventory
        return ProgressSignature(
            satisfied_goal_count=probe.satisfied_goal_count,
            app=observation.app if observation is not None else None,
            local_window_id=(observation.local_window_id if observation is not None else None),
            window_title=observation.window_title if observation is not None else None,
            observation_fingerprint=(observation.fingerprint if observation is not None else None),
            screenshot_digest=screenshot_digest,
            inventory_digest=hashlib.sha256(
                canonical_inventory.encode("utf-8", errors="surrogatepass")
            ).hexdigest(),
        )

    @staticmethod
    def changed(before: ProgressSignature, after: ProgressSignature) -> bool:
        if after.satisfied_goal_count > before.satisfied_goal_count:
            return True
        return any(
            (
                before.app != after.app,
                before.local_window_id != after.local_window_id,
                before.window_title != after.window_title,
                before.observation_fingerprint != after.observation_fingerprint,
                before.screenshot_digest != after.screenshot_digest,
                before.inventory_digest != after.inventory_digest,
            )
        )


def _validated_schedule(
    schedule: Sequence[float],
    *,
    timeout_seconds: float,
) -> tuple[float, ...]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    values = tuple(float(value) for value in schedule)
    if not values or values[0] != 0.0:
        raise ValueError("poll schedule must begin at zero")
    if any(value < 0 or value > timeout_seconds for value in values):
        raise ValueError("poll schedule escapes the requested timeout")
    if any(left >= right for left, right in zip(values, values[1:], strict=False)):
        raise ValueError("poll schedule must be strictly increasing")
    if values[-1] != timeout_seconds:
        values = (*values, timeout_seconds)
    return values


def wait_for_outcome(
    probe: Callable[[], ProgressProbe],
    *,
    before: ProgressProbe,
    timeout_seconds: float = 3.0,
    schedule: Sequence[float] = DEFAULT_POLL_SCHEDULE,
    cancel_requested: Callable[[], bool] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    abort_on_exception: Callable[[BaseException], bool] | None = None,
) -> WaitResult:
    """Poll for the final goal or meaningful progress within one bounded window.

    The offsets are absolute from the beginning of the wait. A slow probe never
    causes an additional full delay, and cancellation is checked before every
    sleep and sample.
    """

    offsets = _validated_schedule(schedule, timeout_seconds=timeout_seconds)
    started = monotonic()
    baseline = ProgressDetector.signature(before)
    latest = before
    meaningful_change = False
    polls = 0

    for offset in offsets:
        if cancel_requested is not None and cancel_requested():
            return WaitResult(
                ActionOutcome.UNOBSERVABLE,
                latest,
                polls,
                max(0.0, monotonic() - started),
                meaningful_change,
            )
        remaining = started + offset - monotonic()
        if remaining > 0:
            sleeper(remaining)
        if cancel_requested is not None and cancel_requested():
            return WaitResult(
                ActionOutcome.UNOBSERVABLE,
                latest,
                polls,
                max(0.0, monotonic() - started),
                meaningful_change,
            )
        try:
            latest = probe()
        except Exception as exc:
            if abort_on_exception is not None and abort_on_exception(exc):
                raise
            # Transient observation failures are normal while a window is
            # navigating. Continue to the next bounded sample.
            continue
        polls += 1
        if latest.goals_complete or latest.task_state_complete:
            return WaitResult(
                ActionOutcome.COMPLETED,
                latest,
                polls,
                max(0.0, monotonic() - started),
                True,
            )
        current = ProgressDetector.signature(latest)
        if latest.task_state_progress or ProgressDetector.changed(baseline, current):
            return WaitResult(
                ActionOutcome.PROGRESS,
                latest,
                polls,
                max(0.0, monotonic() - started),
                True,
            )

    return WaitResult(
        ActionOutcome.PROGRESS if meaningful_change else ActionOutcome.NO_EFFECT,
        latest,
        polls,
        max(0.0, monotonic() - started),
        meaningful_change,
    )


__all__ = [
    "DEFAULT_POLL_SCHEDULE",
    "SKILL_POLL_SCHEDULE",
    "ProgressDetector",
    "ProgressProbe",
    "ProgressSignature",
    "WaitResult",
    "wait_for_outcome",
]
