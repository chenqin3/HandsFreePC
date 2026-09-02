from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import struct
import unicodedata
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .protocol import contains_high_confidence_credential


class VisualOcrError(RuntimeError):
    """A bounded local visual-OCR operation failed closed."""


class VisualOcrConfigurationError(ValueError):
    pass


class SensitiveVisualSurfaceError(VisualOcrError):
    pass


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def is_loopback_visual_ocr_endpoint(endpoint: str) -> bool:
    """Return true only for literal loopback hosts; never perform DNS resolution."""

    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except (TypeError, ValueError):
        return False
    try:
        loopback = ipaddress.ip_address(parsed.hostname or "").is_loopback
    except ValueError:
        loopback = False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and loopback
        and port is not None
        and parsed.path not in {"", "/"}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def validate_visual_ocr_endpoint(
    endpoint: str,
    *,
    allow_remote_screen_ocr: bool,
) -> str:
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise VisualOcrConfigurationError("visual_ocr.endpoint must be a non-empty URL")
    endpoint = endpoint.strip()
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise VisualOcrConfigurationError("visual_ocr.endpoint is not a valid URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname is None
        or port is None
        or parsed.path in {"", "/"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise VisualOcrConfigurationError(
            "visual_ocr.endpoint must be an explicit http(s) URL with host, port, and path"
        )
    if not is_loopback_visual_ocr_endpoint(endpoint) and not allow_remote_screen_ocr:
        raise VisualOcrConfigurationError(
            "non-loopback visual_ocr.endpoint requires allow_remote_screen_ocr=true"
        )
    return endpoint


@dataclass(frozen=True, slots=True)
class VisualOcrBlock:
    text: str
    bbox: tuple[int, int, int, int]
    label: str

    def __post_init__(self) -> None:
        if not self.text or len(self.text) > 512:
            raise ValueError("visual OCR text must contain 1 to 512 characters")
        if any(
            unicodedata.category(character).startswith("C")
            for character in self.text
        ):
            raise ValueError("visual OCR text contains unsupported control characters")
        if len(self.label) > 64:
            raise ValueError("visual OCR label exceeds 64 characters")
        if len(self.bbox) != 4 or any(type(item) is not int for item in self.bbox):
            raise ValueError("visual OCR bbox must contain four integers")
        left, top, right, bottom = self.bbox
        if left < 0 or top < 0 or right <= left or bottom <= top:
            raise ValueError("visual OCR bbox is invalid")


@dataclass(frozen=True, slots=True)
class VisualOcrResult:
    width: int
    height: int
    frame_sha256: str
    blocks: tuple[VisualOcrBlock, ...]


VisualOcrTransport = Callable[[urllib.request.Request, float, int], bytes]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        # A redirect could turn an apparently loopback screenshot upload into
        # a LAN or internet upload. Visual OCR never follows redirects.
        return None


def _default_transport(
    request: urllib.request.Request,
    timeout_seconds: float,
    max_response_bytes: int,
) -> bytes:
    opener = urllib.request.build_opener(_NoRedirectHandler())
    with opener.open(request, timeout=timeout_seconds) as response:  # noqa: S310
        if response.geturl() != request.full_url:
            raise VisualOcrError("visual OCR redirects are forbidden")
        length_header = response.headers.get("Content-Length")
        if length_header is not None:
            try:
                declared = int(length_header)
            except ValueError as exc:
                raise VisualOcrError("visual OCR returned an invalid Content-Length") from exc
            if declared < 0 or declared > max_response_bytes:
                raise VisualOcrError("visual OCR response exceeds the configured size limit")
        payload = response.read(max_response_bytes + 1)
    if len(payload) > max_response_bytes:
        raise VisualOcrError("visual OCR response exceeds the configured size limit")
    return payload


def _png_dimensions(payload: bytes) -> tuple[int, int]:
    if len(payload) < 24 or not payload.startswith(_PNG_SIGNATURE):
        raise VisualOcrError("visual OCR accepts PNG screenshots only")
    width, height = struct.unpack(">II", payload[16:24])
    if width < 1 or height < 1 or width > 16384 or height > 16384:
        raise VisualOcrError("visual OCR screenshot dimensions are outside the bounded range")
    return width, height


def _first_result_mapping(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    candidates: list[Any] = [payload]
    for key in ("prunedResult", "result", "res", "data"):
        candidates.append(payload.get(key))
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        if isinstance(candidate.get("res"), Mapping):
            candidate = candidate["res"]
        if "parsing_res_list" in candidate:
            return candidate
    raise VisualOcrError("visual OCR response has no bounded parsing_res_list")


def _bounded_text(value: Any, *, maximum: int, field: str) -> str:
    if not isinstance(value, str):
        raise VisualOcrError(f"visual OCR {field} must be a string")
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > maximum:
        raise VisualOcrError(f"visual OCR {field} is empty or oversized")
    if "\ufffd" in normalized or any(
        unicodedata.category(character).startswith("C") for character in normalized
    ):
        raise VisualOcrError(f"visual OCR {field} contains invalid Unicode")
    return normalized


def _bbox(value: Any, *, width: int, height: int) -> tuple[int, int, int, int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        raise VisualOcrError("visual OCR block_bbox must contain four numbers")
    coordinates: list[int] = []
    for item in value:
        if type(item) not in {int, float} or not math.isfinite(float(item)):
            raise VisualOcrError("visual OCR block_bbox contains a non-finite coordinate")
        rounded = round(float(item))
        if abs(float(item) - rounded) > 0.01:
            raise VisualOcrError("visual OCR block_bbox coordinates must be integral")
        coordinates.append(int(rounded))
    left, top, right, bottom = coordinates
    if left < 0 or top < 0 or right <= left or bottom <= top:
        raise VisualOcrError("visual OCR block_bbox is invalid")
    if right > width or bottom > height:
        raise VisualOcrError("visual OCR block_bbox escapes the captured window")
    return left, top, right, bottom


def _surface_is_sensitive(blocks: Sequence[VisualOcrBlock]) -> bool:
    """Refuse OCR only for an actual visible secret, never for chat wording.

    Real credential surfaces are already hard-blocked at the UIA layer (password
    and secret-labeled fields). A broad keyword scan over chat OCR text used to
    flag ordinary conversations that merely mention 登录/验证码/支付, disabling
    the whole window; that false positive is worse than useless for a chat app,
    so only a high-confidence secret pattern (keys, tokens, seed phrases) blocks.
    """

    joined = "\n".join(block.text.casefold() for block in blocks)
    return contains_high_confidence_credential(joined)


class VisualOcrClient:
    """Small HTTP client for a user-opted, bounded window-only OCR endpoint."""

    def __init__(
        self,
        endpoint: str,
        *,
        allow_remote_screen_ocr: bool = False,
        timeout_seconds: float = 12.0,
        max_image_bytes: int = 8 * 1024 * 1024,
        max_response_bytes: int = 2 * 1024 * 1024,
        max_items: int = 160,
        max_text_chars: int = 256,
        transport: VisualOcrTransport | None = None,
    ) -> None:
        self.endpoint = validate_visual_ocr_endpoint(
            endpoint,
            allow_remote_screen_ocr=allow_remote_screen_ocr,
        )
        if not 0.1 <= float(timeout_seconds) <= 120:
            raise VisualOcrConfigurationError(
                "visual_ocr.timeout_seconds must be between 0.1 and 120"
            )
        if not 1024 <= int(max_image_bytes) <= 32 * 1024 * 1024:
            raise VisualOcrConfigurationError(
                "visual_ocr.max_image_bytes must be between 1024 and 33554432"
            )
        if not 1024 <= int(max_response_bytes) <= 16 * 1024 * 1024:
            raise VisualOcrConfigurationError(
                "visual_ocr.max_response_bytes must be between 1024 and 16777216"
            )
        if not 1 <= int(max_items) <= 500:
            raise VisualOcrConfigurationError("visual_ocr.max_items must be between 1 and 500")
        if not 8 <= int(max_text_chars) <= 512:
            raise VisualOcrConfigurationError(
                "visual_ocr.max_text_chars must be between 8 and 512"
            )
        self.timeout_seconds = float(timeout_seconds)
        self.max_image_bytes = int(max_image_bytes)
        self.max_response_bytes = int(max_response_bytes)
        self.max_items = int(max_items)
        self.max_text_chars = int(max_text_chars)
        self._transport = transport or _default_transport

    def ocr_png(self, screenshot_png: bytes) -> VisualOcrResult:
        if not isinstance(screenshot_png, bytes):
            raise VisualOcrError("visual OCR screenshot must be immutable bytes")
        if len(screenshot_png) > self.max_image_bytes:
            raise VisualOcrError("visual OCR screenshot exceeds the configured size limit")
        image_width, image_height = _png_dimensions(screenshot_png)
        request = urllib.request.Request(
            self.endpoint,
            data=screenshot_png,
            headers={
                "Content-Type": "image/png",
                "Accept": "application/json",
                "User-Agent": "HandsFreePC-VisualOCR/1",
            },
            method="POST",
        )
        try:
            response = self._transport(
                request,
                self.timeout_seconds,
                self.max_response_bytes,
            )
        except VisualOcrError:
            raise
        except Exception as exc:
            raise VisualOcrError("visual OCR request failed") from exc
        if not isinstance(response, bytes) or len(response) > self.max_response_bytes:
            raise VisualOcrError("visual OCR transport returned an invalid response")
        try:
            decoded = json.loads(response.decode("utf-8", errors="strict"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VisualOcrError("visual OCR response is not valid UTF-8 JSON") from exc
        if not isinstance(decoded, Mapping):
            raise VisualOcrError("visual OCR response root must be a JSON object")
        result = _first_result_mapping(decoded)
        width = result.get("width", image_width)
        height = result.get("height", image_height)
        if type(width) is not int or type(height) is not int:
            raise VisualOcrError("visual OCR response dimensions must be integers")
        if width != image_width or height != image_height:
            raise VisualOcrError("visual OCR response dimensions do not match the screenshot")
        raw_blocks = result.get("parsing_res_list")
        if not isinstance(raw_blocks, list):
            raise VisualOcrError("visual OCR parsing_res_list must be a JSON list")
        if len(raw_blocks) > self.max_items:
            raise VisualOcrError("visual OCR returned more entries than configured")
        blocks: list[VisualOcrBlock] = []
        seen: set[tuple[str, tuple[int, int, int, int]]] = set()
        for raw in raw_blocks:
            if not isinstance(raw, Mapping):
                raise VisualOcrError("visual OCR entries must be JSON objects")
            text = _bounded_text(
                raw.get("block_content"),
                maximum=self.max_text_chars,
                field="block_content",
            )
            bbox = _bbox(raw.get("block_bbox"), width=width, height=height)
            label_value = raw.get("block_label", "text")
            label = _bounded_text(label_value, maximum=64, field="block_label")
            key = (text.casefold(), bbox)
            if key in seen:
                continue
            seen.add(key)
            blocks.append(VisualOcrBlock(text=text, bbox=bbox, label=label))
        if _surface_is_sensitive(blocks):
            raise SensitiveVisualSurfaceError(
                "visual OCR fallback is disabled on authentication, credential, or payment surfaces"
            )
        return VisualOcrResult(
            width=width,
            height=height,
            frame_sha256=hashlib.sha256(screenshot_png).hexdigest(),
            blocks=tuple(blocks),
        )
