from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from handsfree_pc.paths import (
    AmbiguousPathError,
    PathNotFoundError,
    PathResolutionError,
    PathResolver,
    _is_reparse_point,
    _resolve_within,
)


def test_resolve_alias_exact(tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    target = desktop / "说明.txt"
    target.write_text("ok", encoding="utf-8")
    resolver = PathResolver(aliases={"桌面": desktop})
    assert resolver.resolve("桌面\\说明.txt") == target.resolve()


def test_resolve_fuzzy_name_under_search_root(tmp_path: Path) -> None:
    target = tmp_path / "演示项目"
    target.mkdir()
    resolver = PathResolver(search_roots=[tmp_path], threshold=0.70)
    assert resolver.resolve("演示") == target.resolve()


def test_ambiguous_matches_fail_closed(tmp_path: Path) -> None:
    (tmp_path / "报告一.txt").write_text("1", encoding="utf-8")
    (tmp_path / "报告二.txt").write_text("2", encoding="utf-8")
    resolver = PathResolver(search_roots=[tmp_path], threshold=0.4)
    with pytest.raises(AmbiguousPathError):
        resolver.resolve("报告.txt")


def test_missing_path_is_not_invented(tmp_path: Path) -> None:
    resolver = PathResolver(search_roots=[tmp_path])
    with pytest.raises(PathNotFoundError):
        resolver.resolve("不存在的文件")


def test_search_root_does_not_follow_symlink_outside_scope(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "private-target.txt").write_text("outside", encoding="utf-8")
    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable on this Windows host: {exc}")

    resolver = PathResolver(search_roots=[root])

    with pytest.raises(PathNotFoundError):
        resolver.resolve("private-target.txt")


def test_resolved_candidate_must_remain_under_search_root(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()

    assert _resolve_within(root, root) == root.resolve()
    assert _resolve_within(outside, root.resolve()) is None


def test_windows_reparse_attribute_is_rejected() -> None:
    class FakeReparsePath:
        @staticmethod
        def is_symlink():
            return False

        @staticmethod
        def lstat():
            return SimpleNamespace(st_file_attributes=0x400)

    assert _is_reparse_point(FakeReparsePath()) is True


@pytest.mark.parametrize(
    "path",
    [
        r"\\server\share\file.txt",
        "//server/share/file.txt",
        r"\\?\C:\Windows\file.txt",
        r"\\.\PhysicalDrive0",
        "https://example.test/file.txt",
        "file:///C:/Windows/file.txt",
    ],
)
def test_disallowed_paths_are_rejected_before_filesystem_access(monkeypatch, path) -> None:
    def fail_if_touched(*_args, **_kwargs):
        raise AssertionError("filesystem access occurred")

    monkeypatch.setattr(Path, "exists", fail_if_touched)
    resolver = PathResolver()

    with pytest.raises(PathResolutionError):
        resolver.resolve(path)
