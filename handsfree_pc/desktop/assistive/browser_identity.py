from __future__ import annotations

import unicodedata

from ..protocol import DesktopElement, ElementPlane, element_plane

_BROWSER_PROCESSES = frozenset({"chrome.exe", "msedge.exe", "firefox.exe"})
_KNOWN_ADDRESS_AUTOMATION_IDS = {
    "chrome.exe": frozenset({"view_1012"}),
    "msedge.exe": frozenset({"view_1012"}),
    "firefox.exe": frozenset({"urlbar-input"}),
}
_ADDRESS_PHRASES = (
    "address and search bar",
    "address bar",
    "location bar",
    "url bar",
    "omnibox",
    "地址栏",
    "网址栏",
)


def _normalize(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def is_browser_process(process_name: str | None) -> bool:
    return _normalize(process_name) in _BROWSER_PROCESSES


def is_verified_browser_address(
    element: DesktopElement,
    process_name: str | None,
    *,
    require_focused: bool = False,
) -> bool:
    """Recognize browser chrome, never an arbitrary page edit containing a URL."""

    process = _normalize(process_name)
    if process not in _BROWSER_PROCESSES:
        return False
    if (
        element.control_type.casefold() not in {"edit", "combobox"}
        or not element.enabled
        or not element.addressable
        or element.editable is False
        or element.value_observed is not True
        or not element.local_identity
        or not element.browser_chrome
        or element_plane(element) != ElementPlane.INPUT
        or element.composer
        or element.visual_ocr
        or element.password
        or element.secret_labeled
        or element.high_credential
        or (require_focused and element.focused is not True)
    ):
        return False
    automation_id = _normalize(element.automation_id)
    identity = _normalize(f"{element.name} {element.automation_id}")
    return bool(
        automation_id in _KNOWN_ADDRESS_AUTOMATION_IDS.get(process, frozenset())
        or any(phrase in identity for phrase in _ADDRESS_PHRASES)
    )


__all__ = ["is_browser_process", "is_verified_browser_address"]
