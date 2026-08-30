from __future__ import annotations

import hashlib
import inspect
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelAsset:
    name: str
    directory: str
    url: str
    archive_type: str
    expected_files: tuple[str, ...]
    attribution: str
    license_url: str
    sha256: str | None = None
    extra_files: tuple[tuple[str, str], ...] = ()


MODEL_ASSETS = (
    ModelAsset(
        name="Vosk small Chinese 0.22",
        directory="vosk-model-small-cn-0.22",
        url="https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip",
        archive_type="zip",
        expected_files=("am/final.mdl", "conf/model.conf"),
        attribution="Vosk model by Alpha Cephei; Vosk code and small-cn model are Apache-2.0.",
        license_url="https://alphacephei.com/vosk/models",
        sha256="3af8b0e7e0f835ae9d414ce5df580237a3cfb08d586c9fbbb0f7ff29ad5b14ba",
        extra_files=(
            ("https://raw.githubusercontent.com/alphacep/vosk-api/v0.3.45/COPYING", "COPYING"),
        ),
    ),
    ModelAsset(
        name="sherpa-onnx SenseVoice Chinese/English/Japanese/Korean/Cantonese",
        directory="sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
        url=(
            "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/"
            "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2"
        ),
        archive_type="tar.bz2",
        expected_files=("model.int8.onnx", "tokens.txt"),
        attribution=(
            "SenseVoiceSmall by Alibaba DAMO Academy / FunASR, packaged for sherpa-onnx. "
            "Keep the SenseVoice model name and attribution when redistributing a derived package."
        ),
        license_url="https://github.com/FunAudioLLM/SenseVoice/blob/main/MODEL_LICENSE",
        sha256="7d1efa2138a65b0b488df37f8b89e3d91a60676e416f515b952358d83dfd347e",
        extra_files=(
            (
                "https://raw.githubusercontent.com/modelscope/FunASR/main/MODEL_LICENSE",
                "MODEL_LICENSE",
            ),
        ),
    ),
    ModelAsset(
        name="Silero VAD v6.2.1 ONNX",
        directory="silero-vad-v6.2.1",
        url=(
            "https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.1/"
            "src/silero_vad/data/silero_vad.onnx"
        ),
        archive_type="file",
        expected_files=("silero_vad.onnx",),
        attribution="Silero VAD v6.2.1, Copyright 2020-present Silero Team, MIT.",
        license_url="https://github.com/snakers4/silero-vad/blob/v6.2.1/LICENSE",
        sha256="1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
        extra_files=(
            ("https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.1/LICENSE", "LICENSE"),
        ),
    ),
)


class DownloadError(RuntimeError):
    pass


def _safe_destination(root: Path, member_name: str) -> Path:
    target = (root / member_name).resolve()
    if root.resolve() not in (target, *target.parents):
        raise DownloadError(f"Archive member escapes model directory: {member_name}")
    return target


def _download(url: str, destination: Path, progress: Callable[[str], None]) -> str:
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "HandsFreePC/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        total = int(response.headers.get("Content-Length", "0"))
        received = 0
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
            digest.update(chunk)
            received += len(chunk)
            if total:
                progress(f"下载 {destination.name}: {received * 100 / total:.0f}%")
    return digest.hexdigest()


def _download_small_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={"User-Agent": "HandsFreePC/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def _extract_zip(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            _safe_destination(destination, info.filename)
        bundle.extractall(destination)


def _extract_tar(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, mode="r:bz2") as bundle:
        members = bundle.getmembers()
        for member in members:
            _safe_destination(destination, member.name)
            if not (member.isfile() or member.isdir()):
                raise DownloadError(
                    f"Only regular files/directories are accepted in model archives: {member.name}"
                )
        if "filter" in inspect.signature(bundle.extractall).parameters:
            bundle.extractall(destination, members=members, filter="data")
        else:  # Compatibility with early Python 3.11 patch releases.
            bundle.extractall(destination, members=members)


def _verify(asset: ModelAsset, models_dir: Path) -> Path:
    model_dir = models_dir / asset.directory
    missing = [name for name in asset.expected_files if not (model_dir / name).is_file()]
    if missing:
        raise DownloadError(f"{asset.name} is missing expected files: {missing}")
    return model_dir


def _verify_complete(asset: ModelAsset, models_dir: Path) -> Path:
    model_dir = _verify(asset, models_dir)
    required_metadata = [name for _url, name in asset.extra_files]
    required_metadata.append("HANDSFREEPC_MODEL_SOURCE.txt")
    missing = [name for name in required_metadata if not (model_dir / name).is_file()]
    if missing:
        raise DownloadError(f"{asset.name} is missing license/source files: {missing}")
    return model_dir


def download_models(
    models_dir: Path,
    *,
    force: bool = False,
    progress: Callable[[str], None] = print,
) -> list[Path]:
    models_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = models_dir / ".downloads"
    cache_dir.mkdir(exist_ok=True)
    installed: list[Path] = []
    for asset in MODEL_ASSETS:
        model_dir = models_dir / asset.directory
        if not force:
            try:
                complete_model = _verify_complete(asset, models_dir)
            except DownloadError:
                pass
            else:
                progress(f"已存在，跳过: {asset.name}")
                installed.append(complete_model)
                continue
        archive_name = asset.url.rsplit("/", 1)[-1]
        archive = cache_dir / archive_name
        partial = archive.with_suffix(archive.suffix + ".part")
        if partial.exists():
            partial.unlink()
        progress(f"下载: {asset.name}")
        digest = _download(asset.url, partial, progress)
        if asset.sha256 and digest.lower() != asset.sha256.lower():
            partial.unlink(missing_ok=True)
            raise DownloadError(f"SHA-256 mismatch for {asset.name}")
        partial.replace(archive)
        with tempfile.TemporaryDirectory(
            prefix=".handsfreepc-stage-", dir=models_dir
        ) as stage_name:
            stage_root = Path(stage_name)
            staged_model = stage_root / asset.directory
            if asset.archive_type == "zip":
                _extract_zip(archive, stage_root)
            elif asset.archive_type == "tar.bz2":
                _extract_tar(archive, stage_root)
            elif asset.archive_type == "file":
                staged_model.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(archive, staged_model / asset.expected_files[0])
            else:
                raise DownloadError(f"Unsupported archive type: {asset.archive_type}")
            verified = _verify(asset, stage_root)
            for extra_url, relative_name in asset.extra_files:
                _download_small_file(extra_url, verified / relative_name)
            note = (
                f"Model: {asset.name}\n"
                f"Source: {asset.url}\n"
                f"Downloaded UTC: {datetime.now(UTC).isoformat()}\n"
                f"Archive SHA-256: {digest}\n"
                f"Attribution: {asset.attribution}\n"
                f"License information: {asset.license_url}\n"
            )
            (verified / "HANDSFREEPC_MODEL_SOURCE.txt").write_text(note, encoding="utf-8")
            _verify_complete(asset, stage_root)

            backup = stage_root / ".previous-model"
            if model_dir.exists():
                model_dir.replace(backup)
            try:
                verified.replace(model_dir)
            except Exception:
                if backup.exists() and not model_dir.exists():
                    backup.replace(model_dir)
                raise
        progress(f"已安装: {model_dir}")
        installed.append(model_dir)
    return installed
