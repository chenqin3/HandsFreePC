"""Resolve spoken, abbreviated folder or file descriptions to local paths.

"打开d盘研究数据库那个文件夹" should open ``D:\\研究数据仓库`` even though no
token matches exactly: the drive is a hint, "数据库" is the user's shorthand
for the project title, and "那个文件夹" is filler. Two sources are consulted:

* the WorkMap project index (titles, aliases, and their local roots), and
* the entries of a spoken base folder ("下载文件夹里面那个季度总结的网页").

Only a clear winner is returned; a tie or a weak match yields ``None`` so the
caller can ask or fall back instead of opening the wrong thing. Executables are
never candidates: a spoken "文件" is something to look at, not to run.
"""

from __future__ import annotations

import os
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

EXECUTABLE_SUFFIXES = frozenset(
    {
        ".exe",
        ".msi",
        ".bat",
        ".cmd",
        ".ps1",
        ".vbs",
        ".vbe",
        ".js",
        ".jse",
        ".wsf",
        ".wsh",
        ".lnk",
        ".scr",
        ".com",
        ".pif",
        ".reg",
        ".hta",
        ".msc",
    }
)
_DRIVE_HINT_RE = re.compile(r"(?<![a-z])(?P<drive>[a-z])\s*盘", re.IGNORECASE)
_BASE_ALIAS_RE = re.compile(
    r"^(?P<alias>桌面|文档|下载|我的文档|downloads?|desktop|documents?)"
    r"\s*(?:文件夹|目录|folder)?\s*(?:里面|里边|里|中|下|的|内)?\s*(?:的)?\s*"
    r"(?P<rest>.+)$",
    re.IGNORECASE,
)
_FILLER_RE = re.compile(
    r"(?:那个|这个|一个|某个|叫做|叫|名为|里面|里边|里的|里|中的|下的|的|文件夹|目录|"
    r"项目|folder|directory|请|帮我|给我|把)"
)
_LOCATION_CUE_RE = re.compile(
    r"(?:文件夹|目录|文件|盘|里|里面|中的|下的|路径|项目|网页|表格|文档|图片|照片|截图|"
    r"视频|音频|压缩包|脚本|笔记本|幻灯片|数据库|数据|资料|材料|报告|pdf|html|ppt|excel|word)",
    re.IGNORECASE,
)
_EXTENSION_HINTS: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("网页", "html", "网页文件"), (".html", ".htm")),
    (("表格", "excel", "电子表格"), (".xlsx", ".xls", ".csv")),
    (("word", "文档", "word文档"), (".docx", ".doc")),
    (("pdf",), (".pdf",)),
    (("图片", "照片", "截图"), (".png", ".jpg", ".jpeg", ".gif", ".webp")),
    (("视频",), (".mp4", ".mov", ".mkv", ".avi")),
    (("音频", "录音"), (".mp3", ".wav", ".m4a")),
    (("压缩包", "zip"), (".zip", ".rar", ".7z")),
    (("脚本", "python", "py"), (".py",)),
    (("笔记本", "notebook"), (".ipynb",)),
    (("ppt", "幻灯片", "演示文稿"), (".pptx", ".ppt")),
    (("markdown", "md"), (".md",)),
)
_DEFAULT_BASES = {
    "桌面": "Desktop",
    "desktop": "Desktop",
    "文档": "Documents",
    "我的文档": "Documents",
    "document": "Documents",
    "documents": "Documents",
    "下载": "Downloads",
    "download": "Downloads",
    "downloads": "Downloads",
}
_PUNCTUATION_RE = re.compile("[\\s\\-_—–·:：,，.。/\\\\()（）\\[\\]【】'\"“”‘’]+")


def _normalize(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold()
    return _PUNCTUATION_RE.sub("", folded)


def _bigrams(value: str) -> set[str]:
    compact = _normalize(value)
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[index : index + 2] for index in range(len(compact) - 1)}


def _latin_tokens(value: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]{2,}", _normalize(value))}


def _containment_score(description: str, candidate: str) -> float:
    """Fraction of the description's character bigrams found in the candidate.

    Abbreviations ("研究数据库" for "研究数据仓库") keep most of their bigrams
    inside the full title, while unrelated titles share almost none.
    """

    wanted = _bigrams(description)
    if not wanted:
        return 0.0
    have = _bigrams(candidate)
    latin_bonus = 0.0
    latin = _latin_tokens(description)
    if latin:
        present = sum(1 for token in latin if token in _normalize(candidate))
        latin_bonus = 0.25 * present / len(latin)
    return min(1.0, len(wanted & have) / len(wanted) + latin_bonus)


def _dice_score(description: str, candidate: str) -> float:
    """Symmetric bigram overlap; a short query inside a long title scores low."""

    wanted = _bigrams(description)
    have = _bigrams(candidate)
    if not wanted or not have:
        return 0.0
    return 2 * len(wanted & have) / (len(wanted) + len(have))


def has_location_cue(description: str) -> bool:
    """Whether the words point at a place or document rather than a program."""

    return _LOCATION_CUE_RE.search(unicodedata.normalize("NFKC", description)) is not None


@dataclass(frozen=True, slots=True)
class SpokenPathMatch:
    path: Path
    label: str
    score: float
    source: str


@dataclass(frozen=True, slots=True)
class _Scored:
    score: float
    strict_score: float
    label: str
    path: Path
    source: str


class SpokenPathResolver:
    def __init__(
        self,
        *,
        workmap_index: Any | None = None,
        path_aliases: Mapping[str, str] | None = None,
        home: Path | None = None,
        minimum_score: float = 0.7,
        margin: float = 0.15,
        strict_minimum_score: float = 0.8,
        strict_margin: float = 0.3,
    ) -> None:
        self.workmap_index = workmap_index
        self.path_aliases = {
            _normalize(str(name)): str(target) for name, target in (path_aliases or {}).items()
        }
        self.home = Path(home) if home is not None else Path(os.path.expanduser("~"))
        self.minimum_score = float(minimum_score)
        self.margin = float(margin)
        self.strict_minimum_score = float(strict_minimum_score)
        self.strict_margin = float(strict_margin)

    # -- spoken text handling -------------------------------------------------

    @staticmethod
    def split_drive_hint(description: str) -> tuple[str | None, str]:
        match = _DRIVE_HINT_RE.search(description)
        if match is None:
            return None, description
        drive = match.group("drive").upper()
        cleaned = description[: match.start()] + " " + description[match.end() :]
        return drive, cleaned.strip()

    def base_folder(self, description: str) -> tuple[Path | None, str]:
        """Split "下载文件夹里面那个X" into (Downloads, "那个X")."""

        match = _BASE_ALIAS_RE.match(unicodedata.normalize("NFKC", description).strip())
        if match is None:
            return None, description
        alias = match.group("alias")
        configured = self.path_aliases.get(_normalize(alias))
        if configured:
            base = Path(os.path.expandvars(configured))
        else:
            folder = _DEFAULT_BASES.get(alias.casefold())
            if folder is None:
                return None, description
            base = self.home / folder
        return base, match.group("rest").strip()

    @staticmethod
    def extension_hint(description: str) -> tuple[tuple[str, ...], str]:
        compact = _normalize(description)
        for words, extensions in _EXTENSION_HINTS:
            for word in words:
                if _normalize(word) and _normalize(word) in compact:
                    cleaned = re.sub(re.escape(word), " ", description, flags=re.IGNORECASE)
                    return extensions, cleaned
        return (), description

    @staticmethod
    def strip_fillers(description: str) -> str:
        return _FILLER_RE.sub(" ", unicodedata.normalize("NFKC", description)).strip()

    # -- candidates -----------------------------------------------------------

    def _pick(self, scored: Iterable[_Scored], *, strict: bool) -> SpokenPathMatch | None:
        def key(item: _Scored) -> float:
            return item.strict_score if strict else item.score

        minimum = self.strict_minimum_score if strict else self.minimum_score
        margin = self.strict_margin if strict else self.margin
        ordered = sorted(scored, key=lambda item: (-key(item), item.label.casefold()))
        if not ordered or key(ordered[0]) < minimum:
            return None
        if len(ordered) > 1 and key(ordered[0]) - key(ordered[1]) < margin:
            # Two near-identical candidates: refuse rather than guess.
            return None
        best = ordered[0]
        return SpokenPathMatch(
            path=best.path, label=best.label, score=key(best), source=best.source
        )

    def _scored_folder(self, base: Path, description: str) -> list[_Scored]:
        extensions, cleaned = self.extension_hint(description)
        query = self.strip_fillers(cleaned)
        if not _normalize(query):
            return []
        try:
            entries = list(base.iterdir())
        except OSError:
            return []
        scored: list[_Scored] = []
        for entry in entries:
            name = entry.name
            try:
                # Cloud placeholders and dangling reparse points raise here.
                is_file = entry.is_file()
                is_dir = entry.is_dir()
            except OSError:
                continue
            if is_file and entry.suffix.casefold() in EXECUTABLE_SUFFIXES:
                continue
            if extensions and is_file and entry.suffix.casefold() not in extensions:
                continue
            if extensions and is_dir:
                continue
            stem = entry.stem if is_file else name
            score = _containment_score(query, stem)
            if score <= 0:
                continue
            scored.append(_Scored(score, _dice_score(query, stem), name, entry, "folder"))
        return scored

    def _scored_workmap(self, description: str, *, drive: str | None) -> list[_Scored]:
        index = self.workmap_index
        if index is None:
            return []
        query = self.strip_fillers(description)
        if not _normalize(query):
            return []
        search = getattr(index, "search_candidates", None)
        binder = getattr(index, "resolve_candidate_id", None)
        if not callable(search) or not callable(binder):
            return []
        try:
            candidates = search(query, limit=8, minimum_score=0.0)
        except Exception:
            return []
        scored: list[_Scored] = []
        for candidate in candidates:
            if not getattr(candidate, "target_available", False):
                continue
            try:
                target = binder(candidate.target_id)
            except Exception:
                continue
            if target is None:
                continue
            if drive is not None and str(target.drive).rstrip(":").upper() != drive:
                continue
            title = str(getattr(candidate, "title", ""))
            score = max(float(getattr(candidate, "score", 0.0)), _containment_score(query, title))
            scored.append(_Scored(score, _dice_score(query, title), title, Path(target), "workmap"))
        return scored

    def resolve_in_folder(self, base: Path, description: str) -> SpokenPathMatch | None:
        return self._pick(self._scored_folder(base, description), strict=False)

    def resolve_workmap(self, description: str, *, drive: str | None) -> SpokenPathMatch | None:
        return self._pick(self._scored_workmap(description, drive=drive), strict=False)

    def _candidates(self, description: str) -> list[_Scored]:
        drive, text = self.split_drive_hint(description)
        base, rest = self.base_folder(text)
        if base is not None:
            return self._scored_folder(base, rest)
        scored = self._scored_workmap(text, drive=drive)
        if not scored and drive is not None:
            scored = self._scored_folder(Path(f"{drive}:\\"), text)
        return scored

    def resolve(self, description: str, *, strict: bool = False) -> SpokenPathMatch | None:
        """Resolve one spoken description; None when nothing clearly matches.

        ``strict`` demands an unmistakable, symmetric match. Callers use it when
        the words carry no location cue ("打开记事本"), so a project that merely
        contains the spoken word can never hijack an app-launch request.
        """

        if not isinstance(description, str) or not description.strip():
            return None
        _drive, text = self.split_drive_hint(description)
        base, _rest = self.base_folder(text)
        if base is None:
            stripped = self.strip_fillers(text)
            alias_target = self.path_aliases.get(_normalize(stripped))
            if alias_target:
                return SpokenPathMatch(
                    path=Path(os.path.expandvars(alias_target)),
                    label=text.strip(),
                    score=1.0,
                    source="alias",
                )
            known_base = _DEFAULT_BASES.get(_normalize(stripped))
            if known_base:
                # "打开下载文件夹" with no configured alias still means the
                # user's own Downloads folder.
                return SpokenPathMatch(
                    path=self.home / known_base,
                    label=text.strip(),
                    score=1.0,
                    source="alias",
                )
        return self._pick(self._candidates(description), strict=strict)

    def suggest(self, description: str, *, limit: int = 3) -> list[str]:
        """Best-scoring labels for a clarification prompt, regardless of margin."""

        if not isinstance(description, str) or not description.strip():
            return []
        ordered = sorted(
            self._candidates(description), key=lambda item: (-item.score, item.label.casefold())
        )
        return [item.label for item in ordered[: max(0, int(limit))] if item.score > 0]


__all__ = [
    "EXECUTABLE_SUFFIXES",
    "SpokenPathMatch",
    "SpokenPathResolver",
    "has_location_cue",
]
