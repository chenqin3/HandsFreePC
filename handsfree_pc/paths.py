from __future__ import annotations

import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path, PureWindowsPath


class PathResolutionError(RuntimeError):
    pass


class PathNotFoundError(PathResolutionError):
    pass


@dataclass(slots=True)
class PathCandidate:
    path: Path
    score: float


class AmbiguousPathError(PathResolutionError):
    def __init__(self, query: str, candidates: list[PathCandidate]) -> None:
        super().__init__(f"Ambiguous path: {query}")
        self.query = query
        self.candidates = candidates


_CLASSIFIERS_RE = re.compile(r"(?:这个|那个)?(?:文件夹|目录|文件)$")
_URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _clean_component(value: str) -> str:
    return _CLASSIFIERS_RE.sub("", value.strip()).strip()


def is_disallowed_path_text(value: str) -> bool:
    """Reject remote, URI, and Win32 device namespace paths without I/O."""

    stripped = value.strip().strip('"')
    normalized = stripped.replace("/", "\\")
    if normalized.startswith("\\\\"):
        return True
    lowered = normalized.casefold()
    if lowered.startswith(("\\??\\", "\\device\\")):
        return True
    # A drive prefix such as C:\ is local; all other URI-like schemes are
    # outside the 0.1.0 local path contract.
    return bool(_URI_SCHEME_RE.match(stripped)) and not bool(
        re.match(r"(?i)^[a-z]:[\\/]", stripped)
    )


def _similarity(query: str, name: str) -> float:
    query_lower = query.casefold()
    name_lower = name.casefold()
    if query_lower == name_lower:
        return 1.0
    if query_lower in name_lower or name_lower in query_lower:
        return 0.94
    query_stem = Path(query).stem.casefold()
    name_stem = Path(name).stem.casefold()
    return SequenceMatcher(None, query_stem, name_stem).ratio()


def _is_reparse_point(path: Path) -> bool:
    """Reject symlinks and Windows junction/reparse entries during bounded search."""

    if path.is_symlink():
        return True
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _resolve_within(path: Path, root: Path) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None
    return resolved


class PathResolver:
    def __init__(
        self,
        *,
        aliases: dict[str, Path] | None = None,
        search_roots: Iterable[Path] = (),
        threshold: float = 0.78,
        max_results: int = 5,
        max_depth: int = 4,
        max_entries: int = 10_000,
    ) -> None:
        self.aliases = aliases or {}
        self.search_roots = [Path(item) for item in search_roots]
        self.threshold = threshold
        self.max_results = max_results
        self.max_depth = max_depth
        self.max_entries = max_entries

    def resolve(self, query: str, *, current_directory: Path | None = None) -> Path:
        raw = query.strip().strip('"')
        if is_disallowed_path_text(raw):
            raise PathResolutionError("Remote, URI, and device paths are not supported")
        expanded = os.path.expandvars(os.path.expanduser(raw))
        if is_disallowed_path_text(expanded):
            raise PathResolutionError("Expanded path is outside the local path contract")
        alias_path = self._expand_alias(expanded)
        if alias_path is not None:
            expanded = str(alias_path)
            if is_disallowed_path_text(expanded):
                raise PathResolutionError("Alias resolves outside the local path contract")

        direct = Path(expanded)
        if direct.exists():
            return direct.resolve()

        if re.match(r"(?i)^[a-z]:[\\/]", expanded):
            return self._resolve_components(PureWindowsPath(expanded))

        if current_directory is not None:
            local = current_directory / expanded
            if local.exists():
                return local.resolve()

        candidates = self._search(expanded, current_directory=current_directory)
        if not candidates:
            raise PathNotFoundError(f"No path matched: {query}")
        best = candidates[0]
        tied = [item for item in candidates if best.score - item.score < 0.03]
        if len(tied) > 1:
            raise AmbiguousPathError(query, tied[: self.max_results])
        return best.path.resolve()

    def _expand_alias(self, query: str) -> Path | None:
        normalized = query.replace("/", "\\")
        for alias, base in sorted(
            self.aliases.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if normalized == alias:
                return base
            if normalized.startswith(f"{alias}\\"):
                return base / normalized[len(alias) + 1 :]
        return None

    def _resolve_components(self, requested: PureWindowsPath) -> Path:
        anchor = Path(requested.anchor)
        if not anchor.exists():
            raise PathNotFoundError(f"Drive does not exist: {requested.anchor}")
        current = anchor
        for raw_component in requested.parts[1:]:
            component = _clean_component(raw_component)
            if not component:
                continue
            exact = current / component
            if exact.exists():
                current = exact
                continue
            candidates = self._children(current, component)
            if not candidates:
                raise PathNotFoundError(f"No match for '{component}' under {current}")
            best = candidates[0]
            tied = [item for item in candidates if best.score - item.score < 0.03]
            if len(tied) > 1:
                raise AmbiguousPathError(str(requested), tied[: self.max_results])
            current = best.path
        return current.resolve()

    def _children(self, directory: Path, query: str) -> list[PathCandidate]:
        try:
            entries = list(directory.iterdir())
        except (OSError, PermissionError):
            return []
        result = [
            PathCandidate(item, _similarity(query, item.name))
            for item in entries
            if _similarity(query, item.name) >= self.threshold
        ]
        return sorted(result, key=lambda item: (-item.score, len(str(item.path))))

    def _search(self, query: str, *, current_directory: Path | None) -> list[PathCandidate]:
        roots = ([current_directory] if current_directory else []) + self.search_roots
        results: list[PathCandidate] = []
        seen: set[Path] = set()
        visited = 0
        for root in roots:
            if root is None or not root.exists():
                continue
            root = root.resolve()
            queue: list[tuple[Path, int]] = [(root, 0)]
            while queue and visited < self.max_entries:
                directory, depth = queue.pop(0)
                if _resolve_within(directory, root) is None:
                    continue
                try:
                    entries = list(directory.iterdir())
                except (OSError, PermissionError):
                    continue
                for item in entries:
                    visited += 1
                    if _is_reparse_point(item):
                        continue
                    resolved_item = _resolve_within(item, root)
                    if resolved_item is None or resolved_item in seen:
                        continue
                    seen.add(resolved_item)
                    score = _similarity(query, resolved_item.name)
                    if score >= self.threshold:
                        results.append(PathCandidate(resolved_item, score))
                    if resolved_item.is_dir() and depth < self.max_depth:
                        queue.append((resolved_item, depth + 1))
                    if visited >= self.max_entries:
                        break
        results.sort(key=lambda item: (-item.score, len(str(item.path))))
        return results[: self.max_results]
