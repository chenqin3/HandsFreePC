from __future__ import annotations

import json
import struct
import tomllib
import urllib.request
from pathlib import Path

import pytest

from handsfree_pc.desktop import visual_ocr
from handsfree_pc.desktop.visual_ocr import (
    SensitiveVisualSurfaceError,
    VisualOcrClient,
    VisualOcrConfigurationError,
    VisualOcrError,
    is_loopback_visual_ocr_endpoint,
)


def _png(width: int = 100, height: int = 80) -> bytes:
    # The client deliberately performs a bounded header check; image decoding
    # belongs to the exact-window driver and server.
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(
        ">II", width, height
    )


def _response(*blocks, width: int = 100, height: int = 80) -> bytes:
    return json.dumps(
        {
            "prunedResult": {
                "width": width,
                "height": height,
                "parsing_res_list": list(blocks),
            }
        },
        ensure_ascii=False,
    ).encode()


def _block(text: str = "Chat", bbox=(10, 12, 42, 30), label: str = "text"):
    return {
        "block_content": text,
        "block_bbox": list(bbox),
        "block_label": label,
    }


def test_visual_ocr_endpoint_is_literal_loopback_by_default() -> None:
    assert is_loopback_visual_ocr_endpoint(
        "http://127.0.0.1:8766/layout-parsing"
    )
    assert is_loopback_visual_ocr_endpoint(
        "http://[::1]:8766/layout-parsing"
    )
    assert not is_loopback_visual_ocr_endpoint(
        "http://localhost:8766/layout-parsing"
    )
    assert not is_loopback_visual_ocr_endpoint(
        "http://192.168.10.119:8089/layout-parsing"
    )
    with pytest.raises(VisualOcrConfigurationError, match="allow_remote_screen_ocr"):
        VisualOcrClient("http://192.168.10.119:8089/layout-parsing")
    with pytest.raises(VisualOcrConfigurationError, match="allow_remote_screen_ocr"):
        VisualOcrClient("http://localhost:8766/layout-parsing")


def test_windows_package_declares_the_set_of_marks_image_dependency() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )

    assert any(
        requirement.startswith("Pillow>=")
        for requirement in pyproject["project"]["optional-dependencies"]["windows"]
    )


def test_visual_ocr_never_follows_a_redirect_to_lan(monkeypatch) -> None:
    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def geturl(self):
            return "http://192.168.10.119:8089/layout-parsing"

        def read(self, _limit):
            return _response(_block())

    class Opener:
        def open(self, _request, *, timeout):
            assert timeout == 12
            return Response()

    monkeypatch.setattr(visual_ocr.urllib.request, "build_opener", lambda *_args: Opener())
    client = VisualOcrClient("http://127.0.0.1:8766/layout-parsing")

    with pytest.raises(VisualOcrError, match="redirect"):
        client.ocr_png(_png())


def test_remote_visual_ocr_requires_the_separate_explicit_consent() -> None:
    client = VisualOcrClient(
        "http://192.168.10.119:8089/layout-parsing",
        allow_remote_screen_ocr=True,
        transport=lambda *_args: _response(_block()),
    )

    assert client.ocr_png(_png()).blocks[0].text == "Chat"


def test_client_sends_only_one_bounded_raw_png_request() -> None:
    calls: list[tuple[urllib.request.Request, float, int]] = []

    def transport(request, timeout, max_bytes):
        calls.append((request, timeout, max_bytes))
        return _response(_block("文件传输助手"))

    client = VisualOcrClient(
        "http://127.0.0.1:8766/layout-parsing",
        timeout_seconds=3,
        transport=transport,
    )
    result = client.ocr_png(_png())

    request, timeout, max_bytes = calls[0]
    assert request.method == "POST"
    assert request.full_url == "http://127.0.0.1:8766/layout-parsing"
    assert request.headers["Content-type"] == "image/png"
    assert request.data == _png()
    assert timeout == 3
    assert max_bytes == 2 * 1024 * 1024
    assert result.width == 100
    assert result.height == 80
    assert result.blocks[0].bbox == (10, 12, 42, 30)
    assert len(result.frame_sha256) == 64


@pytest.mark.parametrize(
    "response",
    [
        b"not-json",
        b"[]",
        _response(_block(bbox=(10, 10, 101, 30))),
        _response(_block(), width=99),
    ],
)
def test_invalid_or_window_escaping_response_fails_closed(response: bytes) -> None:
    client = VisualOcrClient(
        "http://127.0.0.1:8766/layout-parsing",
        transport=lambda *_args: response,
    )

    with pytest.raises(VisualOcrError):
        client.ocr_png(_png())


def test_more_than_configured_visual_regions_fails_instead_of_truncating() -> None:
    client = VisualOcrClient(
        "http://127.0.0.1:8766/layout-parsing",
        max_items=1,
        transport=lambda *_args: _response(_block("A"), _block("B", (50, 12, 70, 30))),
    )

    with pytest.raises(VisualOcrError, match="more entries"):
        client.ocr_png(_png())


@pytest.mark.parametrize(
    "text",
    ["Password", "验证码", "立即付款", "sk-proj-" + "abcdefghijklmnop"],
)
def test_authentication_payment_and_credential_surfaces_are_rejected(text: str) -> None:
    client = VisualOcrClient(
        "http://127.0.0.1:8766/layout-parsing",
        transport=lambda *_args: _response(_block(text)),
    )

    with pytest.raises(SensitiveVisualSurfaceError):
        client.ocr_png(_png())
