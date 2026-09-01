from __future__ import annotations

import os
from pathlib import Path

import handsfree_pc.executables as executables


def _touch_executable(path: Path, *, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"executable")
    os.utime(path, (mtime, mtime))
    return path


def test_path_resolution_wins_over_codex_desktop_fallback(tmp_path, monkeypatch) -> None:
    path_executable = tmp_path / "on-path" / "codex.exe"
    monkeypatch.setattr(executables.shutil, "which", lambda _name: str(path_executable))
    monkeypatch.setattr(executables.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local-app-data"))

    assert executables.resolve_executable("codex") == str(path_executable)


def test_codex_fallback_requires_local_app_data(monkeypatch) -> None:
    monkeypatch.setattr(executables.shutil, "which", lambda _name: None)
    monkeypatch.setattr(executables.os, "name", "nt")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert executables.resolve_executable("codex") is None


def test_newest_versioned_codex_wins_over_root_install(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(executables.shutil, "which", lambda _name: None)
    monkeypatch.setattr(executables.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    bin_directory = tmp_path / "OpenAI" / "Codex" / "bin"
    _touch_executable(bin_directory / "codex.exe", mtime=300)
    _touch_executable(bin_directory / "old-version" / "codex.exe", mtime=100)
    newest = _touch_executable(bin_directory / "new-version" / "codex.exe", mtime=200)

    assert executables.resolve_executable("codex.exe") == str(newest)


def test_custom_name_and_explicit_path_do_not_use_codex_fallback(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(executables.shutil, "which", lambda _name: None)
    monkeypatch.setattr(executables.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _touch_executable(tmp_path / "OpenAI" / "Codex" / "bin" / "version" / "codex.exe", mtime=1)

    assert executables.resolve_executable("my-codex") is None
    assert executables.resolve_executable(str(tmp_path / "codex.exe")) is None
    assert executables.resolve_executable(r"tools\codex.exe") is None


def test_codex_fallback_is_windows_only(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(executables.shutil, "which", lambda _name: None)
    monkeypatch.setattr(executables.os, "name", "posix")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _touch_executable(tmp_path / "OpenAI" / "Codex" / "bin" / "version" / "codex.exe", mtime=1)

    assert executables.resolve_executable("codex") is None


def test_directory_enumeration_error_still_allows_root_candidate(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(executables.shutil, "which", lambda _name: None)
    monkeypatch.setattr(executables.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    root_candidate = _touch_executable(
        tmp_path / "OpenAI" / "Codex" / "bin" / "codex.exe", mtime=1
    )
    monkeypatch.setattr(Path, "iterdir", lambda _path: (_ for _ in ()).throw(PermissionError()))

    assert executables.resolve_executable("codex") == str(root_candidate)


def test_mtime_error_does_not_abort_candidate_selection(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(executables.shutil, "which", lambda _name: None)
    monkeypatch.setattr(executables.os, "name", "nt")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    bin_directory = tmp_path / "OpenAI" / "Codex" / "bin"
    unreadable_mtime = _touch_executable(bin_directory / "unknown" / "codex.exe", mtime=300)
    known_mtime = _touch_executable(bin_directory / "known" / "codex.exe", mtime=100)
    real_getmtime = executables.os.path.getmtime

    def getmtime(path: os.PathLike[str] | str) -> float:
        if Path(path) == unreadable_mtime:
            raise OSError("mtime unavailable")
        return real_getmtime(path)

    monkeypatch.setattr(executables.os.path, "getmtime", getmtime)

    assert executables.resolve_executable("codex") == str(known_mtime)
