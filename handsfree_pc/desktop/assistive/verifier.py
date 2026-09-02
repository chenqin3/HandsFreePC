from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any
from urllib.parse import unquote, urlsplit

from ...windows.native import DesktopUnavailableError, ForegroundIntegrityBoundary
from ...windows.uia import PasswordFieldError
from ..protocol import DesktopObservation, ElementPlane, element_plane
from ..visual_ocr import SensitiveVisualSurfaceError
from .browser_identity import is_browser_process, is_verified_browser_address
from .models import Goal, GoalKind, TaskSpec

_APP_ALIASES: dict[str, tuple[str, ...]] = {
    "claude": ("claude", "克劳德"),
    "codex": ("codex", "chatgpt", "chat gpt", "代码助手"),
    "chrome": ("chrome", "google chrome", "谷歌浏览器"),
    "explorer": ("explorer", "file explorer", "资源管理器", "文件资源管理器"),
    "wechat": ("wechat", "weixin", "微信"),
}


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[\s\u200b\u200e\u200f]+", "", normalized)


def _target_aliases(target: str) -> tuple[str, ...]:
    normalized = _normalize(target)
    for family, aliases in _APP_ALIASES.items():
        if normalized == _normalize(family) or any(
            normalized == _normalize(item) for item in aliases
        ):
            return tuple(_normalize(item) for item in (family, *aliases))
    return (normalized,)


def _inventory_entries(raw: str) -> tuple[dict[str, Any], ...]:
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(item for item in parsed if isinstance(item, dict))


def _entry_matches_app(entry: dict[str, Any], target: str) -> bool:
    aliases = _target_aliases(target)
    for field in ("app", "display_name", "process_name", "class_name"):
        value = _normalize(str(entry.get(field) or ""))
        executable_stem = value.removesuffix(".exe")
        if any(
            alias
            and (
                value == alias
                or executable_stem == alias
                or value.startswith(f"{alias}-")
            )
            for alias in aliases
        ):
            return True
    if len(aliases) == 1:
        # A spoken name outside the alias table may still name one exact
        # window by its title ("切换到 Design" for a claude.exe window titled
        # "Design"). Alias families never match by title so that "Google
        # Chrome" in every browser title cannot widen a family match.
        title = _normalize(str(entry.get("window_title") or ""))
        wanted = aliases[0]
        if title and len(wanted) >= 2 and wanted in title:
            return True
    return False


def _has_rich_semantic_surface(observation: DesktopObservation) -> bool:
    """Mirror the driver's rich-surface test for goal verifiability.

    Qt render-only windows (WeChat) and thin Electron shells (Codex) expose
    only a Pane or a few title-bar buttons. Text-level goals in such windows
    cannot be proved locally, so they are planner-judged instead of doomed.
    """

    inputs = [
        element
        for element in observation.elements
        if element.addressable
        and element.enabled
        and not element.visual_ocr
        and element_plane(element) == ElementPlane.INPUT
        and element.editable is not False
    ]
    controls = [
        element
        for element in observation.elements
        if element.addressable
        and element.enabled
        and not element.visual_ocr
        and element_plane(element) == ElementPlane.CONTROL
    ]
    return bool(
        any(element.composer for element in inputs)
        or (inputs and len(controls) >= 5)
        or len(controls) >= 8
    )


def _observation_text(observation: DesktopObservation | None) -> str:
    if observation is None:
        return ""
    values = [observation.window_title or "", observation.accessibility_text]
    for element in observation.elements:
        values.extend((element.name, element.value or ""))
    return "\n".join(values)


def _url_parts(value: str) -> tuple[str, int | None, str, str, str] | None:
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = urlsplit(candidate if "://" in candidate else f"https://{candidate}")
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return None
        hostname = parsed.hostname.casefold().rstrip(".").removeprefix("www.")
        port = parsed.port
        if (parsed.scheme.casefold(), port) in {("http", 80), ("https", 443)}:
            port = None
    except ValueError:
        return None
    path = unquote(parsed.path or "/").rstrip("/") or "/"
    return hostname, port, path, parsed.query, parsed.fragment


def _address_matches_url(target: str, candidate: str | None) -> bool:
    target_parts = _url_parts(target)
    candidate_parts = _url_parts(candidate or "")
    if target_parts is None or candidate_parts is None:
        return False
    target_host, target_port, target_path, target_query, target_fragment = target_parts
    host, port, path, query, fragment = candidate_parts
    if host != target_host or port != target_port:
        return False
    if target_path != "/" and path != target_path:
        return False
    if target_query and query != target_query:
        return False
    return not target_fragment or fragment == target_fragment


def _conversation_title_matches(goal: Goal, title: str | None) -> bool:
    target = _normalize(goal.target)
    normalized_title = _normalize(title)
    if not target or not normalized_title:
        return False
    if normalized_title == target:
        return True
    aliases = _target_aliases(goal.app or "") if goal.app else ()
    separators = ("-", "–", "—", "|", "·", ":", "：")
    return any(
        normalized_title in {f"{target}{separator}{alias}", f"{alias}{separator}{target}"}
        for alias in aliases
        if alias
        for separator in separators
    )


@dataclass(frozen=True, slots=True)
class GoalCheck:
    goal: Goal
    satisfied: bool
    evidence: str
    # False when no local signal can ever prove this goal (free-form intent,
    # or a window without a semantic UIA surface). The controller then
    # accepts the planner's completion judgement instead of failing forever.
    verifiable: bool = True


@dataclass(frozen=True, slots=True)
class VerificationResult:
    checks: tuple[GoalCheck, ...]
    inventory: str
    observation: DesktopObservation | None = None

    @property
    def completed(self) -> bool:
        return bool(self.checks) and all(check.satisfied for check in self.checks)

    @property
    def satisfied_count(self) -> int:
        return sum(check.satisfied for check in self.checks)

    @property
    def pending(self) -> tuple[Goal, ...]:
        return tuple(check.goal for check in self.checks if not check.satisfied)

    @property
    def pending_unverifiable(self) -> bool:
        """True when every unmet goal is one no local verifier can decide."""

        unmet = [check for check in self.checks if not check.satisfied]
        return bool(unmet) and all(not check.verifiable for check in unmet)

    @property
    def summary(self) -> str:
        if self.completed:
            return f"全部 {len(self.checks)} 个任务目标已成立"
        pending = ", ".join(
            f"{check.goal.kind.value}:{check.goal.target}"
            for check in self.checks
            if not check.satisfied
        )
        return f"{self.satisfied_count}/{len(self.checks)} 个目标成立；待完成 {pending}"


class GoalVerifier:
    """Check task-level observable goals without a proof-step ledger."""

    def __init__(self, *, native: Any | None = None) -> None:
        self._native = native

    @staticmethod
    def _native_from_driver(driver: Any) -> Any | None:
        backend = getattr(driver, "_native_backend", None)
        if callable(backend):
            try:
                return backend()
            except Exception:
                return None
        return getattr(driver, "native", None)

    @staticmethod
    def _goal_app(goal: Goal, observation: DesktopObservation | None) -> str | None:
        if goal.app:
            return goal.app
        return observation.app if observation is not None else None

    def _check_app_foreground(
        self,
        goal: Goal,
        entries: tuple[dict[str, Any], ...],
    ) -> GoalCheck:
        matched = [
            entry
            for entry in entries
            if entry.get("foreground") is True and _entry_matches_app(entry, goal.target)
        ]
        return GoalCheck(
            goal,
            bool(matched),
            "matching foreground window" if matched else "target app is not foreground",
        )

    def _check_path(self, goal: Goal, native: Any | None) -> GoalCheck:
        target = os.path.expandvars(goal.target)
        try:
            state = native.path_open_state(Path(target)) if native is not None else None
        except (
            DesktopUnavailableError,
            ForegroundIntegrityBoundary,
            PasswordFieldError,
            SensitiveVisualSurfaceError,
        ):
            raise
        except Exception as exc:
            return GoalCheck(goal, False, f"path state unavailable: {type(exc).__name__}")
        satisfied = bool(isinstance(state, dict) and state.get("verified") is True)
        kind = state.get("kind") if isinstance(state, dict) else "unavailable"
        return GoalCheck(goal, satisfied, f"path verification: {kind}")

    @staticmethod
    def _app_scope_matches(
        goal: Goal,
        observation: DesktopObservation | None,
    ) -> bool:
        if goal.app is None:
            return True
        if observation is None:
            return False
        return _entry_matches_app(
            {
                "app": observation.app,
                "process_name": observation.process_name,
                "class_name": observation.class_name,
            },
            goal.app,
        )

    def _check_textual(
        self,
        goal: Goal,
        observation: DesktopObservation | None,
    ) -> GoalCheck:
        if goal.kind == GoalKind.FREE_FORM:
            return GoalCheck(
                goal,
                False,
                "free-form completion is planner-judged; no local verifier applies",
                verifiable=False,
            )
        if goal.kind == GoalKind.FILE_SENT:
            return GoalCheck(
                goal,
                False,
                "file delivery is proved by the sending skill, not by UIA",
                verifiable=False,
            )
        if observation is None or not self._app_scope_matches(goal, observation):
            return GoalCheck(goal, False, "goal app has no current observation")
        check = self._textual_evidence(goal, observation)
        if check.satisfied or _has_rich_semantic_surface(observation):
            return check
        # Positive evidence always counts. Absent evidence in a window with no
        # semantic UIA surface proves nothing, so the goal is planner-judged.
        return GoalCheck(
            goal,
            False,
            "window exposes no semantic UIA surface; completion is planner-judged",
            verifiable=False,
        )

    @staticmethod
    def _textual_evidence(goal: Goal, observation: DesktopObservation) -> GoalCheck:
        haystack = _normalize(_observation_text(observation))
        if goal.kind == GoalKind.URL_LOADED:
            address_match = False
            for element in observation.elements:
                is_address = is_verified_browser_address(
                    element,
                    observation.process_name,
                )
                # A focused omnibox is an editable draft, not proof that the
                # browser committed a navigation.  Chrome can expose the typed
                # value before Enter while the old page is still displayed.
                if is_address and element.focused is False:
                    address_match = address_match or _address_matches_url(
                        goal.target,
                        element.value,
                    )
            # The omnibox shows the new URL the instant Enter is pressed; the
            # page itself has loaded only once the web document reports it.
            documents = [
                element
                for element in observation.elements
                if element.control_type.casefold() == "document" and element.value
            ]
            if documents:
                document_loaded = any(
                    _address_matches_url(goal.target, document.value) for document in documents
                )
            else:
                # A browser tab that is still loading exposes no document yet.
                document_loaded = not is_browser_process(observation.process_name)
            return GoalCheck(
                goal,
                address_match and document_loaded,
                "driver-proven unfocused browser-chrome address and loaded document",
            )
        target = _normalize(goal.target)
        if goal.kind == GoalKind.TEXT_VISIBLE:
            return GoalCheck(
                goal, bool(target and target in haystack), "visible UI text inspection"
            )
        if goal.kind == GoalKind.CONVERSATION_SELECTED:
            selected = any(
                target == _normalize(element.name)
                and element.selected is True
                for element in observation.elements
            )
            title_match = _conversation_title_matches(goal, observation.window_title)
            return GoalCheck(
                goal,
                bool(selected or title_match),
                "explicit selected state or exact conversation window title",
            )
        if goal.kind == GoalKind.INPUT_CONTAINS:
            matching = [
                element
                for element in observation.elements
                if target in _normalize(element.value)
                and (
                    element.focused is True
                    or element.composer
                    or element_plane(element) == ElementPlane.INPUT
                )
            ]
            return GoalCheck(goal, bool(matching), "editable/composer value inspection")
        if goal.kind == GoalKind.MESSAGE_SENT:
            visible = bool(target and target in haystack)
            still_in_input = any(
                target in _normalize(element.value) and element_plane(element) == ElementPlane.INPUT
                for element in observation.elements
            )
            return GoalCheck(
                goal, visible and not still_in_input, "message visible outside composer"
            )
        if goal.kind == GoalKind.ELEMENT_STATE:
            name, separator, state = goal.target.rpartition(":")
            if not separator:
                return GoalCheck(goal, False, "element_state requires name:state")
            expected = _normalize(state)
            for element in observation.elements:
                if _normalize(name) not in _normalize(element.name):
                    continue
                actual = {
                    "selected": element.selected,
                    "focused": element.focused,
                    "enabled": element.enabled,
                }.get(expected)
                if actual is True:
                    return GoalCheck(goal, True, f"element {expected} state")
            return GoalCheck(goal, False, f"element is not {expected}")
        return GoalCheck(goal, False, "free-form goals need further agent actions")

    def evaluate(
        self,
        task: TaskSpec,
        driver: Any,
        *,
        inventory: str | None = None,
        observation: DesktopObservation | None = None,
        cancel_event: Event | None = None,
    ) -> VerificationResult:
        if inventory is None:
            inventory = driver.list_apps(cancel_event=cancel_event)
        entries = _inventory_entries(inventory)
        native = self._native or self._native_from_driver(driver)
        checks: list[GoalCheck] = []
        for goal in task.goals:
            if goal.kind == GoalKind.APP_FOREGROUND:
                checks.append(self._check_app_foreground(goal, entries))
            elif goal.kind == GoalKind.PATH_OPEN:
                checks.append(self._check_path(goal, native))
            else:
                checks.append(self._check_textual(goal, observation))
        return VerificationResult(tuple(checks), inventory, observation)


__all__ = ["GoalCheck", "GoalVerifier", "VerificationResult"]
