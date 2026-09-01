from __future__ import annotations

import os
import re
import stat
import unicodedata
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path, PureWindowsPath


class PathResolutionError(RuntimeError):
    pass


class PathNotFoundError(PathResolutionError):
    pass


class PathSearchBudgetExceeded(PathResolutionError):
    """The bounded filesystem lookup stopped before it could prove a result."""


class PathSemanticSelectionError(PathResolutionError):
    """A bounded ambiguous-child selection could not be rebound locally."""


@dataclass(slots=True)
class PathCandidate:
    path: Path
    score: float


@dataclass(frozen=True, slots=True)
class PathIdentityBinding:
    """A local path object and its already-observed parent identity."""

    path: Path
    identity: tuple[int, int, int, int, int, int]
    parent: Path
    parent_identity: tuple[int, int, int, int, int, int]


PathChildSelector = Callable[
    [str, tuple[PathCandidate, ...]],
    PathCandidate | None,
]


@dataclass(frozen=True, slots=True)
class _BoundChildCandidate:
    candidate: PathCandidate
    basename: str
    identity: tuple[int, int, int, int, int, int]


class AmbiguousPathError(PathResolutionError):
    def __init__(
        self,
        query: str,
        candidates: list[PathCandidate],
        *,
        candidate_count: int | None = None,
    ) -> None:
        super().__init__(f"Ambiguous path: {query}")
        self.query = query
        self.candidates = candidates
        self.candidate_count = candidate_count or len(candidates)


_CLASSIFIERS_RE = re.compile(r"(?:这个|那个)?(?:文件夹|目录|文件)$")
_URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
_COMPONENT_PUNCTUATION_RE = re.compile(r"[\s\-_—–·:：,，.。（）()\[\]【】]+")


def _clean_component(value: str) -> str:
    return _CLASSIFIERS_RE.sub("", value.strip()).strip()


def _normalized_component(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", _clean_component(value)).casefold()
    return _COMPONENT_PUNCTUATION_RE.sub("", normalized)


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
    query_lower = _normalized_component(query)
    name_lower = _normalized_component(name)
    if not query_lower or not name_lower:
        return 0.0
    # A spoken/typed extension is an explicit part of the requested identity.
    # Stem similarity must never turn ``report.docx`` into ``report.xlsx``.
    # When the user omits an extension we still allow the normal fuzzy stem
    # lookup, which is useful for natural speech such as "open the report".
    query_suffix = unicodedata.normalize("NFKC", Path(_clean_component(query)).suffix).casefold()
    name_suffix = unicodedata.normalize("NFKC", Path(_clean_component(name)).suffix).casefold()
    if query_suffix and query_suffix != name_suffix:
        return 0.0
    if query_lower == name_lower:
        return 1.0
    if query_lower in name_lower or name_lower in query_lower:
        return 0.94
    query_stem = _normalized_component(Path(query).stem)
    name_stem = _normalized_component(Path(name).stem)
    return SequenceMatcher(None, query_stem, name_stem).ratio()


@dataclass(slots=True)
class _SearchBudget:
    remaining: int
    exhausted: bool = False

    def note(self) -> None:
        if self.remaining <= 0:
            self.exhausted = True
            raise PathSearchBudgetExceeded("Path lookup candidate budget was exhausted")
        self.remaining -= 1


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


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(value.st_mode),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
        int(value.st_dev),
        int(value.st_ino),
    )


def bind_path_identity(path: Path) -> PathIdentityBinding:
    """Bind one existing non-reparse file/directory to its current object."""

    try:
        resolved = Path(path).resolve(strict=True)
        parent = resolved.parent.resolve(strict=True)
        if _is_reparse_point(resolved) or (parent != resolved and _is_reparse_point(parent)):
            raise PathSemanticSelectionError("Path identity binding rejects reparse points")
        before = resolved.lstat()
        parent_before = parent.lstat()
        if not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)):
            raise PathSemanticSelectionError(
                "Path identity binding requires a regular file or directory"
            )
        rebound = _resolve_within(resolved, parent)
        after = resolved.lstat()
        parent_after = parent.lstat()
    except PathResolutionError:
        raise
    except (OSError, RuntimeError) as exc:
        raise PathSemanticSelectionError("Path identity could not be bound") from exc
    if rebound != resolved or _stat_identity(before) != _stat_identity(after):
        raise PathSemanticSelectionError("Path identity changed while it was being bound")
    if _stat_identity(parent_before) != _stat_identity(parent_after):
        raise PathSemanticSelectionError("Path parent changed while it was being bound")
    return PathIdentityBinding(
        path=resolved,
        identity=_stat_identity(before),
        parent=parent,
        parent_identity=_stat_identity(parent_before),
    )


def verify_path_identity_binding(binding: PathIdentityBinding) -> bool:
    """Return whether a bound path still denotes the same non-reparse object."""

    if not isinstance(binding, PathIdentityBinding):
        return False
    try:
        if _is_reparse_point(binding.path) or (
            binding.parent != binding.path and _is_reparse_point(binding.parent)
        ):
            return False
        current = binding.path.lstat()
        parent_current = binding.parent.lstat()
        resolved_parent = binding.parent.resolve(strict=True)
        resolved = _resolve_within(binding.path, resolved_parent)
    except (OSError, RuntimeError):
        return False
    return bool(
        resolved_parent == binding.parent
        and resolved == binding.path
        and _stat_identity(current) == binding.identity
        and _stat_identity(parent_current) == binding.parent_identity
        and (stat.S_ISDIR(current.st_mode) or stat.S_ISREG(current.st_mode))
    )


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
        ambiguity_margin: float = 0.04,
        max_components: int = 32,
    ) -> None:
        self.aliases = aliases or {}
        self.search_roots = [Path(item) for item in search_roots]
        self.threshold = threshold
        self.max_results = max_results
        self.max_depth = max_depth
        self.max_entries = max_entries
        self.ambiguity_margin = ambiguity_margin
        self.max_components = max_components
        if not 0.0 <= self.threshold <= 1.0:
            raise ValueError("Path threshold must be between 0 and 1")
        if not 0.0 <= self.ambiguity_margin <= 1.0:
            raise ValueError("Path ambiguity margin must be between 0 and 1")
        if self.max_results < 1 or self.max_depth < 1 or self.max_entries < 1:
            raise ValueError("Path lookup limits must be positive")
        if self.max_components < 1:
            raise ValueError("Path component limit must be positive")

    def resolve(
        self,
        query: str,
        *,
        current_directory: Path | None = None,
        ambiguous_child_selector: PathChildSelector | None = None,
        semantic_bindings: list[PathIdentityBinding] | None = None,
    ) -> Path:
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
            return self._resolve_components(
                PureWindowsPath(expanded),
                ambiguous_child_selector=ambiguous_child_selector,
                semantic_bindings=semantic_bindings,
            )

        if current_directory is not None:
            local = current_directory / expanded
            if local.exists():
                return local.resolve()

        candidates = self._search(expanded, current_directory=current_directory)
        if not candidates:
            raise PathNotFoundError(f"No path matched: {query}")
        return self._select_unique(query, candidates).path.resolve()

    def resolve_relative(
        self,
        query: str,
        *,
        current_directory: Path,
        ambiguous_child_selector: PathChildSelector | None = None,
        semantic_bindings: list[PathIdentityBinding] | None = None,
    ) -> Path:
        """Resolve a continuation below one already verified directory.

        This is deliberately component-by-component. It never falls back to a
        recursive whole-disk search, and every selected component must exist.
        """

        raw = query.strip().strip('"').replace("/", "\\")
        if not raw or is_disallowed_path_text(raw):
            raise PathResolutionError("Relative path is empty or outside the local contract")
        requested = PureWindowsPath(raw)
        if requested.is_absolute() or requested.drive or requested.root:
            raise PathResolutionError("A path continuation must remain relative")
        components = tuple(
            _clean_component(part) for part in requested.parts if part not in {"", "."}
        )
        if not components or any(part in {"", ".."} or ":" in part for part in components):
            raise PathResolutionError("Path continuation contains an unsafe component")
        if len(components) > self.max_components:
            raise PathResolutionError("Path continuation has too many components")
        root = Path(current_directory).resolve(strict=True)
        if not root.is_dir():
            raise PathResolutionError("Path continuation requires an existing directory")
        budget = _SearchBudget(self.max_entries)
        current = root
        for component in components:
            current = self._resolve_child(
                current,
                component,
                budget=budget,
                query=query,
                ambiguous_child_selector=ambiguous_child_selector,
                semantic_bindings=semantic_bindings,
            )
            try:
                current.relative_to(root)
            except ValueError as exc:
                raise PathResolutionError(
                    "Resolved continuation escaped its verified root"
                ) from exc
        return current.resolve(strict=True)

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

    def _resolve_components(
        self,
        requested: PureWindowsPath,
        *,
        ambiguous_child_selector: PathChildSelector | None = None,
        semantic_bindings: list[PathIdentityBinding] | None = None,
    ) -> Path:
        anchor = Path(requested.anchor)
        if not anchor.exists():
            raise PathNotFoundError(f"Drive does not exist: {requested.anchor}")
        components = tuple(requested.parts[1:])
        if len(components) > self.max_components:
            raise PathResolutionError("Path has too many components")
        current = anchor
        budget = _SearchBudget(self.max_entries)
        for raw_component in components:
            component = _clean_component(raw_component)
            if not component:
                continue
            current = self._resolve_child(
                current,
                component,
                budget=budget,
                query=str(requested),
                ambiguous_child_selector=ambiguous_child_selector,
                semantic_bindings=semantic_bindings,
            )
        return current.resolve()

    def _resolve_child(
        self,
        directory: Path,
        component: str,
        *,
        budget: _SearchBudget,
        query: str,
        ambiguous_child_selector: PathChildSelector | None = None,
        semantic_bindings: list[PathIdentityBinding] | None = None,
    ) -> Path:
        exact = directory / component
        if exact.exists():
            return exact.resolve(strict=True)
        candidates = self._children(directory, component, budget=budget)
        if not candidates:
            raise PathNotFoundError(f"No match for '{component}' under {directory}")
        try:
            return self._select_unique(query, candidates).path.resolve(strict=True)
        except AmbiguousPathError as ambiguity:
            if ambiguous_child_selector is None or not 2 <= ambiguity.candidate_count <= 5:
                raise
            return self._select_and_rebind_child(
                directory,
                component,
                ambiguity,
                ambiguous_child_selector,
                semantic_bindings=semantic_bindings,
            )

    def _select_and_rebind_child(
        self,
        directory: Path,
        component: str,
        ambiguity: AmbiguousPathError,
        selector: PathChildSelector,
        *,
        semantic_bindings: list[PathIdentityBinding] | None,
    ) -> Path:
        """Select one ambiguous immediate child, then prove it did not change.

        The callback may use semantic help, but it receives only a bounded
        in-memory candidate tuple.  The filesystem path returned by that help
        is never trusted: only the exact candidate object supplied here is
        accepted, and its parent, reparse status, type, and stat identity are
        checked again after the callback returns.
        """

        try:
            parent = directory.resolve(strict=True)
            parent_before = _stat_identity(parent.lstat())
        except (OSError, RuntimeError) as exc:
            raise PathSemanticSelectionError(
                "Ambiguous child parent could not be bound"
            ) from exc
        if not parent.is_dir():
            raise PathSemanticSelectionError("Ambiguous child parent is not a directory")

        bound: list[_BoundChildCandidate] = []
        for original in ambiguity.candidates:
            basename = original.path.name
            if not basename or basename in {".", ".."}:
                continue
            unresolved = parent / basename
            try:
                if _is_reparse_point(unresolved):
                    continue
                before = unresolved.lstat()
                if not (stat.S_ISDIR(before.st_mode) or stat.S_ISREG(before.st_mode)):
                    continue
                resolved = _resolve_within(unresolved, parent)
                after = unresolved.lstat()
            except OSError:
                continue
            if resolved is None or resolved.parent != parent:
                continue
            identity = _stat_identity(before)
            if identity != _stat_identity(after):
                continue
            candidate = PathCandidate(resolved, original.score)
            bound.append(
                _BoundChildCandidate(
                    candidate=candidate,
                    basename=basename,
                    identity=identity,
                )
            )
        if len(bound) != ambiguity.candidate_count or not 2 <= len(bound) <= 5:
            raise PathSemanticSelectionError(
                "Ambiguous child candidates changed before semantic selection"
            )

        try:
            selected = selector(component, tuple(item.candidate for item in bound))
        except PathResolutionError:
            raise
        except Exception as exc:
            raise PathSemanticSelectionError("Ambiguous child selector failed") from exc
        selected_binding = next(
            (item for item in bound if selected is item.candidate),
            None,
        )
        if selected_binding is None:
            raise PathSemanticSelectionError(
                "Ambiguous child selector declined or returned an unknown candidate"
            )

        unresolved = parent / selected_binding.basename
        try:
            if _stat_identity(parent.lstat()) != parent_before or not parent.is_dir():
                raise PathSemanticSelectionError(
                    "Ambiguous child parent changed during semantic selection"
                )
            if _is_reparse_point(unresolved):
                raise PathSemanticSelectionError(
                    "Selected child became a reparse point during semantic selection"
                )
            rebound_stat = unresolved.lstat()
            rebound = _resolve_within(unresolved, parent)
        except PathSemanticSelectionError:
            raise
        except OSError as exc:
            raise PathSemanticSelectionError(
                "Selected child disappeared during semantic selection"
            ) from exc
        if rebound is None or rebound.parent != parent:
            raise PathSemanticSelectionError("Selected child escaped its verified parent")
        if _stat_identity(rebound_stat) != selected_binding.identity:
            raise PathSemanticSelectionError(
                "Selected child changed during semantic selection"
            )
        if _similarity(component, selected_binding.basename) < self.threshold:
            raise PathSemanticSelectionError(
                "Selected child no longer matches the spoken component"
            )
        if semantic_bindings is not None:
            semantic_binding = PathIdentityBinding(
                path=rebound,
                identity=selected_binding.identity,
                parent=parent,
                parent_identity=parent_before,
            )
            if not verify_path_identity_binding(semantic_binding):
                raise PathSemanticSelectionError(
                    "Selected child changed before its identity could be retained"
                )
            semantic_bindings.append(semantic_binding)
        return rebound

    def _select_unique(self, query: str, candidates: list[PathCandidate]) -> PathCandidate:
        best = candidates[0]
        competing = [
            item
            for item in candidates[1:]
            if best.score - item.score < self.ambiguity_margin
        ]
        if competing:
            ambiguous = [best, *competing]
            raise AmbiguousPathError(
                query,
                ambiguous[: self.max_results],
                candidate_count=len(ambiguous),
            )
        return best

    @staticmethod
    def _bounded_entries(directory: Path, budget: _SearchBudget) -> Iterable[Path]:
        try:
            iterator = directory.iterdir()
            while budget.remaining > 0:
                try:
                    item = next(iterator)
                except StopIteration:
                    return
                budget.note()
                yield item
            try:
                next(iterator)
            except StopIteration:
                return
            budget.exhausted = True
            raise PathSearchBudgetExceeded("Path lookup candidate budget was exhausted")
        except (PermissionError, FileNotFoundError, NotADirectoryError):
            return

    def _children(
        self,
        directory: Path,
        query: str,
        *,
        budget: _SearchBudget | None = None,
    ) -> list[PathCandidate]:
        active_budget = budget or _SearchBudget(self.max_entries)
        try:
            entries = self._bounded_entries(directory, active_budget)
            result = [
                PathCandidate(item, score)
                for item in entries
                if not _is_reparse_point(item)
                and (score := _similarity(query, item.name)) >= self.threshold
            ]
        except (OSError, PermissionError):
            return []
        return sorted(result, key=lambda item: (-item.score, len(str(item.path))))

    def _search(self, query: str, *, current_directory: Path | None) -> list[PathCandidate]:
        roots = ([current_directory] if current_directory else []) + self.search_roots
        results: list[PathCandidate] = []
        seen: set[Path] = set()
        budget = _SearchBudget(self.max_entries)
        for root in roots:
            if root is None or not root.exists():
                continue
            if budget.remaining <= 0:
                # An earlier root consumed the exact budget.  A later root has
                # not been inspected, so uniqueness cannot be proved.
                raise PathSearchBudgetExceeded("Path lookup candidate budget was exhausted")
            root = root.resolve()
            queue: deque[tuple[Path, int]] = deque([(root, 0)])
            while queue and budget.remaining > 0:
                directory, depth = queue.popleft()
                if _resolve_within(directory, root) is None:
                    continue
                try:
                    entries = self._bounded_entries(directory, budget)
                except (OSError, PermissionError):
                    continue
                for item in entries:
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
            if queue:
                # Consuming exactly the final budget item can leave queued
                # directories without causing ``_bounded_entries`` itself to
                # request one more item.  Treat that as unproven, even if an
                # attractive early match was already found.
                budget.exhausted = True
        if budget.exhausted:
            raise PathSearchBudgetExceeded("Path lookup candidate budget was exhausted")
        results.sort(key=lambda item: (-item.score, len(str(item.path))))
        return results[: self.max_results]
