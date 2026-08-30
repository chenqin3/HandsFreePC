from __future__ import annotations

from pathlib import Path

import pytest

from handsfree_pc.config import load_settings


@pytest.fixture
def settings(tmp_path: Path):
    return load_settings(tmp_path / "config.yaml", allow_missing=True)
