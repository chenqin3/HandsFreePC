from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any


class UIAError(RuntimeError):
    pass


class UIAUnavailableError(UIAError):
    pass


class ElementNotFoundError(UIAError):
    pass


class PasswordFieldError(UIAError):
    pass


class UIAPostconditionError(UIAError):
    pass


class AmbiguousElementError(UIAError):
    def __init__(self, names: Sequence[str], candidates: Sequence[ElementMatch]) -> None:
        super().__init__(f"Ambiguous UI element for names: {list(names)!r}")
        self.names = tuple(names)
        self.candidates = tuple(candidates)


@dataclass(frozen=True, slots=True)
class ElementMatch:
    name: str
    control_type: str
    score: float
    exact: bool
    automation_id: str | None = None
    runtime_id: tuple[int, ...] | str | None = None

    def to_evidence(self) -> dict[str, object]:
        return {
            "name": self.name,
            "control_type": self.control_type,
            "score": round(self.score, 4),
            "exact": self.exact,
            "automation_id": self.automation_id,
            "runtime_id": self.runtime_id,
        }


@dataclass(slots=True)
class _LocatedElement:
    wrapper: Any
    match: ElementMatch


def _normalize(value: str) -> str:
    return " ".join(value.casefold().strip().split())


def _score(requested: str, actual: str) -> float:
    requested_normalized = _normalize(requested)
    actual_normalized = _normalize(actual)
    if not requested_normalized or not actual_normalized:
        return 0.0
    if requested_normalized == actual_normalized:
        return 1.0
    if requested_normalized in actual_normalized or actual_normalized in requested_normalized:
        return 0.94
    return SequenceMatcher(None, requested_normalized, actual_normalized).ratio()


def _safe_call(value: Any, name: str, default: Any = None) -> Any:
    attribute = getattr(value, name, None)
    if attribute is None:
        return default
    try:
        return attribute() if callable(attribute) else attribute
    except Exception:
        return default


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    return None


class UIABackend:
    """Optional pywinauto UIA adapter with ambiguity-closed semantic matching."""

    def __init__(
        self,
        *,
        desktop_factory: Callable[..., Any] | None = None,
        threshold: float = 0.78,
        ambiguity_margin: float = 0.03,
        postcondition_timeout: float = 1.5,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not 0 < threshold <= 1:
            raise ValueError("threshold must be in (0, 1]")
        if not 0 <= ambiguity_margin < 1:
            raise ValueError("ambiguity_margin must be in [0, 1)")
        if postcondition_timeout < 0:
            raise ValueError("postcondition_timeout cannot be negative")
        self._desktop_factory = desktop_factory
        self.threshold = threshold
        self.ambiguity_margin = ambiguity_margin
        self.postcondition_timeout = postcondition_timeout
        self._monotonic = monotonic
        self._sleep = sleeper

    def _desktop(self) -> Any:
        if self._desktop_factory is not None:
            return self._desktop_factory(backend="uia")
        try:
            from pywinauto import Desktop
        except ImportError as exc:
            raise UIAUnavailableError(
                "pywinauto is required for semantic UI Automation actions"
            ) from exc
        return Desktop(backend="uia")

    def _root(self, hwnd: int) -> Any:
        try:
            root = self._desktop().window(handle=int(hwnd))
            wait = getattr(root, "wait", None)
            if callable(wait):
                wait("exists visible enabled", timeout=3)
            return root
        except UIAError:
            raise
        except Exception as exc:
            raise UIAError(f"Could not attach UI Automation to window {int(hwnd)}") from exc

    @staticmethod
    def _element_name(element: Any) -> str:
        text = _safe_call(element, "window_text", "")
        if text:
            return str(text).strip()
        info = getattr(element, "element_info", None)
        return str(getattr(info, "name", "") or "").strip()

    @staticmethod
    def _control_type(element: Any) -> str:
        info = getattr(element, "element_info", None)
        value = getattr(info, "control_type", None)
        if value:
            return str(value)
        return str(_safe_call(element, "friendly_class_name", "") or "")

    @staticmethod
    def _automation_id(element: Any) -> str | None:
        info = getattr(element, "element_info", None)
        value = getattr(info, "automation_id", None)
        return str(value) if value else None

    @staticmethod
    def _runtime_id(element: Any) -> tuple[int, ...] | str | None:
        info = getattr(element, "element_info", None)
        value = _safe_call(info, "runtime_id", None)
        if value is None:
            return None
        if isinstance(value, list | tuple):
            return tuple(int(item) for item in value)
        return str(value)

    @staticmethod
    def _is_password(element: Any) -> bool:
        """Read UIA_IsPasswordPropertyId (30019) through common wrapper surfaces."""

        info = getattr(element, "element_info", None)
        for owner in (element, info):
            if owner is None:
                continue
            for attribute_name in ("is_password", "password", "IsPassword"):
                value = _safe_call(owner, attribute_name, None)
                coerced = _coerce_bool(value)
                if coerced is not None:
                    return coerced

        properties = _safe_call(element, "get_properties", None)
        if isinstance(properties, dict):
            for key in ("is_password", "password", "IsPassword"):
                coerced = _coerce_bool(properties.get(key))
                if coerced is not None:
                    return coerced

        for owner in (info, element):
            if owner is None:
                continue
            raw_element = None
            for attribute_name in ("element", "_element", "uia_element"):
                candidate = getattr(owner, attribute_name, None)
                if candidate is not None:
                    raw_element = candidate
                    break
            get_property = getattr(raw_element, "GetCurrentPropertyValue", None)
            if callable(get_property):
                try:
                    coerced = _coerce_bool(get_property(30019))
                except Exception as exc:
                    raise UIAError("Could not read the UIA password property") from exc
                if coerced is not None:
                    return coerced
        return False

    def _assert_not_password(self, element: Any) -> None:
        if self._is_password(element):
            raise PasswordFieldError("Refusing to focus or type into a password field")

    @staticmethod
    def _selection_state(element: Any) -> bool | None:
        selected = _coerce_bool(_safe_call(element, "is_selected", None))
        if selected is not None:
            return selected
        selection_item = getattr(element, "iface_selection_item", None)
        selected = _coerce_bool(getattr(selection_item, "CurrentIsSelected", None))
        if selected is not None:
            return selected
        toggle_state = _safe_call(element, "get_toggle_state", None)
        if isinstance(toggle_state, int):
            return toggle_state == 1
        return None

    def _tree_signature(self, hwnd: int) -> tuple[tuple[str, str, str, str], ...]:
        root = self._root(hwnd)
        try:
            elements = list(root.descendants())
        except Exception as exc:
            raise UIAError(f"Could not snapshot UI elements in window {int(hwnd)}") from exc
        signature = [
            (
                repr(self._runtime_id(element)),
                self._automation_id(element) or "",
                self._control_type(element),
                self._element_name(element),
            )
            for element in elements
            if _safe_call(element, "is_visible", True) is not False
        ]
        return tuple(signature)

    def _verify_click_postcondition(
        self,
        hwnd: int,
        element: Any,
        *,
        before_tree: tuple[tuple[str, str, str, str], ...],
        before_selected: bool | None,
    ) -> str:
        deadline = self._monotonic() + self.postcondition_timeout
        while True:
            selected = self._selection_state(element)
            if selected is True and before_selected is not True:
                return "selected"
            if _safe_call(element, "has_keyboard_focus", None) is True:
                return "focused"
            if _safe_call(element, "exists", None) is False:
                return "element_disappeared"
            if self._tree_signature(hwnd) != before_tree:
                return "uia_tree_changed"
            if self._monotonic() >= deadline:
                break
            self._sleep(0.05)
        raise UIAPostconditionError(
            "UI action returned without an observable selection, focus, disappearance, "
            "or UI tree change"
        )

    def _candidates(
        self,
        hwnd: int,
        *,
        control_types: Iterable[str] = (),
        require_name: bool = True,
    ) -> list[Any]:
        root = self._root(hwnd)
        try:
            elements = list(root.descendants())
        except Exception as exc:
            raise UIAError(f"Could not enumerate UI elements in window {int(hwnd)}") from exc
        allowed_types = {_normalize(item) for item in control_types if item.strip()}
        result: list[Any] = []
        for element in elements:
            if _safe_call(element, "is_visible", True) is False:
                continue
            if _safe_call(element, "is_enabled", True) is False:
                continue
            if allowed_types and _normalize(self._control_type(element)) not in allowed_types:
                continue
            if self._element_name(element) or not require_name:
                result.append(element)
        return result

    def find_named(
        self,
        hwnd: int,
        names: str | Sequence[str],
        *,
        control_types: Iterable[str] = (),
    ) -> _LocatedElement:
        requested_names = [names] if isinstance(names, str) else list(names)
        requested_names = [item.strip() for item in requested_names if item.strip()]
        if not requested_names:
            raise ValueError("At least one non-empty UI element name is required")
        scored: list[tuple[float, Any, ElementMatch]] = []
        for element in self._candidates(hwnd, control_types=control_types):
            actual = self._element_name(element)
            score = max(_score(requested, actual) for requested in requested_names)
            if score < self.threshold:
                continue
            match = ElementMatch(
                name=actual,
                control_type=self._control_type(element),
                score=score,
                exact=score == 1.0,
                automation_id=self._automation_id(element),
                runtime_id=self._runtime_id(element),
            )
            scored.append((score, element, match))
        if not scored:
            raise ElementNotFoundError(f"No enabled visible UI element matched {requested_names!r}")
        scored.sort(key=lambda item: item[0], reverse=True)
        best_score = scored[0][0]
        ambiguous = [item[2] for item in scored if best_score - item[0] < self.ambiguity_margin]
        if len(ambiguous) > 1:
            raise AmbiguousElementError(requested_names, ambiguous)
        return _LocatedElement(wrapper=scored[0][1], match=scored[0][2])

    def click_named(
        self,
        hwnd: int,
        names: str | Sequence[str],
        *,
        control_types: Iterable[str] = ("Button", "ListItem", "TreeItem", "TabItem"),
    ) -> dict[str, object]:
        located = self.find_named(hwnd, names, control_types=control_types)
        element = located.wrapper
        before_tree = self._tree_signature(hwnd)
        before_selected = self._selection_state(element)
        if before_selected is True:
            evidence = located.match.to_evidence()
            evidence.update(
                {
                    "operation": "noop",
                    "method": None,
                    "postcondition": "already_selected",
                    "postcondition_verified": True,
                }
            )
            return evidence
        method = "invoke"
        try:
            invoke = getattr(element, "invoke", None)
            if callable(invoke):
                invoke()
            else:
                method = "click_input"
                click_input = getattr(element, "click_input", None)
                if not callable(click_input):
                    raise UIAError("Matched UI element exposes neither invoke nor click_input")
                click_input()
        except UIAError:
            raise
        except Exception as exc:
            raise UIAError(f"Could not activate UI element {located.match.name!r}") from exc
        postcondition = self._verify_click_postcondition(
            hwnd,
            element,
            before_tree=before_tree,
            before_selected=before_selected,
        )
        evidence = located.match.to_evidence()
        evidence.update(
            {
                "operation": "click",
                "method": method,
                "postcondition": postcondition,
                "postcondition_verified": True,
            }
        )
        return evidence

    def focus_named(
        self,
        hwnd: int,
        names: str | Sequence[str],
        *,
        control_types: Iterable[str] = ("Edit", "Document"),
    ) -> dict[str, object]:
        located = self.find_named(hwnd, names, control_types=control_types)
        self._assert_not_password(located.wrapper)
        set_focus = getattr(located.wrapper, "set_focus", None)
        if not callable(set_focus):
            raise UIAError(f"Matched element {located.match.name!r} cannot receive focus")
        try:
            set_focus()
        except Exception as exc:
            raise UIAError(f"Could not focus UI element {located.match.name!r}") from exc
        has_focus = _safe_call(located.wrapper, "has_keyboard_focus", None)
        if has_focus is not True:
            raise UIAError(f"UI element {located.match.name!r} did not receive keyboard focus")
        evidence = located.match.to_evidence()
        evidence.update({"operation": "focus", "focus_verified": True})
        return evidence

    def focus_text_entry(
        self,
        hwnd: int,
        *,
        names: Sequence[str] = (
            "Message",
            "Prompt",
            "Ask anything",
            "Message Codex",
            "Chat input",
            "输入消息",
            "发送消息",
            "给 Codex 发送消息",
        ),
    ) -> dict[str, object]:
        try:
            return self.focus_named(hwnd, names, control_types=("Edit", "Document"))
        except ElementNotFoundError as not_found:
            candidates = self._candidates(
                hwnd,
                control_types=("Edit", "Document"),
                require_name=False,
            )
            if not candidates:
                raise
            matches = [
                ElementMatch(
                    name=self._element_name(item),
                    control_type=self._control_type(item),
                    score=1.0,
                    exact=True,
                    automation_id=self._automation_id(item),
                    runtime_id=self._runtime_id(item),
                )
                for item in candidates
            ]
            if len(candidates) > 1:
                raise AmbiguousElementError(names, matches) from not_found
            element = candidates[0]
            self._assert_not_password(element)
            set_focus = getattr(element, "set_focus", None)
            if not callable(set_focus):
                raise UIAError("The only text entry element cannot receive focus") from not_found
            try:
                set_focus()
            except Exception as exc:
                raise UIAError("Could not focus the only text entry element") from exc
            if _safe_call(element, "has_keyboard_focus", None) is not True:
                raise UIAError(
                    "The text entry element did not receive keyboard focus"
                ) from not_found
            evidence = matches[0].to_evidence()
            evidence.update({"operation": "focus", "focus_verified": True})
            return evidence

    def verify_focused_text_entry(self, hwnd: int) -> dict[str, object]:
        """Verify that keyboard focus remains on one non-password text entry control."""

        candidates = self._candidates(
            hwnd,
            control_types=("Edit", "Document"),
            require_name=False,
        )
        focused = [
            element
            for element in candidates
            if _safe_call(element, "has_keyboard_focus", None) is True
        ]
        if not focused:
            raise UIAError("No text entry element has verified keyboard focus")
        if len(focused) > 1:
            matches = [
                ElementMatch(
                    name=self._element_name(item),
                    control_type=self._control_type(item),
                    score=1.0,
                    exact=True,
                    automation_id=self._automation_id(item),
                    runtime_id=self._runtime_id(item),
                )
                for item in focused
            ]
            raise AmbiguousElementError(("focused text entry",), matches)
        element = focused[0]
        self._assert_not_password(element)
        match = ElementMatch(
            name=self._element_name(element),
            control_type=self._control_type(element),
            score=1.0,
            exact=True,
            automation_id=self._automation_id(element),
            runtime_id=self._runtime_id(element),
        )
        evidence = match.to_evidence()
        evidence.update(
            {
                "operation": "verify_focus",
                "focus_verified": True,
                "password_field": False,
            }
        )
        return evidence

    def verify_named_selected(
        self,
        hwnd: int,
        names: str | Sequence[str],
        *,
        control_types: Iterable[str] = ("ListItem", "TreeItem", "TabItem", "Button"),
    ) -> dict[str, object]:
        located = self.find_named(hwnd, names, control_types=control_types)
        if self._selection_state(located.wrapper) is not True:
            raise UIAPostconditionError(
                f"UI element {located.match.name!r} is present but not selected"
            )
        evidence = located.match.to_evidence()
        evidence.update(
            {
                "operation": "verify_selection",
                "selection_verified": True,
            }
        )
        return evidence


# Short alias retained for ergonomic imports.
UIA = UIABackend
