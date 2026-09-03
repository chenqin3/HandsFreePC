from __future__ import annotations

import difflib
import re
import unicodedata

_PUNCTUATION_RE = re.compile(r"[，。！？；、,:：;!?]+")
_SPACE_RE = re.compile(r"\s+")
_WAKE_SUFFIX_TRIM = " \t\r\n,，.。;；!！?？:：、"
_WAKE_CLOSING_QUOTES = frozenset("\"'”’」』》〉】)]}")


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


# One stray leading character is tolerated (the audio slice may start mid-syllable),
# unless it negates, quotes, or reports the phrase.
_LEADING_REJECT = frozenset("不别没说\"'“「『《〈【")


def confirm_control_phrase(text: str, phrase: str, *, max_leading: int = 1) -> str | None:
    """Confirm that an accurate transcript starts with ``phrase``, tolerating one ASR slip.

    The keyword spotter is eager; the command transcriber is not.  Return the
    canonical phrase followed by whatever was said after it, or None when the
    transcript does not support the spotter's claim: chatter that merely
    contained the phrase, a hallucinated word, quoted, negated or reported
    speech.  Phrases of four or more characters may differ by one character
    (substituted, dropped or inserted); shorter phrases must match exactly.
    """

    needle = compact_text(phrase)
    if not needle:
        return None
    compact_parts: list[str] = []
    source_end_by_compact_character: list[int] = []
    for source_index, character in enumerate(text):
        normalized_character = compact_text(character)
        compact_parts.append(normalized_character)
        source_end_by_compact_character.extend([source_index + 1] * len(normalized_character))
    compact = "".join(compact_parts)
    if not compact:
        return None
    allowed_edits = 1 if len(needle) >= 4 else 0
    best: tuple[tuple[int, bool, bool, int], int] | None = None
    for lead in range(0, max_leading + 1):
        if lead and compact[lead - 1] in _LEADING_REJECT:
            break
        for length in {len(needle) - allowed_edits, len(needle), len(needle) + allowed_edits}:
            if length <= 0 or lead + length > len(compact):
                continue
            window = compact[lead : lead + length]
            blocks = difflib.SequenceMatcher(None, needle, window, autojunk=False)
            matching = [block for block in blocks.get_matching_blocks() if block.size]
            matches = sum(block.size for block in matching)
            if matches < len(needle) - allowed_edits or length - matches > allowed_edits:
                continue
            # The window must begin with the phrase; a stray first character is a
            # "lead" and is judged by the rules above, never hidden in the window.
            if not matching or matching[0].a != 0 or matching[0].b != 0:
                continue
            end = lead + length
            source_end = source_end_by_compact_character[end - 1]
            at_boundary = source_end >= len(text) or compact_text(text[source_end]) == ""
            score = (matches, at_boundary, length == len(needle), -abs(length - len(needle)))
            if best is None or score > best[0]:
                best = (score, end)
    if best is None:
        return None
    source_end = source_end_by_compact_character[best[1] - 1]
    suffix = text[source_end:].lstrip(_WAKE_SUFFIX_TRIM)
    if suffix and suffix[0] in _WAKE_CLOSING_QUOTES:
        return None
    return f"{phrase} {suffix}".strip() if suffix else phrase


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
