from __future__ import annotations

import hashlib
import io
import json
import math
import re
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from ..config import AppProfile
from ..windows.native import (
    AmbiguousWindowError,
    NativeFocusSnapshot,
    NativeWindows,
    WindowInfo,
    WindowNotFoundError,
)
from ..windows.text import sanitize_windows_ui_text
from ..windows.uia import PasswordFieldError, UIABackend, UIAError, UIAUnavailableError
from .assistive.browser_identity import is_browser_process, is_verified_browser_address
from .protocol import (
    ActionReceipt,
    BoundedUiText,
    CredentialConfidence,
    DesktopAction,
    DesktopActionType,
    DesktopElement,
    DesktopElementAction,
    DesktopExpandCollapseState,
    DesktopObservation,
    DesktopScrollAxis,
    ElementPlane,
    credential_findings,
    element_plane,
    is_allowed_desktop_key,
)
from .visual_ocr import (
    SensitiveVisualSurfaceError,
    VisualOcrBlock,
    VisualOcrClient,
    VisualOcrError,
    VisualOcrResult,
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
    root: Any


@dataclass(frozen=True, slots=True)
class _VisualTargetBinding:
    """Private exact-window binding for one planner-visible visual region."""

    hwnd: int
    window_rect: tuple[int, int, int, int]
    text: str
    bbox: tuple[int, int, int, int]
    label: str
    frame_sha256: str
    target_sha256: str
    local_identity: str
    viewport: bool = False
    screenshot_png: bytes = field(default=b"", repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class _VisualTextClickBinding:
    """One exact visual point click awaiting native caret confirmation."""

    hwnd: int
    local_window_id: str
    window_rect: tuple[int, int, int, int]
    x: int
    y: int


@dataclass(frozen=True, slots=True)
class _VisualTextFocusBinding:
    """One single-use rendered input proven by Win32 focus/caret evidence."""

    click: _VisualTextClickBinding
    target_process_id: int
    target_thread_id: int
    focus_hwnd: int
    caret_hwnd: int


@dataclass(frozen=True, slots=True)
class _DynamicWindowBinding:
    """One inventory identifier pinned to one exact top-level window."""

    app_id: str
    window: WindowInfo
    profile: AppProfile
    display_name: str


@dataclass(frozen=True, slots=True)
class _PendingVisualChange:
    """One pre-action frame bound to the exact window that must replace it."""

    window: WindowInfo
    frame_sha256: str


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


def _is_missing_uia_pattern(exc: Exception) -> bool:
    """Recognize pywinauto's optional-pattern sentinel without importing it."""

    return (
        type(exc).__name__ == "NoPatternInterfaceError"
        and type(exc).__module__.startswith("pywinauto.")
    )


def _normalized_text(value: Any) -> str:
    return sanitize_windows_ui_text(value)


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


_EXPAND_COLLAPSE_STATES = {
    0: DesktopExpandCollapseState.COLLAPSED,
    1: DesktopExpandCollapseState.EXPANDED,
    2: DesktopExpandCollapseState.PARTIALLY_EXPANDED,
    3: DesktopExpandCollapseState.LEAF_NODE,
}


def _expand_collapse_state(value: Any) -> DesktopExpandCollapseState | None:
    if isinstance(value, bool):
        return None
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = None
    if numeric in _EXPAND_COLLAPSE_STATES:
        return _EXPAND_COLLAPSE_STATES[numeric]
    if isinstance(value, str):
        normalized = re.sub(r"[\s-]+", "_", value.strip().casefold())
        normalized = normalized.removeprefix("expandcollapsestate_")
        try:
            return DesktopExpandCollapseState(normalized)
        except ValueError:
            return None
    return None


def _uia_capability_metadata(
    wrapper: Any,
    *,
    read_attr: Callable[[Any, str, Any], Any] = _safe_attr,
    allow_activation: bool,
) -> tuple[
    tuple[DesktopElementAction, ...],
    DesktopExpandCollapseState | None,
    tuple[DesktopScrollAxis, ...] | None,
]:
    """Probe only positive UIA pattern interfaces; never infer from control type.

    Pattern properties on live Electron trees can raise while the UI mutates.
    ``read_attr`` is therefore injected by observation code so one bad pattern
    is counted and omitted without losing the rest of the element.
    """

    actions: set[DesktopElementAction] = set()

    def interface_action(
        interface_name: str,
        interface_method: str,
        wrapper_method: str,
        action: DesktopElementAction,
    ) -> bool:
        interface = read_attr(wrapper, interface_name, None)
        if interface is None:
            return False
        available = bool(
            callable(read_attr(interface, interface_method, None))
            and callable(read_attr(wrapper, wrapper_method, None))
        )
        if available:
            actions.add(action)
        return available

    if allow_activation:
        activation_available = False
        activation_available |= interface_action(
            "iface_invoke",
            "Invoke",
            "invoke",
            DesktopElementAction.INVOKE,
        )
        activation_available |= interface_action(
            "iface_selection_item",
            "Select",
            "select",
            DesktopElementAction.SELECT,
        )
        activation_available |= interface_action(
            "iface_toggle",
            "Toggle",
            "toggle",
            DesktopElementAction.TOGGLE,
        )
        if activation_available:
            actions.add(DesktopElementAction.CLICK)

    expand_state: DesktopExpandCollapseState | None = None
    expand_interface = read_attr(wrapper, "iface_expand_collapse", None)
    if expand_interface is not None:
        expand_state = _expand_collapse_state(
            read_attr(expand_interface, "CurrentExpandCollapseState", None)
        )
        can_expand = bool(
            callable(read_attr(expand_interface, "Expand", None))
            and callable(read_attr(wrapper, "expand", None))
        )
        can_collapse = bool(
            callable(read_attr(expand_interface, "Collapse", None))
            and callable(read_attr(wrapper, "collapse", None))
        )
        if can_expand and expand_state in {
            DesktopExpandCollapseState.COLLAPSED,
            DesktopExpandCollapseState.PARTIALLY_EXPANDED,
        }:
            actions.add(DesktopElementAction.EXPAND)
        if can_collapse and expand_state in {
            DesktopExpandCollapseState.EXPANDED,
            DesktopExpandCollapseState.PARTIALLY_EXPANDED,
        }:
            actions.add(DesktopElementAction.COLLAPSE)

    scroll_item_interface = read_attr(wrapper, "iface_scroll_item", None)
    if (
        scroll_item_interface is not None
        and callable(read_attr(scroll_item_interface, "ScrollIntoView", None))
        and callable(read_attr(wrapper, "scroll_into_view", None))
    ):
        actions.add(DesktopElementAction.SCROLL_INTO_VIEW)

    scroll_axes: tuple[DesktopScrollAxis, ...] | None = None
    scroll_interface = read_attr(wrapper, "iface_scroll", None)
    if scroll_interface is not None:
        axes: list[DesktopScrollAxis] = []
        if _coerce_boolish(
            read_attr(scroll_interface, "CurrentHorizontallyScrollable", None)
        ) is True:
            axes.append(DesktopScrollAxis.HORIZONTAL)
        if _coerce_boolish(
            read_attr(scroll_interface, "CurrentVerticallyScrollable", None)
        ) is True:
            axes.append(DesktopScrollAxis.VERTICAL)
        scroll_axes = tuple(axes)
        if (
            axes
            and callable(read_attr(scroll_interface, "Scroll", None))
            and callable(read_attr(wrapper, "scroll", None))
        ):
            actions.add(DesktopElementAction.SCROLL)

    return (
        tuple(sorted(actions, key=lambda item: item.value)),
        expand_state,
        scroll_axes,
    )


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
        discover_all_windows: bool = False,
        activate_on_observe: bool = False,
        capture_screenshots: bool = False,
        strict_visual_postcondition: bool = True,
        activate_before_execute: bool = True,
        same_window_browser_navigation_fast_path: bool = False,
        visual_screenshot_enabled: bool | None = None,
        automatic_visual_screenshots: bool = True,
        visual_ocr_client: VisualOcrClient | None = None,
        visual_ocr_apps: tuple[str, ...] = (),
        visual_ocr_bbox_tolerance_pixels: int = 8,
        visual_clicker: Callable[[int, int], None] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_elements < 1 or max_elements > 2000:
            raise ValueError("max_elements must be between 1 and 2000")
        self.profiles = {key.casefold(): value for key, value in profiles.items()}
        self._native = native
        self._desktop_factory = desktop_factory
        self.max_elements = max_elements
        self._discover_all_windows = bool(discover_all_windows)
        self._activate_on_observe = bool(activate_on_observe)
        self._capture_screenshots = bool(capture_screenshots)
        self._strict_visual_postcondition = bool(strict_visual_postcondition)
        self._activate_before_execute = bool(activate_before_execute)
        self._same_window_browser_navigation_fast_path = bool(
            same_window_browser_navigation_fast_path
        )
        if not 0 <= int(visual_ocr_bbox_tolerance_pixels) <= 32:
            raise ValueError("visual_ocr_bbox_tolerance_pixels must be between 0 and 32")
        self._visual_ocr_client = visual_ocr_client
        self._visual_screenshot_enabled = (
            visual_ocr_client is not None
            if visual_screenshot_enabled is None
            else bool(visual_screenshot_enabled)
        )
        self._automatic_visual_screenshots = bool(automatic_visual_screenshots)
        self._visual_ocr_apps = frozenset(item.strip().casefold() for item in visual_ocr_apps)
        if any(not item for item in self._visual_ocr_apps):
            raise ValueError("visual_ocr_apps must contain non-empty app names")
        self._visual_ocr_bbox_tolerance_pixels = int(
            visual_ocr_bbox_tolerance_pixels
        )
        self._visual_clicker = visual_clicker
        self._monotonic = monotonic
        self._sleep = sleeper
        self._generation = 0
        self._snapshots: dict[str, _Snapshot] = {}
        self._pending_observation: set[str] = set()
        # Visual postconditions belong to an exact top-level window, not to an
        # app alias. A configured canonical name and a freshly enumerated
        # dynamic ID may both refer to the same HWND across consecutive steps.
        self._pending_visual_change: dict[int, _PendingVisualChange] = {}
        self._visual_text_click: dict[int, _VisualTextClickBinding] = {}
        self._visual_text_focus: dict[int, _VisualTextFocusBinding] = {}
        self._dynamic_windows: dict[str, _DynamicWindowBinding] = {}
        # Dynamic IDs are opaque lifecycle aliases, not encodings of HWND/PID.
        # The sequence never resets while this driver object exists, so a
        # handle that disappears and is later reused cannot resurrect an old
        # cloud-visible app ID.
        self._dynamic_id_nonce = secrets.token_bytes(32)
        self._dynamic_id_sequence = 0
        self._issued_dynamic_ids: set[str] = set()
        self._active_cancellations: set[threading.Event] = set()
        self._task_context = ""
        self._lock = threading.RLock()

    def set_task_context(self, task: str | None) -> None:
        """Set a local-only hint used solely when a large UI tree is bounded."""

        if task is not None and (not isinstance(task, str) or len(task) > 16000):
            raise ValueError("desktop task context must be a bounded string or null")
        with self._lock:
            self._task_context = " ".join((task or "").casefold().split())
            self._visual_text_click.clear()
            self._visual_text_focus.clear()

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
        profile = self.profiles.get(normalized)
        if profile is not None:
            return profile
        if self._discover_all_windows:
            with self._lock:
                binding = self._dynamic_windows.get(normalized)
            if binding is not None:
                return binding.profile
        raise WindowsUiaDriverError(f"application is not configured: {app!r}")

    def _visual_enabled_for_window(self, app: str, profile: AppProfile) -> bool:
        """Match one configured app or every fresh dynamic window via ``*``.

        The wildcard is deliberately effective only for the all-window driver
        used by ``local_unrestricted``.  Direct driver construction therefore
        cannot turn ``*`` into an implicit expansion of a configured-app scope.
        """

        if not self._visual_screenshot_enabled or not self._automatic_visual_screenshots:
            return False
        if self._discover_all_windows and "*" in self._visual_ocr_apps:
            return True
        normalized = self._normalize_app(app)
        return bool(
            normalized in self._visual_ocr_apps
            or profile.name.strip().casefold() in self._visual_ocr_apps
        )

    def _retain_canonical_dynamic_alias(
        self,
        *,
        app: str,
        window: WindowInfo,
        profile: AppProfile,
    ) -> None:
        """Keep a successfully observed canonical app pinned to its exact HWND."""

        if not self._discover_all_windows or app not in self.profiles:
            return
        binding = _DynamicWindowBinding(
            app_id=app,
            window=window,
            profile=profile,
            display_name=profile.name,
        )
        with self._lock:
            duplicate_ids = tuple(
                app_id
                for app_id, candidate in self._dynamic_windows.items()
                if app_id != app and candidate.window.hwnd == window.hwnd
            )
            for duplicate_id in duplicate_ids:
                self._dynamic_windows.pop(duplicate_id, None)
                self._snapshots.pop(duplicate_id, None)
                self._pending_observation.discard(duplicate_id)
            self._dynamic_windows[app] = binding

    @staticmethod
    def _same_window_identity(
        expected: WindowInfo,
        actual: WindowInfo,
        *,
        allow_title_change: bool = False,
    ) -> bool:
        return bool(
            expected.hwnd == actual.hwnd
            and expected.process_id == actual.process_id
            and (expected.process_name or "").casefold() == (actual.process_name or "").casefold()
            and (allow_title_change or expected.title == actual.title)
        )

    def _resolve_window(
        self,
        app: str,
        *,
        allow_dynamic_title_change: bool = False,
    ) -> WindowInfo:
        normalized = self._normalize_app(app)
        if self._discover_all_windows:
            with self._lock:
                binding = self._dynamic_windows.get(normalized)
            if binding is not None:
                matches = {item.hwnd: item for item in self._native_backend().enumerate_windows()}
                current = matches.get(binding.window.hwnd)
                if current is None:
                    raise WindowsUiaStaleObservation(
                        "the selected top-level window is no longer visible"
                    )
                if not self._same_window_identity(
                    binding.window,
                    current,
                    allow_title_change=allow_dynamic_title_change,
                ):
                    raise WindowsUiaStaleObservation(
                        "the selected top-level window identity changed after inventory"
                    )
                if allow_dynamic_title_change and current.title != binding.window.title:
                    with self._lock:
                        latest = self._dynamic_windows.get(normalized)
                        if latest != binding:
                            raise WindowsUiaStaleObservation(
                                "the selected top-level window binding changed"
                            )
                        self._dynamic_windows[normalized] = replace(
                            binding,
                            window=current,
                        )
                return current
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

    @staticmethod
    def _window_matches_profile(window: WindowInfo, profile: AppProfile) -> bool:
        patterns = tuple(item.strip().casefold() for item in profile.title_patterns if item.strip())
        processes = {item.strip().casefold() for item in profile.process_names if item.strip()}
        title_matches = not patterns or any(
            pattern in window.title.casefold() for pattern in patterns
        )
        process_matches = not processes or (
            window.process_name is not None and window.process_name.casefold() in processes
        )
        return title_matches and process_matches and bool(patterns or processes)

    @staticmethod
    def _dynamic_window_slug(window: WindowInfo) -> str:
        source = (window.process_name or "window").removesuffix(".exe").casefold()
        slug = re.sub(r"[^a-z0-9_.+-]+", "-", source).strip("-._+")
        return slug[:48] or "window"

    def _allocate_dynamic_window_id(
        self,
        window: WindowInfo,
        *,
        reserved_ids: set[str],
    ) -> str:
        """Allocate one opaque ID that is never reused by this driver.

        Stability comes from retaining the resulting binding for the lifetime
        of its exact HWND, not from recomputing an identifier from mutable
        inventory metadata.  The token contains no HWND, PID, title, or other
        reversible local window identity.
        """

        slug = self._dynamic_window_slug(window)
        with self._lock:
            occupied = (
                reserved_ids
                | set(self.profiles)
                | set(self._dynamic_windows)
                | self._issued_dynamic_ids
            )
            while True:
                self._dynamic_id_sequence += 1
                sequence = self._dynamic_id_sequence.to_bytes(16, "big")
                token = hashlib.sha256(self._dynamic_id_nonce + sequence).hexdigest()[:16]
                candidate = f"{slug}-{token}"
                if candidate in occupied:
                    continue
                self._issued_dynamic_ids.add(candidate)
                return candidate

    @staticmethod
    def _immediate_parent_process_name(process_id: int) -> str | None:
        """Return one content-free process-family hint for helper windows.

        Some rendered desktop apps create a separate top-level helper process
        for search or navigation results.  We intentionally inspect only the
        immediate parent: walking the full ancestor chain could misclassify an
        ordinary app as Explorer merely because Explorer launched it.
        """

        if process_id <= 0:
            return None
        try:
            import psutil

            parent = psutil.Process(process_id).parent()
            if parent is None:
                return None
            name = parent.name()
        except Exception:
            return None
        return name.strip().casefold() if isinstance(name, str) and name.strip() else None

    def _dynamic_profile(self, window: WindowInfo) -> tuple[AppProfile, str]:
        # A window title commonly becomes the active document or conversation
        # name.  An exact executable match is therefore the stable primary app
        # identity; requiring a brand word in the changing title misclassified
        # rendered WeChat/Claude windows as anonymous dynamic apps.
        process_name = (window.process_name or "").strip().casefold()
        if process_name:
            process_matches = [
                profile
                for profile in self.profiles.values()
                if process_name
                in {item.strip().casefold() for item in profile.process_names if item.strip()}
            ]
            if len(process_matches) == 1:
                profile = process_matches[0]
                return profile, profile.name
        parent_process_name = self._immediate_parent_process_name(window.process_id)
        if parent_process_name:
            parent_process_matches = [
                profile
                for profile in self.profiles.values()
                if parent_process_name
                in {
                    item.strip().casefold()
                    for item in profile.process_names
                    if item.strip()
                }
            ]
            if len(parent_process_matches) == 1:
                profile = parent_process_matches[0]
                return profile, profile.name
        for profile in self.profiles.values():
            if self._window_matches_profile(window, profile):
                return profile, profile.name
        process_name = (window.process_name or "Window").removesuffix(".exe")
        display_name = process_name.strip() or "Window"
        return (
            AppProfile(
                name=display_name[:128],
                process_names=[window.process_name] if window.process_name else [],
                executable=None,
                title_patterns=[window.title],
                search_hotkey=None,
                native_voice_hotkey=None,
                voice_button_names=[],
            ),
            display_name[:128],
        )

    @staticmethod
    def _capture_window_png(root: Any) -> bytes | None:
        """Best-effort in-memory screenshot for visual planner context."""

        capture = _safe_attr(root, "capture_as_image", None)
        if not callable(capture):
            return None
        try:
            image = capture()
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            payload = stream.getvalue()
        except Exception:
            return None
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            return None
        return payload

    @staticmethod
    def _capture_window_rect(
        root: Any,
        *,
        width: int,
        height: int,
    ) -> tuple[int, int, int, int]:
        rectangle_member = _safe_attr(root, "rectangle", None)
        try:
            rectangle = rectangle_member() if callable(rectangle_member) else rectangle_member
            if rectangle is None:
                raise ValueError("missing rectangle")
            if isinstance(rectangle, (tuple, list)) and len(rectangle) == 4:
                values = tuple(int(item) for item in rectangle)
            else:
                values = tuple(
                    int(
                        member() if callable(member) else member
                    )
                    for member in (
                        _safe_attr(rectangle, "left", None),
                        _safe_attr(rectangle, "top", None),
                        _safe_attr(rectangle, "right", None),
                        _safe_attr(rectangle, "bottom", None),
                    )
                )
        except (TypeError, ValueError) as exc:
            raise WindowsUiaDriverError(
                "visual OCR requires an exact captured-window rectangle"
            ) from exc
        left, top, right, bottom = values
        if right <= left or bottom <= top:
            raise WindowsUiaDriverError("visual OCR captured-window rectangle is invalid")
        if right - left != width or bottom - top != height:
            raise WindowsUiaDriverError(
                "visual OCR screenshot dimensions do not match its exact window rectangle"
            )
        return values

    @staticmethod
    def _distance_to_interval(value: int, start: int, end: int) -> int:
        if value < start:
            return start - value
        if value > end:
            return value - end
        return 0

    @classmethod
    def _focus_snapshot_matches_visual_click(
        cls,
        snapshot: NativeFocusSnapshot | None,
        click: _VisualTextClickBinding,
        *,
        allow_horizontal_drift: bool = False,
    ) -> bool:
        """Bind a system caret to the exact screenshot point without OCR."""

        if (
            snapshot is None
            or not snapshot.is_bound_to(click.hwnd)
            or not snapshot.has_visible_system_caret
            or snapshot.caret_rect_screen is None
        ):
            return False
        window_left, window_top, window_right, window_bottom = click.window_rect
        caret_left, caret_top, caret_right, caret_bottom = snapshot.caret_rect_screen
        if (
            window_right <= window_left
            or window_bottom <= window_top
            or caret_right < caret_left
            or caret_bottom <= caret_top
        ):
            return False
        caret_x = (caret_left + caret_right) // 2
        caret_y = (caret_top + caret_bottom) // 2
        if not (
            window_left <= caret_x < window_right
            and window_top <= caret_y < window_bottom
        ):
            return False
        click_x = window_left + click.x
        click_y = window_top + click.y
        height = window_bottom - window_top
        width = window_right - window_left
        y_tolerance = max(32, min(96, height // 12))
        if cls._distance_to_interval(click_y, caret_top, caret_bottom) > y_tolerance:
            return False
        if not allow_horizontal_drift:
            x_tolerance = max(48, min(256, width // 5))
            if cls._distance_to_interval(click_x, caret_left, caret_right) > x_tolerance:
                return False
        return True

    @staticmethod
    def _same_native_focus_identity(
        binding: _VisualTextFocusBinding,
        snapshot: NativeFocusSnapshot,
    ) -> bool:
        return bool(
            snapshot.target_process_id == binding.target_process_id
            and snapshot.target_thread_id == binding.target_thread_id
            and snapshot.focus_hwnd == binding.focus_hwnd
            and snapshot.caret_hwnd == binding.caret_hwnd
        )

    def _native_focus_snapshot(self, hwnd: int) -> NativeFocusSnapshot | None:
        getter = getattr(self._native_backend(), "get_focus_snapshot", None)
        if not callable(getter):
            return None
        try:
            snapshot = getter(hwnd)
        except Exception:
            return None
        return snapshot if isinstance(snapshot, NativeFocusSnapshot) else None

    def _refresh_visual_text_focus(
        self,
        *,
        hwnd: int,
        window_rect: tuple[int, int, int, int],
    ) -> bool:
        """Promote/revalidate a rendered field only from fresh native caret evidence."""

        with self._lock:
            pending = self._visual_text_click.get(hwnd)
            existing = self._visual_text_focus.get(hwnd)
        candidate = pending or (existing.click if existing is not None else None)
        if (
            candidate is None
            or candidate.hwnd != hwnd
            or candidate.local_window_id != f"hwnd:{hwnd}"
            or candidate.window_rect != window_rect
        ):
            with self._lock:
                if self._visual_text_click.get(hwnd) == pending:
                    self._visual_text_click.pop(hwnd, None)
                if self._visual_text_focus.get(hwnd) == existing:
                    self._visual_text_focus.pop(hwnd, None)
            return False
        snapshot = self._native_focus_snapshot(hwnd)
        valid = self._focus_snapshot_matches_visual_click(snapshot, candidate)
        if valid and existing is not None:
            assert snapshot is not None
            valid = self._same_native_focus_identity(existing, snapshot)
        if not valid:
            with self._lock:
                if self._visual_text_click.get(hwnd) == pending:
                    self._visual_text_click.pop(hwnd, None)
                if self._visual_text_focus.get(hwnd) == existing:
                    self._visual_text_focus.pop(hwnd, None)
            return False
        assert snapshot is not None
        focus_binding = _VisualTextFocusBinding(
            click=candidate,
            target_process_id=snapshot.target_process_id,
            target_thread_id=snapshot.target_thread_id,
            focus_hwnd=snapshot.focus_hwnd,
            caret_hwnd=snapshot.caret_hwnd,
        )
        with self._lock:
            # A newer click or focus for this HWND must not be overwritten by
            # an observation that started from older native focus evidence.
            if (
                self._visual_text_click.get(hwnd) != pending
                or self._visual_text_focus.get(hwnd) != existing
            ):
                return False
            self._visual_text_click.pop(hwnd, None)
            self._visual_text_focus[hwnd] = focus_binding
        return True

    def _assert_visual_text_focus(
        self,
        binding: _VisualTextFocusBinding,
        *,
        allow_horizontal_drift: bool = False,
    ) -> NativeFocusSnapshot:
        snapshot = self._native_focus_snapshot(binding.click.hwnd)
        if (
            not self._focus_snapshot_matches_visual_click(
                snapshot,
                binding.click,
                allow_horizontal_drift=allow_horizontal_drift,
            )
            or snapshot is None
            or not self._same_native_focus_identity(binding, snapshot)
        ):
            raise WindowsUiaStaleObservation(
                "rendered text focus/caret evidence no longer matches the exact window"
            )
        return snapshot

    @staticmethod
    def _processes_are_related(left_process_id: int, right_process_id: int) -> bool:
        if left_process_id <= 0 or right_process_id <= 0:
            return False
        if left_process_id == right_process_id:
            return True
        try:
            import psutil

            left_ancestors = {item.pid for item in psutil.Process(left_process_id).parents()}
            right_ancestors = {item.pid for item in psutil.Process(right_process_id).parents()}
        except Exception:
            return False
        return bool(
            left_process_id in right_ancestors
            or right_process_id in left_ancestors
        )

    def _wait_for_related_foreground_transition(
        self,
        native: NativeWindows,
        before_window: WindowInfo,
        *,
        timeout: float,
        cancel_event: threading.Event | None,
        operation_cancel: threading.Event,
    ) -> WindowInfo | None:
        remaining = max(0.0, timeout)
        while True:
            self._raise_if_cancelled(cancel_event, operation_cancel)
            current = native.get_foreground_window_info()
            if current is not None and current.hwnd != before_window.hwnd:
                if self._processes_are_related(
                    before_window.process_id,
                    current.process_id,
                ):
                    return current
                return None
            if remaining <= 0.0:
                return None
            interval = min(0.05, remaining)
            started = self._monotonic()
            self._sleep(interval)
            elapsed = max(0.0, self._monotonic() - started)
            remaining -= max(interval, elapsed)

    @staticmethod
    def _is_same_window_browser_address_navigation(
        action: DesktopAction,
        observation: DesktopObservation,
        target: DesktopElement | None,
    ) -> bool:
        """Identify Enter on an exact browser omnibox bound to the same HWND.

        Ordinary Enter keeps the historical two-second related-window watch.
        HTTP(S) navigation in a browser omnibox is expected to stay in the
        current top-level window, so waiting for a new HWND only consumes the
        strict assistive latency budget without adding evidence.
        """

        return bool(
            action.type == DesktopActionType.PRESS_KEY
            and (action.key or "").casefold() in {"enter", "return"}
            and target is not None
            and is_verified_browser_address(
                target,
                observation.process_name,
                require_focused=True,
            )
            and re.match(r"https?://", target.value or "", re.IGNORECASE)
        )

    @staticmethod
    def _raise_if_cancelled(
        cancel_event: threading.Event | None,
        operation_cancel: threading.Event,
    ) -> None:
        if operation_cancel.is_set() or (
            cancel_event is not None and cancel_event.is_set()
        ):
            raise WindowsUiaDriverError("desktop operation was cancelled")

    def _sleep_interruptibly(
        self,
        duration: float,
        *,
        cancel_event: threading.Event | None,
        operation_cancel: threading.Event,
    ) -> None:
        remaining = max(0.0, duration)
        while remaining > 0.0:
            self._raise_if_cancelled(cancel_event, operation_cancel)
            interval = min(0.05, remaining)
            started = self._monotonic()
            self._sleep(interval)
            elapsed = max(0.0, self._monotonic() - started)
            remaining -= max(interval, elapsed)
        self._raise_if_cancelled(cancel_event, operation_cancel)

    def _bind_related_dynamic_window_transition(
        self,
        *,
        app: str,
        before_window: WindowInfo,
        after_window: WindowInfo,
    ) -> str | None:
        with self._lock:
            binding = self._dynamic_windows.get(app)
            if (
                binding is None
                or not self._same_window_identity(binding.window, before_window)
            ):
                return None
            pending = self._pending_visual_change.get(before_window.hwnd)
            if pending is not None:
                if not self._same_window_identity(
                    pending.window,
                    before_window,
                    allow_title_change=True,
                ):
                    raise WindowsUiaStaleObservation(
                        "visual postcondition no longer belongs to the transition source"
                    )
                destination_pending = self._pending_visual_change.get(after_window.hwnd)
                if destination_pending is not None and destination_pending != pending:
                    raise WindowsUiaStaleObservation(
                        "related window already has another pending visual postcondition"
                    )
                self._pending_visual_change.pop(before_window.hwnd, None)
                self._pending_visual_change[after_window.hwnd] = replace(
                    pending,
                    window=after_window,
                )
            self._dynamic_windows[app] = replace(
                binding,
                window=after_window,
            )
            # The destination may already have appeared in the pre-action
            # inventory under another generated ID.  Keep the ID that owns the
            # active task and remove only the duplicate binding for this exact
            # HWND so the next inventory refresh cannot silently rename the
            # task halfway through the verified transition.
            duplicate_ids = tuple(
                app_id
                for app_id, candidate in self._dynamic_windows.items()
                if app_id != app and candidate.window.hwnd == after_window.hwnd
            )
            for duplicate_id in duplicate_ids:
                self._dynamic_windows.pop(duplicate_id, None)
                self._snapshots.pop(duplicate_id, None)
                self._pending_observation.discard(duplicate_id)
        return f"hwnd:{after_window.hwnd}"

    @staticmethod
    def _visual_identity(
        *,
        hwnd: int,
        window_rect: tuple[int, int, int, int],
        text: str,
        bbox: tuple[int, int, int, int],
        label: str,
        viewport: bool,
        target_sha256: str,
    ) -> str:
        payload = json.dumps(
            {
                "hwnd": int(hwnd),
                "window_rect": window_rect,
                "text": text,
                "bbox": bbox,
                "label": label,
                "viewport": viewport,
                "target_sha256": target_sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _visual_crop_sha256(
        screenshot_png: bytes,
        bbox: tuple[int, int, int, int],
    ) -> str:
        try:
            from PIL import Image

            image = Image.open(io.BytesIO(screenshot_png)).convert("RGB")
            crop = image.crop(bbox)
            if crop.width < 1 or crop.height < 1:
                raise ValueError("empty crop")
            digest = hashlib.sha256()
            digest.update(f"RGB:{crop.width}:{crop.height}:".encode("ascii"))
            digest.update(crop.tobytes())
            return digest.hexdigest()
        except Exception as exc:
            raise WindowsUiaDriverError(
                "visual OCR requires a decodable local target crop"
            ) from exc

    @classmethod
    def _make_visual_binding(
        cls,
        *,
        hwnd: int,
        window_rect: tuple[int, int, int, int],
        result: VisualOcrResult,
        screenshot_png: bytes,
        block: VisualOcrBlock | None,
    ) -> _VisualTargetBinding:
        viewport = block is None
        text = "Visual screenshot viewport" if viewport else block.text
        bbox = (0, 0, result.width, result.height) if viewport else block.bbox
        label = "viewport" if viewport else block.label
        target_sha256 = (
            hashlib.sha256(
                f"viewport:{result.width}:{result.height}".encode("ascii")
            ).hexdigest()
            if viewport
            else cls._visual_crop_sha256(screenshot_png, bbox)
        )
        return _VisualTargetBinding(
            hwnd=hwnd,
            window_rect=window_rect,
            text=text,
            bbox=bbox,
            label=label,
            frame_sha256=result.frame_sha256,
            target_sha256=target_sha256,
            local_identity=cls._visual_identity(
                hwnd=hwnd,
                window_rect=window_rect,
                text=text,
                bbox=bbox,
                label=label,
                viewport=viewport,
                target_sha256=target_sha256,
            ),
            viewport=viewport,
            screenshot_png=screenshot_png,
        )

    @staticmethod
    def _screenshot_result(screenshot_png: bytes) -> VisualOcrResult:
        """Describe a complete local PNG without requiring any OCR service."""

        try:
            from PIL import Image

            with Image.open(io.BytesIO(screenshot_png)) as image:
                if image.format != "PNG":
                    raise ValueError("not PNG")
                width, height = image.size
                if width < 1 or height < 1 or width > 16384 or height > 16384:
                    raise ValueError("invalid dimensions")
                image.verify()
        except Exception as exc:
            raise WindowsUiaDriverError(
                "visual planning requires a decodable exact-window screenshot"
            ) from exc
        return VisualOcrResult(
            width=int(width),
            height=int(height),
            frame_sha256=hashlib.sha256(screenshot_png).hexdigest(),
            blocks=(),
        )

    @staticmethod
    def _annotate_visual_regions(
        screenshot_png: bytes,
        indexed_bindings: Mapping[str, _VisualTargetBinding],
    ) -> bytes:
        """Overlay element indexes while preserving the complete captured window."""

        try:
            from PIL import Image, ImageDraw

            image = Image.open(io.BytesIO(screenshot_png)).convert("RGB")
            draw = ImageDraw.Draw(image)
            for index, binding in indexed_bindings.items():
                if binding.viewport:
                    continue
                left, top, right, bottom = binding.bbox
                draw.rectangle((left, top, right - 1, bottom - 1), outline="#ff2d55", width=3)
                marker = f"[{index}]"
                marker_box = draw.textbbox((left, top), marker)
                marker_width = marker_box[2] - marker_box[0] + 4
                marker_height = marker_box[3] - marker_box[1] + 4
                marker_top = max(0, top - marker_height)
                draw.rectangle(
                    (left, marker_top, min(image.width, left + marker_width), top),
                    fill="#ff2d55",
                )
                draw.text((left + 2, marker_top + 1), marker, fill="white")
            stream = io.BytesIO()
            image.save(stream, format="PNG")
            payload = stream.getvalue()
        except Exception as exc:
            raise WindowsUiaDriverError(
                "visual OCR could not create the required set-of-marks screenshot"
            ) from exc
        if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
            raise WindowsUiaDriverError(
                "visual OCR set-of-marks encoder returned an invalid PNG"
            )
        return payload

    @staticmethod
    def _has_rich_uia_surface(elements: tuple[DesktopElement, ...]) -> bool:
        inputs = tuple(
            element
            for element in elements
            if element.addressable
            and element.enabled
            and element_plane(element) == ElementPlane.INPUT
            and element.editable is not False
        )
        business_controls = tuple(
            element
            for element in elements
            if element.addressable
            and element.enabled
            and element_plane(element) == ElementPlane.CONTROL
        )
        # A positively classified composer is sufficient. A plain input is
        # sufficient only alongside a non-trivial business control surface;
        # this keeps a few Chromium title-bar/shell buttons from suppressing
        # augmentation in ChatGPT/Codex and Qt render-only windows.
        return bool(
            any(element.composer for element in inputs)
            or (inputs and len(business_controls) >= 5)
        )

    def _visual_fallback(
        self,
        *,
        hwnd: int,
        root: Any,
        screenshot_png: bytes,
        elements: tuple[DesktopElement, ...],
        wrappers: dict[str, Any],
        stats: dict[str, Any],
        force: bool = False,
    ) -> tuple[tuple[DesktopElement, ...], dict[str, Any], dict[str, Any], bytes]:
        if not self._visual_screenshot_enabled and not force:
            return elements, wrappers, stats, screenshot_png
        rich_uia_surface = self._has_rich_uia_surface(elements)
        # A semantic UIA surface remains the preferred and first-listed action
        # plane.  Keep the complete screenshot viewport as a fallback signal,
        # but do not add duplicate OCR text regions when UIA already exposes a
        # positively classified input/control surface.
        client = None if rich_uia_surface else self._visual_ocr_client
        result = self._screenshot_result(screenshot_png)
        ocr_error: str | None = None
        if client is not None:
            try:
                result = client.ocr_png(screenshot_png)
            except SensitiveVisualSurfaceError:
                raise
            except VisualOcrError as exc:
                # OCR is only an optional text-box enhancement. The complete
                # screenshot and exact-frame viewport remain valid planner input.
                ocr_error = type(exc).__name__
        window_rect = self._capture_window_rect(
            root,
            width=result.width,
            height=result.height,
        )
        visual_text_armed = self._refresh_visual_text_focus(
            hwnd=hwnd,
            window_rect=window_rect,
        )
        next_index = max((int(item.index) for item in elements), default=-1) + 1
        capacity = max(0, self.max_elements - len(elements))
        selected_blocks = result.blocks[: max(0, capacity - 1)]
        bindings: dict[str, _VisualTargetBinding] = {}
        visual_elements: list[DesktopElement] = []
        for block in selected_blocks:
            index = str(next_index)
            next_index += 1
            binding = self._make_visual_binding(
                hwnd=hwnd,
                window_rect=window_rect,
                result=result,
                screenshot_png=screenshot_png,
                block=block,
            )
            bindings[index] = binding
            name_metadata = BoundedUiText.from_text(block.text, maximum=512)
            visual_elements.append(
                DesktopElement(
                    index=index,
                    name=name_metadata.display,
                    control_type="VisualText",
                    automation_id=None,
                    enabled=True,
                    local_identity=binding.local_identity,
                    plane=ElementPlane.CONTROL,
                    editable=False,
                    addressable=True,
                    composer=False,
                    visual_ocr=True,
                    name_metadata=name_metadata,
                    supported_actions=(DesktopElementAction.CLICK,),
                )
            )
        # The viewport is one mandatory frame binding, not another UIA node.
        # It may therefore occupy one synthetic slot beyond ``max_elements``
        # when the semantic UIA budget is already full.  Existing UIA elements
        # remain first so the planner continues to prefer their exact actions.
        index = str(next_index)
        binding = self._make_visual_binding(
            hwnd=hwnd,
            window_rect=window_rect,
            result=result,
            screenshot_png=screenshot_png,
            block=None,
        )
        bindings[index] = binding
        viewport_actions = [
            DesktopElementAction.CLICK,
            DesktopElementAction.SCROLL,
        ]
        if visual_text_armed:
            viewport_actions.append(DesktopElementAction.TYPE_TEXT)
        visual_elements.append(
            DesktopElement(
                index=index,
                name=binding.text,
                control_type="VisualViewport",
                enabled=True,
                local_identity=binding.local_identity,
                plane=ElementPlane.CONTROL,
                editable=False,
                addressable=True,
                composer=False,
                visual_ocr=True,
                supported_actions=tuple(viewport_actions),
                scroll_axes=(DesktopScrollAxis.VERTICAL,),
            )
        )
        combined = elements + tuple(visual_elements)
        combined_wrappers = {**wrappers, **bindings}
        updated = dict(stats)
        updated["visual_screenshot_used"] = True
        updated["visual_ocr_used"] = client is not None and ocr_error is None
        updated["visual_semantic_uia_preferred"] = rich_uia_surface
        if ocr_error is not None:
            updated["visual_ocr_error"] = ocr_error
        updated["visual_ocr_augmented_uia"] = bool(elements)
        updated["visual_ocr_region_count"] = len(visual_elements)
        updated["visual_ocr_text_region_count"] = len(selected_blocks)
        updated["visual_ocr_frame_bound"] = True
        updated["kept"] = len(combined)
        updated["retained"] = len(combined)
        updated["retained_actionable"] = int(updated.get("retained_actionable", 0)) + len(
            visual_elements
        )
        updated["kept_control_count"] = int(updated.get("kept_control_count", 0)) + len(
            visual_elements
        )
        updated["total_element_count"] = max(
            len(combined),
            int(updated.get("total_element_count", len(elements))) + len(visual_elements),
        )
        updated["elements_truncated"] = bool(
            updated.get("elements_truncated") or len(selected_blocks) < len(result.blocks)
        )
        annotated_png = (
            self._annotate_visual_regions(screenshot_png, bindings)
            if selected_blocks
            else screenshot_png
        )
        return (
            combined,
            combined_wrappers,
            updated,
            annotated_png,
        )

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
            label_name = getattr(labeled_by, "CurrentName", "") or getattr(labeled_by, "name", "")
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
        return bool(len(normalized) <= 120 and _SECRET_LABELED_BY_RE.fullmatch(normalized))

    @staticmethod
    def _is_browser_chrome_descendant(wrapper: Any, trusted_root: Any) -> bool:
        """Reach the exact UIA window root through ToolBar, never through Document."""

        root_identity = WindowsUiaDriver._local_identity(trusted_root)
        current = wrapper
        seen: set[tuple[str, object]] = set()
        saw_toolbar = False
        for _depth in range(24):
            parent = _safe_call(current, "parent", None)
            if parent is None or parent is current:
                return False
            parent_identity = WindowsUiaDriver._local_identity(parent)
            parent_control_type = WindowsUiaDriver._raw_control_type(parent).casefold()
            token = (
                ("runtime", parent_identity)
                if parent_identity is not None
                else ("object", id(parent))
            )
            if token in seen:
                return False
            if parent is trusted_root or (
                root_identity is not None and parent_identity == root_identity
            ):
                return saw_toolbar
            seen.add(token)
            if parent_control_type == "document":
                return False
            saw_toolbar = saw_toolbar or parent_control_type == "toolbar"
            current = parent
        return False

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
            aria = _normalized_text(getattr(raw_element, "CurrentAriaProperties", "")).casefold()
            role = _normalized_text(getattr(raw_element, "CurrentAriaRole", "")).casefold()
        except Exception as exc:
            raise WindowsUiaDriverError(
                "could not revalidate the target composer semantics"
            ) from exc
        return role == "textbox" and ("multiline=true" in aria or "contenteditable=true" in aria)

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
        semantic_content = control_type.strip().casefold() in _SEMANTIC_CONTENT_CONTROL_TYPES
        supported_actions, expand_state, scroll_axes = _uia_capability_metadata(
            wrapper,
            allow_activation=not semantic_content,
        )
        return DesktopElement(
            index=index,
            name=name,
            control_type=control_type,
            automation_id=self._automation_id(wrapper),
            value=(_bounded_text(value, maximum=4000).display if value is not None else None),
            value_observed=value_observed,
            selected=selected,
            focused=_safe_call(wrapper, "has_keyboard_focus", None),
            password=password,
            enabled=_safe_call(wrapper, "is_enabled", True) is not False,
            local_identity=self._local_identity(wrapper),
            supported_actions=supported_actions,
            expand_collapse_state=expand_state,
            scroll_axes=scroll_axes,
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
                raise WindowsUiaDriverError("could not enumerate UIA descendants") from exc
            return list(enumerate(wrappers)), len(wrappers), []

        if not callable(raw_descendants) or not callable(wrapper_factory):
            try:
                wrappers = list(root.descendants())
            except Exception as exc:
                raise WindowsUiaDriverError("could not enumerate UIA descendants") from exc
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
                            control_type=_normalized_text(safe_raw_attr(raw_info, "control_type")),
                            automation_id=(
                                _normalized_text(safe_raw_attr(raw_info, "automation_id")) or None
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
        descendants, total_descendants, enumeration_omissions = self._enumerate_descendants(root)

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

        def counted_capability_attr(owner: Any, name: str, default: Any = None) -> Any:
            try:
                return getattr(owner, name, default)
            except Exception as exc:
                # Absence of an optional UIA pattern is a normal negative
                # capability result. Other interface/property failures remain
                # visible in diagnostics but are still contained per element.
                if not _is_missing_uia_pattern(exc):
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
                automation_id=(_normalized_text(safe_info_attr("automation_id")) or None),
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
            return bool(len(label_name) <= 120 and _SECRET_LABELED_BY_RE.fullmatch(label_name))

        include_types = self._profile_control_types(profile)
        content_types = self._profile_content_types(profile)
        browser_profile = any(is_browser_process(name) for name in profile.process_names)
        profile_enabled = bool(
            profile.include_control_types or profile.content_control_types or profile.composer_names
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
                control_type = _normalized_text(counted_call(wrapper, "friendly_class_name", ""))
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
            automation_id = _normalized_text(automation_id_value) if automation_id_value else None
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
                finding.confidence == CredentialConfidence.HIGH for finding in raw_findings
            )
            low_credential = any(
                finding.confidence == CredentialConfidence.LOW for finding in raw_findings
            )
            stats["high_credential_count"] += sum(
                finding.confidence == CredentialConfidence.HIGH for finding in raw_findings
            )
            stats["low_credential_count"] += sum(
                finding.confidence == CredentialConfidence.LOW for finding in raw_findings
            )
            stats["credential_affected_element_count"] += int(high_credential or low_credential)

            normalized_control_type = control_type.casefold()
            content_plane = normalized_control_type in content_types
            raw_element = counted_attr(info, "element", None, count_error=False)
            aria_properties = _normalized_text(
                counted_attr(raw_element, "CurrentAriaProperties", "")
            )
            aria_role = _normalized_text(counted_attr(raw_element, "CurrentAriaRole", ""))
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
            # Only an editable address control can ever be verified browser
            # chrome, and the ancestor walk costs one COM round trip per level,
            # so skip it for every other element (about half of a Chrome
            # observation otherwise).
            browser_chrome = bool(
                self._same_window_browser_navigation_fast_path
                and browser_profile
                and normalized_control_type in {"edit", "combobox"}
                and self._is_browser_chrome_descendant(wrapper, root)
            )

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
                min(profile.max_content_chars, 1024) if content_plane else control_name_limit
            )
            value_limit = profile.max_content_chars if content_plane else 4000
            try:
                name_metadata = _bounded_text(name, maximum=name_limit)
                value_metadata = (
                    _bounded_text(value, maximum=value_limit) if value is not None else None
                )
            except ValueError:
                # One malformed UIA property must not make the entire window
                # unobservable. The content-free digest still participates in
                # freshness, while no invalid text crosses the protocol boundary.
                stats["omitted_invalid_metadata"] += 1
                bounded_entries.append((source_index, "invalid-metadata", private_digest))
                continue
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
            supported_actions, expand_state, scroll_axes = _uia_capability_metadata(
                wrapper,
                read_attr=counted_capability_attr,
                allow_activation=not semantic_content and not dialog_plane,
            )
            scroll_addressable = bool(
                enabled and DesktopElementAction.SCROLL in supported_actions
            )
            addressable = bool(
                not dialog_plane
                and not name_truncated
                and (not semantic_content or scroll_addressable)
            )
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
                    browser_chrome=browser_chrome,
                    high_credential=high_credential,
                    low_credential=low_credential,
                    name_metadata=name_metadata,
                    value_metadata=value_metadata,
                    supported_actions=supported_actions,
                    expand_collapse_state=expand_state,
                    scroll_axes=scroll_axes,
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
                        or bool(supported_actions)
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
            elif normalized_type in {"listitem", "treeitem"} or (
                candidate.element.supported_actions is not None
                and DesktopElementAction.SCROLL in candidate.element.supported_actions
            ):
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

    def bind_app_window(self, app: str, hwnd: int) -> None:
        """Rebind a configured canonical app to one verified foreground HWND.

        Native deterministic actions return the exact HWND they activated. A
        previous canonical alias may still point at another window of the same
        application, so continuous control must consume that evidence instead
        of reactivating the older alias.
        """

        normalized = self._normalize_app(app)
        if not self._discover_all_windows or normalized not in self.profiles:
            return
        if isinstance(hwnd, bool) or not isinstance(hwnd, int) or hwnd <= 0:
            raise WindowsUiaDriverError("native app binding requires a positive HWND")
        native = self._native_backend()
        native.assert_interactive_desktop()
        matches = [window for window in native.enumerate_windows() if window.hwnd == hwnd]
        if len(matches) != 1:
            raise WindowsUiaStaleObservation(
                "the native action HWND is not one unique visible top-level window"
            )
        window = matches[0]
        profile = self.profiles[normalized]
        matched_profile, _display_name = self._dynamic_profile(window)
        if matched_profile != profile:
            raise WindowsUiaStaleObservation(
                "the native action HWND does not match the configured application"
            )
        native.assert_foreground(hwnd)
        replacement = _DynamicWindowBinding(
            app_id=normalized,
            window=window,
            profile=profile,
            display_name=profile.name,
        )
        with self._lock:
            previous = self._dynamic_windows.get(normalized)
            if previous is not None and previous.window.hwnd != hwnd:
                self._pending_visual_change.pop(previous.window.hwnd, None)
            duplicate_ids = tuple(
                app_id
                for app_id, candidate in self._dynamic_windows.items()
                if app_id != normalized and candidate.window.hwnd == hwnd
            )
            for duplicate_id in duplicate_ids:
                self._dynamic_windows.pop(duplicate_id, None)
                self._snapshots.pop(duplicate_id, None)
                self._pending_observation.discard(duplicate_id)
            self._snapshots.pop(normalized, None)
            self._pending_observation.discard(normalized)
            self._dynamic_windows[normalized] = replacement

    def activate_app(self, app: str, *, cancel_event: threading.Event | None = None) -> int:
        """Bring one listed app window to the foreground as an explicit step.

        Assistive observation is passive, so the controller calls this before
        observing a window the planner selected while another app was in front.
        The exact HWND bound at inventory time is activated; a window whose
        identity changed since then is a stale observation, not a new target.
        """

        if cancel_event is not None and cancel_event.is_set():
            raise WindowsUiaDriverError("desktop operation was cancelled")
        native = self._native_backend()
        native.assert_interactive_desktop()
        window = self._resolve_window(app, allow_dynamic_title_change=True)
        foreground = native.get_foreground_window_info()
        if foreground is None or foreground.hwnd != window.hwnd:
            native.activate_window(window.hwnd)
        native.assert_foreground(window.hwnd)
        return window.hwnd

    def list_apps(self, *, cancel_event: threading.Event | None = None) -> str:
        if cancel_event is not None and cancel_event.is_set():
            raise WindowsUiaDriverError("desktop operation was cancelled")
        native = self._native_backend()
        native.assert_interactive_desktop()
        if self._discover_all_windows:
            windows = native.enumerate_windows()
            foreground = native.get_foreground_window_info()
            with self._lock:
                old_bindings = dict(self._dynamic_windows)
            preserved_by_hwnd = {
                binding.window.hwnd: binding
                for binding in old_bindings.values()
            }
            reserved_preserved_ids = {
                binding.app_id for binding in preserved_by_hwnd.values()
            }
            bindings: dict[str, _DynamicWindowBinding] = {}
            visible: list[dict[str, Any]] = []
            for window in windows:
                preserved = preserved_by_hwnd.get(window.hwnd)
                if preserved is not None and self._same_window_identity(
                    preserved.window,
                    window,
                    allow_title_change=True,
                ):
                    app_id = preserved.app_id
                    profile = preserved.profile
                    display_name = preserved.display_name
                else:
                    app_id = self._allocate_dynamic_window_id(
                        window,
                        reserved_ids=reserved_preserved_ids | set(bindings),
                    )
                    profile, display_name = self._dynamic_profile(window)
                if app_id in bindings:
                    # A duplicate HWND/PID record is not a second selectable window.
                    continue
                binding = _DynamicWindowBinding(
                    app_id=app_id,
                    window=window,
                    profile=profile,
                    display_name=display_name,
                )
                bindings[app_id] = binding
                visible.append(
                    {
                        "app": app_id,
                        "display_name": display_name,
                        "foreground": bool(
                            foreground is not None and foreground.hwnd == window.hwnd
                        ),
                        "process_name": window.process_name,
                        "visible_window_count": 1,
                        "window_title": window.title,
                    }
                )
            with self._lock:
                self._dynamic_windows = bindings
                invalidated_ids = {
                    app_id
                    for app_id, old_binding in old_bindings.items()
                    if app_id not in bindings
                    or not self._same_window_identity(
                        old_binding.window,
                        bindings[app_id].window,
                    )
                }
                for app_id in invalidated_ids:
                    old_window = old_bindings[app_id].window
                    self._snapshots.pop(app_id, None)
                    self._pending_observation.discard(app_id)
                    pending = self._pending_visual_change.get(old_window.hwnd)
                    if pending is not None and not any(
                        candidate.window.hwnd == old_window.hwnd
                        and self._same_window_identity(
                            pending.window,
                            candidate.window,
                            allow_title_change=True,
                        )
                        for candidate in bindings.values()
                    ):
                        self._pending_visual_change.pop(old_window.hwnd, None)
            return json.dumps(visible, ensure_ascii=False, sort_keys=True)
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
        capture_screenshot: bool = False,
    ) -> DesktopObservation:
        if cancel_event is not None and cancel_event.is_set():
            raise WindowsUiaDriverError("desktop operation was cancelled")
        normalized = self._normalize_app(app)
        profile = self._profile(app)
        native = self._native_backend()
        native.assert_interactive_desktop()
        with self._lock:
            allow_dynamic_title_change = bool(
                normalized in self._dynamic_windows
                and normalized in self._pending_observation
                and normalized in self._snapshots
            )
        window = self._resolve_window(
            app,
            allow_dynamic_title_change=allow_dynamic_title_change,
        )
        if self._activate_on_observe:
            activated = native.activate_window(window.hwnd)
            if not self._same_window_identity(window, activated):
                raise WindowsUiaStaleObservation(
                    "the selected top-level window changed during activation"
                )
            native.assert_foreground(window.hwnd)
        root = self._root(window.hwnd)
        elements, wrappers, stats = self._elements(root, profile)
        # Assistive observation stays passive and screenshot-free by default.
        # A caller may explicitly escalate this exact window to the existing
        # frame-bound visual path for one planning step.
        visual_enabled_for_window = bool(
            capture_screenshot or self._visual_enabled_for_window(normalized, profile)
        )
        screenshot_png = (
            self._capture_window_png(root)
            if self._capture_screenshots or visual_enabled_for_window
            else None
        )
        if visual_enabled_for_window and screenshot_png is None:
            raise WindowsUiaDriverError(
                "visual OCR could not capture the complete exact target window"
            )
        raw_screenshot_png = screenshot_png
        with self._lock:
            pending_visual_change = self._pending_visual_change.get(window.hwnd)
        if pending_visual_change is not None:
            if not self._same_window_identity(
                pending_visual_change.window,
                window,
                allow_title_change=True,
            ):
                raise WindowsUiaStaleObservation(
                    "visual postcondition belongs to a different window identity"
                )
            if raw_screenshot_png is None:
                raise WindowsUiaDriverError(
                    "visual action postcondition has no fresh window screenshot"
                )
            current_frame = hashlib.sha256(raw_screenshot_png).hexdigest()
            if (
                current_frame == pending_visual_change.frame_sha256
                and self._strict_visual_postcondition
            ):
                raise WindowsUiaDriverError(
                    "visual action produced no observable exact-window change"
                )
        if visual_enabled_for_window:
            assert screenshot_png is not None
            elements, wrappers, stats, screenshot_png = self._visual_fallback(
                hwnd=window.hwnd,
                root=root,
                screenshot_png=screenshot_png,
                elements=elements,
                wrappers=wrappers,
                stats=stats,
                force=capture_screenshot,
            )
        if self._activate_on_observe:
            native.assert_foreground(window.hwnd)
        self._retain_canonical_dynamic_alias(
            app=normalized,
            window=window,
            profile=profile,
        )
        with self._lock:
            current_pending_visual_change = self._pending_visual_change.get(window.hwnd)
            if current_pending_visual_change != pending_visual_change:
                raise WindowsUiaStaleObservation(
                    "visual verification state changed during window observation"
                )
            self._generation += 1
            observation = DesktopObservation(
                app=normalized,
                generation=self._generation,
                accessibility_text=self._accessibility_text(window, elements, stats),
                screenshot_png=screenshot_png,
                window_title=window.title,
                process_name=window.process_name,
                class_name=window.class_name,
                elements=elements,
                local_window_id=f"hwnd:{window.hwnd}",
                total_element_count=int(stats["total_element_count"]),
                elements_truncated=bool(stats["elements_truncated"]),
                skipped_long_content_count=int(stats["skipped_content_count"]),
                property_error_count=int(stats["property_error_count"]),
                high_credential_count=int(stats["high_credential_count"]),
                low_credential_count=int(stats["low_credential_count"]),
                credential_affected_element_count=int(stats["credential_affected_element_count"]),
            )
            self._snapshots[normalized] = _Snapshot(window.hwnd, observation, wrappers, root)
            self._pending_observation.discard(normalized)
            if pending_visual_change is not None:
                self._pending_visual_change.pop(window.hwnd, None)
            return observation

    @staticmethod
    def _assert_element_usable(wrapper: Any, *, allow_password: bool = False) -> None:
        try:
            visible_member = wrapper.is_visible
            visible = visible_member() if callable(visible_member) else visible_member
        except Exception as exc:
            raise WindowsUiaStaleObservation("target visibility could not be revalidated") from exc
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
        trusted_root: Any | None = None,
    ) -> None:
        try:
            current_name = self._raw_name(wrapper)
            current_control_type = self._control_type(wrapper)
            current_automation_id = self._automation_id(wrapper)
            current_password = UIABackend._is_password(wrapper)
            try:
                enabled_member = wrapper.is_enabled
                current_enabled = enabled_member() if callable(enabled_member) else enabled_member
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
            current_browser_chrome = bool(
                expected.browser_chrome
                and trusted_root is not None
                and self._is_browser_chrome_descendant(wrapper, trusted_root)
            )
            (
                current_supported_actions,
                current_expand_state,
                current_scroll_axes,
            ) = _uia_capability_metadata(
                wrapper,
                allow_activation=element_plane(expected)
                not in {ElementPlane.CONTENT, ElementPlane.DIALOG},
            )
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
            expected.browser_chrome,
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
            current_browser_chrome,
        )
        selection_changed = current_selected != expected.selected and not (
            expected.selected is False and current_selected is None
        )
        capability_changed = bool(
            expected.supported_actions is not None
            and (
                current_supported_actions != expected.supported_actions
                or current_expand_state != expected.expand_collapse_state
                or current_scroll_axes != expected.scroll_axes
            )
        )
        if (
            current_state != expected_state
            or selection_changed
            or capability_changed
            or (require_focus and (expected.focused is not True or current_focused is not True))
        ):
            raise WindowsUiaStaleObservation(
                "target element identity or state changed after the observation"
            )

    def _rebind_visual_target(
        self,
        binding: _VisualTargetBinding,
        expected: DesktopElement,
    ) -> tuple[tuple[int, int, int, int], bytes]:
        if not self._visual_screenshot_enabled or not expected.visual_ocr:
            raise WindowsUiaStaleObservation("visual screenshot target is no longer enabled")
        if expected.local_identity != binding.local_identity:
            raise WindowsUiaStaleObservation("visual screenshot target identity changed")
        root = self._root(binding.hwnd)
        screenshot_png = self._capture_window_png(root)
        if screenshot_png is None:
            raise WindowsUiaStaleObservation("visual screenshot target could not be recaptured")
        try:
            screenshot_result = self._screenshot_result(screenshot_png)
            window_rect = self._capture_window_rect(
                root,
                width=screenshot_result.width,
                height=screenshot_result.height,
            )
        except WindowsUiaDriverError as exc:
            raise WindowsUiaStaleObservation(
                "visual screenshot target could not be rebound"
            ) from exc
        if window_rect != binding.window_rect:
            raise WindowsUiaStaleObservation("visual screenshot target window moved or resized")
        if binding.viewport:
            expected_bbox = (0, 0, screenshot_result.width, screenshot_result.height)
            if binding.bbox != expected_bbox:
                raise WindowsUiaStaleObservation("visual screenshot viewport dimensions changed")
            return expected_bbox, screenshot_png
        client = self._visual_ocr_client
        if client is None:
            raise WindowsUiaStaleObservation(
                "OCR text target is unavailable without the optional OCR region service"
            )
        try:
            result = client.ocr_png(screenshot_png)
        except VisualOcrError as exc:
            raise WindowsUiaStaleObservation("visual OCR target could not be rebound") from exc
        if result.width != screenshot_result.width or result.height != screenshot_result.height:
            raise WindowsUiaStaleObservation("visual OCR target dimensions changed")
        tolerance = self._visual_ocr_bbox_tolerance_pixels
        matches = [
            block
            for block in result.blocks
            if block.text == binding.text
            and block.label == binding.label
            and all(
                abs(current - prior) <= tolerance
                for current, prior in zip(block.bbox, binding.bbox, strict=True)
            )
        ]
        if len(matches) != 1:
            raise WindowsUiaStaleObservation(
                "visual OCR target no longer has one unique same-label same-region match"
            )
        rebound_bbox = matches[0].bbox
        if self._visual_crop_sha256(screenshot_png, rebound_bbox) != binding.target_sha256:
            raise WindowsUiaStaleObservation(
                "visual OCR target pixels changed after planning; a new observation is required"
            )
        return rebound_bbox, screenshot_png

    @staticmethod
    def _visual_point_patch_still_matches(
        planned_png: bytes,
        current_png: bytes,
        *,
        x: int,
        y: int,
        width: int,
        height: int,
        radius: int = 32,
    ) -> bool:
        """Ignore caret blink while rejecting a materially changed click target."""

        left = max(0, x - radius)
        top = max(0, y - radius)
        right = min(width, x + radius + 1)
        bottom = min(height, y + radius + 1)
        try:
            from PIL import Image

            planned = Image.open(io.BytesIO(planned_png)).convert("RGB").crop(
                (left, top, right, bottom)
            )
            current = Image.open(io.BytesIO(current_png)).convert("RGB").crop(
                (left, top, right, bottom)
            )
            if planned.size != current.size or planned.width < 1 or planned.height < 1:
                return False
            changed_pixels = 0
            absolute_difference = 0
            planned_bytes = planned.tobytes()
            current_bytes = current.tobytes()
            for offset in range(0, len(planned_bytes), 3):
                channel_difference = tuple(
                    abs(before - after)
                    for before, after in zip(
                        planned_bytes[offset : offset + 3],
                        current_bytes[offset : offset + 3],
                        strict=True,
                    )
                )
                absolute_difference += sum(channel_difference)
                if max(channel_difference) > 24:
                    changed_pixels += 1
            pixel_count = planned.width * planned.height
            # A two-pixel caret blinking across a normal input height is well
            # below this bound. A moved/changed control, icon, or text label is
            # not. The mean-difference guard also rejects broad low-contrast
            # changes that evade the per-pixel threshold.
            return bool(
                changed_pixels <= max(24, int(pixel_count * 0.04))
                and absolute_difference <= pixel_count * 3 * 6
            )
        except Exception:
            return False

    def _click_visual_region(self, x: int, y: int) -> None:
        if self._visual_clicker is not None:
            self._visual_clicker(x, y)
            return
        try:
            from pywinauto import mouse

            mouse.click(button="left", coords=(x, y))
        except Exception as exc:
            raise WindowsUiaDriverError("visual OCR semantic click failed") from exc

    @staticmethod
    def _invoke(
        wrapper: Any,
        expected: DesktopElement,
        *,
        foreground_guard: Callable[[], None],
    ) -> str:
        capability_methods = (
            (DesktopElementAction.INVOKE, "invoke"),
            (DesktopElementAction.SELECT, "select"),
            (DesktopElementAction.TOGGLE, "toggle"),
        )
        declared_methods = (
            tuple(
                method
                for capability, method in capability_methods
                if expected.supported_actions is not None
                and capability in expected.supported_actions
            )
            if expected.supported_actions
            else ("invoke", "select", "toggle")
        )
        for name in declared_methods:
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
            if self._activate_before_execute:
                native.activate_window(snapshot.hwnd)
            native.assert_foreground(snapshot.hwnd)
            self._assert_element_usable(wrapper)
            self._assert_element_still_bound(
                wrapper,
                element,
                require_focus=True,
                trusted_root=snapshot.root,
            )
            method = self._set_value(wrapper, "")
            native.assert_foreground(snapshot.hwnd)
            return method
        except Exception:
            with self._lock:
                self._snapshots.pop(normalized, None)
            raise

    def assert_browser_address_bound(
        self,
        before: DesktopObservation,
        element: DesktopElement,
        *,
        require_focus: bool = False,
    ) -> None:
        """Revalidate private browser-chrome provenance without changing state."""

        normalized = self._normalize_app(before.app)
        with self._lock:
            snapshot = self._snapshots.get(normalized)
            if (
                snapshot is None
                or snapshot.observation.generation != before.generation
                or before.app.casefold() != normalized
                or normalized in self._pending_observation
            ):
                raise WindowsUiaStaleObservation("browser address used a stale observation")
            observed = [item for item in before.elements if item.index == element.index]
            wrapper = snapshot.wrappers.get(element.index)
            if len(observed) != 1 or observed[0] != element or wrapper is None:
                raise WindowsUiaStaleObservation("browser address binding changed")
        if not is_verified_browser_address(
            element,
            before.process_name,
            require_focused=require_focus,
        ):
            raise WindowsUiaStaleObservation("target lacks private browser-chrome provenance")
        native = self._native_backend()
        native.assert_interactive_desktop()
        window = self._resolve_window(before.app)
        if window.hwnd != snapshot.hwnd:
            raise WindowsUiaStaleObservation("browser address window changed")
        native.assert_foreground(snapshot.hwnd)
        self._assert_element_usable(wrapper)
        self._assert_element_still_bound(
            wrapper,
            element,
            require_focus=require_focus,
            trusted_root=snapshot.root,
        )
        native.assert_foreground(snapshot.hwnd)

    def read_element_state(
        self,
        before: DesktopObservation,
        element: DesktopElement,
    ) -> tuple[str | None, bool | None]:
        """Read one bound element's live value and keyboard focus without a tree walk.

        A full observation of a browser window costs seconds; a deterministic
        skill that just sent keystrokes only needs to know whether its exact
        target has focus and what it now contains. The snapshot must still be
        the one the caller planned against.
        """

        normalized = self._normalize_app(before.app)
        with self._lock:
            snapshot = self._snapshots.get(normalized)
            if (
                snapshot is None
                or snapshot.observation.generation != before.generation
                or before.app.casefold() != normalized
                or normalized in self._pending_observation
            ):
                raise WindowsUiaStaleObservation("element state read used a stale observation")
            wrapper = snapshot.wrappers.get(element.index)
        if wrapper is None or isinstance(wrapper, _VisualTargetBinding):
            raise WindowsUiaStaleObservation("element state read has no bound UIA target")
        _observed, value = self._raw_value_state(wrapper, password=element.password)
        focus = _safe_call(wrapper, "has_keyboard_focus", None)
        return value, (bool(focus) if focus is not None else None)

    def visual_region_bbox(
        self,
        before: DesktopObservation,
        element: DesktopElement,
    ) -> tuple[int, int, int, int] | None:
        """Return the screenshot-local box of one OCR text region from this snapshot.

        Deterministic skills need geometry to reason about list order (which
        entry sits under which section header) and to click through the
        frame-bound viewport; the planner-facing element deliberately carries
        only the text.
        """

        normalized = self._normalize_app(before.app)
        with self._lock:
            snapshot = self._snapshots.get(normalized)
            if snapshot is None or snapshot.observation.generation != before.generation:
                raise WindowsUiaStaleObservation("visual region read used a stale observation")
            wrapper = snapshot.wrappers.get(element.index)
        if not isinstance(wrapper, _VisualTargetBinding) or wrapper.viewport:
            return None
        return wrapper.bbox

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
        operation_cancel = threading.Event()
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
            self._active_cancellations.add(operation_cancel)
        try:
            self._raise_if_cancelled(cancel_event, operation_cancel)
            native = self._native_backend()
            native.assert_interactive_desktop()
            window = self._resolve_window(action.app)
            if window.hwnd != snapshot.hwnd:
                raise WindowsUiaStaleObservation("the selected application window changed")
            if self._activate_before_execute:
                native.activate_window(snapshot.hwnd)
            native.assert_foreground(snapshot.hwnd)
            method = ""
            after_local_window_id: str | None = None
            accepted_foreground_hwnd = snapshot.hwnd
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
                if not isinstance(wrapper, _VisualTargetBinding):
                    with self._lock:
                        self._visual_text_click.pop(snapshot.hwnd, None)
                        self._visual_text_focus.pop(snapshot.hwnd, None)
                    self._assert_element_usable(wrapper)

            if isinstance(wrapper, _VisualTargetBinding):
                assert expected_element is not None
                if not expected_element.visual_ocr:
                    raise WindowsUiaStaleObservation(
                        "visual target lost its immutable source marker"
                    )
                bbox, rebound_screenshot_png = self._rebind_visual_target(
                    wrapper,
                    expected_element,
                )
                native.assert_foreground(snapshot.hwnd)
                if action.type == DesktopActionType.CLICK:
                    with self._lock:
                        self._visual_text_click.pop(snapshot.hwnd, None)
                        self._visual_text_focus.pop(snapshot.hwnd, None)
                    if (
                        action.mouse_button not in {None, "left"}
                        or action.click_count not in {None, 1}
                    ):
                        raise WindowsUiaDriverError(
                            "only one rebound visual left click is enabled"
                        )
                    if (
                        expected_element.supported_actions is None
                        or DesktopElementAction.CLICK
                        not in expected_element.supported_actions
                    ):
                        raise WindowsUiaDriverError(
                            "visual target does not declare semantic click support"
                        )
                    if wrapper.viewport:
                        if not snapshot.observation.local_window_id:
                            raise WindowsUiaStaleObservation(
                                "visual text focus requires an exact local window identity"
                            )
                        if action.x is None or action.y is None:
                            raise WindowsUiaDriverError(
                                "visual viewport click requires one screenshot-local x/y point"
                            )
                        if (
                            not float(action.x).is_integer()
                            or not float(action.y).is_integer()
                        ):
                            raise WindowsUiaDriverError(
                                "visual viewport point must use integer pixels"
                            )
                        local_x = int(action.x)
                        local_y = int(action.y)
                        if not (0 <= local_x < bbox[2] and 0 <= local_y < bbox[3]):
                            raise WindowsUiaDriverError(
                                "visual viewport point escapes the exact screenshot"
                            )
                        patch_still_matches = self._visual_point_patch_still_matches(
                            wrapper.screenshot_png,
                            rebound_screenshot_png,
                            x=local_x,
                            y=local_y,
                            width=bbox[2],
                            height=bbox[3],
                        )
                        if not patch_still_matches:
                            raise WindowsUiaStaleObservation(
                                "visual viewport target patch materially changed after planning; "
                                "a new observation is required"
                            )
                    else:
                        if action.x is not None or action.y is not None:
                            raise WindowsUiaDriverError(
                                "OCR text targets are clicked only at their rebound center"
                            )
                        left, top, right, bottom = bbox
                        local_x = (left + right) // 2
                        local_y = (top + bottom) // 2
                    window_left, window_top, window_right, window_bottom = (
                        wrapper.window_rect
                    )
                    x = window_left + local_x
                    y = window_top + local_y
                    if not (
                        window_left <= x < window_right
                        and window_top <= y < window_bottom
                    ):
                        raise WindowsUiaStaleObservation(
                            "visual target center escapes the exact target window"
                        )
                    native.assert_foreground(snapshot.hwnd)
                    self._click_visual_region(x, y)
                    if wrapper.viewport:
                        with self._lock:
                            self._visual_text_click[snapshot.hwnd] = (
                                _VisualTextClickBinding(
                                    hwnd=snapshot.hwnd,
                                    local_window_id=(
                                        snapshot.observation.local_window_id
                                    ),
                                    window_rect=wrapper.window_rect,
                                    x=local_x,
                                    y=local_y,
                                )
                            )
                    method = (
                        "fresh-frame bound visual point click"
                        if wrapper.viewport
                        else "fresh-frame rebound visual semantic click"
                    )
                elif action.type == DesktopActionType.TYPE_TEXT:
                    if not wrapper.viewport:
                        raise WindowsUiaDriverError(
                            "visual text input requires the exact visual viewport"
                        )
                    if (
                        expected_element.supported_actions is None
                        or DesktopElementAction.TYPE_TEXT
                        not in expected_element.supported_actions
                    ):
                        raise WindowsUiaDriverError(
                            "visual viewport text input was not armed by a prior point click"
                        )
                    assert action.text is not None
                    with self._lock:
                        focus_binding = self._visual_text_focus.get(snapshot.hwnd)
                    if (
                        focus_binding is None
                        or not snapshot.observation.local_window_id
                        or focus_binding.click.local_window_id
                        != snapshot.observation.local_window_id
                    ):
                        raise WindowsUiaStaleObservation(
                            "visual text focus binding is absent or belongs to another window"
                        )
                    if not (
                        0 <= focus_binding.click.x < bbox[2]
                        and 0 <= focus_binding.click.y < bbox[3]
                        and focus_binding.click.window_rect == wrapper.window_rect
                    ):
                        raise WindowsUiaStaleObservation(
                            "visual text focus point escapes the current screenshot"
                        )
                    native.assert_foreground(snapshot.hwnd)
                    self._assert_visual_text_focus(focus_binding)
                    try:
                        native.send_text(action.text)
                    finally:
                        with self._lock:
                            self._visual_text_click.pop(snapshot.hwnd, None)
                            self._visual_text_focus.pop(snapshot.hwnd, None)
                    self._assert_visual_text_focus(
                        focus_binding,
                        allow_horizontal_drift=True,
                    )
                    method = (
                        "single-use exact user-authored visual text input with "
                        "native focus/caret binding"
                    )
                elif action.type == DesktopActionType.SCROLL:
                    with self._lock:
                        self._visual_text_click.pop(snapshot.hwnd, None)
                        self._visual_text_focus.pop(snapshot.hwnd, None)
                    if not wrapper.viewport:
                        raise WindowsUiaDriverError(
                            "visual scrolling requires the exact visual viewport"
                        )
                    if (
                        expected_element.supported_actions is None
                        or DesktopElementAction.SCROLL
                        not in expected_element.supported_actions
                    ):
                        raise WindowsUiaDriverError(
                            "visual viewport does not declare scroll support"
                        )
                    if action.direction not in {"up", "down"}:
                        raise WindowsUiaDriverError(
                            "visual viewport permits vertical scrolling only"
                        )
                    if math.ceil(float(action.pages or 1)) != 1:
                        raise WindowsUiaDriverError(
                            "visual viewport scroll is limited to one fresh page"
                        )
                    native.assert_foreground(snapshot.hwnd)
                    native.send_hotkey("pageup" if action.direction == "up" else "pagedown")
                    method = "fresh-frame rebound visual page scroll"
                else:
                    raise WindowsUiaDriverError(
                        "visual targets permit click, viewport scroll, or one armed search text; "
                        "keys, secondary actions, drag, Enter, and send are disabled"
                    )
                with self._lock:
                    self._pending_visual_change[snapshot.hwnd] = _PendingVisualChange(
                        window=window,
                        frame_sha256=hashlib.sha256(
                            rebound_screenshot_png
                        ).hexdigest(),
                    )
            elif action.type == DesktopActionType.CLICK:
                if wrapper is None:
                    raise WindowsUiaDriverError("coordinate clicks are disabled")
                if action.mouse_button not in {None, "left"} or action.click_count not in {None, 1}:
                    raise WindowsUiaDriverError("only one semantic left click is enabled")
                assert expected_element is not None
                self._assert_element_still_bound(
                    wrapper,
                    expected_element,
                    trusted_root=snapshot.root,
                )
                if element_plane(expected_element) == ElementPlane.INPUT:
                    method = self._physical_focus(
                        wrapper,
                        foreground_guard=lambda: native.assert_foreground(snapshot.hwnd),
                    )
                else:
                    method = self._invoke(
                        wrapper,
                        expected_element,
                        foreground_guard=lambda: native.assert_foreground(snapshot.hwnd),
                    )
            elif action.type == DesktopActionType.PERFORM_SECONDARY_ACTION:
                assert wrapper is not None and action.action_name is not None
                assert expected_element is not None
                self._assert_element_still_bound(
                    wrapper,
                    expected_element,
                    trusted_root=snapshot.root,
                )
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
                    raise WindowsUiaDriverError("type_text target is not a verified editable input")
                self._assert_element_still_bound(
                    wrapper,
                    expected_element,
                    require_focus=True,
                    trusted_root=snapshot.root,
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
                    raise WindowsUiaDriverError("set_value target is not a verified editable input")
                self._assert_element_still_bound(
                    wrapper,
                    expected_element,
                    trusted_root=snapshot.root,
                )
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
                    trusted_root=snapshot.root,
                )
                native.assert_foreground(snapshot.hwnd)
                native.send_hotkey(action.key)
                method = "allow-listed SendInput hotkey"
            elif action.type == DesktopActionType.SCROLL:
                assert wrapper is not None and action.direction is not None
                assert expected_element is not None
                self._assert_element_still_bound(
                    wrapper,
                    expected_element,
                    trusted_root=snapshot.root,
                )
                method_fn = _safe_attr(wrapper, "scroll", None)
                if not callable(method_fn):
                    raise WindowsUiaDriverError("target element does not expose UIA scrolling")
                method_fn(
                    action.direction,
                    "page",
                    count=max(1, math.ceil(float(action.pages or 1))),
                )
                method = "UIA scroll"
            elif action.type == DesktopActionType.DRAG:
                raise WindowsUiaDriverError("coordinate drag is disabled")
            else:  # pragma: no cover - enum exhaustiveness guard
                raise WindowsUiaDriverError("unsupported desktop action")
            if action.type in {
                DesktopActionType.CLICK,
                DesktopActionType.PERFORM_SECONDARY_ACTION,
                DesktopActionType.PRESS_KEY,
            }:
                same_window_browser_navigation = (
                    self._same_window_browser_navigation_fast_path
                    and self._is_same_window_browser_address_navigation(
                        action,
                        snapshot.observation,
                        expected_element,
                    )
                )
                transitioned = self._wait_for_related_foreground_transition(
                    native,
                    window,
                    timeout=(
                        0.0
                        if same_window_browser_navigation
                        else 2.0
                        if action.type == DesktopActionType.PRESS_KEY
                        else 0.35
                    ),
                    cancel_event=cancel_event,
                    operation_cancel=operation_cancel,
                )
                if transitioned is not None:
                    after_local_window_id = self._bind_related_dynamic_window_transition(
                        app=normalized,
                        before_window=window,
                        after_window=transitioned,
                    )
                    if after_local_window_id is None:
                        raise WindowsUiaStaleObservation(
                            "related foreground transition was not bound to the exact app"
                        )
                    accepted_foreground_hwnd = transitioned.hwnd
                    # Rendered helper windows (notably WeChat's separate
                    # WeChatAppEx search surface) expose their top-level HWND
                    # before the actionable document tree has loaded.  A
                    # precision-first controller should wait for that bounded
                    # transition instead of planning against the transient
                    # shell and guessing a coordinate.
                    self._sleep_interruptibly(
                        2.0,
                        cancel_event=cancel_event,
                        operation_cancel=operation_cancel,
                    )
            native.assert_foreground(accepted_foreground_hwnd)
        except Exception:
            with self._lock:
                self._snapshots.pop(normalized, None)
                pending_hwnds = {snapshot.hwnd}
                current_binding = self._dynamic_windows.get(normalized)
                if current_binding is not None:
                    pending_hwnds.add(current_binding.window.hwnd)
                for pending_hwnd in pending_hwnds:
                    self._pending_visual_change.pop(pending_hwnd, None)
                self._visual_text_click.pop(snapshot.hwnd, None)
                self._visual_text_focus.pop(snapshot.hwnd, None)
            raise
        finally:
            with self._lock:
                self._active_cancellations.discard(operation_cancel)
        return ActionReceipt(
            action=action,
            accepted=True,
            before_generation=before.generation,
            driver_message=f"Windows UIA accepted one atomic action via {method}",
            after_local_window_id=after_local_window_id,
        )

    def cancel(self) -> bool:
        with self._lock:
            active_cancellations = tuple(self._active_cancellations)
        for operation_cancel in active_cancellations:
            operation_cancel.set()
        return bool(active_cancellations)

    def close(self) -> None:
        with self._lock:
            self._snapshots.clear()
            self._pending_observation.clear()
            self._pending_visual_change.clear()
            self._visual_text_click.clear()
            self._visual_text_focus.clear()
            self._dynamic_windows.clear()
