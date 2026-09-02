from __future__ import annotations

import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from threading import Event
from typing import Any, Literal
from urllib.parse import urlsplit

from ....windows.native import (
    DesktopUnavailableError,
    ForegroundIntegrityBoundary,
    NativeWindows,
    WindowInfo,
)
from ....windows.uia import PasswordFieldError
from ...protocol import (
    DesktopAction,
    DesktopActionType,
    DesktopDriver,
    DesktopElement,
    DesktopObservation,
)
from ..browser_identity import is_verified_browser_address
from ..models import TaskSpec
from ..policy import AssistivePolicy, PolicyDisposition

BrowserNavigationStatus = Literal[
    "succeeded",
    "retryable_failure",
    "policy_rejected",
    "hard_failure",
]

_BROWSER_PROCESSES = {
    "chrome": frozenset({"chrome.exe"}),
}


def _normalize(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


@dataclass(frozen=True, slots=True)
class BrowserNavigationResult:
    status: BrowserNavigationStatus
    message: str
    details: Mapping[str, object] = field(default_factory=dict)
    observation: DesktopObservation | None = None


class BrowserNavigationSkill:
    """Navigate a verified browser address bar without invoking an LLM planner."""

    def __init__(
        self,
        native: NativeWindows,
        driver: DesktopDriver,
        policy: AssistivePolicy,
        profiles: Mapping[str, Any],
        *,
        address_timeout: float = 0.75,
        typed_timeout: float = 1.5,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.native = native
        self.driver = driver
        self.policy = policy
        self.profiles = profiles
        self.address_timeout = max(0.0, float(address_timeout))
        self.typed_timeout = max(0.0, float(typed_timeout))
        self._monotonic = monotonic
        self._sleep = sleeper

    @staticmethod
    def _cancelled(cancel_event: Event | None) -> bool:
        return bool(cancel_event is not None and cancel_event.is_set())

    @staticmethod
    def _navigation_url(raw_url: str) -> tuple[str | None, bool]:
        candidate = raw_url.strip()
        if not candidate or any(character.isspace() for character in candidate):
            return None, False
        if "://" not in candidate:
            candidate = f"https://{candidate}"
        try:
            parsed = urlsplit(candidate)
            _ = parsed.port
        except ValueError:
            return None, False
        credential_bearing = parsed.username is not None or parsed.password is not None
        if credential_bearing:
            return None, True
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return None, False
        return candidate, False

    def _processes_for(self, app: str) -> frozenset[str]:
        normalized_app = _normalize(app)
        family = normalized_app.split("-", 1)[0]
        profile = next(
            (
                profile
                for key, profile in self.profiles.items()
                if _normalize(key) in {normalized_app, family}
            ),
            None,
        )
        configured = frozenset(
            _normalize(item)
            for item in (getattr(profile, "process_names", ()) or ())
            if str(item).strip()
        )
        return configured or _BROWSER_PROCESSES.get(family, frozenset())

    def _bound_foreground(self, app: str, hwnd: int | None = None) -> WindowInfo | None:
        foreground = self.native.get_foreground_window_info()
        processes = self._processes_for(app)
        if (
            foreground is None
            or not processes
            or _normalize(foreground.process_name) not in processes
            or (hwnd is not None and foreground.hwnd != hwnd)
        ):
            return None
        self.native.assert_foreground(foreground.hwnd)
        return foreground

    @staticmethod
    def _address_target(
        observation: DesktopObservation,
        *,
        require_focused: bool,
    ) -> DesktopElement | None:
        candidates = [
            element
            for element in observation.elements
            if is_verified_browser_address(
                element,
                observation.process_name,
                require_focused=require_focused,
            )
        ]
        return candidates[0] if len(candidates) == 1 else None

    def _bound_observation(
        self,
        app: str,
        *,
        hwnd: int,
        observation: DesktopObservation | None,
        cancel_event: Event | None,
    ) -> DesktopObservation | None:
        current = observation
        if current is None:
            current = self.driver.observe(app, cancel_event=cancel_event)
        if (
            current.local_window_id != f"hwnd:{hwnd}"
            or _normalize(current.process_name) not in self._processes_for(app)
        ):
            return None
        return current

    def _wait_for_state(
        self,
        observation: DesktopObservation,
        element: DesktopElement,
        read_state: Callable[[DesktopObservation, DesktopElement], tuple[str | None, bool | None]],
        *,
        predicate: Callable[[str | None, bool | None], bool],
        timeout: float,
        cancel_event: Event | None,
    ) -> bool:
        """Poll the bound address bar's live state instead of re-walking the window."""

        deadline = self._monotonic() + timeout
        while True:
            if self._cancelled(cancel_event):
                return False
            value, focused = read_state(observation, element)
            if predicate(value, focused):
                return True
            if self._monotonic() >= deadline:
                return False
            self._sleep(0.05)

    @staticmethod
    def _same_url_text(value: str | None, url: str) -> bool:
        candidate = (value or "").strip()
        return candidate == url or candidate.rstrip("/") == url.rstrip("/")

    @staticmethod
    def _policy_status(disposition: PolicyDisposition, hard_block: bool) -> BrowserNavigationStatus:
        if disposition == PolicyDisposition.BLOCK and hard_block:
            return "hard_failure"
        return "policy_rejected"

    def navigate(
        self,
        task: TaskSpec,
        *,
        app: str,
        raw_url: str,
        observation: DesktopObservation | None = None,
        cancel_event: Event | None = None,
    ) -> BrowserNavigationResult:
        url, credential_bearing = self._navigation_url(raw_url)
        if url is None:
            return BrowserNavigationResult(
                "hard_failure" if credential_bearing else "retryable_failure",
                (
                    "Credential-bearing URLs are blocked from deterministic browser navigation"
                    if credential_bearing
                    else "Only a valid HTTP(S) URL can use deterministic browser navigation"
                ),
            )
        try:
            self.native.assert_interactive_desktop()
            foreground = self._bound_foreground(app)
            if foreground is None:
                return BrowserNavigationResult(
                    "retryable_failure",
                    "The requested browser is not the exact foreground window",
                )
            if self._cancelled(cancel_event):
                return BrowserNavigationResult("retryable_failure", "Browser navigation cancelled")
            current = self._bound_observation(
                app,
                hwnd=foreground.hwnd,
                observation=observation,
                cancel_event=cancel_event,
            )
            address = (
                self._address_target(current, require_focused=False)
                if current is not None
                else None
            )
            if current is None or address is None:
                return BrowserNavigationResult(
                    "retryable_failure",
                    "The browser did not expose one exact UIA-bound address bar",
                )
            focus_policy = self.policy.authorize_browser_address_focus(current, address.index)
            if focus_policy.disposition != PolicyDisposition.AUTO:
                return BrowserNavigationResult(
                    self._policy_status(focus_policy.disposition, focus_policy.hard_block),
                    focus_policy.reason,
                    observation=current,
                )
            revalidate = getattr(self.driver, "assert_browser_address_bound", None)
            if not callable(revalidate):
                return BrowserNavigationResult(
                    "retryable_failure",
                    "The desktop driver cannot revalidate browser-chrome provenance",
                    observation=current,
                )
            revalidate(current, address, require_focus=address.focused is True)
            read_state = getattr(self.driver, "read_element_state", None)
            if not callable(read_state):
                return BrowserNavigationResult(
                    "retryable_failure",
                    "The desktop driver cannot read the bound address bar state",
                    observation=current,
                )
            # Keystrokes go to the OS focus, so every step below is gated on
            # the bound HWND being foreground and on the exact omnibox
            # reporting keyboard focus and the expected text. Chrome exposes
            # extra focused nodes (page inputs, the omnibox popup) once the
            # address bar is active, which is why the driver's one-focused-
            # element guard is not used here.
            focus_actions = 0
            if address.focused is not True:
                self.native.assert_foreground(foreground.hwnd)
                self.native.send_hotkey("ctrl+l")
                focus_actions = 1
                self.native.assert_foreground(foreground.hwnd)
                if not self._wait_for_state(
                    current,
                    address,
                    read_state,
                    predicate=lambda _value, focused: focused is True,
                    timeout=self.address_timeout,
                    cancel_event=cancel_event,
                ):
                    return BrowserNavigationResult(
                        "retryable_failure",
                        "The exact browser address bar did not gain focus after Ctrl+L",
                        observation=current,
                    )
            type_action = DesktopAction(
                DesktopActionType.TYPE_TEXT,
                app=current.app,
                generation=current.generation,
                element_index=address.index,
                text=url,
            )
            type_policy = self.policy.evaluate(task, type_action, current)
            if type_policy.disposition != PolicyDisposition.AUTO:
                return BrowserNavigationResult(
                    self._policy_status(type_policy.disposition, type_policy.hard_block),
                    type_policy.reason,
                    observation=current,
                )
            self.native.assert_foreground(foreground.hwnd)
            self.native.send_text(url)
            if not self._wait_for_state(
                current,
                address,
                read_state,
                predicate=lambda value, focused: focused is True
                and self._same_url_text(value, url),
                timeout=self.typed_timeout,
                cancel_event=cancel_event,
            ):
                return BrowserNavigationResult(
                    "retryable_failure",
                    "The exact requested URL is not present in the bound address bar",
                    observation=current,
                )
            enter_action = DesktopAction(
                DesktopActionType.PRESS_KEY,
                app=current.app,
                generation=current.generation,
                element_index=address.index,
                key="enter",
            )
            enter_policy = self.policy.evaluate(task, enter_action, current)
            if enter_policy.disposition != PolicyDisposition.AUTO:
                return BrowserNavigationResult(
                    self._policy_status(enter_policy.disposition, enter_policy.hard_block),
                    enter_policy.reason,
                    observation=current,
                )
            self.native.assert_foreground(foreground.hwnd)
            self.native.send_hotkey("enter")
            return BrowserNavigationResult(
                "succeeded",
                "Submitted the exact requested URL through the bound browser address bar",
                {"hwnd": foreground.hwnd, "actions": focus_actions + 2},
                observation=current,
            )
        except (
            DesktopUnavailableError,
            ForegroundIntegrityBoundary,
            PasswordFieldError,
        ) as exc:
            return BrowserNavigationResult(
                "hard_failure",
                "A secure desktop, foreground boundary, or credential target blocked navigation",
                {"error_type": type(exc).__name__},
            )
        except Exception as exc:
            return BrowserNavigationResult(
                "retryable_failure",
                "Deterministic browser navigation failed through the local Windows adapter",
                {
                    "error_type": type(exc).__name__,
                    # Driver messages name the failed check ("stale observation",
                    # "not the observed focused element"); keep them bounded.
                    "error": " ".join(str(exc).split())[:200],
                },
            )


__all__ = [
    "BrowserNavigationResult",
    "BrowserNavigationSkill",
    "BrowserNavigationStatus",
]
