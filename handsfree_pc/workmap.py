from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from pathlib import Path, PureWindowsPath

_PROFILE_HEADER_LINE_LIMIT = 32
_PROJECT_ROW_RE = re.compile(
    r"^\|\s*\[(?P<title>[^\]]+)\]\((?P<link>projects[/\\].+?\.md)\)[^|]*\|"
    r"\s*(?P<sessions>[^|]*)\|\s*(?P<recent>[^|]*)\|\s*(?P<summary>[^|]*)\|\s*$"
)
_EXPLICIT_ROOT_RE = re.compile(
    r"^\s*[-*]?\s*(?:项目根目录|项目路径|项目目录|路径)\s*[:：]\s*"
    r"`(?P<path>[A-Za-z]:[\\/][^`]+)`"
)
_OPEN_REQUEST_RE = re.compile(
    r"^\s*(?:请\s*)?(?:打开|进入|查看)\s*(?P<target>.+?)\s*$",
    re.IGNORECASE,
)
_NEGATION_RE = re.compile(r"(?:不要|别再?|不许|禁止|无需|不用|取消)\s*(?:打开|进入|查看)")
_QUOTE_RE = re.compile(r"[\"'“”‘’「」『』]")
_MULTI_CLAUSE_RE = re.compile(r"(?:然后|接着|随后|同时|并且|再|；|;|\r|\n|，|,)")
_TRAILING_SENTENCE_RE = re.compile(r"[。！!？?]+$")
_WINDOWS_PATH_RE = re.compile(r"(?i)(?:[A-Z]:[\\/]|\\\\).*?(?=$|[，。；;|])")
_POSIX_LOCAL_PATH_RE = re.compile(r"(?i)(?:/mnt/[a-z]/|/(?:home|users?|root)/).*?(?=$|[，。；;|])")
_ENV_LOCAL_PATH_RE = re.compile(
    r"(?i)%(?:USERPROFILE|HOME|LOCALAPPDATA|APPDATA)%[\\/].*?(?=$|[，。；;|])"
)
_URI_RE = re.compile(r"(?i)\b(?:https?|file|ssh|smb)://[^\s，。；;|]+")
_MARKDOWN_CODE_RE = re.compile(r"`([^`]*)`")
_HOME_PATH_RE = re.compile(r"(?i)(?:~[\\/]).*?(?=$|[，。；;|])")
_CREDENTIAL_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|aws[_-]?secret[_-]?access[_-]?key|"
    r"authorization|bearer|token|password|secret|credential)\s*[:=]\s*"
    r"(?:bearer\s+)?[^\s，。；;|`]+"
)
_KNOWN_TOKEN_RE = re.compile(
    r"(?i)\b(?:sk|ghp|github_pat|xox[baprs]|AIza|AKIA)[-_A-Za-z0-9]{12,}\b"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{8,}\b")
_SPACE_RE = re.compile(r"\s+")
_PUNCTUATION_RE = re.compile(r"[\s\-_—–·:：,，.。/\\()（）\[\]【】]+")


class WorkMapError(RuntimeError):
    """Base error for a malformed or unavailable WorkMap index."""


class WorkMapConfigurationError(WorkMapError):
    """Raised when an explicit alias cannot be bound safely."""


@dataclass(frozen=True, slots=True)
class WorkMapAliasTarget:
    """A local alias binding to a project and an optional path below its root."""

    project: str
    relative_path: str | None = None


@dataclass(frozen=True, slots=True)
class WorkMapProject:
    """Minimal metadata parsed from the generated project table and profile header."""

    project_id: str
    title: str
    summary: str
    recent_activity: str
    root: Path | None


@dataclass(frozen=True, slots=True)
class WorkMapCandidate:
    """Path-free candidate metadata suitable for a bounded planner hint."""

    target_id: str
    project_id: str
    project_title: str
    title: str
    summary: str
    score: float
    has_local_root: bool
    target_available: bool

    def planner_hint(self) -> dict[str, object]:
        return {
            "target_id": self.target_id,
            "display_name": _safe_summary(self.title, maximum=100),
            "project_name": _safe_summary(self.project_title, maximum=100),
            "summary": self.summary,
            "score": round(self.score, 4),
            "has_local_root": self.has_local_root,
            "target_available": self.target_available,
        }


@dataclass(frozen=True, slots=True)
class _AliasBinding:
    alias: str
    project_id: str
    relative_parts: tuple[str, ...]


AliasValue = WorkMapAliasTarget | str | Mapping[str, object]


def _normalize_key(value: str) -> str:
    return _PUNCTUATION_RE.sub("", value).casefold()


def _normalize_search(value: str) -> str:
    normalized = _normalize_key(value)
    for source, replacement in (
        ("数据仓库", "库"),
        ("数据库", "库"),
        ("数据仓", "库"),
    ):
        normalized = normalized.replace(source, replacement)
    return normalized


def _safe_summary(value: str, *, maximum: int = 160) -> str:
    compact = _SPACE_RE.sub(" ", value).strip()
    compact = _URI_RE.sub("[link]", compact)
    compact = _WINDOWS_PATH_RE.sub("[local path]", compact)
    compact = _POSIX_LOCAL_PATH_RE.sub("[local path]", compact)
    compact = _ENV_LOCAL_PATH_RE.sub("[local path]", compact)
    compact = _HOME_PATH_RE.sub("[local path]", compact)
    compact = _CREDENTIAL_ASSIGNMENT_RE.sub("<redacted-credential>", compact)
    compact = _KNOWN_TOKEN_RE.sub("<redacted-credential>", compact)
    compact = _JWT_RE.sub("<redacted-credential>", compact)
    compact = _MARKDOWN_CODE_RE.sub("[code omitted]", compact)
    return compact[:maximum]


def _coerce_alias_target(value: AliasValue) -> WorkMapAliasTarget:
    if isinstance(value, WorkMapAliasTarget):
        return value
    if isinstance(value, str):
        return WorkMapAliasTarget(project=value)
    if not isinstance(value, Mapping):
        raise WorkMapConfigurationError("WorkMap alias target must be a string or mapping")
    unknown_fields = set(value) - {"project", "relative_path"}
    if unknown_fields:
        raise WorkMapConfigurationError("WorkMap alias target contains unknown fields")
    project = value.get("project")
    relative_path = value.get("relative_path")
    if not isinstance(project, str) or not project.strip():
        raise WorkMapConfigurationError("WorkMap alias target requires a non-empty project")
    if relative_path is not None and not isinstance(relative_path, str):
        raise WorkMapConfigurationError("WorkMap alias relative_path must be a string or null")
    return WorkMapAliasTarget(project=project, relative_path=relative_path)


def _safe_relative_parts(value: str | None) -> tuple[str, ...]:
    if value is None or not value.strip():
        return ()
    candidate = PureWindowsPath(value.strip())
    if candidate.is_absolute() or candidate.drive or candidate.root:
        raise WorkMapConfigurationError("WorkMap alias relative_path must stay below the project")
    parts = tuple(part for part in candidate.parts if part not in {"", "."})
    if not parts or any(part == ".." or ":" in part for part in parts):
        raise WorkMapConfigurationError("WorkMap alias relative_path is unsafe")
    return parts


def _profile_root(profile_path: Path) -> Path | None:
    """Read only a bounded profile header and accept an explicitly labelled Windows root."""

    section = "preamble"
    try:
        with profile_path.open("r", encoding="utf-8", errors="strict") as handle:
            for line_number, line in enumerate(handle, start=1):
                if line_number > _PROFILE_HEADER_LINE_LIMIT:
                    break
                stripped = line.strip()
                if stripped.startswith("## "):
                    heading = stripped.removeprefix("## ").strip()
                    if heading.startswith("位置与"):
                        section = "location"
                        continue
                    break
                if section not in {"preamble", "location"}:
                    continue
                match = _EXPLICIT_ROOT_RE.match(line)
                if match is not None:
                    return Path(match.group("path"))
    except (OSError, UnicodeError):
        return None
    return None


def _score(query: str, candidate: str) -> float:
    query_key = _normalize_search(query)
    candidate_key = _normalize_search(candidate)
    if not query_key or not candidate_key:
        return 0.0
    if query_key == candidate_key:
        return 1.0
    if query_key in candidate_key or candidate_key in query_key:
        return 0.94
    return SequenceMatcher(None, query_key, candidate_key).ratio()


def _binding_identity(binding: _AliasBinding) -> tuple[str, tuple[str, ...]]:
    return binding.project_id.casefold(), binding.relative_parts


def _binding_target_id(binding: _AliasBinding) -> str:
    project_id, relative_parts = _binding_identity(binding)
    payload = "\0".join((project_id, *relative_parts)).encode("utf-8")
    return f"wm-{sha256(payload).hexdigest()[:20]}"


class WorkMapIndex:
    """Read-only local lookup over WorkMap project metadata.

    Only ``WORKMAP.md`` table rows and the first few lines of linked profiles are
    read. Profile narratives, code spans, credentials, and historical body text
    are deliberately outside this index.
    """

    def __init__(
        self,
        projects: Sequence[WorkMapProject],
        *,
        aliases: Mapping[str, AliasValue] | None = None,
    ) -> None:
        self.projects = tuple(projects)
        self._by_id = {project.project_id.casefold(): project for project in self.projects}
        self._by_title: dict[str, list[WorkMapProject]] = {}
        for project in self.projects:
            self._by_title.setdefault(_normalize_key(project.title), []).append(project)
        bindings: dict[str, list[_AliasBinding]] = {}
        for project in self.projects:
            self._append_binding(
                bindings,
                project.title,
                _AliasBinding(project.title, project.project_id, ()),
            )
        for alias, raw_target in (aliases or {}).items():
            if not isinstance(alias, str) or not alias.strip():
                raise WorkMapConfigurationError("WorkMap alias names must be non-empty strings")
            target = _coerce_alias_target(raw_target)
            project = self._project_for_reference(target.project)
            binding = _AliasBinding(
                alias=alias,
                project_id=project.project_id,
                relative_parts=_safe_relative_parts(target.relative_path),
            )
            self._append_binding(bindings, alias, binding)
        self._bindings = {key: tuple(dict.fromkeys(values)) for key, values in bindings.items()}

    @classmethod
    def load(
        cls,
        out_directory: str | Path,
        *,
        aliases: Mapping[str, AliasValue] | None = None,
    ) -> WorkMapIndex:
        out_dir = Path(out_directory).resolve()
        workmap_path = out_dir / "WORKMAP.md"
        projects_dir = (out_dir / "projects").resolve()
        try:
            handle = workmap_path.open("r", encoding="utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            raise WorkMapError("WorkMap project table could not be read") from exc

        projects: list[WorkMapProject] = []
        seen_ids: set[str] = set()
        in_project_index = False
        project_index_closed = False
        try:
            with handle:
                for line in handle:
                    stripped = line.strip()
                    if stripped == "## 项目索引":
                        in_project_index = True
                        continue
                    if not in_project_index:
                        continue
                    if stripped == "---":
                        project_index_closed = True
                        break
                    match = _PROJECT_ROW_RE.match(line)
                    if match is None:
                        continue
                    relative_profile = Path(match.group("link").replace("/", "\\"))
                    profile_path = (out_dir / relative_profile).resolve()
                    try:
                        profile_path.relative_to(projects_dir)
                    except ValueError:
                        continue
                    project_id = profile_path.stem
                    folded_id = project_id.casefold()
                    if folded_id in seen_ids:
                        continue
                    seen_ids.add(folded_id)
                    projects.append(
                        WorkMapProject(
                            project_id=project_id,
                            title=match.group("title").strip(),
                            summary=_safe_summary(match.group("summary")),
                            recent_activity=match.group("recent").strip()[:32],
                            root=_profile_root(profile_path),
                        )
                    )
        except (OSError, UnicodeError) as exc:
            raise WorkMapError("WorkMap project table could not be read") from exc
        if not in_project_index:
            raise WorkMapError("WorkMap project index section was not found")
        if not project_index_closed:
            raise WorkMapError("WorkMap project index section was not closed")
        return cls(projects, aliases=aliases)

    @staticmethod
    def _append_binding(
        bindings: dict[str, list[_AliasBinding]],
        alias: str,
        binding: _AliasBinding,
    ) -> None:
        normalized = _normalize_key(alias)
        if normalized:
            bindings.setdefault(normalized, []).append(binding)

    def _project_for_reference(self, reference: str) -> WorkMapProject:
        by_id = self._by_id.get(reference.strip().casefold())
        if by_id is not None:
            return by_id
        by_title = self._by_title.get(_normalize_key(reference), [])
        if len(by_title) != 1:
            raise WorkMapConfigurationError(
                "WorkMap alias project must name exactly one project title or id"
            )
        return by_title[0]

    def _existing_target(self, binding: _AliasBinding) -> Path | None:
        project = self._by_id.get(binding.project_id.casefold())
        if project is None or project.root is None:
            return None
        try:
            root = project.root.resolve(strict=True)
            target = root.joinpath(*binding.relative_parts).resolve(strict=True)
            target.relative_to(root)
        except (OSError, RuntimeError, ValueError):
            return None
        return target

    def resolve_open_request(self, text: str) -> Path | None:
        """Resolve one complete affirmative open request, never a partial clause."""

        target_text = self._open_request_target(text)
        if target_text is None:
            return None
        bindings = self._bindings.get(_normalize_key(target_text), ())
        if not bindings:
            return None
        unique_bindings = {_binding_identity(binding): binding for binding in bindings}
        if len(unique_bindings) != 1:
            return None
        return self._existing_target(next(iter(unique_bindings.values())))

    @staticmethod
    def _open_request_target(text: str) -> str | None:
        if not isinstance(text, str) or not text.strip():
            return None
        if _QUOTE_RE.search(text) or _NEGATION_RE.search(text) or _MULTI_CLAUSE_RE.search(text):
            return None
        command = _TRAILING_SENTENCE_RE.sub("", text.strip()).strip()
        match = _OPEN_REQUEST_RE.fullmatch(command)
        if match is None:
            return None
        target_text = match.group("target").strip()
        if not target_text or re.search(r"(?:打开|进入|查看)", target_text):
            return None
        return target_text

    def resolve_unique_name(
        self,
        query: str,
        *,
        minimum_score: float = 0.78,
        ambiguity_margin: float = 0.06,
    ) -> Path | None:
        """Resolve a project/alias name only when one candidate clearly wins.

        This method is the bounded local counterpart to planner candidate
        selection.  It does not search project contents and it never treats a
        missing or tied candidate as a successful match.
        """

        if not isinstance(query, str) or not query.strip():
            return None
        if len(_normalize_search(query)) < 3:
            return None
        if not 0.0 <= float(minimum_score) <= 1.0:
            raise ValueError("WorkMap minimum_score must be between 0 and 1")
        if not 0.0 <= float(ambiguity_margin) <= 1.0:
            raise ValueError("WorkMap ambiguity_margin must be between 0 and 1")
        ranked: dict[str, tuple[float, _AliasBinding]] = {}
        for bindings in self._bindings.values():
            for binding in bindings:
                target_id = _binding_target_id(binding)
                score = _score(query, binding.alias)
                current = ranked.get(target_id)
                if current is None or score > current[0]:
                    ranked[target_id] = (score, binding)
        ordered = sorted(
            (
                (score, target_id, binding)
                for target_id, (score, binding) in ranked.items()
                if score >= float(minimum_score)
            ),
            key=lambda item: (-item[0], item[1]),
        )
        if not ordered:
            return None
        best_score, _best_id, best_binding = ordered[0]
        if len(ordered) > 1 and best_score - ordered[1][0] < float(ambiguity_margin):
            return None
        return self._existing_target(best_binding)

    def resolve_candidate_id(self, target_id: str) -> Path | None:
        """Bind one opaque planner candidate id back to an existing local path."""

        if not isinstance(target_id, str) or not target_id.startswith("wm-"):
            return None
        matches: dict[tuple[str, tuple[str, ...]], _AliasBinding] = {}
        for bindings in self._bindings.values():
            for binding in bindings:
                if _binding_target_id(binding) == target_id:
                    matches[_binding_identity(binding)] = binding
        if len(matches) != 1:
            return None
        return self._existing_target(next(iter(matches.values())))

    def search_candidates(
        self,
        query: str,
        *,
        limit: int = 5,
        minimum_score: float = 0.55,
    ) -> tuple[WorkMapCandidate, ...]:
        """Return path-free metadata for local ranking or a separately gated planner hint."""

        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > 20:
            raise ValueError("WorkMap candidate limit must be between 1 and 20")
        if (
            isinstance(minimum_score, bool)
            or not isinstance(minimum_score, int | float)
            or minimum_score < 0.0
            or minimum_score > 1.0
        ):
            raise ValueError("WorkMap minimum_score must be between 0 and 1")
        if not isinstance(query, str) or not query.strip():
            return ()
        ranked: dict[str, tuple[float, _AliasBinding, str]] = {}
        for project in self.projects:
            binding = _AliasBinding(project.title, project.project_id, ())
            score = max(
                _score(query, project.title),
                _score(query, project.summary) * 0.92,
            )
            ranked[_binding_target_id(binding)] = (score, binding, project.title)
        for bindings in self._bindings.values():
            for binding in bindings:
                target_id = _binding_target_id(binding)
                alias_score = _score(query, binding.alias)
                current = ranked.get(target_id)
                if (
                    current is None
                    or alias_score > current[0]
                    or (
                        alias_score == current[0]
                        and binding.alias.casefold() < current[2].casefold()
                    )
                ):
                    ranked[target_id] = (alias_score, binding, binding.alias)
        shortlist = sorted(
            (
                (target_id, score, binding, display_name)
                for target_id, (score, binding, display_name) in ranked.items()
                if score >= float(minimum_score)
            ),
            key=lambda item: (-item[1], item[3].casefold(), item[0]),
        )[:limit]
        candidates: list[WorkMapCandidate] = []
        for target_id, score, binding, display_name in shortlist:
            project = self._by_id[binding.project_id.casefold()]
            candidates.append(
                WorkMapCandidate(
                    target_id=target_id,
                    project_id=project.project_id,
                    project_title=project.title,
                    title=display_name,
                    summary=project.summary,
                    score=score,
                    has_local_root=bool(project.root is not None and project.root.exists()),
                    target_available=self._existing_target(binding) is not None,
                )
            )
        return tuple(candidates)

    def planner_hints(
        self,
        query: str,
        *,
        limit: int = 5,
        minimum_score: float = 0.65,
        available_only: bool = False,
    ) -> tuple[dict[str, object], ...]:
        """Return a serializable, path-free candidate list; this does not grant cloud consent."""

        if not isinstance(available_only, bool):
            raise ValueError("WorkMap available_only must be a boolean")
        pool_limit = min(20, max(limit, limit * 4)) if available_only else limit
        candidates = self.search_candidates(
            query,
            limit=pool_limit,
            minimum_score=minimum_score,
        )
        if available_only:
            candidates = tuple(candidate for candidate in candidates if candidate.target_available)
        return tuple(
            candidate.planner_hint()
            for candidate in candidates[:limit]
        )


__all__ = [
    "AliasValue",
    "WorkMapAliasTarget",
    "WorkMapCandidate",
    "WorkMapConfigurationError",
    "WorkMapError",
    "WorkMapIndex",
    "WorkMapProject",
]
