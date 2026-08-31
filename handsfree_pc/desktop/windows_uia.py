from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from ..config import AppProfile
from ..windows.native import AmbiguousWindowError, NativeWindows, WindowInfo, WindowNotFoundError
from ..windows.uia import PasswordFieldError, UIABackend, UIAError, UIAUnavailableError
from .protocol import (
    ActionReceipt,
    BoundedUiText,
    CredentialConfidence,
    DesktopAction,
    DesktopActionType,
    DesktopElement,
    DesktopObservation,
    ElementPlane,
    credential_findings,
    element_plane,
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


@dataclass(slots=True)
class _ElementCandidate:
    source_index: int
    wrapper: Any
    element: DesktopElement
    actionable: bool
    addressable: bool
    content_plane: bool
    private_digest: str
    summarized_value: bool
    task_relevant: bool


_ACTIONABLE_CONTROL_TYPES = frozenset(
    {
        "button",
        "checkbox",
        "combobox",
        "dataitem",
        "edit",
        "hyperlink",
        "listitem",
        "menuitem",
        "radiobutton",
        "scrollbar",
        "slider",
        "spinner",
        "splitbutton",
        "tabitem",
        "thumb",
        "treeitem",
    }
)
_INTERESTING_CONTROL_TYPES = _ACTIONABLE_CONTROL_TYPES | {
    "dialog",
    "document",
    "group",
    "pane",
    "text",
    "window",
}
_DIALOG_CONTROL_TYPES = frozenset({"dialog", "window"})
_SEMANTIC_CONTENT_CONTROL_TYPES = frozenset(
    {"text", "document", "pane", "group", "image", "dataitem"}
)
_SECRET_LABELED_BY_RE = re.compile(
    r"(?ix)^\s*(?:"
    r"password|passcode|api[ _-]?key|secret(?:\s+key)?|access[ _-]?token|"
    r"verification[ _-]?code|security[ _-]?code|otp|2fa|mfa|pin|"
    r"密码|口令|密钥|访问令牌|验证码|安全码|动态码"
    r")\s*[:：]?\s*$"
)
_SEARCH_IDENTITY_RE = re.compile(r"(?i)\b(?:search|find|filter)\b|搜索|查找|筛选")
_COMPOSER_IDENTITY_RE = re.compile(
    r"(?i)\b(?:prompt|message|composer|chat[-_ ]?input|reply|ask[-_ ]?claude)\b"
    r"|输入|消息|提问|回复"
)


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


def _normalized_text(value: Any) -> str:
    return "" if value is None else str(value).replace("\x00", "")


def _text(value: Any, maximum: int = 1000, *, field: str = "UIA text") -> str:
    normalized = _normalized_text(value)
    if len(normalized) > maximum:
        raise WindowsUiaDriverError(
            f"{field} exceeds the local inspection limit; refusing a truncated observation"
        )
    return normalized


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()


def _local_identity_digest(value: Any) -> str | None:
    """Hash a UIA RuntimeId without exposing the process-local identifier."""

    if value is None:
        return None
    try:
        if isinstance(value, str | bytes | bytearray):
            payload = repr(value)
        else:
            payload = json.dumps(list(value), separators=(",", ":"))
    except (TypeError, ValueError):
        return None
    return hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()


def _bounded_text_summary(value: str, *, field: str) -> str:
    return f"<{field} omitted chars={len(value)} sha256={_text_digest(value)}>"


def _bounded_text(value: str, *, maximum: int) -> BoundedUiText:
    return BoundedUiText.from_text(value, maximum=maximum)


def _coerce_boolish(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return None


def _metadata_digest(
    *,
    name: str,
    control_type: str,
    automation_id: str | None,
    value: str | None,
    selected: bool | None,
    focused: bool | None,
    password: bool,
    enabled: bool,
) -> str:
    payload = json.dumps(
        {
            "automation_id": automation_id,
            "control_type": control_type,
            "enabled": enabled,
            "focused": focused,
            "name": name,
            "password": password,
            "selected": selected,
            "value": value,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()


def _bounded_surface_digest(entries: list[tuple[int, str, str]]) -> str | None:
    if not entries:
        return None
    digest = hashlib.sha256()
    for source_index, reason, private_digest in sorted(entries):
        digest.update(f"{source_index}:{reason}:{private_digest}\n".encode())
    return digest.hexdigest()


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
        self._task_context = ""
        self._lock = threading.RLock()

    def set_task_context(self, task: str | None) -> None:
        """Set a local-only hint used solely when a large UI tree is bounded."""

        if task is not None and (not isinstance(task, str) or len(task) > 16000):
            raise ValueError("desktop task context must be a bounded string or null")
        with self._lock:
            self._task_context = " ".join((task or "").casefold().split())

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
    def _raw_name(element: Any) -> str:
        value = _safe_call(element, "window_text", "")
        if value:
            return _normalized_text(value)
        return _normalized_text(getattr(getattr(element, "element_info", None), "name", ""))

    @staticmethod
    def _name(element: Any) -> str:
        return _text(
            WindowsUiaDriver._raw_name(element),
            1024,
            field="UIA element name",
        )

    @staticmethod
    def _raw_control_type(element: Any) -> str:
        info = getattr(element, "element_info", None)
        value = getattr(info, "control_type", None)
        return _normalized_text(value or _safe_call(element, "friendly_class_name", ""))

    @staticmethod
    def _control_type(element: Any) -> str:
        return _text(
            WindowsUiaDriver._raw_control_type(element),
            120,
            field="UIA control type",
        )

    @staticmethod
    def _raw_automation_id(element: Any) -> str | None:
        value = getattr(getattr(element, "element_info", None), "automation_id", None)
        return _normalized_text(value) if value else None

    @staticmethod
    def _automation_id(element: Any) -> str | None:
        value = WindowsUiaDriver._raw_automation_id(element)
        return _text(value, 300, field="UIA automation id") if value else None

    @staticmethod
    def _local_identity(element: Any) -> str | None:
        info = _safe_attr(element, "element_info", None)
        return _local_identity_digest(_safe_attr(info, "runtime_id", None))

    @staticmethod
    def _raw_value_state(element: Any, *, password: bool) -> tuple[bool, str | None]:
        if password:
            return False, None
        missing = object()
        member = _safe_attr(element, "get_value", missing)
        if member is not missing:
            try:
                value = member() if callable(member) else member
                if value is not None:
                    return True, _normalized_text(value) if value != "" else None
            except Exception:
                pass
        interface = _safe_attr(element, "iface_value", None)
        value = _safe_attr(interface, "CurrentValue", missing)
        if value is not missing and value is not None:
            return True, _normalized_text(value) if value != "" else None
        properties = _safe_call(element, "legacy_properties", None)
        if isinstance(properties, dict):
            for key in ("Value", "value"):
                if key in properties:
                    value = properties[key]
                    if value is not None:
                        return True, _normalized_text(value) if value != "" else None
        return False, None

    @staticmethod
    def _raw_value(element: Any, *, password: bool) -> str | None:
        return WindowsUiaDriver._raw_value_state(element, password=password)[1]

    @staticmethod
    def _value(element: Any, *, password: bool) -> str | None:
        value = WindowsUiaDriver._raw_value(element, password=password)
        if value is None:
            return None
        if len(value) > 4000:
            return _bounded_text_summary(value, field="uia-value")
        return value

    @staticmethod
    def _is_interesting(name: str, control_type: str) -> bool:
        return bool(name or control_type.casefold() in _INTERESTING_CONTROL_TYPES)

    @staticmethod
    def _is_actionable(control_type: str, *, enabled: bool) -> bool:
        return enabled and control_type.casefold() in _ACTIONABLE_CONTROL_TYPES

    @staticmethod
    def _profile_control_types(profile: AppProfile) -> frozenset[str]:
        if profile.include_control_types:
            return frozenset(item.strip().casefold() for item in profile.include_control_types)
        return _INTERESTING_CONTROL_TYPES

    @staticmethod
    def _profile_content_types(profile: AppProfile) -> frozenset[str]:
        return frozenset(item.strip().casefold() for item in profile.content_control_types)

    @staticmethod
    def _is_composer(
        profile: AppProfile,
        *,
        name: str,
        control_type: str,
        focused: bool | None,
        enabled: bool,
        editable: bool | None,
        automation_id: str | None,
        aria_properties: str,
        aria_role: str,
    ) -> bool:
        if not enabled or editable is not True:
            return False
        normalized_name = name.strip().casefold()
        identity = " ".join((normalized_name, (automation_id or "").casefold())).strip()
        if _SEARCH_IDENTITY_RE.search(identity):
            return False
        configured_names = {
            item.strip().casefold() for item in profile.composer_names if item.strip()
        }
        if normalized_name and normalized_name in configured_names:
            return True
        if identity and _COMPOSER_IDENTITY_RE.search(identity):
            return True
        aria = aria_properties.casefold()
        positive_textbox_role = aria_role.strip().casefold() == "textbox"
        positive_multiline = "multiline=true" in aria or "contenteditable=true" in aria
        # Chromium composers may be unnamed Edit/Document nodes. They are
        # promoted only with positive editable-textbox structure; focus and a
        # process-local RuntimeId alone are deliberately insufficient.
        return bool(
            focused is True
            and control_type.strip().casefold() in {"edit", "document"}
            and positive_textbox_role
            and positive_multiline
        )

    @staticmethod
    def _task_mentions_label(task: str, name: str) -> bool:
        label = " ".join(name.casefold().split())
        if not task or len(label) < 2 or len(label) > 500:
            return False
        return label in task

    @staticmethod
    def _runtime_editable_state(
        wrapper: Any,
        *,
        control_type: str,
        enabled: bool,
    ) -> bool | None:
        normalized_type = control_type.strip().casefold()
        if normalized_type not in {"edit", "document", "spinner", "combobox"}:
            return None
        if not enabled:
            return False

        missing = object()

        def member(owner: Any, name: str, *, invoke: bool = False) -> Any:
            if owner is None:
                return missing
            try:
                value = getattr(owner, name)
            except AttributeError:
                return missing
            except Exception as exc:
                raise WindowsUiaDriverError(
                    "could not revalidate the target editability property"
                ) from exc
            if invoke and callable(value):
                try:
                    return value()
                except Exception as exc:
                    raise WindowsUiaDriverError(
                        "could not revalidate the target editability property"
                    ) from exc
            return value

        info = member(wrapper, "element_info")
        raw_element = member(info, "element")
        aria_properties = member(raw_element, "CurrentAriaProperties")
        aria_role = member(raw_element, "CurrentAriaRole")
        explicit_editable: bool | None = None
        explicit_read_only: bool | None = None
        if isinstance(aria_properties, str):
            for item in aria_properties.split(";"):
                key, separator, raw = item.partition("=")
                if not separator:
                    continue
                normalized_key = key.strip().casefold()
                value = _coerce_boolish(raw)
                if normalized_key == "contenteditable" and value is not None:
                    explicit_editable = value
                elif normalized_key in {"readonly", "read-only"} and value is not None:
                    explicit_read_only = value
        candidates = [member(wrapper, "is_read_only", invoke=True)]
        interface = member(wrapper, "iface_value")
        candidates.append(member(interface, "CurrentIsReadOnly"))
        candidates.append(member(raw_element, "CurrentIsReadOnly"))
        legacy = member(wrapper, "legacy_properties", invoke=True)
        if isinstance(legacy, dict):
            candidates.append(legacy.get("IsReadOnly", legacy.get("ReadOnly")))
        for candidate in candidates:
            if candidate is missing:
                continue
            coerced = _coerce_boolish(candidate)
            if coerced is not None:
                explicit_read_only = coerced
                break
        if explicit_read_only is True or explicit_editable is False:
            return False
        if explicit_read_only is False or explicit_editable is True:
            return True
        if normalized_type == "document":
            return isinstance(aria_role, str) and aria_role.strip().casefold() == "textbox"
        return True

    @staticmethod
    def _runtime_secret_labeled_state(wrapper: Any) -> bool:
        try:
            info = getattr(wrapper, "element_info", None)
            raw_element = getattr(info, "element", None)
            labeled_by = getattr(raw_element, "CurrentLabeledBy", None)
            if labeled_by is None:
                labeled_by = getattr(info, "labeled_by", None)
            if labeled_by is None:
                return False
            try:
                if not bool(labeled_by):
                    # comtypes represents an absent UIA element relation as a
                    # false NULL pointer rather than Python ``None``.
                    return False
            except Exception:
                pass
            label_name = (
                getattr(labeled_by, "CurrentName", "")
                or getattr(labeled_by, "name", "")
            )
            if not label_name:
                window_text = getattr(labeled_by, "window_text", None)
                if callable(window_text):
                    label_name = window_text()
            if not label_name:
                get_property = getattr(labeled_by, "GetCurrentPropertyValue", None)
                if callable(get_property):
                    label_name = get_property(30005)
        except Exception as exc:
            raise WindowsUiaDriverError(
                "could not revalidate the target labeled-by relation"
            ) from exc
        normalized = _normalized_text(label_name)
        return bool(
            len(normalized) <= 120
            and _SECRET_LABELED_BY_RE.fullmatch(normalized)
        )

    @staticmethod
    def _runtime_composer_state(wrapper: Any, expected: DesktopElement) -> bool:
        if not expected.composer:
            return False
        # A stable named/profile-matched composer remains semantically bound by
        # the exact name/automation-id checks performed alongside this method.
        if expected.name or expected.automation_id:
            return True
        try:
            info = getattr(wrapper, "element_info", None)
            raw_element = getattr(info, "element", None)
            aria = _normalized_text(
                getattr(raw_element, "CurrentAriaProperties", "")
            ).casefold()
            role = _normalized_text(
                getattr(raw_element, "CurrentAriaRole", "")
            ).casefold()
        except Exception as exc:
            raise WindowsUiaDriverError(
                "could not revalidate the target composer semantics"
            ) from exc
        return role == "textbox" and (
            "multiline=true" in aria or "contenteditable=true" in aria
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
        value_observed, value = self._raw_value_state(wrapper, password=password)
        return DesktopElement(
            index=index,
            name=name,
            control_type=control_type,
            automation_id=self._automation_id(wrapper),
            value=(
                _bounded_text(value, maximum=4000).display
                if value is not None
                else None
            ),
            value_observed=value_observed,
            selected=selected,
            focused=_safe_call(wrapper, "has_keyboard_focus", None),
            password=password,
            enabled=_safe_call(wrapper, "is_enabled", True) is not False,
            local_identity=self._local_identity(wrapper),
        )

    @staticmethod
    def _enumerate_descendants(
        root: Any,
    ) -> tuple[list[tuple[int, Any]], int, list[tuple[int, str, str]]]:
        """Wrap UIA descendants one at a time so one transient node is bounded.

        ``pywinauto`` normally wraps every raw element in one list
        comprehension. Electron can briefly expose a node whose control type
        is ``None`` while switching views; that single node used to discard the
        otherwise valid observation. Raw enumeration lets us omit only the
        malformed node while retaining a content-free digest in diagnostics.
        Test doubles without pywinauto internals continue through the public
        ``descendants`` method.
        """

        try:
            root_info = root.element_info
            backend = root.backend
            raw_descendants = root_info.descendants
            wrapper_factory = backend.generic_wrapper_class
        except (AttributeError, TypeError):
            try:
                wrappers = list(root.descendants())
            except Exception as exc:
                raise WindowsUiaDriverError(
                    "could not enumerate UIA descendants"
                ) from exc
            return list(enumerate(wrappers)), len(wrappers), []

        if not callable(raw_descendants) or not callable(wrapper_factory):
            try:
                wrappers = list(root.descendants())
            except Exception as exc:
                raise WindowsUiaDriverError(
                    "could not enumerate UIA descendants"
                ) from exc
            return list(enumerate(wrappers)), len(wrappers), []

        try:
            raw_elements = list(raw_descendants())
        except Exception as exc:
            raise WindowsUiaDriverError("could not enumerate UIA descendants") from exc

        wrapped: list[tuple[int, Any]] = []
        omissions: list[tuple[int, str, str]] = []

        def safe_raw_attr(owner: Any, name: str) -> Any:
            try:
                return getattr(owner, name, None)
            except Exception:
                return None

        for source_index, raw_info in enumerate(raw_elements):
            try:
                wrapped.append((source_index, wrapper_factory(raw_info)))
            except Exception:
                omissions.append(
                    (
                        source_index,
                        "unwrappable-descendant",
                        _metadata_digest(
                            name=_normalized_text(safe_raw_attr(raw_info, "name")),
                            control_type=_normalized_text(
                                safe_raw_attr(raw_info, "control_type")
                            ),
                            automation_id=(
                                _normalized_text(
                                    safe_raw_attr(raw_info, "automation_id")
                                )
                                or None
                            ),
                            value=None,
                            selected=None,
                            focused=None,
                            password=True,
                            enabled=False,
                        ),
                    )
                )
        return wrapped, len(raw_elements), omissions

    def _elements(
        self,
        root: Any,
        profile: AppProfile,
    ) -> tuple[tuple[DesktopElement, ...], dict[str, Any], dict[str, Any]]:
        descendants, total_descendants, enumeration_omissions = (
            self._enumerate_descendants(root)
        )

        candidates: list[_ElementCandidate] = []
        bounded_entries: list[tuple[int, str, str]] = list(enumeration_omissions)
        stats: dict[str, Any] = {
            "total": total_descendants,
            "kept": 0,
            "skipped": total_descendants,
            "property_errors": len(enumeration_omissions),
            "descendants": total_descendants,
            "visible": 0,
            "interesting": 0,
            "retained": 0,
            "retained_focused": 0,
            "retained_selected": 0,
            "retained_actionable": 0,
            "omitted": 0,
            "omitted_by_max_elements": 0,
            "omitted_oversized_name": 0,
            "summarized_oversized_name": 0,
            "omitted_password_property_error": 0,
            "omitted_oversized_identity": 0,
            "omitted_invalid_metadata": len(enumeration_omissions),
            "omitted_unwrappable_descendant": len(enumeration_omissions),
            "omitted_visibility_property_error": 0,
            "omitted_enabled_property_error": 0,
            "omitted_by_profile": 0,
            "omitted_by_content_limit": 0,
            "skipped_long_content": 0,
            "summarized_long_content": 0,
            "summarized_long_values": 0,
            "bounded_surface_items": 0,
            "bounded_surface_sha256": None,
            "high_credential_count": 0,
            "low_credential_count": 0,
            "credential_affected_element_count": 0,
        }

        missing = object()

        def counted_attr(
            owner: Any,
            name: str,
            default: Any = None,
            *,
            count_error: bool = True,
        ) -> Any:
            try:
                return getattr(owner, name, default)
            except Exception:
                if count_error:
                    stats["property_errors"] += 1
                return default

        def counted_call(
            owner: Any,
            name: str,
            default: Any = None,
            *,
            count_error: bool = True,
        ) -> Any:
            member = counted_attr(owner, name, missing, count_error=count_error)
            if member is missing:
                return default
            if not callable(member):
                return member
            try:
                return member()
            except Exception:
                if count_error:
                    stats["property_errors"] += 1
                return default

        def required_bool_call(owner: Any, name: str) -> tuple[bool, bool]:
            """Return (value, errored) for action-critical UIA state."""

            try:
                member = getattr(owner, name)
                value = member() if callable(member) else member
            except Exception:
                stats["property_errors"] += 1
                return False, True
            if value is True:
                return True, False
            if value is False:
                return False, False
            stats["property_errors"] += 1
            return False, True

        def wrapper_omission_digest(wrapper: Any) -> str:
            try:
                name = _normalized_text(wrapper.window_text())
            except Exception:
                name = ""
            try:
                info = wrapper.element_info
            except Exception:
                info = None

            def safe_info_attr(attribute: str) -> Any:
                try:
                    return getattr(info, attribute, None)
                except Exception:
                    return None

            return _metadata_digest(
                name=name or _normalized_text(safe_info_attr("name")),
                control_type=_normalized_text(safe_info_attr("control_type")),
                automation_id=(
                    _normalized_text(safe_info_attr("automation_id")) or None
                ),
                value=None,
                selected=None,
                focused=None,
                password=True,
                enabled=False,
            )

        def editable_state(
            wrapper: Any,
            info: Any,
            *,
            control_type: str,
            enabled: bool,
        ) -> bool | None:
            normalized_type = control_type.strip().casefold()
            if normalized_type not in {"edit", "document", "spinner", "combobox"}:
                return None
            if not enabled:
                return False

            explicit_editable: bool | None = None
            explicit_read_only: bool | None = None
            raw_element = counted_attr(info, "element", None, count_error=False)
            aria_properties = counted_attr(
                raw_element,
                "CurrentAriaProperties",
                "",
                count_error=False,
            )
            aria_role = _normalized_text(
                counted_attr(raw_element, "CurrentAriaRole", "", count_error=False)
            ).casefold()
            if isinstance(aria_properties, str):
                for item in aria_properties.split(";"):
                    key, separator, raw = item.partition("=")
                    if not separator:
                        continue
                    normalized_key = key.strip().casefold()
                    value = _coerce_boolish(raw)
                    if normalized_key == "contenteditable" and value is not None:
                        explicit_editable = value
                    elif normalized_key in {"readonly", "read-only"} and value is not None:
                        explicit_read_only = value

            wrapper_read_only = counted_call(
                wrapper,
                "is_read_only",
                None,
                count_error=False,
            )
            interface = counted_attr(wrapper, "iface_value", None, count_error=False)
            interface_read_only = counted_attr(
                interface,
                "CurrentIsReadOnly",
                None,
                count_error=False,
            )
            raw_read_only = counted_attr(
                raw_element,
                "CurrentIsReadOnly",
                None,
                count_error=False,
            )
            properties = counted_call(
                wrapper,
                "legacy_properties",
                None,
                count_error=False,
            )
            legacy_read_only = None
            if isinstance(properties, dict):
                legacy_read_only = properties.get("IsReadOnly", properties.get("ReadOnly"))
            for candidate in (
                wrapper_read_only,
                interface_read_only,
                raw_read_only,
                legacy_read_only,
            ):
                coerced = _coerce_boolish(candidate)
                if coerced is not None:
                    explicit_read_only = coerced
                    break

            if explicit_read_only is True or explicit_editable is False:
                return False
            if explicit_read_only is False or explicit_editable is True:
                return True
            if normalized_type == "document":
                # Focus alone does not prove writability. Chromium commonly
                # exposes contenteditable composers with role=textbox.
                return aria_role == "textbox"
            # Edit/Spinner/ComboBox controls are writable unless UIA positively
            # reports read-only state.
            return True

        def secret_labeled_state(info: Any) -> bool:
            raw_element = counted_attr(info, "element", None, count_error=False)
            labeled_by = counted_attr(
                raw_element,
                "CurrentLabeledBy",
                None,
                count_error=False,
            )
            if labeled_by is None:
                labeled_by = counted_attr(info, "labeled_by", None, count_error=False)
            if labeled_by is None:
                return False
            try:
                if not bool(labeled_by):
                    return False
            except Exception:
                pass
            label_name = _normalized_text(
                counted_attr(labeled_by, "CurrentName", "")
                or counted_attr(labeled_by, "name", "")
                or counted_call(labeled_by, "window_text", "")
            )
            if not label_name:
                get_property = counted_attr(
                    labeled_by,
                    "GetCurrentPropertyValue",
                    None,
                )
                if callable(get_property):
                    try:
                        label_name = _normalized_text(get_property(30005))
                    except Exception:
                        stats["property_errors"] += 1
                        return False
            return bool(
                len(label_name) <= 120
                and _SECRET_LABELED_BY_RE.fullmatch(label_name)
            )

        include_types = self._profile_control_types(profile)
        content_types = self._profile_content_types(profile)
        profile_enabled = bool(
            profile.include_control_types
            or profile.content_control_types
            or profile.composer_names
        )
        retained_content_nodes = 0
        with self._lock:
            task_context = self._task_context

        for source_index, wrapper in descendants:
            visible, visibility_error = required_bool_call(wrapper, "is_visible")
            if visibility_error:
                stats["omitted_visibility_property_error"] += 1
                bounded_entries.append(
                    (
                        source_index,
                        "visibility-property-error",
                        wrapper_omission_digest(wrapper),
                    )
                )
                continue
            if not visible:
                continue
            stats["visible"] += 1

            name = _normalized_text(counted_call(wrapper, "window_text", ""))
            info = counted_attr(wrapper, "element_info", None)
            if not name:
                name = _normalized_text(counted_attr(info, "name", ""))
            control_type = _normalized_text(counted_attr(info, "control_type", None))
            if not control_type:
                control_type = _normalized_text(
                    counted_call(wrapper, "friendly_class_name", "")
                )
            try:
                password = UIABackend._is_password(wrapper)
            except Exception:
                stats["property_errors"] += 1
                stats["omitted_password_property_error"] += 1
                bounded_entries.append(
                    (
                        source_index,
                        "password-property-error",
                        _metadata_digest(
                            name=name,
                            control_type=control_type,
                            automation_id=None,
                            value=None,
                            selected=None,
                            focused=None,
                            password=True,
                            enabled=False,
                        ),
                    )
                )
                # Unknown password state is fail-closed for this element, not
                # for every unrelated control in the containing window.
                continue
            try:
                selected = UIABackend._selection_state(wrapper)
            except Exception:
                stats["property_errors"] += 1
                selected = None
            focused = counted_call(wrapper, "has_keyboard_focus", None)
            enabled, enabled_error = required_bool_call(wrapper, "is_enabled")
            if enabled_error:
                stats["omitted_enabled_property_error"] += 1
                bounded_entries.append(
                    (
                        source_index,
                        "enabled-property-error",
                        wrapper_omission_digest(wrapper),
                    )
                )
                continue
            editable = editable_state(
                wrapper,
                info,
                control_type=control_type,
                enabled=enabled,
            )
            secret_labeled = secret_labeled_state(info)
            automation_id_value = counted_attr(info, "automation_id", None)
            automation_id = (
                _normalized_text(automation_id_value) if automation_id_value else None
            )
            local_identity = _local_identity_digest(
                counted_attr(info, "runtime_id", None, count_error=False)
            )
            # ValuePattern is optional. Preserve the distinction between a
            # positively observed empty value and an unreadable value surface.
            value_observed, value = self._raw_value_state(
                wrapper,
                password=password,
            )

            private_digest = _metadata_digest(
                name=name,
                control_type=control_type,
                automation_id=automation_id,
                value=value,
                selected=selected,
                focused=focused,
                password=password,
                enabled=enabled,
            )
            raw_findings = (
                *credential_findings(
                    name,
                    field="name",
                    element_index=str(source_index),
                ),
                *credential_findings(
                    value,
                    field="value",
                    element_index=str(source_index),
                ),
            )
            high_credential = any(
                finding.confidence == CredentialConfidence.HIGH
                for finding in raw_findings
            )
            low_credential = any(
                finding.confidence == CredentialConfidence.LOW
                for finding in raw_findings
            )
            stats["high_credential_count"] += sum(
                finding.confidence == CredentialConfidence.HIGH
                for finding in raw_findings
            )
            stats["low_credential_count"] += sum(
                finding.confidence == CredentialConfidence.LOW
                for finding in raw_findings
            )
            stats["credential_affected_element_count"] += int(
                high_credential or low_credential
            )

            normalized_control_type = control_type.casefold()
            content_plane = normalized_control_type in content_types
            raw_element = counted_attr(info, "element", None, count_error=False)
            aria_properties = _normalized_text(
                counted_attr(raw_element, "CurrentAriaProperties", "")
            )
            aria_role = _normalized_text(
                counted_attr(raw_element, "CurrentAriaRole", "")
            )
            dialog_plane = normalized_control_type in _DIALOG_CONTROL_TYPES or (
                aria_role.casefold() in {"dialog", "alertdialog"}
            )
            if dialog_plane:
                # Chromium commonly exposes modal containers as Group/Pane
                # with an ARIA dialog role. They are structural safety context,
                # never chat content or an executable target.
                content_plane = False
            composer = self._is_composer(
                profile,
                name=name,
                control_type=control_type,
                focused=focused,
                enabled=enabled,
                editable=editable,
                automation_id=automation_id,
                aria_properties=aria_properties,
                aria_role=aria_role,
            )
            if composer and content_plane:
                content_plane = False

            if profile_enabled:
                # Modal/window structure is always retained for local safety,
                # even when an app profile intentionally narrows its planner-
                # addressable controls. It never becomes an executable target.
                included = normalized_control_type in include_types or dialog_plane
                if not included and not content_plane and not composer:
                    stats["omitted_by_profile"] += 1
                    bounded_entries.append((source_index, "profile-filter", private_digest))
                    continue
            elif not self._is_interesting(name, control_type):
                continue
            stats["interesting"] += 1

            if content_plane and retained_content_nodes >= profile.max_content_nodes:
                stats["omitted_by_content_limit"] += 1
                bounded_entries.append((source_index, "content-limit", private_digest))
                continue

            control_name_limit = profile.max_control_name_chars if profile_enabled else 500
            if (
                not control_type
                or len(control_type) > 120
                or (automation_id is not None and len(automation_id) > 300)
            ):
                stats["omitted_oversized_identity"] += 1
                bounded_entries.append((source_index, "oversized-identity", private_digest))
                continue

            name_limit = (
                min(profile.max_content_chars, 1024)
                if content_plane
                else control_name_limit
            )
            name_metadata = _bounded_text(name, maximum=name_limit)
            value_limit = profile.max_content_chars if content_plane else 4000
            value_metadata = (
                _bounded_text(value, maximum=value_limit) if value is not None else None
            )
            bounded_name = name_metadata.display
            bounded_value = value_metadata.display if value_metadata is not None else None
            name_truncated = name_metadata.truncated
            summarized_value = False
            if content_plane:
                long_name = name_truncated
                long_value = bool(value_metadata and value_metadata.truncated)
                if long_name or long_value:
                    if profile.drop_long_content:
                        stats["skipped_long_content"] += 1
                        bounded_entries.append((source_index, "long-content", private_digest))
                        continue
                    if long_value:
                        stats["summarized_long_values"] += 1
                    summarized_value = long_name or long_value
                    stats["summarized_long_content"] += 1
            else:
                if name_truncated:
                    stats["omitted_oversized_name"] += 1
                    stats["summarized_oversized_name"] += 1
                    bounded_entries.append((source_index, "oversized-name", private_digest))
                summarized_value = bool(value_metadata and value_metadata.truncated)
                if summarized_value:
                    stats["summarized_long_values"] += 1

            semantic_content = (
                normalized_control_type in _SEMANTIC_CONTENT_CONTROL_TYPES and not composer
            )
            addressable = not dialog_plane and not semantic_content and not name_truncated
            if editable is False and normalized_control_type in {"edit", "document"}:
                addressable = False
            try:
                item = DesktopElement(
                    index=str(source_index),
                    name=bounded_name,
                    control_type=control_type,
                    automation_id=automation_id,
                    value=bounded_value,
                    value_observed=value_observed,
                    selected=selected,
                    focused=focused,
                    password=password,
                    enabled=enabled,
                    local_identity=local_identity,
                    plane=(
                        ElementPlane.INPUT
                        if composer
                        else ElementPlane.DIALOG
                        if dialog_plane
                        else None
                    ),
                    editable=editable,
                    addressable=addressable,
                    secret_labeled=secret_labeled,
                    composer=composer,
                    high_credential=high_credential,
                    low_credential=low_credential,
                    name_metadata=name_metadata,
                    value_metadata=value_metadata,
                )
            except ValueError:
                stats["omitted_invalid_metadata"] += 1
                bounded_entries.append((source_index, "invalid-metadata", private_digest))
                continue
            if content_plane:
                retained_content_nodes += 1
            candidates.append(
                _ElementCandidate(
                    source_index=source_index,
                    wrapper=wrapper,
                    element=item,
                    actionable=addressable
                    and (
                        composer
                        or self._is_actionable(control_type, enabled=enabled)
                    ),
                    addressable=addressable,
                    content_plane=content_plane,
                    private_digest=private_digest,
                    summarized_value=summarized_value,
                    task_relevant=self._task_mentions_label(task_context, name),
                )
            )

        # Electron navigation pills often expose only aria-current=page on the
        # active item, not SelectionItem/Toggle patterns. Once exactly one
        # configured app mode is positively current, the other configured mode
        # controls can safely be represented as unselected for planning and
        # post-action verification.
        configured_mode_names = {
            item.strip().casefold()
            for labels in profile.mode_names.values()
            for item in labels
            if item.strip()
        }
        mode_candidates = [
            candidate
            for candidate in candidates
            if candidate.addressable
            and candidate.element.name.strip().casefold() in configured_mode_names
        ]
        if sum(candidate.element.selected is True for candidate in mode_candidates) == 1:
            for candidate in mode_candidates:
                if candidate.element.selected is None:
                    candidate.element = replace(candidate.element, selected=False)

        def priority(candidate: _ElementCandidate) -> tuple[bool | int, ...]:
            normalized_type = candidate.element.control_type.strip().casefold()
            if normalized_type in {"button", "tabitem", "menuitem"}:
                type_rank = 0
            elif normalized_type in {"edit", "combobox", "spinner"}:
                type_rank = 1
            elif normalized_type in {"listitem", "treeitem"}:
                type_rank = 2
            else:
                type_rank = 3
            return (
                element_plane(candidate.element) != ElementPlane.DIALOG,
                candidate.element.password is not True,
                candidate.element.focused is not True,
                candidate.element.selected is not True,
                type_rank,
                not candidate.task_relevant,
                not candidate.actionable,
                candidate.source_index,
            )

        prioritized = sorted(candidates, key=priority)
        retained_candidates = prioritized[: self.max_elements]
        omitted_candidates = prioritized[self.max_elements :]
        for candidate in omitted_candidates:
            bounded_entries.append(
                (candidate.source_index, "max-elements", candidate.private_digest)
            )
        for candidate in retained_candidates:
            if candidate.summarized_value:
                bounded_entries.append(
                    (candidate.source_index, "summarized-value", candidate.private_digest)
                )

        retained_candidates.sort(key=lambda candidate: candidate.source_index)
        metadata = tuple(candidate.element for candidate in retained_candidates)
        wrappers = {
            candidate.element.index: candidate.wrapper
            for candidate in retained_candidates
            if candidate.addressable
        }
        stats["kept"] = len(retained_candidates)
        stats["skipped"] = stats["total"] - stats["kept"]
        stats["retained"] = len(retained_candidates)
        stats["retained_focused"] = sum(
            candidate.element.focused is True for candidate in retained_candidates
        )
        stats["retained_selected"] = sum(
            candidate.element.selected is True for candidate in retained_candidates
        )
        stats["retained_actionable"] = sum(
            candidate.actionable for candidate in retained_candidates
        )
        stats["omitted_by_max_elements"] = len(omitted_candidates)
        stats["omitted"] = stats["interesting"] - stats["retained"]
        stats["bounded_surface_items"] = len(bounded_entries)
        stats["bounded_surface_sha256"] = _bounded_surface_digest(bounded_entries)
        stats["total_element_count"] = stats["total"]
        stats["kept_control_count"] = sum(
            element_plane(candidate.element) in {ElementPlane.CONTROL, ElementPlane.INPUT}
            for candidate in retained_candidates
        )
        stats["skipped_content_count"] = (
            stats["skipped_long_content"] + stats["omitted_by_content_limit"]
        )
        stats["truncated_content_count"] = stats["summarized_long_content"]
        stats["property_error_count"] = stats["property_errors"]
        stats["elements_truncated"] = bool(stats["skipped"] or bounded_entries)
        return metadata, wrappers, stats

    @staticmethod
    def _accessibility_text(
        window: WindowInfo,
        elements: tuple[DesktopElement, ...],
        stats: Mapping[str, Any],
    ) -> str:
        header = json.dumps(
            {
                "uia_stats": dict(stats),
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
        profile = self._profile(app)
        native = self._native_backend()
        native.assert_interactive_desktop()
        window = self._resolve_window(app)
        root = self._root(window.hwnd)
        elements, wrappers, stats = self._elements(root, profile)
        with self._lock:
            self._generation += 1
            observation = DesktopObservation(
                app=normalized,
                generation=self._generation,
                accessibility_text=self._accessibility_text(window, elements, stats),
                window_title=window.title,
                elements=elements,
                local_window_id=f"hwnd:{window.hwnd}",
                total_element_count=int(stats["total_element_count"]),
                elements_truncated=bool(stats["elements_truncated"]),
                skipped_long_content_count=int(stats["skipped_content_count"]),
                property_error_count=int(stats["property_error_count"]),
                high_credential_count=int(stats["high_credential_count"]),
                low_credential_count=int(stats["low_credential_count"]),
                credential_affected_element_count=int(
                    stats["credential_affected_element_count"]
                ),
            )
            self._snapshots[normalized] = _Snapshot(window.hwnd, observation, wrappers)
            self._pending_observation.discard(normalized)
            return observation

    @staticmethod
    def _assert_element_usable(wrapper: Any, *, allow_password: bool = False) -> None:
        try:
            visible_member = wrapper.is_visible
            visible = visible_member() if callable(visible_member) else visible_member
        except Exception as exc:
            raise WindowsUiaStaleObservation(
                "target visibility could not be revalidated"
            ) from exc
        if visible is not True:
            raise WindowsUiaStaleObservation("target element is no longer visible")
        try:
            enabled_member = wrapper.is_enabled
            enabled = enabled_member() if callable(enabled_member) else enabled_member
        except Exception as exc:
            raise WindowsUiaStaleObservation(
                "target enabled state could not be revalidated"
            ) from exc
        if enabled is not True:
            raise WindowsUiaStaleObservation("target element is disabled")
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
        try:
            current_name = self._raw_name(wrapper)
            current_control_type = self._control_type(wrapper)
            current_automation_id = self._automation_id(wrapper)
            current_password = UIABackend._is_password(wrapper)
            try:
                enabled_member = wrapper.is_enabled
                current_enabled = (
                    enabled_member() if callable(enabled_member) else enabled_member
                )
            except Exception as exc:
                raise WindowsUiaStaleObservation(
                    "target enabled state could not be revalidated"
                ) from exc
            if current_enabled is not True:
                raise WindowsUiaStaleObservation("target element is disabled")
            current_value_observed, current_value = self._raw_value_state(
                wrapper,
                password=current_password,
            )
            current_selected = UIABackend._selection_state(wrapper)
            current_focused = _safe_call(wrapper, "has_keyboard_focus", None)
            current_local_identity = self._local_identity(wrapper)
            current_editable = self._runtime_editable_state(
                wrapper,
                control_type=current_control_type,
                enabled=current_enabled,
            )
            current_secret_labeled = self._runtime_secret_labeled_state(wrapper)
            current_composer = self._runtime_composer_state(wrapper, expected)
        except (UIAError, WindowsUiaDriverError):
            raise
        except Exception as exc:
            raise WindowsUiaStaleObservation(
                "target element identity could not be revalidated"
            ) from exc

        expected_name_digest = (
            expected.name_metadata.sha256
            if expected.name_metadata is not None
            else _text_digest(expected.name)
        )
        expected_name_length = (
            expected.name_metadata.original_length
            if expected.name_metadata is not None
            else len(expected.name)
        )
        expected_value_digest = (
            expected.value_metadata.sha256
            if expected.value_metadata is not None
            else _text_digest(expected.value)
            if expected.value is not None
            else None
        )
        expected_value_length = (
            expected.value_metadata.original_length
            if expected.value_metadata is not None
            else len(expected.value)
            if expected.value is not None
            else None
        )
        expected_state = (
            expected_name_digest,
            expected_name_length,
            expected.control_type,
            expected.automation_id,
            expected_value_digest,
            expected_value_length,
            expected.value_observed,
            expected.password,
            expected.enabled,
            expected.local_identity,
            expected.editable,
            expected.secret_labeled,
            expected.composer,
        )
        current_state = (
            _text_digest(current_name),
            len(current_name),
            current_control_type,
            current_automation_id,
            _text_digest(current_value) if current_value is not None else None,
            len(current_value) if current_value is not None else None,
            current_value_observed,
            current_password,
            current_enabled,
            current_local_identity,
            current_editable,
            current_secret_labeled,
            current_composer,
        )
        selection_changed = current_selected != expected.selected and not (
            expected.selected is False and current_selected is None
        )
        if current_state != expected_state or selection_changed or (
            require_focus and (expected.focused is not True or current_focused is not True)
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
    def _physical_focus(wrapper: Any, *, foreground_guard: Callable[[], None]) -> str:
        """Use only a foreground-guarded physical click as the focus primitive."""

        method = _safe_attr(wrapper, "click_input", None)
        if callable(method):
            foreground_guard()
            method(button="left", double=False)
            return "click_input_focus"
        raise WindowsUiaDriverError("target element has no safe focus primitive")

    @staticmethod
    def _secondary(
        wrapper: Any,
        action_name: str,
        *,
        foreground_guard: Callable[[], None],
    ) -> str:
        normalized = action_name.strip().casefold()
        if normalized == "clickfocus":
            return WindowsUiaDriver._physical_focus(
                wrapper,
                foreground_guard=foreground_guard,
            )
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

    def clear_app_doctor_draft(
        self,
        before: DesktopObservation,
        element: DesktopElement,
        *,
        expected_text: str,
    ) -> str:
        """Clear only the exact random draft created by ``app-doctor``.

        This diagnostic cleanup is intentionally outside the planner action
        vocabulary. It cannot clear arbitrary user text: both the fixed token
        shape and the freshly observed exact value must match.
        """

        if re.fullmatch(r"HandsFreePC-DRAFT-[0-9a-f]{10}-中文", expected_text) is None:
            raise WindowsUiaDriverError("refusing to clear text that is not an app-doctor draft")
        normalized = self._normalize_app(before.app)
        with self._lock:
            snapshot = self._snapshots.get(normalized)
            if (
                snapshot is None
                or snapshot.observation.generation != before.generation
                or before.app.casefold() != normalized
            ):
                raise WindowsUiaStaleObservation("draft cleanup used a stale observation")
            if normalized in self._pending_observation:
                raise WindowsUiaStaleObservation(
                    "a fresh observation is required before draft cleanup"
                )
            observed = [item for item in before.elements if item.index == element.index]
            if len(observed) != 1 or observed[0] != element:
                raise WindowsUiaStaleObservation("draft cleanup target is not uniquely bound")
            if (
                element.value != expected_text
                or not element.value_observed
                or element.focused is not True
                or element.password
                or not element.composer
                or not element.addressable
                or element_plane(element) != ElementPlane.INPUT
                or element.editable is False
            ):
                raise WindowsUiaDriverError(
                    "draft cleanup requires the exact focused non-password app composer"
                )
            wrapper = snapshot.wrappers.get(element.index)
            if wrapper is None:
                raise WindowsUiaStaleObservation("draft cleanup target is not addressable")
            self._pending_observation.add(normalized)
        try:
            native = self._native_backend()
            native.assert_interactive_desktop()
            window = self._resolve_window(before.app)
            if window.hwnd != snapshot.hwnd:
                raise WindowsUiaStaleObservation("the draft composer window changed")
            native.activate_window(snapshot.hwnd)
            native.assert_foreground(snapshot.hwnd)
            self._assert_element_usable(wrapper)
            self._assert_element_still_bound(wrapper, element, require_focus=True)
            method = self._set_value(wrapper, "")
            native.assert_foreground(snapshot.hwnd)
            return method
        except Exception:
            with self._lock:
                self._snapshots.pop(normalized, None)
            raise

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
                if element_plane(expected_element) == ElementPlane.INPUT:
                    method = self._physical_focus(
                        wrapper,
                        foreground_guard=lambda: native.assert_foreground(snapshot.hwnd),
                    )
                else:
                    method = self._invoke(
                        wrapper,
                        foreground_guard=lambda: native.assert_foreground(snapshot.hwnd),
                    )
            elif action.type == DesktopActionType.PERFORM_SECONDARY_ACTION:
                assert wrapper is not None and action.action_name is not None
                assert expected_element is not None
                self._assert_element_still_bound(wrapper, expected_element)
                method = self._secondary(
                    wrapper,
                    action.action_name,
                    foreground_guard=lambda: native.assert_foreground(snapshot.hwnd),
                )
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
                if (
                    not expected_element.addressable
                    or element_plane(expected_element) != ElementPlane.INPUT
                    or expected_element.editable is False
                ):
                    raise WindowsUiaDriverError(
                        "type_text target is not a verified editable input"
                    )
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
                if (
                    not expected_element.addressable
                    or element_plane(expected_element) != ElementPlane.INPUT
                    or expected_element.editable is False
                ):
                    raise WindowsUiaDriverError(
                        "set_value target is not a verified editable input"
                    )
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
