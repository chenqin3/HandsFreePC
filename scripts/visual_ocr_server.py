"""Optional loopback PaddleOCR-VL service for HandsFreePC visual fallback.

Run this inside a WSL environment that already has PaddleOCR-VL, FastAPI,
Uvicorn, Pillow, and NumPy installed. The server keeps screenshots in memory,
returns only bounded text boxes, and refuses a non-loopback bind unless the
operator supplies ``--allow-remote-bind``.
"""

import argparse
import io
import ipaddress
import threading
from collections.abc import Mapping, Sequence
from typing import Any


def _plain(value: Any) -> Any:
    to_list = getattr(value, "tolist", None)
    return to_list() if callable(to_list) else value


def _result_mapping(result: Any) -> Mapping[str, Any]:
    payload = getattr(result, "json", None)
    payload = payload() if callable(payload) else payload
    if not isinstance(payload, Mapping):
        raise RuntimeError("PaddleOCR-VL result.json did not return a mapping")
    nested = payload.get("res")
    return nested if isinstance(nested, Mapping) else payload


def _line_blocks(parsed: Mapping[str, Any], *, min_score: float) -> list[dict[str, Any]]:
    """Turn PP-OCR line results into the same block shape as layout parsing.

    Line-level boxes are what a UI needs: each list entry, menu item, or
    header becomes its own clickable region instead of one merged paragraph.
    """

    texts = _plain(parsed.get("rec_texts")) or []
    scores = _plain(parsed.get("rec_scores")) or []
    boxes = _plain(parsed.get("rec_boxes"))
    if not boxes:
        boxes = []
        for polygon in _plain(parsed.get("dt_polys")) or []:
            xs = [float(point[0]) for point in polygon]
            ys = [float(point[1]) for point in polygon]
            boxes.append([min(xs), min(ys), max(xs), max(ys)])
    blocks: list[dict[str, Any]] = []
    for index, text in enumerate(texts):
        if index >= len(boxes):
            break
        score = float(scores[index]) if index < len(scores) else 1.0
        content = str(text).strip()
        if not content or score < min_score:
            continue
        box = [int(round(float(value))) for value in boxes[index][:4]]
        blocks.append({"block_content": content, "block_bbox": box, "block_label": "text"})
    return blocks


def create_app(
    *,
    max_image_bytes: int,
    max_items: int,
    engine: str = "vl",
    ocr_models: str = "server",
    min_score: float = 0.5,
):
    try:
        import numpy as np
        from fastapi import FastAPI, HTTPException, Request
        from paddleocr import PaddleOCR, PaddleOCRVL
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - deployment environment only
        raise RuntimeError(
            "Install PaddleOCR-VL, FastAPI, Uvicorn, Pillow, and NumPy in WSL first"
        ) from exc

    Image.MAX_IMAGE_PIXELS = 40_000_000
    app = FastAPI(title="HandsFreePC local visual OCR", docs_url=None, redoc_url=None)
    pipeline_holder: list[Any] = []
    pipeline_lock = threading.Lock()

    def pipeline():
        with pipeline_lock:
            if not pipeline_holder:
                if engine == "ppocr":
                    pipeline_holder.append(
                        PaddleOCR(
                            use_doc_orientation_classify=False,
                            use_doc_unwarping=False,
                            use_textline_orientation=False,
                            text_detection_model_name=f"PP-OCRv5_{ocr_models}_det",
                            text_recognition_model_name=f"PP-OCRv5_{ocr_models}_rec",
                        )
                    )
                else:
                    pipeline_holder.append(PaddleOCRVL())
            return pipeline_holder[0]

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    async def layout_parsing(request: Request) -> dict[str, Any]:
        if request.headers.get("content-type", "").partition(";")[0].strip().casefold() not in {
            "image/png",
            "application/octet-stream",
        }:
            raise HTTPException(status_code=415, detail="PNG screenshots only")
        declared_length = request.headers.get("content-length")
        if declared_length is not None:
            try:
                if int(declared_length) > max_image_bytes:
                    raise HTTPException(status_code=413, detail="screenshot too large")
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="invalid Content-Length") from exc
        chunks: list[bytes] = []
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > max_image_bytes:
                raise HTTPException(status_code=413, detail="screenshot too large")
            chunks.append(chunk)
        payload = b"".join(chunks)
        try:
            image = Image.open(io.BytesIO(payload))
            if image.format != "PNG":
                raise ValueError("not PNG")
            width, height = image.size
            if width < 1 or height < 1 or width > 16384 or height > 16384:
                raise ValueError("invalid dimensions")
            image.load()
            rgb = image.convert("RGB")
        except Exception as exc:
            raise HTTPException(status_code=422, detail="invalid PNG screenshot") from exc
        try:
            predictions = pipeline().predict(np.asarray(rgb))
            prediction = next(iter(predictions))
            parsed = _result_mapping(prediction)
            if engine == "ppocr":
                raw_blocks = _line_blocks(parsed, min_score=min_score)
            else:
                raw_blocks = parsed.get("parsing_res_list", [])
        except Exception as exc:
            raise HTTPException(status_code=500, detail="OCR inference failed") from exc
        if not isinstance(raw_blocks, Sequence) or isinstance(raw_blocks, (str, bytes)):
            raise HTTPException(status_code=500, detail="OCR returned invalid regions")
        if len(raw_blocks) > max_items:
            raise HTTPException(status_code=422, detail="OCR returned too many regions")
        blocks: list[dict[str, Any]] = []
        for raw in raw_blocks:
            if not isinstance(raw, Mapping):
                raise HTTPException(status_code=500, detail="OCR returned invalid region")
            blocks.append(
                {
                    "block_content": str(raw.get("block_content", "")),
                    "block_bbox": _plain(raw.get("block_bbox")),
                    "block_label": str(raw.get("block_label", "text")),
                }
            )
        return {
            "prunedResult": {
                "width": width,
                "height": height,
                "parsing_res_list": blocks,
            }
        }

    app.post("/layout-parsing")(layout_parsing)

    return app


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--max-image-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--max-items", type=int, default=160)
    parser.add_argument("--allow-remote-bind", action="store_true")
    parser.add_argument(
        "--engine",
        choices=("vl", "ppocr"),
        default="vl",
        help="vl = PaddleOCR-VL layout blocks; ppocr = PP-OCRv5 line boxes (fast, UI-friendly)",
    )
    parser.add_argument(
        "--ocr-models",
        choices=("mobile", "server"),
        default="server",
        help="PP-OCRv5 detection/recognition model size used by --engine ppocr",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.5,
        help="Drop PP-OCR lines whose recognition confidence is below this value",
    )
    args = parser.parse_args()
    try:
        loopback_bind = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback_bind = False
    if not loopback_bind and not args.allow_remote_bind:
        parser.error("non-loopback --host requires --allow-remote-bind")
    if not 1024 <= args.max_image_bytes <= 32 * 1024 * 1024:
        parser.error("--max-image-bytes is outside the supported range")
    if not 1 <= args.max_items <= 500:
        parser.error("--max-items must be between 1 and 500")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - deployment environment only
        raise RuntimeError("Install Uvicorn in the WSL OCR environment first") from exc
    if not 0.0 <= args.min_score <= 1.0:
        parser.error("--min-score must be between 0 and 1")
    uvicorn.run(
        create_app(
            max_image_bytes=args.max_image_bytes,
            max_items=args.max_items,
            engine=args.engine,
            ocr_models=args.ocr_models,
            min_score=args.min_score,
        ),
        host=args.host,
        port=args.port,
        access_log=False,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - deployment entry point
    raise SystemExit(main())
