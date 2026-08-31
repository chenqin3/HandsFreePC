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


@dataclass(frozen=True, slots=True)
class _ElementIdentity:
    name: str
    control_type: str
    automation_id: str | None
    runtime_id: tuple[int, ...] | str | None
    exists: bool | None
    visible: bool | None
    enabled: bool | None
    password: bool


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


def _safe_attr(value: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(value, name, default)
    except Exception:
        return default


def _safe_call(value: Any, name: str, default: Any = None) -> Any:
    attribute = _safe_attr(value, name, None)
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
        foreground_guard: Callable[[int], None] | None = None,
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
        self._foreground_guard = foreground_guard or self._assert_foreground

    @staticmethod
    def _assert_foreground(hwnd: int) -> None:
        from .native import NativeWindows

        NativeWindows().assert_foreground(hwnd)

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
    def _element_value(element: Any) -> str | None:
        value = _safe_call(element, "get_value", None)
        if value is None:
            iface_value = getattr(element, "iface_value", None)
            value = getattr(iface_value, "CurrentValue", None)
        if value is None:
            properties = _safe_call(element, "legacy_properties", None)
            if isinstance(properties, dict):
                value = properties.get("Value") or properties.get("value")
        return str(value) if value is not None else None

    @staticmethod
    def _is_password(element: Any) -> bool:
        """Read UIA_IsPasswordPropertyId (30019) through common wrapper surfaces."""

        missing = object()
        read_errors: list[Exception] = []

        def read_member(owner: Any, attribute_name: str, *, invoke: bool = True) -> Any:
            try:
                member = getattr(owner, attribute_name)
            except AttributeError:
                return missing
            except Exception as exc:
                read_errors.append(exc)
                return missing
            if not callable(member) or not invoke:
                return member
            try:
                return member()
            except Exception as exc:
                read_errors.append(exc)
                return missing

        try:
            info = getattr(element, "element_info", None)
        except Exception as exc:
            read_errors.append(exc)
            info = None
        for owner in (element, info):
            if owner is None:
                continue
            for attribute_name in ("is_password", "password", "IsPassword"):
                value = read_member(owner, attribute_name)
                if value is missing:
                    continue
                coerced = _coerce_bool(value)
                if coerced is not None:
                    return coerced

        properties = read_member(element, "get_properties")
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
                candidate = read_member(owner, attribute_name)
                if candidate is missing:
                    continue
                if candidate is not None:
                    raw_element = candidate
                    break
            if raw_element is None:
                continue
            get_property = read_member(
                raw_element,
                "GetCurrentPropertyValue",
                invoke=False,
            )
            if callable(get_property):
                try:
                    coerced = _coerce_bool(get_property(30019))
                except Exception as exc:
                    read_errors.append(exc)
                    continue
                if coerced is not None:
                    return coerced
        if read_errors:
            raise UIAError("Could not read the UIA password property") from read_errors[0]
        return False

    def _assert_not_password(self, element: Any) -> None:
        if self._is_password(element):
            raise PasswordFieldError("Refusing to focus or type into a password field")

    def _element_identity(self, element: Any) -> _ElementIdentity:
        return _ElementIdentity(
            name=self._element_name(element),
            control_type=self._control_type(element),
            automation_id=self._automation_id(element),
            runtime_id=self._runtime_id(element),
            exists=_coerce_bool(_safe_call(element, "exists", None)),
            visible=_coerce_bool(_safe_call(element, "is_visible", None)),
            enabled=_coerce_bool(_safe_call(element, "is_enabled", None)),
            password=self._is_password(element),
        )

    def _assert_identity_unchanged(
        self,
        element: Any,
        expected: _ElementIdentity,
    ) -> None:
        current = self._element_identity(element)
        if current != expected:
            raise UIAError("UI target identity changed immediately before input")
        if current.exists is False or current.visible is not True or current.enabled is not True:
            raise UIAError("UI target is not currently present, visible, and enabled")
        if current.password:
            raise PasswordFieldError("Refusing to act on a password field")

    @staticmethod
    def _assert_located_match(identity: _ElementIdentity, match: ElementMatch) -> None:
        if (
            identity.name != match.name
            or identity.control_type != match.control_type
            or identity.automation_id != match.automation_id
            or identity.runtime_id != match.runtime_id
        ):
            raise UIAError("UI target identity changed after exact matching")

    @staticmethod
    def _selection_state(element: Any) -> bool | None:
        selected = _coerce_bool(_safe_call(element, "is_selected", None))
        if selected is not None:
            return selected
        selection_item = _safe_attr(element, "iface_selection_item", None)
        selected = _coerce_bool(_safe_attr(selection_item, "CurrentIsSelected", None))
        if selected is not None:
            return selected
        toggle_state = _safe_call(element, "get_toggle_state", None)
        if isinstance(toggle_state, int):
            return toggle_state == 1
        info = _safe_attr(element, "element_info", None)
        raw_aria = _safe_attr(
            _safe_attr(info, "element", None),
            "CurrentAriaProperties",
            "",
        )
        if isinstance(raw_aria, str):
            aria: dict[str, str] = {}
            for item in raw_aria.split(";"):
                key, separator, value = item.partition("=")
                if separator:
                    aria[key.strip().casefold()] = value.strip().casefold()
            for key in ("selected", "pressed", "checked"):
                if aria.get(key) in {"true", "1"}:
                    return True
                if aria.get(key) in {"false", "0"}:
                    return False
            if "current" in aria:
                return aria["current"] not in {"", "false", "0", "none"}
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

    def click_named_exact(
        self,
        hwnd: int,
        names: str | Sequence[str],
        *,
        control_types: Iterable[str] = ("Button", "ListItem", "TreeItem", "TabItem"),
    ) -> dict[str, object]:
        located = self.find_named(hwnd, names, control_types=control_types)
        if located.match.exact is not True:
            raise ElementNotFoundError(
                f"No exact enabled visible UI element matched the configured label {names!r}"
            )
        element = located.wrapper
        target_identity = self._element_identity(element)
        self._assert_located_match(target_identity, located.match)
        self._assert_identity_unchanged(element, target_identity)
        before_tree = self._tree_signature(hwnd)
        before_selected = self._selection_state(element)
        if before_selected is True:
            self._assert_identity_unchanged(element, target_identity)
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
                # Once Invoke has been called, an exception leaves its side
                # effect unknown. Never retry the same logical action through
                # physical input; the caller must re-observe and fail closed.
                self._assert_identity_unchanged(element, target_identity)
                invoke()
            else:
                method = "click_input"
                click_input = getattr(element, "click_input", None)
                if not callable(click_input):
                    raise UIAError("Matched UI element exposes neither invoke nor click_input")
                # Physical click_input targets screen coordinates. Re-check the
                # exact HWND immediately before that input, not only in the
                # executor before/after this method.
                self._foreground_guard(int(hwnd))
                self._assert_identity_unchanged(element, target_identity)
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

    def click_named(
        self,
        hwnd: int,
        names: str | Sequence[str],
        *,
        control_types: Iterable[str] = ("Button", "ListItem", "TreeItem", "TabItem"),
    ) -> dict[str, object]:
        """Backward-compatible alias; semantic clicks are exact-only."""

        return self.click_named_exact(hwnd, names, control_types=control_types)

    def focus_named(
        self,
        hwnd: int,
        names: str | Sequence[str],
        *,
        control_types: Iterable[str] = ("Edit", "Document"),
    ) -> dict[str, object]:
        located = self.find_named(hwnd, names, control_types=control_types)
        if located.match.exact is not True:
            raise ElementNotFoundError(f"No exact UI element matched names: {names!r}")
        self._assert_not_password(located.wrapper)
        target_identity = self._element_identity(located.wrapper)
        self._assert_located_match(target_identity, located.match)
        self._assert_identity_unchanged(located.wrapper, target_identity)
        set_focus = getattr(located.wrapper, "set_focus", None)
        if not callable(set_focus):
            raise UIAError(f"Matched element {located.match.name!r} cannot receive focus")
        try:
            self._assert_identity_unchanged(located.wrapper, target_identity)
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
            if any(self._element_name(item) for item in candidates):
                raise ElementNotFoundError(
                    "No exact named text entry matched, and the fallback was not unnamed"
                ) from not_found
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
            target_identity = self._element_identity(element)
            self._assert_identity_unchanged(element, target_identity)
            set_focus = getattr(element, "set_focus", None)
            if not callable(set_focus):
                raise UIAError("The only text entry element cannot receive focus") from not_found
            try:
                self._assert_identity_unchanged(element, target_identity)
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

    def verify_focused_text_contains(self, hwnd: int, expected: str) -> dict[str, object]:
        """Verify exact input on the unique focused non-password text control.

        The observed value itself is deliberately not returned as evidence.
        """

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
        if len(focused) != 1:
            raise UIAPostconditionError("Exactly one text entry must retain focus after text input")
        element = focused[0]
        self._assert_not_password(element)
        value = self._element_value(element)
        if value is None or expected not in value:
            raise UIAPostconditionError("The exact input text is absent from the focused UIA value")
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
                "operation": "verify_text_value",
                "focus_verified": True,
                "input_text_verified": True,
                "character_count": len(expected),
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
        if located.match.exact is not True:
            raise UIAPostconditionError("Selected postcondition matched only fuzzily")
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

    def verify_named_focused(
        self,
        hwnd: int,
        names: str | Sequence[str],
        *,
        control_types: Iterable[str] = ("ListItem", "TreeItem", "TabItem", "Button"),
    ) -> dict[str, object]:
        """Re-resolve an exact semantic target after a UI tree transition and verify focus."""

        located = self.find_named(hwnd, names, control_types=control_types)
        if located.match.exact is not True:
            raise UIAPostconditionError("Focused postcondition matched only fuzzily")
        if _safe_call(located.wrapper, "has_keyboard_focus", None) is not True:
            raise UIAPostconditionError(
                f"UI element {located.match.name!r} is present but not focused"
            )
        evidence = located.match.to_evidence()
        evidence.update(
            {
                "operation": "verify_focus",
                "focus_verified": True,
            }
        )
        return evidence


# Short alias retained for ergonomic imports.
UIA = UIABackend
