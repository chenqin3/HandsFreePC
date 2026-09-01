from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from handsfree_pc.paths import (
    AmbiguousPathError,
    PathCandidate,
    PathNotFoundError,
    PathResolutionError,
    PathResolver,
    PathSearchBudgetExceeded,
    PathSemanticSelectionError,
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


def test_deep_absolute_path_resolves_each_slightly_misspoken_component(tmp_path: Path) -> None:
    target = tmp_path / "年度招聘资料" / "已处理数据" / "最终汇总报告.txt"
    target.parent.mkdir(parents=True)
    target.write_text("verified", encoding="utf-8")
    query = str(target).replace("招聘", "招骋").replace("已处理", "已处里")
    query = query.replace("报告.txt", "报表.txt")
    resolver = PathResolver(threshold=0.72)

    assert resolver.resolve(query) == target.resolve()


def test_relative_path_can_be_spoken_one_verified_layer_at_a_time(tmp_path: Path) -> None:
    first = tmp_path / "年度招聘资料"
    second = first / "已处理数据"
    second.mkdir(parents=True)
    resolver = PathResolver(threshold=0.72)

    resolved_first = resolver.resolve_relative("年度招骋资料", current_directory=tmp_path)
    resolved_second = resolver.resolve_relative("已处理数据", current_directory=resolved_first)

    assert resolved_first == first.resolve()
    assert resolved_second == second.resolve()


def test_relative_fuzzy_candidates_must_have_a_clear_winner(tmp_path: Path) -> None:
    (tmp_path / "年度报告一").mkdir()
    (tmp_path / "年度报告二").mkdir()
    resolver = PathResolver(threshold=0.70)

    with pytest.raises(AmbiguousPathError):
        resolver.resolve_relative("年度报告", current_directory=tmp_path)


def test_bounded_child_selector_can_choose_an_ambiguous_layer_and_continue_tail(
    tmp_path: Path,
) -> None:
    first = tmp_path / "年度报告甲"
    target = tmp_path / "年度报告乙" / "已处理数据"
    first.mkdir()
    target.mkdir(parents=True)
    seen: list[tuple[str, tuple[str, ...]]] = []
    resolver = PathResolver(threshold=0.70)

    def choose(component: str, candidates: tuple[PathCandidate, ...]):
        seen.append((component, tuple(item.path.name for item in candidates)))
        return next(item for item in candidates if item.path.name == "年度报告乙")

    resolved = resolver.resolve_relative(
        r"年度报告\已处理数据",
        current_directory=tmp_path,
        ambiguous_child_selector=choose,
    )

    assert resolved == target.resolve()
    assert len(seen) == 1
    assert seen[0][0] == "年度报告"
    assert set(seen[0][1]) == {"年度报告甲", "年度报告乙"}


def test_bounded_child_selector_rejects_forged_and_stale_candidates(tmp_path: Path) -> None:
    first = tmp_path / "年度报告甲"
    second = tmp_path / "年度报告乙"
    first.mkdir()
    second.mkdir()
    resolver = PathResolver(threshold=0.70)

    def forged(_component: str, candidates: tuple[PathCandidate, ...]):
        return PathCandidate(candidates[0].path, candidates[0].score)

    with pytest.raises(PathSemanticSelectionError, match="unknown candidate"):
        resolver.resolve_relative(
            "年度报告",
            current_directory=tmp_path,
            ambiguous_child_selector=forged,
        )

    def stale(_component: str, candidates: tuple[PathCandidate, ...]):
        chosen = next(item for item in candidates if item.path.name == "年度报告乙")
        chosen.path.rmdir()
        return chosen

    with pytest.raises(PathSemanticSelectionError, match="changed|disappeared"):
        resolver.resolve_relative(
            "年度报告",
            current_directory=tmp_path,
            ambiguous_child_selector=stale,
        )


def test_bounded_child_selector_never_runs_for_budget_suffix_or_more_than_five(
    tmp_path: Path,
) -> None:
    calls = 0

    def should_not_run(_component: str, _candidates: tuple[PathCandidate, ...]):
        nonlocal calls
        calls += 1
        return None

    budget_root = tmp_path / "budget"
    budget_root.mkdir()
    for index in range(3):
        (budget_root / f"年度报告{index}").mkdir()
    with pytest.raises(PathSearchBudgetExceeded):
        PathResolver(threshold=0.70, max_entries=1).resolve_relative(
            "年度报告",
            current_directory=budget_root,
            ambiguous_child_selector=should_not_run,
        )

    suffix_root = tmp_path / "suffix"
    suffix_root.mkdir()
    (suffix_root / "report.xlsx").write_text("x", encoding="utf-8")
    (suffix_root / "report.pptx").write_text("x", encoding="utf-8")
    with pytest.raises(PathNotFoundError):
        PathResolver(threshold=0.70).resolve_relative(
            "report.docx",
            current_directory=suffix_root,
            ambiguous_child_selector=should_not_run,
        )

    crowded_root = tmp_path / "crowded"
    crowded_root.mkdir()
    for suffix in "甲乙丙丁戊己":
        (crowded_root / f"年度报告{suffix}").mkdir()
    with pytest.raises(AmbiguousPathError):
        PathResolver(threshold=0.70).resolve_relative(
            "年度报告",
            current_directory=crowded_root,
            ambiguous_child_selector=should_not_run,
        )

    assert calls == 0


def test_component_lookup_stops_at_the_configured_candidate_budget(tmp_path: Path) -> None:
    for index in range(6):
        (tmp_path / f"unrelated-{index}").mkdir()
    resolver = PathResolver(max_entries=3)

    with pytest.raises(PathSearchBudgetExceeded):
        resolver.resolve_relative("missing", current_directory=tmp_path)


def test_exact_deep_components_need_no_directory_enumeration_budget(tmp_path: Path) -> None:
    target = tmp_path / "exact-one" / "exact-two" / "exact-three"
    target.mkdir(parents=True)
    resolver = PathResolver(max_entries=1)

    assert resolver.resolve(str(target)) == target.resolve()


def test_fuzzy_component_does_not_accept_an_early_match_before_budget_exhaustion(
    tmp_path: Path,
) -> None:
    (tmp_path / "年度招聘资料").mkdir()
    for index in range(5):
        (tmp_path / f"unrelated-{index}").mkdir()
    resolver = PathResolver(threshold=0.70, max_entries=1)

    with pytest.raises(PathSearchBudgetExceeded):
        resolver.resolve_relative("年度招骋资料", current_directory=tmp_path)


def test_recursive_search_does_not_accept_early_match_when_exact_budget_leaves_queue(
    tmp_path: Path,
) -> None:
    early = tmp_path / "annual-report"
    early.mkdir()
    (early / "unscanned-duplicate.txt").write_text("later", encoding="utf-8")
    resolver = PathResolver(
        search_roots=[tmp_path],
        threshold=0.70,
        max_entries=1,
        max_depth=2,
    )

    with pytest.raises(PathSearchBudgetExceeded):
        resolver.resolve("annual report")


def test_explicit_file_extension_never_fuzzy_matches_a_different_extension(
    tmp_path: Path,
) -> None:
    (tmp_path / "report.xlsx").write_text("wrong type", encoding="utf-8")
    resolver = PathResolver(search_roots=[tmp_path], threshold=0.70)

    with pytest.raises(PathNotFoundError):
        resolver.resolve("report.docx")


def test_omitted_extension_can_still_match_a_unique_file_stem(tmp_path: Path) -> None:
    target = tmp_path / "annual-report.xlsx"
    target.write_text("ok", encoding="utf-8")
    resolver = PathResolver(search_roots=[tmp_path], threshold=0.70)

    assert resolver.resolve("annual report") == target.resolve()


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
