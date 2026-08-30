from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .models import ActionType, Plan


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
        int(value.st_dev),
        int(value.st_ino),
    )


def _file_object_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    # Windows can report permission/ctime details differently for Path.stat()
    # and fstat() on the same handle. These fields remain comparable across both.
    return (
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_dev),
        int(value.st_ino),
    )


def _bind_plan_paths_unlocked(plan: Plan) -> str:
    """Hash a plan together with stable identities for every path it can open."""

    plan.validate()
    targets: list[dict[str, object]] = []
    for action in plan.actions:
        if action.type != ActionType.OPEN_PATH:
            continue
        if not action.path:
            raise ValueError("open_path confirmation has no target")
        resolved = Path(action.path).resolve(strict=True)
        before = resolved.stat()
        is_directory = stat.S_ISDIR(before.st_mode)
        if not is_directory and not stat.S_ISREG(before.st_mode):
            raise ValueError("open_path confirmation target is not a file or directory")
        item: dict[str, object] = {
            "path": str(resolved),
            "mode": int(before.st_mode),
            "size": int(before.st_size),
            "mtime_ns": int(before.st_mtime_ns),
            "ctime_ns": int(before.st_ctime_ns),
            "device": int(before.st_dev),
            "inode": int(before.st_ino),
            "is_directory": is_directory,
        }
        if is_directory:
            after = resolved.stat()
            if _stat_identity(before) != _stat_identity(after):
                raise RuntimeError("confirmation target changed while it was being bound")
        else:
            digest = hashlib.sha256()
            with resolved.open("rb") as stream:
                opened_before = os.fstat(stream.fileno())
                if _file_object_identity(before) != _file_object_identity(opened_before):
                    raise RuntimeError("confirmation target changed before hashing")
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
                opened_after = os.fstat(stream.fileno())
            after = resolved.stat()
            identities = {
                _file_object_identity(before),
                _file_object_identity(opened_before),
                _file_object_identity(opened_after),
                _file_object_identity(after),
            }
            if len(identities) != 1 or _stat_identity(before) != _stat_identity(after):
                raise RuntimeError("confirmation target changed while it was being hashed")
            item["sha256"] = digest.hexdigest()
        targets.append(item)
    payload = json.dumps(
        {
            "plan": plan.to_dict(),
            "source": plan.source,
            "targets": targets,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _path_targets(plan: Plan) -> tuple[Path, ...]:
    targets: list[Path] = []
    for action in plan.actions:
        if action.type != ActionType.OPEN_PATH:
            continue
        if not action.path:
            raise ValueError("open_path confirmation has no target")
        targets.append(Path(action.path).resolve(strict=True))
    return tuple(targets)


def _open_windows_read_guards(targets: tuple[Path, ...]) -> list[int]:
    import ctypes
    from ctypes import wintypes

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = ctypes.windll.kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    generic_read = 0x80000000
    file_share_read = 0x00000001
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_flag_backup_semantics = 0x02000000
    invalid_handle = ctypes.c_void_p(-1).value
    handles: list[int] = []
    try:
        for target in targets:
            flags = file_flag_backup_semantics if target.is_dir() else file_attribute_normal
            handle = create_file(
                str(target),
                generic_read,
                file_share_read,
                None,
                open_existing,
                flags,
                None,
            )
            raw_handle = int(handle)
            if raw_handle == invalid_handle:
                error = ctypes.get_last_error()
                raise OSError(error, "could not lock confirmed path against replacement")
            handles.append(raw_handle)
    except Exception:
        for handle in reversed(handles):
            close_handle(handle)
        raise
    return handles


def _close_windows_read_guards(handles: list[int]) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.windll.kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    for handle in reversed(handles):
        close_handle(handle)


def bind_plan_paths(plan: Plan) -> str:
    """Hash path targets while denying concurrent Windows writes and replacement."""

    targets = _path_targets(plan)
    handles: list[int] = []
    try:
        if targets and os.name == "nt":
            handles = _open_windows_read_guards(targets)
        return _bind_plan_paths_unlocked(plan)
    finally:
        if handles:
            _close_windows_read_guards(handles)


@contextmanager
def guard_plan_paths(plan: Plan, expected_digest: str) -> Iterator[None]:
    """Hold confirmed Windows targets stable through their verified shell-open boundary."""

    targets = _path_targets(plan)
    handles: list[int] = []
    streams: list[object] = []
    try:
        if targets and os.name == "nt":
            handles = _open_windows_read_guards(targets)
        elif targets:
            # HandsFreePC executes on Windows. Keeping file descriptors open plus
            # before/after binding provides conservative behavior for unit tests
            # and diagnostics on other platforms, without claiming rename exclusion.
            streams = [target.open("rb") for target in targets if target.is_file()]
        current_digest = _bind_plan_paths_unlocked(plan) if handles else bind_plan_paths(plan)
        if current_digest != expected_digest:
            raise RuntimeError("confirmed path identity changed before execution")
        yield
        current_digest = _bind_plan_paths_unlocked(plan) if handles else bind_plan_paths(plan)
        if current_digest != expected_digest:
            raise RuntimeError("confirmed path identity changed during execution")
    finally:
        for stream in reversed(streams):
            stream.close()
        if handles:
            _close_windows_read_guards(handles)
