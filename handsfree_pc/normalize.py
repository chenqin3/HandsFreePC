from __future__ import annotations

import re
import unicodedata

_PUNCTUATION_RE = re.compile(r"[，。！？；、,:：;!?]+")
_SPACE_RE = re.compile(r"\s+")
_WAKE_SUFFIX_TRIM = " \t\r\n,，.。;；!！?？:：、"
_WAKE_CLOSING_QUOTES = frozenset('"\'”’」』》〉】)]}')


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).strip().lower()
    value = _PUNCTUATION_RE.sub(" ", value)
    return _SPACE_RE.sub(" ", value).strip()


def compact_text(text: str) -> str:
    return normalize_text(text).replace(" ", "")


def strip_control_prefix(text: str, prefixes: list[str]) -> str:
    normalized = normalize_text(text)
    compact = normalized.replace(" ", "")
    for prefix in prefixes:
        candidate = compact_text(prefix)
        if compact.startswith(candidate):
            return compact[len(candidate) :]
    return compact


def phrase_in_text(text: str, phrases: list[str]) -> str | None:
    haystack = compact_text(text)
    for phrase in sorted(phrases, key=lambda item: len(compact_text(item)), reverse=True):
        if compact_text(phrase) in haystack:
            return phrase
    return None


def phrase_equals(text: str, phrases: list[str]) -> str | None:
    """Match a configured phrase only when it is the complete utterance.

    Consent-bearing commands use this stricter helper. Normalization still
    tolerates whitespace, case, and ordinary punctuation introduced by ASR.
    """

    utterance = compact_text(text)
    for phrase in phrases:
        if utterance == compact_text(phrase):
            return phrase
    return None


def wake_suffix(text: str, wake_phrases: list[str]) -> tuple[str | None, str]:
    """Match an affirmative, utterance-initial wake phrase and return its suffix.

    The configured phrase is data, so this does not assume any particular wake
    words. Ordinary ASR whitespace and punctuation are ignored, but words
    before the phrase are not: negations, quotations, and reported speech must
    never be reinterpreted as an invocation merely because they contain it.
    """

    compact_parts: list[str] = []
    source_end_by_compact_character: list[int] = []
    for source_index, character in enumerate(text):
        normalized_character = compact_text(character)
        compact_parts.append(normalized_character)
        source_end_by_compact_character.extend([source_index + 1] * len(normalized_character))
    compact = "".join(compact_parts)
    for phrase in sorted(wake_phrases, key=lambda item: len(compact_text(item)), reverse=True):
        needle = compact_text(phrase)
        if not needle:
            continue
        if compact.startswith(needle):
            source_end = source_end_by_compact_character[len(needle) - 1]
            raw_suffix = text[source_end:]
            if (
                needle[-1].isascii()
                and needle[-1].isalnum()
                and raw_suffix
                and raw_suffix[0].isascii()
                and raw_suffix[0].isalnum()
            ):
                continue
            suffix = raw_suffix.lstrip(_WAKE_SUFFIX_TRIM)
            if suffix and suffix[0] in _WAKE_CLOSING_QUOTES:
                continue
            return phrase, suffix
    return None, ""


_CHINESE_ORDINALS = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}


def parse_ordinal(text: str) -> int | None:
    compact = compact_text(text)
    match = re.search(r"第([一二两三四五六七八九十]|\d+)个?", compact)
    if not match:
        return None
    token = match.group(1)
    return int(token) if token.isdigit() else _CHINESE_ORDINALS.get(token)
