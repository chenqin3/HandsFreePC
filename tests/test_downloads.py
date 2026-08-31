from __future__ import annotations

import hashlib

import pytest

import handsfree_pc.downloads as downloads
from handsfree_pc.downloads import DownloadError, ModelAsset


def test_english_delimiter_model_metadata_is_pinned() -> None:
    asset = next(
        item for item in downloads.MODEL_ASSETS if item.directory == "vosk-model-small-en-us-0.15"
    )

    assert asset.url == "https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip"
    assert asset.sha256 == "30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498"
    assert asset.expected_files == ("am/final.mdl", "conf/model.conf")
    assert asset.extra_files == (
        ("https://raw.githubusercontent.com/alphacep/vosk-api/v0.3.45/COPYING", "COPYING"),
    )


def test_license_failure_never_leaves_a_half_installed_model(tmp_path, monkeypatch) -> None:
    content = b"model"
    asset = ModelAsset(
        name="test model",
        directory="test-model",
        url="https://example.test/model.bin",
        archive_type="file",
        expected_files=("model.bin",),
        attribution="test attribution",
        license_url="https://example.test/license",
        sha256=hashlib.sha256(content).hexdigest(),
        extra_files=(("https://example.test/license", "LICENSE"),),
    )
    monkeypatch.setattr(downloads, "MODEL_ASSETS", (asset,))

    def fake_download(_url, destination, _progress):
        destination.write_bytes(content)
        return hashlib.sha256(content).hexdigest()

    monkeypatch.setattr(downloads, "_download", fake_download)
    monkeypatch.setattr(
        downloads,
        "_download_small_file",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(DownloadError("license failed")),
    )

    with pytest.raises(DownloadError, match="license failed"):
        downloads.download_models(tmp_path, progress=lambda _message: None)

    assert not (tmp_path / asset.directory).exists()

    def write_license(_url, destination):
        destination.write_text("license", encoding="utf-8")

    monkeypatch.setattr(downloads, "_download_small_file", write_license)
    installed = downloads.download_models(tmp_path, progress=lambda _message: None)

    model_dir = tmp_path / asset.directory
    assert installed == [model_dir]
    assert (model_dir / "model.bin").read_bytes() == content
    assert (model_dir / "LICENSE").read_text(encoding="utf-8") == "license"
    assert (model_dir / "HANDSFREEPC_MODEL_SOURCE.txt").is_file()
