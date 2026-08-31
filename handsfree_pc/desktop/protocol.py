from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from threading import Event
from typing import Any, Protocol, runtime_checkable


class CredentialConfidence(StrEnum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class CredentialFinding:
    """A content-free credential classification safe to log and aggregate."""

    kind: str
    confidence: CredentialConfidence
    field: str
    element_index: str | None = None


class ElementPlane(StrEnum):
    CONTROL = "control"
    CONTENT = "content"
    INPUT = "input"
    DIALOG = "dialog"


@dataclass(frozen=True, slots=True)
class BoundedUiText:
    """A display-safe UIA string plus an exact local identity for the original.

    The original text is intentionally not retained.  Its digest lets the
    driver detect stale bindings even when a long label or value is represented
    by a bounded display string.
    """

    display: str
    original_length: int
    sha256: str
    truncated: bool

    def __post_init__(self) -> None:
        _validate_unicode(self.display, label="bounded UI text", maximum=16000)
        if self.original_length < 0:
            raise ValueError("bounded UI text original_length must be non-negative")
        if not re.fullmatch(r"[0-9a-f]{64}", self.sha256):
            raise ValueError("bounded UI text sha256 must be a lowercase SHA-256 digest")
        if not self.truncated:
            if self.original_length != len(self.display):
                raise ValueError("untruncated UI text length must match its display")
            digest = hashlib.sha256(
                self.display.encode("utf-8", errors="surrogatepass")
            ).hexdigest()
            if digest != self.sha256:
                raise ValueError("untruncated UI text digest must match its display")

    @classmethod
    def from_text(cls, value: str, *, maximum: int) -> BoundedUiText:
        if maximum < 1:
            raise ValueError("bounded UI text maximum must be positive")
        digest = hashlib.sha256(value.encode("utf-8", errors="surrogatepass")).hexdigest()
        if len(value) <= maximum:
            return cls(value, len(value), digest, False)
        # Length and digest live in typed metadata; repeating them inside the
        # display would let the marker itself exceed a small configured bound.
        if maximum == 1:
            display = "…"
        else:
            visible = maximum - 1
            head = max(1, (visible * 2) // 3)
            tail = max(0, visible - head)
            display = value[:head] + "…" + (value[-tail:] if tail else "")
        assert len(display) <= maximum
        return cls(display, len(value), digest, True)

    def payload(self) -> dict[str, Any]:
        return {
            "display": self.display,
            "original_length": self.original_length,
            "sha256": self.sha256,
            "truncated": self.truncated,
        }


_EXPLICIT_CREDENTIAL_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----", re.IGNORECASE)),
    (
        "openai_api_key",
        re.compile(r"(?<![A-Za-z0-9_-])sk-(?:proj-)?[A-Za-z0-9_-]{16,}", re.IGNORECASE),
    ),
    (
        "github_token",
        re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{20,}", re.IGNORECASE),
    ),
    (
        "github_token",
        re.compile(
            r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{20,}(?![A-Za-z0-9_])",
            re.IGNORECASE,
        ),
    ),
    ("aws_access_key", re.compile(r"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])")),
    (
        "bearer_token",
        re.compile(
            r"\b(?:authorization\s*:\s*)?bearer\s+[A-Za-z0-9._~+/=-]{16,}",
            re.IGNORECASE,
        ),
    ),
)
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])([A-Za-z0-9_-]{8,})\.([A-Za-z0-9_-]{8,})\."
    r"([A-Za-z0-9_-]{8,})(?![A-Za-z0-9_-])"
)
# Backwards-compatible private name used by older tests and downstream imports.
_CREDENTIAL_VALUE_PATTERNS = tuple(pattern for _, pattern in _EXPLICIT_CREDENTIAL_PATTERNS)
_TOKEN_SHAPE_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z0-9_+/=.-]{40,512}(?![A-Za-z0-9_])")


def _token_shaped(candidate: str) -> bool:
    classes = sum(
        (
            any(character.islower() for character in candidate),
            any(character.isupper() for character in candidate),
            any(character.isdigit() for character in candidate),
            any(not character.isalnum() for character in candidate),
        )
    )
    return classes >= 3 and len(set(candidate)) >= 16


def _valid_jwt_segment(value: str) -> bool:
    try:
        padding = "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
        return isinstance(json.loads(decoded.decode("utf-8")), dict)
    except (UnicodeError, ValueError, json.JSONDecodeError):
        return False


def _credential_matches(
    value: str,
    *,
    include_low: bool = True,
) -> list[tuple[int, int, str, CredentialConfidence]]:
    matches: list[tuple[int, int, str, CredentialConfidence]] = []
    for kind, pattern in _EXPLICIT_CREDENTIAL_PATTERNS:
        matches.extend(
            (match.start(), match.end(), kind, CredentialConfidence.HIGH)
            for match in pattern.finditer(value)
        )
    for match in _JWT_RE.finditer(value):
        if _valid_jwt_segment(match.group(1)) and _valid_jwt_segment(match.group(2)):
            matches.append((match.start(), match.end(), "jwt", CredentialConfidence.HIGH))
    if include_low:
        for match in _TOKEN_SHAPE_RE.finditer(value):
            if _token_shaped(match.group(0)):
                matches.append(
                    (match.start(), match.end(), "opaque_identifier", CredentialConfidence.LOW)
                )
    # Prefer high-confidence matches when a broad opaque-identifier span overlaps.
    matches.sort(
        key=lambda item: (
            item[0],
            0 if item[3] == CredentialConfidence.HIGH else 1,
            -item[1],
        )
    )
    selected: list[tuple[int, int, str, CredentialConfidence]] = []
    for candidate in matches:
        start, end, _, confidence = candidate
        overlaps = [item for item in selected if not (end <= item[0] or start >= item[1])]
        if not overlaps:
            selected.append(candidate)
            continue
        if confidence == CredentialConfidence.HIGH and all(
            item[3] == CredentialConfidence.LOW for item in overlaps
        ):
            selected = [item for item in selected if item not in overlaps]
            selected.append(candidate)
    return sorted(selected, key=lambda item: item[0])


def credential_findings(
    value: str | None,
    *,
    field: str = "text",
    element_index: str | None = None,
    include_low: bool = True,
) -> tuple[CredentialFinding, ...]:
    if not value:
        return ()
    return tuple(
        CredentialFinding(kind, confidence, field, element_index)
        for _, _, kind, confidence in _credential_matches(value, include_low=include_low)
    )


def contains_credential_like_value(value: str) -> bool:
    """Compatibility predicate; callers must inspect confidence before blocking."""

    return bool(credential_findings(value))


def contains_high_confidence_credential(value: str | None) -> bool:
    return any(
        finding.confidence == CredentialConfidence.HIGH
        for finding in credential_findings(value, include_low=False)
    )


def redact_credential_like_text(value: str | None) -> str | None:
    if value is None:
        return None
    matches = _credential_matches(value)
    if not matches:
        return value
    output: list[str] = []
    cursor = 0
    for start, end, _, confidence in matches:
        output.append(value[cursor:start])
        output.append(
            "<redacted-credential>"
            if confidence == CredentialConfidence.HIGH
            else "<redacted-opaque-id>"
        )
        cursor = end
    output.append(value[cursor:])
    return "".join(output)


class DesktopActionType(StrEnum):
    CLICK = "click"
    PERFORM_SECONDARY_ACTION = "perform_secondary_action"
    SCROLL = "scroll"
    DRAG = "drag"
    TYPE_TEXT = "type_text"
    PRESS_KEY = "press_key"
    SET_VALUE = "set_value"


class DesktopDecisionKind(StrEnum):
    OBSERVE = "observe"
    ACTION = "action"
    DONE = "done"
    FAIL = "fail"


class DesktopExpectationKind(StrEnum):
    APP_VISIBLE = "app_visible"
    TEXT_PRESENT = "text_present"
    TEXT_ABSENT = "text_absent"
    FOCUSED_CONTAINS = "focused_contains"
    ELEMENT_SELECTED = "element_selected"
    LAST_ACTION_VERIFIED = "last_action_verified"


ALLOWED_DESKTOP_KEYS = frozenset(
    {
        "tab",
        "shift+tab",
        "enter",
        "return",
        "escape",
        "space",
        "pageup",
        "pagedown",
        "home",
        "end",
        "left",
        "up",
        "right",
        "down",
    }
)


def normalized_desktop_key(value: str) -> str:
    return "+".join(part.strip().casefold() for part in value.split("+"))


def is_allowed_desktop_key(value: str | None) -> bool:
    return isinstance(value, str) and normalized_desktop_key(value) in ALLOWED_DESKTOP_KEYS


def _validate_text(value: str, *, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    _validate_unicode(value, label=label, maximum=maximum)
    return value.strip()


def _validate_unicode(
    value: str,
    *,
    label: str,
    maximum: int,
    allow_empty: bool = True,
) -> str:
    """Reject lossy or spoofable strings before hashing or crossing a CLI boundary."""

    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"{label} must be non-empty")
    if len(value) > maximum:
        raise ValueError(f"{label} exceeds {maximum} characters")
    if "\ufffd" in value:
        raise ValueError(f"{label} contains damaged Unicode")
    if any(
        unicodedata.category(character).startswith("C") and character not in {"\n", "\t"}
        for character in value
    ):
        raise ValueError(f"{label} contains unsupported control characters")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} is not valid UTF-8 text") from exc
    return value


@dataclass(frozen=True, slots=True)
class DesktopElement:
    """Sanitized UI Automation metadata bound to one observation."""

    index: str
    name: str
    control_type: str
    automation_id: str | None = None
    value: str | None = None
    # Distinguishes a positively observed empty value from an unsupported or
    # unreadable ValuePattern. Draft smoke may write only to the former.
    value_observed: bool = False
    selected: bool | None = None
    focused: bool | None = None
    password: bool = False
    enabled: bool = True
    # A driver-local digest of a stable accessibility identity (for example a
    # UIA RuntimeId). It is verifier evidence only and is never planner data.
    local_identity: str | None = None
    # Drivers may explicitly promote an otherwise content-like UIA type (most
    # notably an unfocused, profile-named Electron Document composer) onto the
    # input plane. This semantic classification contains no UI text and remains
    # bound to the immutable observation.
    plane: ElementPlane | None = None
    # ``editable`` is a positive driver observation for Document inputs.  None
    # preserves compatibility for ordinary Edit elements constructed by older
    # callers; a Document is never inferred editable from focus alone.
    editable: bool | None = None
    # Truncated labels remain visible for diagnostics and freshness but cannot
    # be used as planner action targets.
    addressable: bool = True
    # True only when the driver followed an explicit UIA LabeledBy relation to
    # a short credential-field label. The label text itself remains local.
    secret_labeled: bool = False
    # Positive, app-profile-aware composer classification. Focus or RuntimeId
    # alone is never enough to set this flag.
    composer: bool = False
    high_credential: bool = False
    low_credential: bool = False
    name_metadata: BoundedUiText | None = None
    value_metadata: BoundedUiText | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.index, str) or not self.index.isdecimal():
            raise ValueError("desktop element index must contain only decimal digits")
        _validate_unicode(self.name, label="desktop element name", maximum=1024)
        _validate_unicode(
            self.control_type,
            label="desktop element control_type",
            maximum=128,
            allow_empty=False,
        )
        if self.automation_id is not None:
            _validate_unicode(
                self.automation_id,
                label="desktop element automation_id",
                maximum=1024,
            )
        if self.value is not None:
            _validate_unicode(self.value, label="desktop element value", maximum=16000)
            if not self.value_observed:
                # Direct callers historically supplied value without a
                # separate readability flag. Preserve that unambiguous case.
                object.__setattr__(self, "value_observed", True)
        if not isinstance(self.value_observed, bool):
            raise ValueError("desktop element value_observed must be a boolean")
        if self.local_identity is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.local_identity
        ):
            raise ValueError("desktop element local_identity must be a SHA-256 digest")
        if self.plane is not None and not isinstance(self.plane, ElementPlane):
            raise ValueError("desktop element plane must be an ElementPlane value")
        if self.editable is not None and not isinstance(self.editable, bool):
            raise ValueError("desktop element editable must be a boolean or null")
        if not isinstance(self.addressable, bool):
            raise ValueError("desktop element addressable must be a boolean")
        if not isinstance(self.secret_labeled, bool):
            raise ValueError("desktop element secret_labeled must be a boolean")
        if not isinstance(self.composer, bool):
            raise ValueError("desktop element composer must be a boolean")
        if not isinstance(self.high_credential, bool) or not isinstance(
            self.low_credential,
            bool,
        ):
            raise ValueError("desktop element credential flags must be booleans")
        if self.name_metadata is not None:
            if not isinstance(self.name_metadata, BoundedUiText):
                raise ValueError("desktop element name_metadata must be BoundedUiText")
            if self.name_metadata.display != self.name:
                raise ValueError("desktop element name must match bounded name display")
        if self.value_metadata is not None:
            if not isinstance(self.value_metadata, BoundedUiText):
                raise ValueError("desktop element value_metadata must be BoundedUiText")
            if self.value_metadata.display != self.value:
                raise ValueError("desktop element value must match bounded value display")
        if self.plane == ElementPlane.INPUT and self.editable is False and not self.password:
            raise ValueError("non-editable desktop elements cannot use the input plane")
        if self.password and self.value:
            raise ValueError("password element values must never enter an observation")

    def planner_payload(self) -> dict[str, Any]:
        # Automation IDs are local verifier identities, not planning context.
        # Keeping them off the cloud surface also avoids sending framework-
        # generated UUIDs that can resemble credentials.
        return {
            "index": self.index,
            "name": redact_credential_like_text(self.name),
            "control_type": self.control_type,
            "plane": element_plane(self).value,
            # Element values may contain chat history, draft text, document
            # bodies, or credentials. They stay local for verification and
            # freshness but are never planner context.
            "value": None,
            "value_observed": self.value_observed,
            "selected": self.selected,
            "focused": self.focused,
            "password": self.password,
            "enabled": self.enabled,
            "editable": self.editable,
            "addressable": self.addressable,
            "composer": self.composer,
            # Credential classifications remain local. Labels have already
            # been redacted and values are never planner payload.
        }

    def fingerprint_payload(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "control_type": self.control_type,
            "automation_id": self.automation_id,
            "value": self.value,
            "value_observed": self.value_observed,
            "selected": self.selected,
            "focused": self.focused,
            "password": self.password,
            "enabled": self.enabled,
            "local_identity": self.local_identity,
            "plane": element_plane(self).value,
            "editable": self.editable,
            "addressable": self.addressable,
            "secret_labeled": self.secret_labeled,
            "composer": self.composer,
            "high_credential": self.high_credential,
            "low_credential": self.low_credential,
            "name_metadata": (
                self.name_metadata.payload() if self.name_metadata is not None else None
            ),
            "value_metadata": (
                self.value_metadata.payload() if self.value_metadata is not None else None
            ),
        }


_CONTROL_PLANE_TYPES = frozenset(
    {
        "button",
        "tabitem",
        "menuitem",
        "checkbox",
        "radiobutton",
        "listitem",
        "treeitem",
        "combobox",
        "hyperlink",
        "thumb",
        "scrollbar",
    }
)
_INPUT_PLANE_TYPES = frozenset({"edit", "spinner"})
_DIALOG_PLANE_TYPES = frozenset({"dialog", "window"})
_CONTENT_PLANE_TYPES = frozenset({"text", "document", "pane", "group", "image", "dataitem"})


def element_plane(element: DesktopElement) -> ElementPlane:
    """Classify UIA metadata without interpreting untrusted element text."""

    control_type = element.control_type.strip().casefold()
    if element.password:
        return ElementPlane.INPUT
    if element.plane is not None:
        return element.plane
    if control_type in _DIALOG_PLANE_TYPES:
        return ElementPlane.DIALOG
    if control_type in _INPUT_PLANE_TYPES and element.editable is not False:
        return ElementPlane.INPUT
    if control_type in _CONTROL_PLANE_TYPES:
        return ElementPlane.CONTROL
    if control_type in _CONTENT_PLANE_TYPES:
        return ElementPlane.CONTENT
    return ElementPlane.CONTENT


@dataclass(frozen=True, slots=True)
class DesktopObservation:
    """One immutable point-in-time snapshot of exactly one application window."""

    app: str
    generation: int
    accessibility_text: str
    screenshot_png: bytes | None = None
    window_title: str | None = None
    elements: tuple[DesktopElement, ...] = ()
    captured_at: float = field(default_factory=time.monotonic)
    local_window_id: str | None = None
    total_element_count: int | None = None
    elements_truncated: bool = False
    skipped_long_content_count: int = 0
    property_error_count: int = 0
    # Content-free credential diagnostics are counted before UI elements are
    # bounded or filtered.  ``None`` distinguishes an older/non-UIA producer
    # from a producer that positively observed zero findings.
    high_credential_count: int | None = None
    low_credential_count: int | None = None
    credential_affected_element_count: int | None = None

    def __post_init__(self) -> None:
        _validate_text(self.app, label="observation app", maximum=256)
        if self.generation < 1:
            raise ValueError("observation generation must be positive")
        _validate_unicode(
            self.accessibility_text,
            label="accessibility_text",
            maximum=1_000_000,
        )
        if self.screenshot_png is not None and not isinstance(self.screenshot_png, bytes):
            raise ValueError("screenshot_png must be bytes or null")
        if self.window_title is not None:
            _validate_unicode(self.window_title, label="window_title", maximum=4096)
        if any(not isinstance(item, DesktopElement) for item in self.elements):
            raise ValueError("observation elements must contain DesktopElement values")
        if self.local_window_id is not None:
            _validate_unicode(
                self.local_window_id,
                label="local_window_id",
                maximum=256,
                allow_empty=False,
            )
        if self.total_element_count is not None and self.total_element_count < len(self.elements):
            raise ValueError("total_element_count cannot be smaller than retained elements")
        if not isinstance(self.elements_truncated, bool):
            raise ValueError("elements_truncated must be a boolean")
        if self.skipped_long_content_count < 0 or self.property_error_count < 0:
            raise ValueError("desktop observation counters must be non-negative")
        credential_counts = (
            self.high_credential_count,
            self.low_credential_count,
            self.credential_affected_element_count,
        )
        if any(value is not None and value < 0 for value in credential_counts):
            raise ValueError("desktop credential counters must be non-negative")
        if any(value is None for value in credential_counts) and any(
            value is not None for value in credential_counts
        ):
            raise ValueError("desktop credential counters must be supplied together")
        indexes = [item.index for item in self.elements]
        if len(indexes) != len(set(indexes)):
            raise ValueError("observation element indexes must be unique")

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        digest.update(self.app.casefold().encode("utf-8"))
        digest.update(b"\0")
        digest.update((self.window_title or "").encode("utf-8"))
        digest.update(b"\0local-window\0")
        digest.update((self.local_window_id or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(self.accessibility_text.encode("utf-8"))
        digest.update(
            json.dumps(
                {
                    "elements_truncated": self.elements_truncated,
                    "property_error_count": self.property_error_count,
                    "skipped_long_content_count": self.skipped_long_content_count,
                    "total_element_count": self.total_element_count,
                    "high_credential_count": self.high_credential_count,
                    "low_credential_count": self.low_credential_count,
                    "credential_affected_element_count": (
                        self.credential_affected_element_count
                    ),
                },
                sort_keys=True,
            ).encode("utf-8")
        )
        digest.update(
            json.dumps(
                [item.fingerprint_payload() for item in self.elements],
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        )
        if self.screenshot_png is not None:
            digest.update(b"\0image\0")
            digest.update(self.screenshot_png)
        return digest.hexdigest()

    def planner_context(self, *, max_chars: int) -> dict[str, Any]:
        if max_chars < 1000:
            raise ValueError("max_chars must be at least 1000")
        # Accessibility text and structured elements repeat much of the same UIA
        # state. Bound their *combined serialized size* so a large element tree
        # cannot bypass max_observation_chars.
        text_budget = max(1, max_chars // 2)
        text = self.accessibility_text[:text_budget]
        payload: dict[str, Any] = {
            "app": self.app,
            "generation": self.generation,
            "window_title": redact_credential_like_text(self.window_title),
            "accessibility_text": redact_credential_like_text(text),
            "accessibility_truncated": len(text) < len(self.accessibility_text),
            "elements": [],
            "elements_truncated": False,
            "screenshot_available": self.screenshot_png is not None,
            "observation_stats": {
                "total_element_count": self.total_element_count,
                "elements_truncated": self.elements_truncated,
                "skipped_long_content_count": self.skipped_long_content_count,
                "property_error_count": self.property_error_count,
                "high_credential_count": self.high_credential_count,
                "low_credential_count": self.low_credential_count,
                "credential_affected_element_count": (
                    self.credential_affected_element_count
                ),
            },
        }
        for item in self.elements:
            candidate = [*payload["elements"], item.planner_payload()]
            payload["elements"] = candidate
            if len(json.dumps(payload, ensure_ascii=False, sort_keys=True)) > max_chars:
                payload["elements"].pop()
                payload["elements_truncated"] = True
                break
        while len(json.dumps(payload, ensure_ascii=False, sort_keys=True)) > max_chars and text:
            overflow = len(json.dumps(payload, ensure_ascii=False, sort_keys=True)) - max_chars
            text = text[: max(0, len(text) - max(1, overflow))]
            payload["accessibility_text"] = text
            payload["accessibility_truncated"] = True
        if len(json.dumps(payload, ensure_ascii=False, sort_keys=True)) > max_chars:
            raise ValueError("max_chars is too small for desktop observation metadata")
        return payload


@dataclass(frozen=True, slots=True)
class DesktopAction:
    """One allow-listed desktop action tied to a fresh observation generation."""

    type: DesktopActionType
    app: str
    generation: int
    element_index: str | None = None
    x: float | None = None
    y: float | None = None
    click_count: int | None = None
    mouse_button: str | None = None
    direction: str | None = None
    pages: float | None = None
    from_x: float | None = None
    from_y: float | None = None
    to_x: float | None = None
    to_y: float | None = None
    action_name: str | None = None
    text: str | None = None
    key: str | None = None
    value: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.type, DesktopActionType):
            raise ValueError("desktop action type is invalid")
        _validate_text(self.app, label="desktop action app", maximum=256)
        if self.generation < 1:
            raise ValueError("desktop action generation must be positive")
        self._validate_fields()

    def _validate_fields(self) -> None:
        fields = {
            "element_index": self.element_index,
            "x": self.x,
            "y": self.y,
            "click_count": self.click_count,
            "mouse_button": self.mouse_button,
            "direction": self.direction,
            "pages": self.pages,
            "from_x": self.from_x,
            "from_y": self.from_y,
            "to_x": self.to_x,
            "to_y": self.to_y,
            "action_name": self.action_name,
            "text": self.text,
            "key": self.key,
            "value": self.value,
        }
        applicable = {
            DesktopActionType.CLICK: {
                "element_index",
                "x",
                "y",
                "click_count",
                "mouse_button",
            },
            DesktopActionType.PERFORM_SECONDARY_ACTION: {"element_index", "action_name"},
            DesktopActionType.SCROLL: {"element_index", "direction", "pages"},
            DesktopActionType.DRAG: {"from_x", "from_y", "to_x", "to_y"},
            DesktopActionType.TYPE_TEXT: {"element_index", "text"},
            DesktopActionType.PRESS_KEY: {"element_index", "key"},
            DesktopActionType.SET_VALUE: {"element_index", "value"},
        }[self.type]
        unexpected = sorted(
            name for name, value in fields.items() if value is not None and name not in applicable
        )
        if unexpected:
            raise ValueError(
                f"desktop action {self.type.value} has inapplicable fields: {unexpected}"
            )
        coordinates = (self.x, self.y, self.from_x, self.from_y, self.to_x, self.to_y)
        if any(
            value is not None and (not math.isfinite(value) or value < 0) for value in coordinates
        ):
            raise ValueError("desktop coordinates must be finite non-negative numbers")
        if self.element_index is not None and not self.element_index.isdecimal():
            raise ValueError("element_index must contain only decimal digits")
        if self.type == DesktopActionType.CLICK:
            if self.element_index is None and (self.x is None or self.y is None):
                raise ValueError("click requires element_index or x/y")
            if self.click_count is not None and self.click_count not in {1, 2}:
                raise ValueError("click_count must be 1 or 2")
            if self.mouse_button is not None and self.mouse_button not in {
                "left",
                "right",
                "middle",
            }:
                raise ValueError("mouse_button is invalid")
        elif self.type == DesktopActionType.PERFORM_SECONDARY_ACTION:
            if self.element_index is None:
                raise ValueError("perform_secondary_action requires element_index")
            _validate_text(self.action_name or "", label="secondary action", maximum=120)
        elif self.type == DesktopActionType.SCROLL:
            if self.element_index is None:
                raise ValueError("scroll requires element_index")
            if self.direction not in {"up", "down", "left", "right"}:
                raise ValueError("scroll direction is invalid")
            if self.pages is not None and (
                not math.isfinite(self.pages) or not 0 < self.pages <= 10
            ):
                raise ValueError("scroll pages must be in (0, 10]")
        elif self.type == DesktopActionType.DRAG:
            if any(value is None for value in (self.from_x, self.from_y, self.to_x, self.to_y)):
                raise ValueError("drag requires from_x/from_y/to_x/to_y")
        elif self.type == DesktopActionType.TYPE_TEXT:
            if self.element_index is None:
                raise ValueError("type_text requires the observed focused element_index")
            _validate_text(self.text or "", label="type_text text", maximum=4000)
        elif self.type == DesktopActionType.PRESS_KEY:
            if self.element_index is None:
                raise ValueError("press_key requires the observed focused element_index")
            _validate_text(self.key or "", label="press_key key", maximum=120)
            if not is_allowed_desktop_key(self.key):
                raise ValueError("press_key key is outside the navigation-only allow-list")
        elif self.type == DesktopActionType.SET_VALUE:
            if self.element_index is None:
                raise ValueError("set_value requires element_index")
            _validate_text(self.value or "", label="set_value value", maximum=4000)

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, app: str, generation: int) -> DesktopAction:
        if not isinstance(value, dict):
            raise ValueError("desktop action must be an object")
        allowed = {
            "type",
            "element_index",
            "x",
            "y",
            "click_count",
            "mouse_button",
            "direction",
            "pages",
            "from_x",
            "from_y",
            "to_x",
            "to_y",
            "action_name",
            "text",
            "key",
            "value",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown desktop action fields: {sorted(unknown)}")
        return cls(
            type=DesktopActionType(value["type"]),
            app=app,
            generation=generation,
            **{key: item for key, item in value.items() if key != "type" and item is not None},
        )

    def tool_arguments(self) -> dict[str, Any]:
        arguments: dict[str, Any] = {"app": self.app}
        mapping = {
            "element_index": self.element_index,
            "x": self.x,
            "y": self.y,
            "click_count": self.click_count,
            "mouse_button": self.mouse_button,
            "direction": self.direction,
            "pages": self.pages,
            "from_x": self.from_x,
            "from_y": self.from_y,
            "to_x": self.to_x,
            "to_y": self.to_y,
            "action": self.action_name,
            "text": self.text,
            "key": self.key,
            "value": self.value,
        }
        arguments.update({key: value for key, value in mapping.items() if value is not None})
        return arguments


@dataclass(frozen=True, slots=True)
class DesktopExpectation:
    kind: DesktopExpectationKind
    text: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, DesktopExpectationKind):
            raise ValueError("desktop expectation kind is invalid")
        if self.kind not in {
            DesktopExpectationKind.APP_VISIBLE,
            DesktopExpectationKind.LAST_ACTION_VERIFIED,
        }:
            _validate_text(self.text or "", label="expectation text", maximum=500)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DesktopExpectation:
        if not isinstance(value, dict) or set(value) - {"kind", "text"}:
            raise ValueError("desktop expectation has unknown fields")
        return cls(kind=DesktopExpectationKind(value["kind"]), text=value.get("text"))


@dataclass(frozen=True, slots=True)
class DesktopDecision:
    kind: DesktopDecisionKind
    reason: str
    app: str | None = None
    action: DesktopAction | None = None
    expectation: DesktopExpectation | None = None

    def __post_init__(self) -> None:
        _validate_text(self.reason, label="desktop decision reason", maximum=300)
        if self.kind == DesktopDecisionKind.OBSERVE:
            _validate_text(self.app or "", label="observe app", maximum=256)
            if self.action is not None or self.expectation is not None:
                raise ValueError("observe decision cannot contain action or expectation")
        elif self.kind == DesktopDecisionKind.ACTION:
            if self.action is None or self.app != self.action.app:
                raise ValueError("action decision requires one action for the selected app")
            if self.expectation is None:
                raise ValueError("action decision requires a task-specific postcondition")
            if self.expectation.kind in {
                DesktopExpectationKind.APP_VISIBLE,
                DesktopExpectationKind.LAST_ACTION_VERIFIED,
            }:
                raise ValueError("action postcondition must describe a task-specific UI change")
        elif self.kind == DesktopDecisionKind.DONE:
            if self.expectation is None or self.action is not None:
                raise ValueError("done decision requires an expectation and no action")
        elif self.kind == DesktopDecisionKind.FAIL:
            if self.action is not None or self.expectation is not None:
                raise ValueError("fail decision cannot contain action or expectation")

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        *,
        observation: DesktopObservation | None,
    ) -> DesktopDecision:
        if not isinstance(value, dict):
            raise ValueError("desktop decision must be an object")
        allowed = {"kind", "reason", "app", "action", "expectation"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"desktop decision has unknown fields: {sorted(unknown)}")
        reason_value = value.get("reason")
        if not isinstance(reason_value, str):
            raise ValueError("desktop decision reason must be a string")
        kind = DesktopDecisionKind(value["kind"])
        app_value = value.get("app")
        if app_value is not None and not isinstance(app_value, str):
            raise ValueError("desktop decision app must be a string or null")
        app = app_value.strip() if isinstance(app_value, str) and app_value.strip() else None
        action: DesktopAction | None = None
        if value.get("action") is not None:
            if observation is None:
                raise ValueError("an action decision requires a current observation")
            if app is None or app.casefold() != observation.app.casefold():
                raise ValueError("an action decision must target the observed application")
            app = observation.app
            action = DesktopAction.from_dict(
                value["action"],
                app=observation.app,
                generation=observation.generation,
            )
        expectation: DesktopExpectation | None = None
        if value.get("expectation") is not None:
            expectation = DesktopExpectation.from_dict(value["expectation"])
        return cls(
            kind=kind,
            reason=reason_value,
            app=app,
            action=action,
            expectation=expectation,
        )


@dataclass(frozen=True, slots=True)
class ActionReceipt:
    action: DesktopAction
    accepted: bool
    before_generation: int
    driver_message: str = ""


@dataclass(frozen=True, slots=True)
class DesktopTaskResult:
    success: bool
    message: str
    needs_confirmation: bool = False
    confirmation_id: str | None = None
    cancelled: bool = False
    timed_out: bool = False


@runtime_checkable
class DesktopDriver(Protocol):
    def start(self) -> None: ...

    def list_apps(self, *, cancel_event: Event | None = None) -> str: ...

    def observe(self, app: str, *, cancel_event: Event | None = None) -> DesktopObservation: ...

    def execute(
        self,
        action: DesktopAction,
        before: DesktopObservation,
        *,
        cancel_event: Event | None = None,
    ) -> ActionReceipt: ...

    def cancel(self) -> bool: ...

    def close(self) -> None: ...
