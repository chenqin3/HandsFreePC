from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..config import AppProfile
from ..windows.native import AmbiguousWindowError, NativeWindows, WindowInfo, WindowNotFoundError
from ..windows.uia import PasswordFieldError, UIABackend, UIAError, UIAUnavailableError
from .protocol import (
    ActionReceipt,
    DesktopAction,
    DesktopActionType,
    DesktopElement,
    DesktopObservation,
    is_allowed_desktop_key,
)


class WindowsUiaDriverError(RuntimeError):
    pass


class WindowsUiaStaleObservation(WindowsUiaDriverError):
    pass


@dataclass(slots=True)
class _Snapshot:
    hwnd: int
    observation: DesktopObservation
    wrappers: dict[str, Any]


def _safe_attr(owner: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(owner, name, default)
    except Exception:
        return default


def _safe_call(owner: Any, name: str, default: Any = None) -> Any:
    member = _safe_attr(owner, name, None)
    if member is None:
        return default
    try:
        return member() if callable(member) else member
    except Exception:
        return default


def _text(value: Any, maximum: int = 1000, *, field: str = "UIA text") -> str:
    if value is None:
        return ""
    normalized = str(value).replace("\x00", "")
    if len(normalized) > maximum:
        raise WindowsUiaDriverError(
            f"{field} exceeds the local inspection limit; refusing a truncated observation"
        )
    return normalized


class WindowsUiaDriver:
    """Owned Windows driver using Win32 window binding and pywinauto UIA.

    The driver exposes a finite action vocabulary only. Every element index is
    scoped to one app, HWND, and observation generation; one action invalidates
    the snapshot until the caller observes again.
    """

    def __init__(
        self,
        profiles: Mapping[str, AppProfile],
        *,
        native: NativeWindows | None = None,
        desktop_factory: Callable[..., Any] | None = None,
        max_elements: int = 500,
    ) -> None:
        if max_elements < 1 or max_elements > 2000:
            raise ValueError("max_elements must be between 1 and 2000")
        self.profiles = {key.casefold(): value for key, value in profiles.items()}
        self._native = native
        self._desktop_factory = desktop_factory
        self.max_elements = max_elements
        self._generation = 0
        self._snapshots: dict[str, _Snapshot] = {}
        self._pending_observation: set[str] = set()
        self._lock = threading.RLock()

    def _native_backend(self) -> NativeWindows:
        if self._native is None:
            self._native = NativeWindows()
        return self._native

    def _desktop(self) -> Any:
        if self._desktop_factory is not None:
            return self._desktop_factory(backend="uia")
        try:
            from pywinauto import Desktop
        except ImportError as exc:
            raise UIAUnavailableError("pywinauto is required for the Windows UIA driver") from exc
        return Desktop(backend="uia")

    @staticmethod
    def _normalize_app(app: str) -> str:
        if not isinstance(app, str) or not app.strip():
            raise ValueError("app must be a non-empty string")
        return app.strip().casefold()

    def _profile(self, app: str) -> AppProfile:
        normalized = self._normalize_app(app)
        try:
            return self.profiles[normalized]
        except KeyError as exc:
            raise WindowsUiaDriverError(f"application is not configured: {app!r}") from exc

    def _resolve_window(self, app: str) -> WindowInfo:
        profile = self._profile(app)
        native = self._native_backend()
        matches = native.find_windows(
            title_patterns=profile.title_patterns,
            process_names=profile.process_names,
        )
        if not matches:
            raise WindowNotFoundError(f"no visible configured window matched {app!r}")
        if len(matches) == 1:
            return matches[0]
        foreground = native.get_foreground_window_info()
        foreground_matches = [
            item for item in matches if foreground is not None and item.hwnd == foreground.hwnd
        ]
        if len(foreground_matches) == 1:
            return foreground_matches[0]
        raise AmbiguousWindowError(matches)

    def _root(self, hwnd: int) -> Any:
        try:
            root = self._desktop().window(handle=int(hwnd))
            wait = getattr(root, "wait", None)
            if callable(wait):
                wait("exists visible enabled", timeout=3)
            return root
        except (UIAError, UIAUnavailableError):
            raise
        except Exception as exc:
            raise WindowsUiaDriverError(f"could not attach UIA to window {hwnd}") from exc

    @staticmethod
    def _name(element: Any) -> str:
        value = _safe_call(element, "window_text", "")
        if value:
            return _text(value, 500, field="UIA element name")
        return _text(
            getattr(getattr(element, "element_info", None), "name", ""),
            500,
            field="UIA element name",
        )

    @staticmethod
    def _control_type(element: Any) -> str:
        info = getattr(element, "element_info", None)
        value = getattr(info, "control_type", None)
        return _text(
            value or _safe_call(element, "friendly_class_name", ""),
            120,
            field="UIA control type",
        )

    @staticmethod
    def _automation_id(element: Any) -> str | None:
        value = getattr(getattr(element, "element_info", None), "automation_id", None)
        return _text(value, 300, field="UIA automation id") if value else None

    @staticmethod
    def _value(element: Any, *, password: bool) -> str | None:
        if password:
            return None
        value = _safe_call(element, "get_value", None)
        if value is None:
            value = _safe_attr(_safe_attr(element, "iface_value", None), "CurrentValue", None)
        if value is None:
            properties = _safe_call(element, "legacy_properties", None)
            if isinstance(properties, dict):
                value = properties.get("Value")
        return _text(value, 4000, field="UIA element value") if value not in (None, "") else None

    @staticmethod
    def _is_interesting(element: Any, name: str, control_type: str) -> bool:
        return bool(
            name
            or control_type.casefold()
            in {
                "button",
                "checkbox",
                "document",
                "edit",
                "hyperlink",
                "listitem",
                "menuitem",
                "radiobutton",
                "tabitem",
                "treeitem",
            }
        )

    def _element_metadata(self, wrapper: Any, index: str) -> DesktopElement:
        name = self._name(wrapper)
        control_type = self._control_type(wrapper)
        try:
            password = UIABackend._is_password(wrapper)
        except Exception:
            password = True
        try:
            selected = UIABackend._selection_state(wrapper)
        except Exception:
            selected = None
        return DesktopElement(
            index=index,
            name=name,
            control_type=control_type,
            automation_id=self._automation_id(wrapper),
            value=self._value(wrapper, password=password),
            selected=selected,
            focused=_safe_call(wrapper, "has_keyboard_focus", None),
            password=password,
            enabled=_safe_call(wrapper, "is_enabled", True) is not False,
        )

    def _elements(self, root: Any) -> tuple[tuple[DesktopElement, ...], dict[str, Any]]:
        try:
            descendants = list(root.descendants())
        except Exception as exc:
            raise WindowsUiaDriverError("could not enumerate UIA descendants") from exc
        metadata: list[DesktopElement] = []
        wrappers: dict[str, Any] = {}
        for wrapper in descendants:
            if _safe_call(wrapper, "is_visible", True) is False:
                continue
            name = self._name(wrapper)
            control_type = self._control_type(wrapper)
            if not self._is_interesting(wrapper, name, control_type):
                continue
            if len(metadata) >= self.max_elements:
                raise WindowsUiaDriverError(
                    "UIA surface exceeds the configured element inspection limit; "
                    "refusing a partial observation"
                )
            index = str(len(metadata))
            item = self._element_metadata(wrapper, index)
            metadata.append(item)
            wrappers[index] = wrapper
        return tuple(metadata), wrappers

    @staticmethod
    def _accessibility_text(window: WindowInfo, elements: tuple[DesktopElement, ...]) -> str:
        header = json.dumps(
            {
                "window_title": window.title,
                "process_name": window.process_name,
                "process_id": window.process_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        lines = [header]
        for item in elements:
            values = item.planner_payload()
            fields = [
                f"{key}={json.dumps(value, ensure_ascii=False, sort_keys=True)}"
                for key, value in values.items()
                if key != "index"
            ]
            lines.append(f"{item.index} {' '.join(fields)}")
        return "\n".join(lines)

    def start(self) -> None:
        self._native_backend().assert_interactive_desktop()
        self._desktop()

    def list_apps(self, *, cancel_event: threading.Event | None = None) -> str:
        if cancel_event is not None and cancel_event.is_set():
            raise WindowsUiaDriverError("desktop operation was cancelled")
        native = self._native_backend()
        native.assert_interactive_desktop()
        visible: list[dict[str, Any]] = []
        for app, profile in self.profiles.items():
            matches = native.find_windows(
                title_patterns=profile.title_patterns,
                process_names=profile.process_names,
            )
            if matches:
                visible.append({"app": app, "visible_window_count": len(matches)})
        return json.dumps(visible, ensure_ascii=False, sort_keys=True)

    def observe(
        self,
        app: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> DesktopObservation:
        if cancel_event is not None and cancel_event.is_set():
            raise WindowsUiaDriverError("desktop operation was cancelled")
        normalized = self._normalize_app(app)
        native = self._native_backend()
        native.assert_interactive_desktop()
        window = self._resolve_window(app)
        root = self._root(window.hwnd)
        elements, wrappers = self._elements(root)
        with self._lock:
            self._generation += 1
            observation = DesktopObservation(
                app=normalized,
                generation=self._generation,
                accessibility_text=self._accessibility_text(window, elements),
                window_title=window.title,
                elements=elements,
                local_window_id=f"hwnd:{window.hwnd}",
            )
            self._snapshots[normalized] = _Snapshot(window.hwnd, observation, wrappers)
            self._pending_observation.discard(normalized)
            return observation

    @staticmethod
    def _assert_element_usable(wrapper: Any, *, allow_password: bool = False) -> None:
        if _safe_call(wrapper, "is_visible", True) is False:
            raise WindowsUiaStaleObservation("target element is no longer visible")
        if _safe_call(wrapper, "is_enabled", True) is False:
            raise WindowsUiaDriverError("target element is disabled")
        if not allow_password:
            try:
                password = UIABackend._is_password(wrapper)
            except Exception as exc:
                raise PasswordFieldError("could not verify the UIA password property") from exc
            if password:
                raise PasswordFieldError("refusing to control a password element")

    def _assert_element_still_bound(
        self,
        wrapper: Any,
        expected: DesktopElement,
        *,
        require_focus: bool = False,
    ) -> None:
        current = self._element_metadata(wrapper, expected.index)
        expected_state = (
            expected.name,
            expected.control_type,
            expected.automation_id,
            expected.value,
            expected.selected,
            expected.password,
            expected.enabled,
        )
        current_state = (
            current.name,
            current.control_type,
            current.automation_id,
            current.value,
            current.selected,
            current.password,
            current.enabled,
        )
        if current_state != expected_state or (
            require_focus and (expected.focused is not True or current.focused is not True)
        ):
            raise WindowsUiaStaleObservation(
                "target element identity or state changed after the observation"
            )

    @staticmethod
    def _invoke(wrapper: Any, *, foreground_guard: Callable[[], None]) -> str:
        for name in ("invoke", "select", "toggle"):
            method = _safe_attr(wrapper, name, None)
            if callable(method):
                method()
                return name
        method = _safe_attr(wrapper, "click_input", None)
        if callable(method):
            foreground_guard()
            method(button="left", double=False)
            return "click_input"
        raise WindowsUiaDriverError("target element has no allow-listed activation method")

    @staticmethod
    def _secondary(wrapper: Any, action_name: str) -> str:
        normalized = action_name.strip().casefold()
        allowed = {
            "invoke": "invoke",
            "select": "select",
            "toggle": "toggle",
            "expand": "expand",
            "collapse": "collapse",
            "scrollintoview": "scroll_into_view",
            "setfocus": "set_focus",
        }
        method_name = allowed.get(normalized)
        if method_name is None:
            raise WindowsUiaDriverError("secondary action is not allow-listed")
        method = _safe_attr(wrapper, method_name, None)
        if not callable(method):
            raise WindowsUiaDriverError(f"target element does not support {normalized}")
        method()
        return method_name

    @staticmethod
    def _set_value(wrapper: Any, value: str) -> str:
        method = _safe_attr(wrapper, "set_edit_text", None)
        if callable(method):
            method(value)
            return "set_edit_text"
        iface_value = _safe_attr(wrapper, "iface_value", None)
        method = _safe_attr(iface_value, "SetValue", None)
        if callable(method):
            method(value)
            return "ValuePattern.SetValue"
        raise WindowsUiaDriverError("target element does not expose a writable value pattern")

    def execute(
        self,
        action: DesktopAction,
        before: DesktopObservation,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ActionReceipt:
        normalized = self._normalize_app(action.app)
        if cancel_event is not None and cancel_event.is_set():
            raise WindowsUiaDriverError("desktop operation was cancelled")
        with self._lock:
            snapshot = self._snapshots.get(normalized)
            if (
                snapshot is None
                or snapshot.observation.generation != before.generation
                or action.generation != before.generation
                or before.app.casefold() != normalized
            ):
                raise WindowsUiaStaleObservation("desktop action used a stale observation")
            if normalized in self._pending_observation:
                raise WindowsUiaStaleObservation(
                    "a fresh observation is required after each action"
                )
            self._pending_observation.add(normalized)
        try:
            native = self._native_backend()
            native.assert_interactive_desktop()
            window = self._resolve_window(action.app)
            if window.hwnd != snapshot.hwnd:
                raise WindowsUiaStaleObservation("the selected application window changed")
            native.activate_window(snapshot.hwnd)
            native.assert_foreground(snapshot.hwnd)
            method = ""
            wrapper: Any | None = None
            expected_element: DesktopElement | None = None
            if action.element_index is not None:
                wrapper = snapshot.wrappers.get(action.element_index)
                if wrapper is None:
                    raise WindowsUiaStaleObservation("element index is not in this observation")
                expected_matches = [
                    element
                    for element in snapshot.observation.elements
                    if element.index == action.element_index
                ]
                if len(expected_matches) != 1:
                    raise WindowsUiaStaleObservation(
                        "element index has no unique structured observation target"
                    )
                expected_element = expected_matches[0]
                self._assert_element_usable(wrapper)

            if action.type == DesktopActionType.CLICK:
                if wrapper is None:
                    raise WindowsUiaDriverError("coordinate clicks are disabled")
                if action.mouse_button not in {None, "left"} or action.click_count not in {None, 1}:
                    raise WindowsUiaDriverError("only one semantic left click is enabled")
                assert expected_element is not None
                self._assert_element_still_bound(wrapper, expected_element)
                method = self._invoke(
                    wrapper,
                    foreground_guard=lambda: native.assert_foreground(snapshot.hwnd),
                )
            elif action.type == DesktopActionType.PERFORM_SECONDARY_ACTION:
                assert wrapper is not None and action.action_name is not None
                assert expected_element is not None
                self._assert_element_still_bound(wrapper, expected_element)
                method = self._secondary(wrapper, action.action_name)
            elif action.type == DesktopActionType.TYPE_TEXT:
                if wrapper is None or _safe_call(wrapper, "has_keyboard_focus", None) is not True:
                    raise WindowsUiaDriverError(
                        "type_text target is not the observed focused element"
                    )
                focused = [
                    item
                    for item in snapshot.wrappers.values()
                    if _safe_call(item, "has_keyboard_focus", None) is True
                ]
                if len(focused) != 1 or focused[0] is not wrapper:
                    raise WindowsUiaDriverError("exactly one observed element must have focus")
                self._assert_element_usable(wrapper)
                assert expected_element is not None
                self._assert_element_still_bound(
                    wrapper,
                    expected_element,
                    require_focus=True,
                )
                assert action.text is not None
                native.assert_foreground(snapshot.hwnd)
                native.send_text(action.text)
                method = "Unicode SendInput"
            elif action.type == DesktopActionType.SET_VALUE:
                assert wrapper is not None and action.value is not None
                assert expected_element is not None
                self._assert_element_still_bound(wrapper, expected_element)
                method = self._set_value(wrapper, action.value)
            elif action.type == DesktopActionType.PRESS_KEY:
                if wrapper is None or _safe_call(wrapper, "has_keyboard_focus", None) is not True:
                    raise WindowsUiaDriverError(
                        "press_key target is not the observed focused element"
                    )
                if not is_allowed_desktop_key(action.key):
                    raise WindowsUiaDriverError(
                        "press_key is outside the navigation-only allow-list"
                    )
                assert action.key is not None
                assert expected_element is not None
                self._assert_element_still_bound(
                    wrapper,
                    expected_element,
                    require_focus=True,
                )
                native.assert_foreground(snapshot.hwnd)
                native.send_hotkey(action.key)
                method = "allow-listed SendInput hotkey"
            elif action.type == DesktopActionType.SCROLL:
                assert wrapper is not None and action.direction is not None
                assert expected_element is not None
                self._assert_element_still_bound(wrapper, expected_element)
                method_fn = _safe_attr(wrapper, "scroll", None)
                if not callable(method_fn):
                    raise WindowsUiaDriverError("target element does not expose UIA scrolling")
                method_fn(action.direction, int(action.pages or 1))
                method = "UIA scroll"
            elif action.type == DesktopActionType.DRAG:
                raise WindowsUiaDriverError("coordinate drag is disabled")
            else:  # pragma: no cover - enum exhaustiveness guard
                raise WindowsUiaDriverError("unsupported desktop action")
            native.assert_foreground(snapshot.hwnd)
        except Exception:
            with self._lock:
                self._snapshots.pop(normalized, None)
            raise
        return ActionReceipt(
            action=action,
            accepted=True,
            before_generation=before.generation,
            driver_message=f"Windows UIA accepted one atomic action via {method}",
        )

    def cancel(self) -> bool:
        return False

    def close(self) -> None:
        with self._lock:
            self._snapshots.clear()
            self._pending_observation.clear()
