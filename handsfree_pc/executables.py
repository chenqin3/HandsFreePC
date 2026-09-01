"""Resolve external executables, including the Codex Desktop Windows bundle."""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _is_file(path: Path) -> bool:
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def _mtime(path: Path) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return float("-inf")


def resolve_executable(name: str) -> str | None:
    """Return an executable path, with a narrow Codex Desktop fallback on Windows.

    Normal ``PATH`` resolution always wins.  The fallback applies only to the
    bare ``codex`` command names; explicit paths and custom executable names are
    never redirected into the Codex Desktop installation.
    """

    resolved = shutil.which(name)
    if resolved is not None:
        return resolved

    if os.name != "nt" or name.casefold() not in {"codex", "codex.exe"}:
        return None

    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None

    bin_directory = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
    versioned_candidates: list[Path] = []
    try:
        children = list(bin_directory.iterdir())
    except OSError:
        children = []

    for child in children:
        try:
            is_directory = child.is_dir()
        except OSError:
            continue
        if not is_directory:
            continue
        candidate = child / "codex.exe"
        if _is_file(candidate):
            versioned_candidates.append(candidate)

    if versioned_candidates:
        newest = max(
            versioned_candidates,
            key=lambda candidate: (_mtime(candidate), str(candidate)),
        )
        return str(newest)

    root_candidate = bin_directory / "codex.exe"
    return str(root_candidate) if _is_file(root_candidate) else None
