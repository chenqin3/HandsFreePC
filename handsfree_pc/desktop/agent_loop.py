from __future__ import annotations

import inspect
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, replace

from ..computer_control import ComputerControlResult
from ..models import ActionType, Plan, RiskLevel, clone_plan
from ..path_binding import bind_plan_paths, guard_plan_paths
from .native_skills import NativeRouteStatus, NativeSkillRouter
from .protocol import (
    DesktopAction,
    DesktopActionType,
    DesktopDecisionKind,
    DesktopDriver,
    DesktopElement,
    DesktopExpectation,
    DesktopExpectationKind,
    DesktopObservation,
    ElementPlane,
    element_plane,
    redact_credential_like_text,
    visual_state_binding_token,
)
from .safety import (
    DesktopActionBinding,
    DesktopConfirmation,
    DesktopSafetyDisposition,
    DesktopSafetyPolicy,
    DesktopSafetyProfile,
    action_matches_next_user_step,
    affirmatively_authorized_app_scope,
    expectation_is_terminal_user_condition,
    expectation_matches_user_step,
    local_dictation_user_text,
    natural_search_step_count,
    target_matches_explicit_text_step,
    text_step_has_explicit_target,
    user_action_step_clause,
    user_action_step_count,
    window_activation_matches_next_user_step,
)
from .step_planner import DesktopPlannerError, DesktopStepPlanner
from .verifier import DesktopVerifier, VerificationResult


@dataclass(slots=True)
class _TaskState:
    task: str
    apps: str
    allowed_apps: frozenset[str]
    observation: DesktopObservation | None
    history: list[str]
    last_verification: VerificationResult | None
    last_action_expectation: DesktopExpectation | None
    last_action: DesktopAction | None
    last_action_target: str | None
    steps: int
    verified_action_count: int
    verified_user_step_count: int
    remaining_seconds: float
    action_dispatched: bool = False
    unobservable_dynamic_apps: set[str] = field(default_factory=set)
    stale_replans: int = 0
    instrumental_reveal_count: int = 0
    instrumental_reveal_action_counts: dict[tuple[str, ...], int] = field(
        default_factory=dict
    )
    instrumental_reveal_fingerprints: set[str] = field(default_factory=set)
    visual_completion_candidate: tuple[str, str, int] | None = None
    visual_point_click_count: int = 0
    visual_point_region_counts: dict[tuple[str, ...], int] = field(default_factory=dict)
    related_window_navigation_pending: bool = False
    related_window_id: str | None = None
    related_window_destination: str | None = None


@dataclass(slots=True)
class _PendingConfirmation:
    confirmation_id: str
    summary: str
    expires_at: float
    state: _TaskState | None = None
    action: DesktopAction | None = None
    binding: DesktopConfirmation | None = None
    action_expectation: DesktopExpectation | None = None
    counts_as_user_step: bool = True
    native_plan: Plan | None = None
    native_user_text: str | None = None
    native_binding_digest: str | None = None


@dataclass(frozen=True, slots=True)
class _DictationBinding:
    """One locally observed composer identity, never an inferred text target."""

    app: str
    local_window_id: str
    element_identity: str


_APP_ALIASES: dict[str, tuple[str, ...]] = {
    "codex": (
        "codex",
        "科德克斯",
        "代码助手",
        "chatgpt",
        "chat gpt",
        "聊天gpt",
        "聊天 gpt",
    ),
    # Scoped aliases for common SenseVoice renderings of ``Claude``.  They are
    # still subject to the affirmative app-scope gate below.
    "claude": ("claude", "克劳德", "cloud", "cloloud"),
    "chrome": ("chrome", "google chrome", "谷歌浏览器", "浏览器"),
    "explorer": ("explorer", "file explorer", "资源管理器", "文件资源管理器"),
    "wechat": ("wechat", "weixin", "微信"),
}

_EXPLICIT_APP_SCOPE_SLOT_PATTERNS = (
    re.compile(
        r"\b(?:go|navigate|switch)\s+to\s+(?:the\s+)?"
        r"(?P<app>[A-Za-z][A-Za-z0-9_.+ -]{0,63}?)"
        r"(?:\s+(?:app|application))?"
        r"(?=\s*(?:[,;]\s*)?(?:(?:and|then)\s+)?(?:click|open|switch|"
        r"enter|type|press|scroll|activate|focus)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:open|launch|select)\s+(?:the\s+)?"
        r"(?P<app>[A-Za-z][A-Za-z0-9_.+ -]{0,63}?)"
        r"(?:\s+(?:app|application))?"
        r"(?=\s*(?:[,;]\s*)?(?:(?:and|then)\s+)?(?:click|open|switch|"
        r"enter|type|press|scroll|activate|focus)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:use|using|with)\s+(?:the\s+)?"
        r"(?P<app>[A-Za-z][A-Za-z0-9_.+ -]{0,63}?)\s*"
        r"(?=(?:to\s+)?(?:click|open|switch|select|choose|enter|type|press|"
        r"scroll|activate|focus)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:using|with)\s+(?:the\s+)?"
        r"(?P<app>[A-Za-z][A-Za-z0-9_.+ -]{0,63}?)\s*"
        r"(?=$|[,.。；;:：]|\s+(?:to|and|then)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:in|inside|within|on)\s+(?:the\s+)?"
        r"(?P<app>[A-Za-z][A-Za-z0-9_.+ -]{0,63}?)"
        r"(?:\s+(?:app|application))?\s*"
        r"(?=[,;:，；：]|\b(?:click|open|switch|select|choose|enter|type|press|"
        r"scroll|activate|focus)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:in|inside|within|on)\s+(?:the\s+)?"
        r"(?P<app>[A-Za-z][A-Za-z0-9_.+ -]{0,63}?)"
        r"(?:\s+(?:app|application))?\s*"
        r"(?=$|[,.。；;:：]|\s+(?:to|and|then)\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\w)(?:打开|启动|选择|进入|(?:切换|导航)(?:到|至)?)\s*"
        r"(?P<app>[\w.+ -]{1,64}?)(?:\s*(?:app|应用))?"
        r"(?=\s*(?:[,，]\s*)?(?:然后|并且|并|再)\s*"
        r"(?:点击|打开|切换|选择|进入|输入|按|滚动))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\w)(?:在|于)\s*(?P<app>[\w.+ -]{1,64}?)"
        r"(?:\s*(?:app|应用))?\s*(?:里面|里|中|内|上)?"
        r"\s*(?:的\s*)?"
        r"(?=\s*(?:点击|打开|切换|选择|进入|输入|按|滚动))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\w)(?:在|于)\s*(?P<app>[\w.+ -]{1,64}?)"
        r"(?:\s*(?:app|应用))?\s*(?:里面|里|中|内|上)?"
        r"\s*(?:的\s*)?"
        r"\s*(?=$|[,，。；;:：]|(?:以便|然后|并且|并|来|从而))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:点击|打开|切换|选择|进入)\s*"
        r"(?P<app>[\w.+ -]{1,64}?)(?:\s*(?:app|应用))?"
        r"(?:里面|里|中|内|上)的",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?<!\w)(?:使用|用|去到|到|去)\s*"
        r"(?P<app>[\w.+ -]{1,64}?)(?:\s*(?:app|应用))?"
        r"(?=\s*(?:点击|打开|切换|选择|进入|输入|按|滚动))",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[,.!?;，。！？；]\s*)"
        r"(?P<app>[A-Za-z\u4e00-\u9fff][\w.+ -]{0,63}?)\s*[:：]"
        r"(?=\s*(?:click|open|switch|select|choose|enter|type|press|scroll|"
        r"activate|focus|点击|打开|切换|选择|进入|输入|按|滚动))",
        re.IGNORECASE,
    ),
)

_APP_SCOPE_CANDIDATE_ACTION_RE = re.compile(
    r"(?:点击|打开|切换|选择|进入|输入|按|滚动|"
    r"\b(?:click|open|switch|select|choose|enter|type|press|scroll|activate|focus)\b)",
    re.IGNORECASE,
)

_SAFE_LOCAL_EXCEPTION_TYPES = frozenset(
    {
        "AmbiguousWindowError",
        "ForegroundIntegrityBoundary",
        "PasswordFieldError",
        "UIAUnavailableError",
        "WindowNotFoundError",
        "WindowsUiaDriverError",
        "WindowsUiaStaleObservation",
    }
)

_MAX_STALE_REPLANS = 2
_MAX_INSTRUMENTAL_REVEALS = 8
_MAX_IDENTICAL_INSTRUMENTAL_REVEALS = 4
_MAX_VISUAL_POINT_CLICKS = 16
_MAX_IDENTICAL_VISUAL_POINT_REGIONS = 2
_MAX_PRE_ACTION_OBSERVE_QUARANTINES = 3
_DYNAMIC_APP_ID_RE = re.compile(r"^[a-z0-9_.+-]+-[0-9a-f]{16}$", re.IGNORECASE)

_DICTATION_INTENT_RE = re.compile(
    r"(?:开始听写|进入听写|打开听写|开启听写|"
    r"开始语音输入|打开语音输入|开启语音输入|接下来(?:我会)?语音输入|"
    r"开始输入|开始对话|"
    r"\b(?:start|begin|enter|open|enable)\s+(?:voice\s+)?(?:dictation|input)\b|"
    r"\bstart\s+(?:the\s+)?(?:chat|conversation)\b)",
    re.IGNORECASE,
)
_NEGATED_DICTATION_INTENT_RE = re.compile(
    r"(?:不要|别|不再|停止|结束|退出)"
    r"(?:开始|进入|打开|开启|使用)?"
    r"(?:听写|语音输入|输入模式|输入|对话)|"
    r"\b(?:do\s+not|don't|stop|end|exit)\b[^\n]{0,24}"
    r"(?:dictation|voice\s+input|input\s+mode|conversation)\b",
    re.IGNORECASE,
)
_DICTATION_EXIT_UTTERANCES = frozenset(
    {
        "退出听写",
        "结束听写",
        "停止听写",
        "退出输入模式",
        "结束输入模式",
        "停止输入模式",
        "退出语音输入",
        "结束语音输入",
        "停止语音输入",
        "exitdictation",
        "enddictation",
        "stopdictation",
        "exitinputmode",
        "endinputmode",
        "stopinputmode",
    }
)
_DICTATION_ONLY_UTTERANCES = frozenset(
    {
        "开始听写",
        "进入听写",
        "打开听写",
        "开启听写",
        "开始语音输入",
        "打开语音输入",
        "开启语音输入",
        "开始输入",
        "开始对话",
        "startdictation",
        "begindictation",
        "entervoiceinput",
        "startvoiceinput",
        "startinput",
        "startchat",
        "startconversation",
    }
)


def _compact_control_text(value: str) -> str:
    """Normalize only control phrases; dictated payloads always remain untouched."""

    return re.sub(r"[\s，。！？；、,:：;.!?]+", "", value).casefold()


def _explicit_dictation_intent(task: str) -> bool:
    return bool(
        _DICTATION_INTENT_RE.search(task)
        and _NEGATED_DICTATION_INTENT_RE.search(task) is None
    )


def _dictation_exit_utterance(task: str) -> bool:
    compact = _compact_control_text(task)
    if compact.startswith("请"):
        compact = compact[1:]
    return compact in _DICTATION_EXIT_UTTERANCES


def _dictation_only_utterance(task: str) -> bool:
    compact = _compact_control_text(task)
    if compact.startswith("请"):
        compact = compact[1:]
    return compact in _DICTATION_ONLY_UTTERANCES


def _strip_leading_control_prefix(
    task: str,
    prefixes: tuple[str, ...],
) -> str | None:
    """Return the original suffix after a configured leading control prefix.

    Matching ignores ordinary ASR whitespace/punctuation, while the remaining
    command keeps its original word boundaries for the ordinary planner.
    """

    normalized_prefixes = sorted(
        {
            _compact_control_text(prefix)
            for prefix in prefixes
            if isinstance(prefix, str) and _compact_control_text(prefix)
        },
        key=len,
        reverse=True,
    )
    if not normalized_prefixes:
        return None
    compact_parts: list[str] = []
    source_end_by_character: list[int] = []
    for source_index, character in enumerate(task):
        normalized = _compact_control_text(character)
        compact_parts.append(normalized)
        source_end_by_character.extend([source_index + 1] * len(normalized))
    compact = "".join(compact_parts)
    for prefix in normalized_prefixes:
        if compact.startswith(prefix):
            source_end = source_end_by_character[len(prefix) - 1]
            return task[source_end:].lstrip(" \t\r\n,，.。;；!！?？:：、")
    return None


def _is_instrumental_reveal(
    action: DesktopAction,
    expectation: DesktopExpectation | None,
) -> bool:
    """Return whether an action is a non-terminal reveal aid for the planner."""

    if (
        expectation is None
        or expectation.kind != DesktopExpectationKind.LAST_ACTION_VERIFIED
    ):
        return False
    return bool(
        action.type == DesktopActionType.SCROLL
        or (
            action.type == DesktopActionType.PERFORM_SECONDARY_ACTION
            and (action.action_name or "").strip().casefold()
            in {"expand", "scrollintoview"}
        )
    )


def _instrumental_reveal_signature(
    action: DesktopAction,
    observation: DesktopObservation,
) -> tuple[str, ...]:
    """Build a generation-independent identity for one repeated reveal action."""

    target = next(
        (
            element
            for element in observation.elements
            if element.index == action.element_index
        ),
        None,
    )
    target_identity = "missing"
    if target is not None:
        target_identity = (
            target.local_identity
            or target.automation_id
            or f"{target.control_type.strip().casefold()}:{target.index}"
        )
    window_identity = observation.local_window_id or observation.app.strip().casefold()
    if action.type == DesktopActionType.SCROLL:
        operation = (action.direction or "").strip().casefold()
    else:
        operation = (action.action_name or "").strip().casefold()
    return (
        observation.app.strip().casefold(),
        window_identity,
        target_identity,
        action.type.value,
        operation,
    )


def _visual_point_click_signature(
    action: DesktopAction,
    observation: DesktopObservation,
) -> tuple[str, ...] | None:
    """Return a coarse exact-window region for bounding repeated visual guesses."""

    if (
        action.type != DesktopActionType.CLICK
        or action.x is None
        or action.y is None
        or action.element_index is None
    ):
        return None
    target = next(
        (
            element
            for element in observation.elements
            if element.index == action.element_index
        ),
        None,
    )
    if (
        target is None
        or not target.visual_ocr
        or target.control_type != "VisualViewport"
    ):
        return None
    return (
        observation.app.strip().casefold(),
        observation.local_window_id or "missing-window",
        str(int(action.x) // 64),
        str(int(action.y) // 64),
    )


def _related_result_destination(target_name: str, task: str) -> str | None:
    """Return the exact destination prefix of one explicit result affordance."""

    normalized_label = " ".join(target_name.split())
    match = re.search(
        r"(?:前往|go\s+to|open\s+in\s+(?:app|application))\s*$",
        normalized_label,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    stem = normalized_label[: match.start()].strip(" :-—–|，,。")
    words = stem.split()
    candidates = [" ".join(words[:end]) for end in range(len(words), 0, -1)]
    if not words and stem:
        candidates.append(stem)
    normalized_task = " ".join(task.split()).casefold()
    return next(
        (
            candidate
            for candidate in candidates
            if len(candidate.casefold()) >= 2
            and candidate.casefold() in normalized_task
        ),
        None,
    )


def _related_destination_terminal_condition(
    expectation: DesktopExpectation | None,
    destination: str | None,
) -> bool:
    """Bind a fresh related child window to its exact result destination."""

    if (
        expectation is None
        or destination is None
        or expectation.kind
        not in {
            DesktopExpectationKind.TEXT_PRESENT,
            DesktopExpectationKind.ELEMENT_SELECTED,
            DesktopExpectationKind.FOCUSED_CONTAINS,
        }
        or not expectation.text
    ):
        return False
    return " ".join(expectation.text.split()).casefold() == destination.casefold()


def _fresh_semantic_target_is_still_bound(
    action: DesktopAction,
    planned: DesktopObservation,
    fresh: DesktopObservation,
) -> bool:
    """Ignore unrelated animation only when one exact UIA target survived.

    Full-window screenshots commonly change while a rendered result page is
    loading or animating.  For a non-visual semantic action, the exact local
    UIA identity is the action binding; the driver will still revalidate that
    element immediately before dispatch.  Visual points never use this bridge.
    """

    if (
        action.element_index is None
        or action.x is not None
        or action.y is not None
        or not planned.local_window_id
        or planned.local_window_id != fresh.local_window_id
        or planned.app.strip().casefold() != fresh.app.strip().casefold()
    ):
        return False
    planned_targets = tuple(
        element for element in planned.elements if element.index == action.element_index
    )
    fresh_targets = tuple(
        element for element in fresh.elements if element.index == action.element_index
    )
    if len(planned_targets) != 1 or len(fresh_targets) != 1:
        return False
    before_target = planned_targets[0]
    after_target = fresh_targets[0]
    return bool(
        not before_target.visual_ocr
        and not after_target.visual_ocr
        and before_target.addressable
        and after_target.addressable
        and before_target.enabled
        and after_target.enabled
        and before_target.local_identity
        and before_target.local_identity == after_target.local_identity
        and before_target.control_type == after_target.control_type
    )


def _safe_exception_message(exc: Exception) -> str:
    name = type(exc).__name__
    if name not in _SAFE_LOCAL_EXCEPTION_TYPES:
        return name
    value = redact_credential_like_text(str(exc).strip()) or name
    return value[:240]


def _visible_apps(payload: str) -> tuple[str, ...]:
    """Parse the driver's local app inventory without forwarding free-form text."""

    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("desktop driver returned a non-JSON app inventory") from exc
    if not isinstance(value, list) or len(value) > 256:
        raise ValueError("desktop driver returned an invalid app inventory")
    names: list[str] = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("app"), str):
            raise ValueError("desktop driver app inventory contains an invalid entry")
        name = item["app"].strip().casefold()
        if not name or len(name) > 128 or not re.fullmatch(r"[\w .+-]+", name):
            raise ValueError("desktop driver app inventory contains an unsafe identifier")
        if name not in names:
            names.append(name)
    return tuple(names)


def _is_exact_dynamic_inventory_app(payload: str, app: str) -> bool:
    """Recognize one per-HWND inventory entry without trusting its title."""

    normalized = app.strip().casefold()
    if not _DYNAMIC_APP_ID_RE.fullmatch(normalized):
        return False
    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(value, list):
        return False
    matches = [
        item
        for item in value
        if isinstance(item, dict)
        and isinstance(item.get("app"), str)
        and item["app"].strip().casefold() == normalized
    ]
    if len(matches) != 1:
        return False
    entry = matches[0]
    # ``discover_all_windows`` emits these fields for an exact dynamic HWND.
    # Configured profile rows intentionally do not qualify for failover.
    return bool(
        entry.get("visible_window_count") == 1
        and "foreground" in entry
        and "display_name" in entry
        and "process_name" in entry
        and "window_title" in entry
    )


def _without_inventory_apps(payload: str, excluded: set[str]) -> str:
    """Remove locally quarantined app IDs while preserving valid inventory JSON."""

    _visible_apps(payload)
    value = json.loads(payload)
    normalized = {item.strip().casefold() for item in excluded}
    filtered = [
        item
        for item in value
        if item["app"].strip().casefold() not in normalized
    ]
    return json.dumps(filtered, ensure_ascii=False, sort_keys=True)


def _window_entry_labels(
    entry: dict[str, object],
    *,
    observed_title: str | None = None,
    include_window_titles: bool = True,
) -> tuple[str, ...]:
    """Return trusted app identity labels plus optional untrusted window titles.

    Process/profile identity may expand a canonical alias.  A page-controlled
    window title never does: a Chrome tab titled ``Claude`` must not become a
    Claude application merely because its title contains that word.
    """

    labels: list[str] = []
    for key, value in (
        ("app", entry.get("app")),
        ("display_name", entry.get("display_name")),
        ("process_name", entry.get("process_name")),
    ):
        if not isinstance(value, str):
            continue
        label = value.strip()
        if key == "process_name" and label.casefold().endswith(".exe"):
            label = label[:-4].strip()
        if (
            label
            and len(label) <= 256
            and label.casefold() not in {item.casefold() for item in labels}
        ):
            labels.append(label)
    for canonical, aliases in _APP_ALIASES.items():
        if any(
            label.casefold() == canonical
            or label.casefold() in {alias.casefold() for alias in aliases}
            for label in labels
        ):
            labels.extend(
                alias
                for alias in aliases
                if alias.casefold() not in {item.casefold() for item in labels}
            )
    if include_window_titles:
        for value in (entry.get("window_title"), observed_title):
            if not isinstance(value, str):
                continue
            label = value.strip()
            if (
                label
                and len(label) <= 256
                and label.casefold() not in {item.casefold() for item in labels}
            ):
                labels.append(label)
    return tuple(labels)


def _window_candidate_labels(
    inventory: str,
    observation: DesktopObservation,
) -> tuple[str, ...]:
    try:
        entries = json.loads(inventory)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(entries, list):
        return ()
    selected = next(
        (
            item
            for item in entries
            if isinstance(item, dict)
            and isinstance(item.get("app"), str)
            and item["app"].strip().casefold() == observation.app.strip().casefold()
        ),
        None,
    )
    if selected is None:
        return ()
    return _window_entry_labels(selected, observed_title=observation.window_title)


def _explicit_step_window_scope(
    task: str,
    *,
    inventory: str,
    completed_steps: int,
) -> tuple[bool, frozenset[str]]:
    """Resolve an explicitly named app/window for the current spoken step."""

    clause = user_action_step_clause(task, step=completed_steps)
    if clause is None:
        return False, frozenset()
    try:
        entries = json.loads(inventory)
    except (TypeError, json.JSONDecodeError):
        return False, frozenset()
    if not isinstance(entries, list):
        return False, frozenset()
    known_scope_labels = {
        alias.casefold()
        for canonical, aliases in _APP_ALIASES.items()
        for alias in (canonical, *aliases)
        if _app_scope_is_affirmative(alias, clause)
    }
    scored_matches: list[tuple[tuple[int, int, int], str]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("app"), str):
            continue
        trusted_labels = _window_entry_labels(entry, include_window_titles=False)
        all_labels = _window_entry_labels(entry)
        trusted_keys = {label.casefold() for label in trusted_labels}
        for label in all_labels:
            if not _app_scope_is_affirmative(label, clause):
                continue
            is_trusted_identity = label.casefold() in trusted_keys
            if not is_trusted_identity and label.casefold() in known_scope_labels:
                # A web page can choose its own title.  It cannot impersonate
                # a known application identity such as Claude or Chrome.
                continue
            position = clause.casefold().rfind(label.casefold())
            if position < 0:
                continue
            # The latest explicit scope wins.  At the same textual endpoint,
            # a longer exact window title beats a contained process name, and
            # trusted process/profile identity beats an equally named title.
            score = (
                position + len(label),
                len(label),
                int(is_trusted_identity),
            )
            scored_matches.append((score, entry["app"].strip().casefold()))
    matched: set[str] = set()
    if scored_matches:
        best_score = max(score for score, _app in scored_matches)
        matched = {app for score, app in scored_matches if score == best_score}
    known_scope = bool(known_scope_labels)
    return bool(matched or known_scope), frozenset(matched)


def _switch_only_window_request(
    task: str,
    *,
    inventory: str,
    observation: DesktopObservation,
) -> bool:
    """Accept zero-action completion only for one explicit pure window switch."""

    explicit_scope, scoped_apps = _explicit_step_window_scope(
        task,
        inventory=inventory,
        completed_steps=0,
    )
    if (
        not explicit_scope
        or observation.app.strip().casefold() not in scoped_apps
    ):
        return False

    labels = _window_candidate_labels(inventory, observation)
    if not labels:
        return False
    source = re.sub(r"切换道", "切换到", task.strip(), flags=re.IGNORECASE)
    for label in labels:
        escaped = re.escape(label)
        chinese = re.compile(
            rf"\s*(?:请|麻烦)?\s*(?:帮我)?\s*"
            rf"(?:切换到|切换至|切到|转到|打开|进入|激活|显示)\s*"
            rf"(?:桌面(?:上)?的?\s*)?{escaped}\s*(?:app|应用|程序|窗口)?\s*[。.!！?？]*\s*",
            re.IGNORECASE,
        )
        english = re.compile(
            rf"\s*(?:please\s+)?(?:switch\s+to|go\s+to|navigate\s+to|open|activate|"
            rf"focus|show)\s+(?:the\s+)?{escaped}\s*"
            rf"(?:app|application|program|window)?\s*[.!?]*\s*",
            re.IGNORECASE,
        )
        if (chinese.fullmatch(source) or english.fullmatch(source)) and _app_scope_is_affirmative(
            label,
            source,
        ):
            return True
    return False


def _app_scope_is_affirmative(candidate: str, task: str) -> bool:
    """Apply shared quote, payload, and negation gates to alternate scope grammar."""

    if affirmatively_authorized_app_scope(candidate, task):
        return True
    escaped = re.escape(candidate)
    rewrites = (
        re.sub(
            rf"\b(?:use|using|with)\s+(?:the\s+)?{escaped}\b",
            lambda _match: f"in {candidate}",
            task,
            flags=re.IGNORECASE,
        ),
        re.sub(
            rf"\b(?:go|navigate|switch)\s+to\s+(?:the\s+)?{escaped}(?=$|[\s,.;:])",
            lambda _match: f"in {candidate}",
            task,
            flags=re.IGNORECASE,
        ),
        re.sub(
            rf"\b(?:open|launch|select)\s+(?:the\s+)?{escaped}(?=$|[\s,.;:])",
            lambda _match: f"in {candidate}",
            task,
            flags=re.IGNORECASE,
        ),
        re.sub(
            rf"(?<!\w)(?:使用|用|去到|到|去)\s*{escaped}",
            lambda _match: f"在{candidate}中",
            task,
            flags=re.IGNORECASE,
        ),
        re.sub(
            rf"(?<!\w)(?:打开|启动|选择|进入|(?:切换|导航)(?:到|至)?)\s*"
            rf"{escaped}(?:\s*(?:app|应用))?\s*(?:[,，]\s*)?"
            rf"(?:然后|并且|并|再)(?=\s*(?:点击|打开|切换|选择|进入|输入|按|滚动))",
            lambda _match: f"在{candidate}中",
            task,
            flags=re.IGNORECASE,
        ),
        re.sub(
            rf"(?<!\w)(?:打开|启动|选择|进入|(?:切换|导航)(?:到|至)?)\s*{escaped}",
            lambda _match: f"在{candidate}中",
            task,
            flags=re.IGNORECASE,
        ),
        re.sub(
            rf"(?<![\w]){escaped}\s*[:：]",
            lambda _match: f"In {candidate},",
            task,
            flags=re.IGNORECASE,
        ),
        re.sub(
            rf"\b(?:in|inside|within|on)\s+(?:the\s+)?{escaped}"
            rf"(?:\s+(?:app|application))?(?:['’]s|\s+)"
            rf"[^,，。；;.!！？?\n]{{1,96}}?(?:\s*[,，:：]\s*|\s+)"
            rf"(?=(?:type|input|fill|write|set(?:\s+the)?\s+value)\b)",
            lambda _match: f"in {candidate}, ",
            task,
            flags=re.IGNORECASE,
        ),
    )
    return any(
        rewritten != task and affirmatively_authorized_app_scope(candidate, rewritten)
        for rewritten in rewrites
    )


def _normalized_field_label(value: str) -> str:
    label = re.sub(r"\s+", " ", value.strip(" \t\r\n,，:：'\"“”‘’")).casefold()
    label = re.sub(r"^(?:the\s+)", "", label, flags=re.IGNORECASE)
    label = re.sub(
        r"\s*(?:input\s+box|text\s+box|field|box|editor|composer|输入框|字段)\s*$",
        "",
        label,
        flags=re.IGNORECASE,
    )
    return label.strip()


def _explicit_preposed_text_fields(
    task: str,
    *,
    inventory: str,
    completed_steps: int,
    scoped_apps: frozenset[str],
) -> tuple[str, ...]:
    """Extract a field named between an app location and a text-entry verb."""

    clause = user_action_step_clause(task, step=completed_steps)
    if clause is None:
        return ()
    try:
        entries = json.loads(inventory)
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(entries, list):
        return ()
    fields: list[str] = []
    app_identity_keys: set[str] = set()
    for entry in entries:
        if isinstance(entry, dict):
            app_identity_keys.update(
                _normalized_field_label(label)
                for label in _window_entry_labels(entry, include_window_titles=False)
                if label.strip()
            )
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("app"), str)
            or (
                scoped_apps
                and entry["app"].strip().casefold() not in scoped_apps
            )
        ):
            continue
        labels = sorted(
            _window_entry_labels(entry, include_window_titles=False),
            key=len,
            reverse=True,
        )
        for label in labels:
            escaped = re.escape(label)
            patterns = (
                re.compile(
                    rf"\b(?:in|inside|within|on)\s+(?:the\s+)?{escaped}"
                    rf"(?:\s+(?:app|application))?(?:['’]s|\s+)"
                    rf"(?P<field>[^,，。；;.!！？?\n]{{1,96}}?)"
                    rf"(?:\s*[,，:：]\s*|\s+)"
                    rf"(?=(?:type|input|fill|write|set(?:\s+the)?\s+value)\b)",
                    re.IGNORECASE,
                ),
                re.compile(
                    rf"(?:在|于)\s*{escaped}(?:\s*(?:app|应用))?(?:的|\s+)"
                    rf"(?P<field>[^，。；,;.!！？?\n]{{1,96}}?)"
                    rf"(?=(?:输入|填写|写入|设置值|设值))",
                    re.IGNORECASE,
                ),
            )
            for pattern in patterns:
                for match in pattern.finditer(clause):
                    field = _normalized_field_label(match.group("field"))
                    if field and field not in fields:
                        fields.append(field)
    generic_patterns = (
        re.compile(
            r"(?:在|于)\s*(?P<field>[^，。；,;.!！？?\n]{1,96}?)\s*"
            r"(?=(?:输入|填写|写入|设置值|设值))",
            re.IGNORECASE,
        ),
        re.compile(
            r"\b(?:in|inside|within|on)\s+(?:the\s+)?"
            r"(?P<field>[^,，。；;.!！？?\n]{1,96}?)"
            r"(?:\s*[,，:：]\s*|\s+)"
            r"(?=(?:type|input|fill|write|set(?:\s+the)?\s+value)\b)",
            re.IGNORECASE,
        ),
    )
    for pattern in generic_patterns:
        for match in pattern.finditer(clause):
            field = _normalized_field_label(match.group("field"))
            if field and field not in app_identity_keys and field not in fields:
                fields.append(field)
    return tuple(fields)


def _explicitly_named_apps(task: str, visible_apps: tuple[str, ...]) -> frozenset[str]:
    matched: set[str] = set()
    normalized_apps = {item.strip().casefold() for item in visible_apps}
    has_dedicated_chatgpt_profile = "chatgpt" in normalized_apps
    chatgpt_aliases = ("chatgpt", "chat gpt", "聊天gpt", "聊天 gpt")
    for app in visible_apps:
        aliases = (
            chatgpt_aliases
            if app.strip().casefold() == "chatgpt"
            else _APP_ALIASES.get(app, (app,))
        )
        if app.strip().casefold() == "codex" and has_dedicated_chatgpt_profile:
            # The bundled profile uses ChatGPT spellings as a compatibility
            # alias because Codex Desktop currently runs as ChatGPT.exe.  A
            # user may also configure a distinct ``chatgpt`` profile; in that
            # case the exact profile must win instead of making the spoken
            # scope ambiguous between two independently addressable apps.
            aliases = tuple(
                alias for alias in aliases if alias.strip().casefold() not in chatgpt_aliases
            )
        if any(
            _app_scope_is_affirmative(alias, task)
            or _app_scope_is_affirmative(alias, _repair_asr_app_scope_prefix(task, alias))
            for alias in aliases
        ):
            matched.add(app)
    return frozenset(matched)


def _repair_asr_app_scope_prefix(task: str, alias: str) -> str:
    """Repair one observed ASR homophone only before a known app alias."""

    escaped = re.escape(alias)
    suffix_boundary = r"(?![a-z0-9_-])" if alias.isascii() else ""
    return re.sub(
        rf"切换道(?=\s*{escaped}{suffix_boundary})",
        "切换到",
        task,
        flags=re.IGNORECASE,
    )


def _unsupported_explicit_app_scopes(
    task: str,
    known_apps: tuple[str, ...],
) -> tuple[str, ...]:
    """Detect an explicit location slot that cannot map to a configured app.

    This is local fail-closed parsing only: unknown names are never forwarded
    and must not silently inherit a previous trusted application.
    """

    known_aliases = {
        alias.casefold() for app in known_apps for alias in _APP_ALIASES.get(app, (app,))
    }
    unknown: list[str] = []
    anaphoric_references = frozenset(
        {"其", "其中", "这", "这个", "那", "那个", "该", "本", "此", "上述", "前述"}
    )

    for pattern in _EXPLICIT_APP_SCOPE_SLOT_PATTERNS:
        for match in pattern.finditer(task):
            candidate = " ".join(match.group("app").strip().casefold().split())
            if re.sub(r"\s+", "", candidate) in anaphoric_references:
                # These words refer back to an already named object/app.  They are
                # not a new explicit application scope and must never be guessed or
                # fuzzy-mapped to one.
                continue
            if (
                candidate
                and _APP_SCOPE_CANDIDATE_ACTION_RE.search(candidate) is None
                and candidate not in known_aliases
                and candidate not in unknown
                and _app_scope_is_affirmative(candidate, task)
            ):
                unknown.append(candidate)
    return tuple(unknown)


class DesktopAgentLoopController:
    """Persistent observe -> one action -> observe -> local verify controller."""

    def __init__(
        self,
        *,
        native_router: NativeSkillRouter,
        driver: DesktopDriver | None,
        planner: DesktopStepPlanner | None,
        verifier: DesktopVerifier | None = None,
        safety: DesktopSafetyPolicy | None = None,
        timeout_seconds: float = 300.0,
        confirmation_timeout_seconds: float = 15.0,
        max_steps: int = 20,
        control_prefixes: tuple[str, ...] | list[str] | None = None,
        monotonic: object = time.monotonic,
        sleeper: object = time.sleep,
        diagnostics: object | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if confirmation_timeout_seconds <= 0:
            raise ValueError("confirmation_timeout_seconds must be positive")
        if max_steps < 1 or max_steps > 100:
            raise ValueError("max_steps must be between 1 and 100")
        self.native_router = native_router
        self.driver = driver
        self.planner = planner
        self.verifier = verifier or DesktopVerifier()
        self.safety = safety or DesktopSafetyPolicy()
        self.timeout_seconds = float(timeout_seconds)
        self.confirmation_timeout_seconds = float(confirmation_timeout_seconds)
        self.max_steps = int(max_steps)
        self._monotonic = monotonic
        self._sleep = sleeper
        self.diagnostics = diagnostics
        self._session_id = str(uuid.uuid4())
        self._execution_lock = threading.Lock()
        self._lifecycle_lock = threading.RLock()
        self._current_cancel: threading.Event | None = None
        self._pending: _PendingConfirmation | None = None
        self._closed = False
        self._trusted_app_context: str | None = None
        self._trusted_window_id: str | None = None
        configured_prefixes = control_prefixes
        if configured_prefixes is None:
            router_settings = getattr(native_router, "settings", None)
            app_settings = getattr(router_settings, "app", None)
            configured_prefixes = getattr(app_settings, "control_prefixes", ())
        self._control_prefixes = tuple(
            prefix.strip()
            for prefix in configured_prefixes or ()
            if isinstance(prefix, str) and prefix.strip()
        )
        self._dictation_binding: _DictationBinding | None = None

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def pending_confirmation_id(self) -> str | None:
        with self._lifecycle_lock:
            return self._pending.confirmation_id if self._pending is not None else None

    def _cancelled(self, external: threading.Event | None) -> bool:
        current = self._current_cancel
        return bool(
            (current is not None and current.is_set())
            or (external is not None and external.is_set())
        )

    def _trace(
        self,
        *,
        stage: str,
        error_code: str,
        safe_message: str,
        app: str | None = None,
        generation: int | None = None,
        level: str = "info",
    ) -> None:
        event = getattr(self.diagnostics, "event", None)
        if not callable(event):
            return
        try:
            event(
                stage=stage,
                error_code=error_code,
                safe_message=safe_message,
                level=level,
                session_id=self._session_id,
                app=app,
                generation=generation,
            )
        except Exception:
            return

    @staticmethod
    def _failure(
        message: str,
        *,
        stage: str,
        error_code: str,
        timed_out: bool = False,
        exception_type: str | None = None,
        app: str | None = None,
        generation: int | None = None,
    ) -> ComputerControlResult:
        safe_message = (redact_credential_like_text(message) or "desktop control failed")[:320]
        return ComputerControlResult(
            success=False,
            message=f"FAILURE: {safe_message}",
            timed_out=timed_out,
            stage=stage,
            error_code=error_code,
            safe_message=safe_message,
            exception_type=exception_type,
            app=app,
            generation=generation,
        )

    @staticmethod
    def _cancelled_result(
        message: str = "桌面任务已取消",
        *,
        stage: str = "runtime",
    ) -> ComputerControlResult:
        safe_message = message[:320]
        return ComputerControlResult(
            success=False,
            message=f"FAILURE: {safe_message}",
            cancelled=True,
            stage=stage,
            error_code="CANCELLED",
            safe_message=safe_message,
        )

    def _publish_native_success(
        self,
        message: str,
        *,
        cancel_event: threading.Event | None,
        context_expected: bool,
        context_refreshed: bool,
    ) -> ComputerControlResult:
        """Linearize native completion against cancellation under one lock."""

        with self._lifecycle_lock:
            current = self._current_cancel
            cancelled = bool(
                self._closed
                or (current is not None and current.is_set())
                or (cancel_event is not None and cancel_event.is_set())
            )
            if cancelled:
                self._trusted_app_context = None
                self._trusted_window_id = None
                return self._cancelled_result(
                    "本机动作完成检查期间收到取消；不发布成功状态",
                    stage="native_route",
                )
            if context_expected and not context_refreshed:
                message += "；未建立连续窗口上下文，下一条指令必须再次明确应用"
            return ComputerControlResult(success=True, message=message)

    def _set_driver_task_context(self, task: str | None) -> None:
        if self.driver is None:
            return
        setter = getattr(self.driver, "set_task_context", None)
        if callable(setter):
            try:
                setter(None if task is None else task[:16000])
            except Exception:
                # Task relevance only affects bounded-element priority; it is
                # never allowed to weaken binding or make the driver unusable.
                return

    def _confirmation_result(
        self,
        pending: _PendingConfirmation,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult:
        with self._lifecycle_lock:
            current = self._current_cancel
            if (
                self._closed
                or current is None
                or current.is_set()
                or (cancel_event is not None and cancel_event.is_set())
            ):
                return self._cancelled_result()
            self._pending = pending
        return ComputerControlResult(
            success=False,
            message=f"NEEDS_CONFIRMATION: {pending.summary}",
            needs_confirmation=True,
            confirmation_id=pending.confirmation_id,
        )

    @staticmethod
    def _native_binding(plan: Plan) -> str:
        return bind_plan_paths(plan)

    @staticmethod
    def _native_confirmation_summary(plan: Plan, digest: str) -> str:
        actions = ",".join(action.type.value for action in plan.actions)
        targets = [action.path for action in plan.actions if action.path]
        target = targets[0] if len(targets) == 1 else f"{len(targets)} validated targets"
        target_display = (target or "configured UI target")[:180]
        return f"native action={actions[:64]}; exact target={target_display}; binding={digest[:10]}"

    def _native_confirmation(
        self,
        plan: Plan,
        user_text: str,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult:
        plan = clone_plan(plan)
        if self._cancelled(cancel_event):
            return self._cancelled_result(stage="native_route")
        try:
            binding = self._native_binding(plan)
        except (OSError, RuntimeError, ValueError):
            return self._failure(
                "无法建立本机确认目标的稳定身份",
                stage="native_route",
                error_code="NATIVE_CONFIRMATION_BINDING_FAILED",
            )
        if self._cancelled(cancel_event):
            return self._cancelled_result(stage="native_route")
        confirmation_id = f"native-{uuid.uuid4().hex}"
        return self._confirmation_result(
            _PendingConfirmation(
                confirmation_id=confirmation_id,
                summary=self._native_confirmation_summary(plan, binding),
                expires_at=float(self._monotonic()) + self.confirmation_timeout_seconds,
                native_plan=clone_plan(plan),
                native_user_text=user_text,
                native_binding_digest=binding,
            ),
            cancel_event=cancel_event,
        )

    def _run_native(
        self,
        instruction: str,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult | None:
        route = self.native_router.route
        try:
            parameters = inspect.signature(route).parameters.values()
            supports_cancel = any(
                parameter.name == "cancel_event"
                or parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in parameters
            )
        except (TypeError, ValueError):
            supports_cancel = False
        result = (
            route(instruction, cancel_event=cancel_event)
            if supports_cancel
            else route(instruction)
        )
        if result.plan is not None and any(action.app for action in result.plan.actions):
            # An explicit app-scoped native request supersedes the preceding
            # window even when the router executed a switch concurrently with
            # cancellation, or this request later fails or needs approval.
            self._clear_trusted_context()
        if self._cancelled(cancel_event):
            return self._cancelled_result(stage="native_route")
        if result.status == NativeRouteStatus.MISS:
            return None
        if result.status == NativeRouteStatus.SUCCEEDED:
            context_expected = bool(
                result.plan is not None
                and self.driver is not None
                and self.safety.profile == DesktopSafetyProfile.PERSONAL_TRUSTED
                and len(
                    {
                        action.app.strip().casefold()
                        for action in result.plan.actions
                        if isinstance(action.app, str) and action.app.strip()
                    }
                )
                == 1
            )
            context_refreshed = self._refresh_trusted_context_after_native(
                result.plan,
                user_text=instruction,
                cancel_event=cancel_event,
            )
            return self._publish_native_success(
                "LOCAL_VERIFIED_COMPLETION: 确定性本机技能已完成并返回本地证据",
                cancel_event=cancel_event,
                context_expected=context_expected,
                context_refreshed=context_refreshed,
            )
        if result.status == NativeRouteStatus.CONFIRMATION_REQUIRED and result.plan is not None:
            return self._native_confirmation(
                result.plan,
                instruction,
                cancel_event=cancel_event,
            )
        if result.status == NativeRouteStatus.BLOCKED:
            return self._failure(
                "确定性本机安全策略阻止了该操作",
                stage="native_route",
                error_code="NATIVE_ROUTE_BLOCKED",
            )
        if result.status == NativeRouteStatus.FAILED and result.plan is not None:
            action_types = {action.type for action in result.plan.actions}
            ui_workflow_types = {
                ActionType.ACTIVATE_APP,
                ActionType.OPEN_MODE,
                ActionType.OPEN_CONVERSATION,
                ActionType.ENTER_DICTATION,
            }
            workflow_specific_types = {
                ActionType.OPEN_MODE,
                ActionType.OPEN_CONVERSATION,
                ActionType.ENTER_DICTATION,
            }
            if (
                self.safety.profile == DesktopSafetyProfile.LOCAL_UNRESTRICTED
                and self.driver is not None
                and self.planner is not None
                and bool(action_types)
                and action_types.issubset(ui_workflow_types)
                and bool(action_types.intersection(workflow_specific_types))
            ):
                # A native UIA helper may fail on multiple app windows even
                # though the generic planner can disambiguate them from a fresh
                # inventory and screenshot. Never carry the stale capability.
                self._clear_trusted_context()
                self._trace(
                    stage="native_route",
                    error_code="NATIVE_UI_WORKFLOW_FALLBACK",
                    safe_message=(
                        "A failed app-only native workflow fell back to fresh generic UI control"
                    ),
                )
                return None
        return self._failure(
            "确定性本机技能未能完成该操作",
            stage="native_route",
            error_code="NATIVE_ROUTE_FAILED",
        )

    def _ensure_generic_components(self) -> ComputerControlResult | None:
        if self.driver is None:
            return self._failure(
                "没有配置可用的本地桌面驱动",
                stage="runtime",
                error_code="DRIVER_NOT_CONFIGURED",
            )
        if self.planner is None:
            return self._failure(
                "该命令未命中本机技能，且没有启用单步规划器",
                stage="plan",
                error_code="PLANNER_NOT_CONFIGURED",
            )
        return None

    def _remaining(self, started_at: float) -> float:
        return max(0.0, self.timeout_seconds - (float(self._monotonic()) - started_at))

    def _trusted_context(self) -> tuple[str, str] | None:
        if self.safety.profile not in {
            DesktopSafetyProfile.PERSONAL_TRUSTED,
            DesktopSafetyProfile.LOCAL_UNRESTRICTED,
        }:
            return None
        with self._lifecycle_lock:
            if self._trusted_app_context is None or self._trusted_window_id is None:
                return None
            return self._trusted_app_context, self._trusted_window_id

    def _clear_trusted_context(self) -> None:
        with self._lifecycle_lock:
            self._trusted_app_context = None
            self._trusted_window_id = None
            self._dictation_binding = None

    def _dictation_context(self) -> _DictationBinding | None:
        if self.safety.profile != DesktopSafetyProfile.LOCAL_UNRESTRICTED:
            return None
        with self._lifecycle_lock:
            return self._dictation_binding

    def _clear_dictation_context(self) -> None:
        with self._lifecycle_lock:
            self._dictation_binding = None

    @staticmethod
    def _eligible_dictation_elements(
        observation: DesktopObservation,
    ) -> tuple[DesktopElement, ...]:
        """Return positively observed composer inputs eligible for binding."""

        return tuple(
            element
            for element in observation.elements
            if element.local_identity
            and element.focused is True
            and element.enabled
            and element.addressable
            and element.composer
            and not element.password
            and not element.secret_labeled
            and not element.high_credential
            and not element.low_credential
            and element_plane(element) == ElementPlane.INPUT
            and element.editable is not False
        )

    @classmethod
    def _binding_from_observation(
        cls,
        observation: DesktopObservation,
    ) -> _DictationBinding | None:
        if not observation.local_window_id:
            return None
        eligible = cls._eligible_dictation_elements(observation)
        if len(eligible) != 1:
            return None
        target = eligible[0]
        identity = target.local_identity
        if not identity:
            return None
        if (
            sum(
                element.local_identity == identity
                for element in observation.elements
            )
            != 1
        ):
            return None
        return _DictationBinding(
            app=observation.app.strip().casefold(),
            local_window_id=observation.local_window_id,
            element_identity=identity,
        )

    @classmethod
    def _bound_dictation_element(
        cls,
        observation: DesktopObservation,
        binding: _DictationBinding,
    ) -> DesktopElement | None:
        if (
            observation.app.strip().casefold() != binding.app
            or not observation.local_window_id
            or observation.local_window_id != binding.local_window_id
        ):
            return None
        matches = [
            element
            for element in cls._eligible_dictation_elements(observation)
            if element.local_identity == binding.element_identity
        ]
        if len(matches) != 1:
            return None
        if (
            sum(
                element.local_identity == binding.element_identity
                for element in observation.elements
            )
            != 1
        ):
            return None
        return matches[0]

    def _bind_dictation_from_observation(
        self,
        observation: DesktopObservation,
        *,
        task: str,
    ) -> bool:
        if (
            self.safety.profile != DesktopSafetyProfile.LOCAL_UNRESTRICTED
            or not _explicit_dictation_intent(task)
        ):
            return False
        binding = self._binding_from_observation(observation)
        if binding is None:
            self._clear_dictation_context()
            return False
        with self._lifecycle_lock:
            if self._closed or self._cancelled(None):
                self._dictation_binding = None
                return False
            self._dictation_binding = binding
        self._trace(
            stage="dictation",
            error_code="DICTATION_BOUND",
            safe_message="A unique focused local composer was bound for continuous dictation",
            app=observation.app,
            generation=observation.generation,
        )
        return True

    def _remember_trusted_context(
        self,
        state: _TaskState,
        result: ComputerControlResult,
    ) -> None:
        if (
            not result.success
            or self.safety.profile
            not in {
                DesktopSafetyProfile.PERSONAL_TRUSTED,
                DesktopSafetyProfile.LOCAL_UNRESTRICTED,
            }
            or state.observation is None
            or not state.observation.local_window_id
        ):
            return
        with self._lifecycle_lock:
            if self._closed or (self._current_cancel is not None and self._current_cancel.is_set()):
                return
            self._trusted_app_context = state.observation.app.strip().casefold()
            self._trusted_window_id = state.observation.local_window_id

    def _refresh_trusted_context_after_native(
        self,
        plan: Plan | None,
        *,
        user_text: str,
        cancel_event: threading.Event | None,
    ) -> bool:
        """Bind a deterministic app action to one freshly inspected HWND.

        Native execution has its own local postconditions, but it previously did
        not give the next queued utterance a desktop scope.  The context is
        established only from a fresh UIA observation after the native action.
        """

        self._clear_trusted_context()
        if (
            plan is None
            or self.driver is None
            or self.safety.profile
            not in {
                DesktopSafetyProfile.PERSONAL_TRUSTED,
                DesktopSafetyProfile.LOCAL_UNRESTRICTED,
            }
            or self._cancelled(cancel_event)
        ):
            return False
        apps = {
            action.app.strip().casefold()
            for action in plan.actions
            if isinstance(action.app, str) and action.app.strip()
        }
        if len(apps) != 1:
            return False
        app = next(iter(apps))
        try:
            self._set_driver_task_context(user_text)
            self.driver.start()
            observation = self.driver.observe(
                app,
                cancel_event=self._current_cancel,
            )
        except Exception:
            return False
        if (
            self._cancelled(cancel_event)
            or observation.app.strip().casefold() != app
            or not observation.local_window_id
        ):
            return False
        inspection = self.safety.inspect_observation(
            observation,
            user_text=user_text,
        )
        if inspection.disposition == DesktopSafetyDisposition.BLOCK:
            return False
        with self._lifecycle_lock:
            if self._closed or self._cancelled(cancel_event):
                return False
            self._trusted_app_context = app
            self._trusted_window_id = observation.local_window_id
        self._bind_dictation_from_observation(observation, task=user_text)
        return True

    def _drive(
        self,
        state: _TaskState,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult:
        assert self.driver is not None and self.planner is not None
        unrestricted = self.safety.profile == DesktopSafetyProfile.LOCAL_UNRESTRICTED
        deadline = float(self._monotonic()) + state.remaining_seconds
        while state.steps < self.max_steps:
            if self._cancelled(cancel_event):
                return self._cancelled_result(stage="runtime")
            remaining = deadline - float(self._monotonic())
            if remaining <= 0:
                return self._failure(
                    "桌面任务超过时间上限",
                    stage="runtime",
                    error_code="DESKTOP_TASK_TIMEOUT",
                    timed_out=True,
                )
            state.remaining_seconds = remaining
            if unrestricted and state.steps > 0:
                try:
                    inventory = self.driver.list_apps(cancel_event=self._current_cancel)
                    inventory = _without_inventory_apps(
                        inventory,
                        state.unobservable_dynamic_apps,
                    )
                    visible_apps = _visible_apps(inventory)
                except Exception as exc:
                    return self._failure(
                        f"刷新可见窗口失败：{_safe_exception_message(exc)}",
                        stage="list_apps",
                        error_code="APP_INVENTORY_REFRESH_FAILED",
                        exception_type=type(exc).__name__,
                    )
                if not visible_apps:
                    if state.unobservable_dynamic_apps:
                        return self._failure(
                            "可观察的精确窗口候选已经耗尽",
                            stage="observe_driver",
                            error_code="OBSERVE_DRIVER_FAILED",
                        )
                    return self._failure(
                        "刷新后没有可供桌面规划器观察的可见窗口",
                        stage="list_apps",
                        error_code="NO_VISIBLE_WINDOWS",
                    )
                state.apps = inventory
                state.allowed_apps = frozenset(visible_apps)
                if (
                    state.observation is not None
                    and state.observation.app.strip().casefold() not in state.allowed_apps
                ):
                    state.history.append("the previously observed window is no longer visible")
                    state.observation = None
                self._trace(
                    stage="list_apps",
                    error_code="APP_INVENTORY_REFRESHED",
                    safe_message="The unrestricted visible-window inventory was refreshed",
                )
            try:
                planner_observation = (
                    self.safety.planner_observation(
                        state.observation,
                        user_text=state.task,
                    )
                    if state.observation is not None
                    else None
                )
                decision = self.planner.decide(
                    state.task,
                    apps=state.apps,
                    observation=planner_observation,
                    history=state.history,
                    cancel_event=self._current_cancel,
                )
                if (
                    decision.kind == DesktopDecisionKind.DONE
                    and state.observation is not None
                    and state.observation.screenshot_png is not None
                    and state.observation.local_window_id
                    and decision.app is not None
                    and decision.app.strip().casefold()
                    == state.observation.app.strip().casefold()
                    and len(
                        tuple(
                            element
                            for element in state.observation.elements
                            if element.visual_ocr
                            and element.control_type == "VisualViewport"
                            and element.enabled
                            and element.addressable
                        )
                    )
                    == 1
                ):
                    # The cloud-facing planner observation deliberately omits
                    # the local HWND.  Bind its visual DONE proposal only after
                    # the response returns, using the private full observation;
                    # the model can never author or guess this frame token.
                    decision = replace(
                        decision,
                        expectation=DesktopExpectation(
                            DesktopExpectationKind.VISUAL_STATE_VERIFIED,
                            visual_state_binding_token(state.observation),
                        ),
                    )
            except DesktopPlannerError as exc:
                return self._failure(
                    f"单步规划器失败：{_safe_exception_message(exc)}",
                    stage="plan",
                    error_code="PLANNER_FAILED",
                    exception_type=type(exc).__name__,
                )
            except Exception as exc:
                return self._failure(
                    f"单步规划器发生内部错误：{type(exc).__name__}",
                    stage="plan",
                    error_code="PLANNER_INTERNAL_ERROR",
                    exception_type=type(exc).__name__,
                )
            state.steps += 1
            self._trace(
                stage="plan",
                error_code=f"PLANNER_DECISION_{decision.kind.value.upper()}",
                safe_message="The desktop planner returned one structured decision",
                app=decision.app,
                generation=(
                    state.observation.generation if state.observation is not None else None
                ),
            )
            is_visual_done_decision = bool(
                decision.kind == DesktopDecisionKind.DONE
                and decision.expectation is not None
                and decision.expectation.kind
                == DesktopExpectationKind.VISUAL_STATE_VERIFIED
            )
            if not is_visual_done_decision:
                state.visual_completion_candidate = None

            if decision.kind == DesktopDecisionKind.FAIL:
                return self._failure(
                    "单步规划器无法提出安全且可本地核验的下一步",
                    stage="plan",
                    error_code="PLANNER_NO_SAFE_STEP",
                )

            if decision.kind == DesktopDecisionKind.OBSERVE:
                assert decision.app is not None
                requested_app = decision.app.strip().casefold()
                if requested_app in state.unobservable_dynamic_apps:
                    return self._failure(
                        "规划器重复请求了本任务中已确认不可观察的窗口候选",
                        stage="observe_driver",
                        error_code="OBSERVE_DRIVER_FAILED",
                    )
                if requested_app not in state.allowed_apps:
                    return self._failure(
                        "规划器请求观察未在本次口述中明确授权的应用",
                        stage="plan",
                        error_code="PLANNER_APP_SCOPE_VIOLATION",
                    )
                try:
                    observation = self.driver.observe(
                        decision.app,
                        cancel_event=self._current_cancel,
                    )
                except Exception as exc:
                    may_reselect_window = bool(
                        unrestricted
                        and not state.action_dispatched
                        and _is_exact_dynamic_inventory_app(state.apps, decision.app)
                    )
                    if may_reselect_window:
                        state.unobservable_dynamic_apps.add(requested_app)
                        try:
                            fresh_inventory = self.driver.list_apps(
                                cancel_event=self._current_cancel
                            )
                            fresh_inventory = _without_inventory_apps(
                                fresh_inventory,
                                state.unobservable_dynamic_apps,
                            )
                            remaining_apps = _visible_apps(fresh_inventory)
                        except Exception as refresh_exc:
                            return self._failure(
                                "窗口观察失败，且无法刷新剩余候选",
                                stage="observe_driver",
                                error_code="OBSERVE_DRIVER_FAILED",
                                exception_type=type(refresh_exc).__name__,
                            )
                        state.apps = fresh_inventory
                        state.allowed_apps = frozenset(remaining_apps)
                        state.observation = None
                        state.history.append(
                            "one exact local window candidate was unavailable to "
                            "observation and was removed before replanning"
                        )
                        self._trace(
                            stage="observe_driver",
                            error_code="OBSERVE_CANDIDATE_QUARANTINED",
                            safe_message=(
                                "One exact local window candidate was removed before "
                                "any action dispatch"
                            ),
                        )
                        if (
                            len(state.unobservable_dynamic_apps)
                            >= _MAX_PRE_ACTION_OBSERVE_QUARANTINES
                            or not remaining_apps
                        ):
                            return self._failure(
                                "可观察的精确窗口候选已经耗尽",
                                stage="observe_driver",
                                error_code="OBSERVE_DRIVER_FAILED",
                                exception_type=type(exc).__name__,
                            )
                        continue
                    return self._failure(
                        f"桌面观察失败：{_safe_exception_message(exc)}",
                        stage="observe_driver",
                        error_code="OBSERVE_DRIVER_FAILED",
                        exception_type=type(exc).__name__,
                        app=decision.app,
                    )
                if observation.app.strip().casefold() not in state.allowed_apps:
                    return self._failure(
                        "桌面驱动返回了本次任务授权范围外的应用",
                        stage="observe_driver",
                        error_code="OBSERVED_APP_SCOPE_MISMATCH",
                        app=observation.app,
                        generation=observation.generation,
                    )
                inspection = self.safety.inspect_observation(
                    observation,
                    user_text=state.task,
                )
                if inspection.disposition == DesktopSafetyDisposition.BLOCK:
                    return self._failure(
                        f"本地安全策略阻止读取该界面：{inspection.reason}",
                        stage="observe_safety",
                        error_code="OBSERVATION_BLOCKED",
                        app=observation.app,
                        generation=observation.generation,
                    )
                state.observation = observation
                if unrestricted:
                    explicit_scope, scoped_apps = _explicit_step_window_scope(
                        state.task,
                        inventory=state.apps,
                        completed_steps=state.verified_user_step_count,
                    )
                    if (
                        (not explicit_scope or observation.app.strip().casefold() in scoped_apps)
                        and window_activation_matches_next_user_step(
                            _window_candidate_labels(state.apps, observation),
                            state.task,
                            completed_steps=state.verified_user_step_count,
                        )
                    ):
                        state.verified_user_step_count += 1
                        state.history.append(
                            "locally verified the next explicit exact-window activation"
                        )
                self._trace(
                    stage="observe_driver",
                    error_code="WINDOW_OBSERVED",
                    safe_message="A fresh local application-window observation is available",
                    app=observation.app,
                    generation=observation.generation,
                )
                state.history.append(
                    f"observed {observation.app} generation {observation.generation}"
                )
                continue

            if decision.kind == DesktopDecisionKind.ACTION:
                if state.observation is None or decision.action is None:
                    return self._failure(
                        "单步规划器在没有当前观察时要求动作",
                        stage="plan",
                        error_code="ACTION_WITHOUT_OBSERVATION",
                    )
                action = decision.action
                if action.app.strip().casefold() not in state.allowed_apps:
                    return self._failure(
                        "规划器动作超出本次口述授权的应用范围",
                        stage="plan",
                        error_code="PLANNER_APP_SCOPE_VIOLATION",
                    )
                target = next(
                    (
                        element
                        for element in state.observation.elements
                        if element.index == action.element_index
                    ),
                    None,
                )
                target_label = target.name if target is not None else ""
                if unrestricted:
                    explicit_scope, scoped_apps = _explicit_step_window_scope(
                        state.task,
                        inventory=state.apps,
                        completed_steps=state.verified_user_step_count,
                    )
                    input_bound_action = bool(
                        action.type
                        in {
                            DesktopActionType.TYPE_TEXT,
                            DesktopActionType.SET_VALUE,
                        }
                        or (
                            target is not None
                            and element_plane(target) == ElementPlane.INPUT
                            and action.type
                            in {
                                DesktopActionType.CLICK,
                                DesktopActionType.PERFORM_SECONDARY_ACTION,
                                DesktopActionType.PRESS_KEY,
                            }
                        )
                    )
                    explicit_text_target = text_step_has_explicit_target(
                        state.task,
                        step=state.verified_user_step_count,
                    )
                    preposed_text_fields = _explicit_preposed_text_fields(
                        state.task,
                        inventory=state.apps,
                        completed_steps=state.verified_user_step_count,
                        scoped_apps=scoped_apps,
                    )
                    target_matches_text_field = bool(
                        target_matches_explicit_text_step(
                            target_label,
                            state.task,
                            step=state.verified_user_step_count,
                        )
                        or (
                            target_label
                            and _normalized_field_label(target_label)
                            in preposed_text_fields
                        )
                    )
                    if (
                        explicit_scope
                        and input_bound_action
                        and action.app.strip().casefold() not in scoped_apps
                    ):
                        return self._failure(
                            "输入或搜索动作没有绑定到本句明确指定的窗口",
                            stage="action_safety",
                            error_code="EXPLICIT_STEP_WINDOW_MISMATCH",
                            app=state.observation.app,
                            generation=state.observation.generation,
                        )
                    if (
                        input_bound_action
                        and (explicit_text_target or preposed_text_fields)
                        and not target_matches_text_field
                    ):
                        return self._failure(
                            "输入动作没有绑定到本句明确指定的字段",
                            stage="action_safety",
                            error_code="EXPLICIT_TEXT_TARGET_MISMATCH",
                            app=state.observation.app,
                            generation=state.observation.generation,
                        )
                direct_action_match = action_matches_next_user_step(
                    action,
                    target_label,
                    state.task,
                    completed_steps=state.verified_user_step_count,
                )
                direct_expectation_match = bool(
                    decision.expectation is not None
                    and expectation_matches_user_step(
                        action,
                        target_label,
                        decision.expectation,
                        state.task,
                        completed_steps=state.verified_user_step_count,
                    )
                )
                instrumental_reveal = bool(
                    unrestricted
                    and _is_instrumental_reveal(action, decision.expectation)
                )
                if unrestricted:
                    # The private-machine profile may infer locally verified
                    # navigation bridges, but only an exact match for the next
                    # spoken action advances the explicit user-step counter.
                    # Reveal-only bridges are bounded separately below.
                    binding = (
                        DesktopActionBinding.USER_STEP
                        if (
                            not instrumental_reveal
                            and direct_action_match
                            and direct_expectation_match
                        )
                        else DesktopActionBinding.NAVIGATION_BRIDGE
                    )
                else:
                    binding = (
                        DesktopActionBinding.USER_STEP
                        if direct_action_match and direct_expectation_match
                        else self.safety.classify_personal_action_binding(
                            action,
                            target,
                            decision.expectation,
                            user_text=state.task,
                            completed_steps=state.verified_user_step_count,
                        )
                    )
                if binding is None:
                    return self._failure(
                        "规划器动作未对应用户要求或可信本机导航步骤",
                        stage="action_safety",
                        error_code="ACTION_NOT_BOUND_TO_TASK",
                        app=state.observation.app,
                        generation=state.observation.generation,
                    )
                if (
                    unrestricted
                    and explicit_scope
                    and binding == DesktopActionBinding.USER_STEP
                    and action.app.strip().casefold() not in scoped_apps
                ):
                    return self._failure(
                        "最终用户步骤没有在本句明确指定的窗口中执行",
                        stage="action_safety",
                        error_code="EXPLICIT_STEP_WINDOW_MISMATCH",
                        app=state.observation.app,
                        generation=state.observation.generation,
                    )
                if decision.expectation is None:
                    return self._failure(
                        "动作缺少可本地核验的后置条件",
                        stage="plan",
                        error_code="ACTION_POSTCONDITION_MISSING",
                    )
                safety_result = self.safety.evaluate(
                    action,
                    state.observation,
                    user_text=state.task,
                    expectation=decision.expectation,
                )
                if safety_result.disposition == DesktopSafetyDisposition.BLOCK:
                    return self._failure(
                        f"本地安全策略阻止动作：{safety_result.reason}",
                        stage="action_safety",
                        error_code="ACTION_BLOCKED",
                        app=state.observation.app,
                        generation=state.observation.generation,
                    )
                if safety_result.needs_confirmation:
                    assert safety_result.confirmation is not None
                    pending = _PendingConfirmation(
                        confirmation_id=f"desktop-{uuid.uuid4().hex}",
                        summary=safety_result.confirmation.summary[:320],
                        expires_at=float(self._monotonic()) + self.confirmation_timeout_seconds,
                        state=state,
                        action=action,
                        binding=safety_result.confirmation,
                        action_expectation=decision.expectation,
                        counts_as_user_step=binding == DesktopActionBinding.USER_STEP,
                    )
                    return self._confirmation_result(pending, cancel_event=cancel_event)
                self._trace(
                    stage="execute",
                    error_code="ACTION_DISPATCHED",
                    safe_message="One observation-bound desktop action is ready for dispatch",
                    app=state.observation.app,
                    generation=state.observation.generation,
                )
                action_result = self._perform_action(
                    state,
                    action,
                    expectation=decision.expectation,
                    counts_as_user_step=binding == DesktopActionBinding.USER_STEP,
                    cancel_event=cancel_event,
                )
                if action_result is not None:
                    return action_result
                continue

            if decision.kind == DesktopDecisionKind.DONE:
                visual_viewports = (
                    tuple(
                        element
                        for element in state.observation.elements
                        if element.visual_ocr
                        and element.control_type == "VisualViewport"
                    )
                    if state.observation is not None
                    else ()
                )
                visual_state_proposal = bool(
                    unrestricted
                    and decision.expectation is not None
                    and decision.expectation.kind
                    == DesktopExpectationKind.VISUAL_STATE_VERIFIED
                    and state.observation is not None
                    and state.observation.screenshot_png is not None
                    and state.observation.local_window_id
                    and decision.app is not None
                    and decision.app.strip().casefold()
                    == state.observation.app.strip().casefold()
                    and decision.expectation.text
                    == visual_state_binding_token(state.observation)
                    and len(visual_viewports) == 1
                    and visual_viewports[0].enabled
                    and visual_viewports[0].addressable
                )
                visual_candidate = state.visual_completion_candidate
                visual_state_completion = bool(
                    visual_state_proposal
                    and visual_candidate is not None
                    and state.observation is not None
                    and visual_candidate[0]
                    == state.observation.app.strip().casefold()
                    and visual_candidate[1] == state.observation.local_window_id
                    and state.observation.generation > visual_candidate[2]
                )
                if visual_state_proposal and not visual_state_completion:
                    assert state.observation is not None
                    if visual_candidate is not None:
                        return self._failure(
                            "两次视觉完成判断没有绑定到同一个更新后的窗口",
                            stage="verify_completion",
                            error_code="VISUAL_COMPLETION_BINDING_CHANGED",
                            app=state.observation.app,
                            generation=state.observation.generation,
                        )
                    stale_visual_app = state.observation.app
                    stale_visual_window = state.observation.local_window_id
                    assert stale_visual_window is not None
                    state.visual_completion_candidate = (
                        stale_visual_app.strip().casefold(),
                        stale_visual_window,
                        state.observation.generation,
                    )
                    try:
                        fresh_visual_observation = self.driver.observe(
                            stale_visual_app,
                            cancel_event=self._current_cancel,
                        )
                    except Exception as exc:
                        return self._failure(
                            f"视觉完成复核前重新观察失败：{_safe_exception_message(exc)}",
                            stage="observe_driver",
                            error_code="VISUAL_COMPLETION_REOBSERVE_FAILED",
                            exception_type=type(exc).__name__,
                            app=stale_visual_app,
                        )
                    if (
                        fresh_visual_observation.app.strip().casefold()
                        != stale_visual_app.strip().casefold()
                        or not fresh_visual_observation.local_window_id
                        or fresh_visual_observation.local_window_id
                        != stale_visual_window
                    ):
                        return self._failure(
                            "视觉完成复核时精确窗口绑定发生变化",
                            stage="observe_driver",
                            error_code="VISUAL_COMPLETION_WINDOW_CHANGED",
                            app=fresh_visual_observation.app,
                            generation=fresh_visual_observation.generation,
                        )
                    if (
                        fresh_visual_observation.generation
                        <= state.observation.generation
                        or fresh_visual_observation.captured_at
                        < state.observation.captured_at
                    ):
                        return self._failure(
                            "视觉完成复核没有取得更新一代的窗口截图",
                            stage="observe_driver",
                            error_code="VISUAL_COMPLETION_NOT_FRESH",
                            app=fresh_visual_observation.app,
                            generation=fresh_visual_observation.generation,
                        )
                    fresh_visual_inspection = self.safety.inspect_observation(
                        fresh_visual_observation,
                        user_text=state.task,
                    )
                    if (
                        fresh_visual_inspection.disposition
                        == DesktopSafetyDisposition.BLOCK
                    ):
                        return self._failure(
                            "视觉完成复核的最新窗口观察被本地策略阻止",
                            stage="observe_safety",
                            error_code="VISUAL_COMPLETION_OBSERVATION_BLOCKED",
                            app=fresh_visual_observation.app,
                            generation=fresh_visual_observation.generation,
                        )
                    state.observation = fresh_visual_observation
                    state.history.append(
                        "fresh exact-window screenshot requested for visual completion confirmation"
                    )
                    self._trace(
                        stage="observe_driver",
                        error_code="VISUAL_COMPLETION_REOBSERVED",
                        safe_message=(
                            "A second exact-window screenshot is ready for visual completion review"
                        ),
                        app=fresh_visual_observation.app,
                        generation=fresh_visual_observation.generation,
                    )
                    continue
                if (
                    decision.expectation is not None
                    and decision.expectation.kind
                    == DesktopExpectationKind.LAST_ACTION_VERIFIED
                ):
                    return self._failure(
                        "界面变化本身不能作为桌面任务的最终完成条件",
                        stage="verify_completion",
                        error_code="INSTRUMENTAL_REVEAL_CANNOT_COMPLETE",
                    )
                if state.observation is None:
                    return self._failure(
                        "规划器报告完成，但没有可核验的本地观察",
                        stage="verify_completion",
                        error_code="COMPLETION_OBSERVATION_MISSING",
                    )
                if unrestricted and not visual_state_completion:
                    stale_completion_app = state.observation.app
                    stale_completion_window = state.observation.local_window_id
                    try:
                        completion_observation = self.driver.observe(
                            stale_completion_app,
                            cancel_event=self._current_cancel,
                        )
                    except Exception as exc:
                        return self._failure(
                            f"完成验收前重新观察失败：{_safe_exception_message(exc)}",
                            stage="observe_driver",
                            error_code="COMPLETION_REOBSERVE_FAILED",
                            exception_type=type(exc).__name__,
                            app=stale_completion_app,
                        )
                    if (
                        completion_observation.app.strip().casefold()
                        not in state.allowed_apps
                        or completion_observation.app.strip().casefold()
                        != stale_completion_app.strip().casefold()
                        or not stale_completion_window
                        or not completion_observation.local_window_id
                        or completion_observation.local_window_id != stale_completion_window
                    ):
                        return self._failure(
                            "完成验收前可见窗口绑定已经变化",
                            stage="observe_driver",
                            error_code="COMPLETION_WINDOW_CHANGED",
                            app=completion_observation.app,
                            generation=completion_observation.generation,
                        )
                    if (
                        completion_observation.generation <= state.observation.generation
                        or completion_observation.captured_at < state.observation.captured_at
                    ):
                        return self._failure(
                            "完成验收前没有取得更新一代的窗口观察",
                            stage="observe_driver",
                            error_code="COMPLETION_OBSERVATION_NOT_FRESH",
                            app=completion_observation.app,
                            generation=completion_observation.generation,
                        )
                    completion_inspection = self.safety.inspect_observation(
                        completion_observation,
                        user_text=state.task,
                    )
                    if completion_inspection.disposition == DesktopSafetyDisposition.BLOCK:
                        return self._failure(
                            f"完成验收前本地安全策略阻止读取界面：{completion_inspection.reason}",
                            stage="observe_safety",
                            error_code="COMPLETION_OBSERVATION_BLOCKED",
                            app=completion_observation.app,
                            generation=completion_observation.generation,
                    )
                    state.observation = completion_observation
                spoken_step_count = user_action_step_count(state.task)
                if unrestricted and spoken_step_count > 0:
                    terminal_step = min(
                        state.verified_user_step_count,
                        spoken_step_count - 1,
                    )
                    explicit_scope, scoped_apps = _explicit_step_window_scope(
                        state.task,
                        inventory=state.apps,
                        completed_steps=terminal_step,
                    )
                    if (
                        explicit_scope
                        and state.observation.app.strip().casefold() not in scoped_apps
                    ):
                        return self._failure(
                            "完成条件不在用户明确指定的窗口中",
                            stage="verify_completion",
                            error_code="COMPLETION_EXPLICIT_WINDOW_MISMATCH",
                            app=state.observation.app,
                            generation=state.observation.generation,
                        )
                zero_action_switch = bool(
                    unrestricted
                    and decision.expectation is not None
                    and decision.expectation.kind == DesktopExpectationKind.APP_VISIBLE
                    and decision.app is not None
                    and decision.app.strip().casefold() == state.observation.app.strip().casefold()
                    and bool(state.observation.local_window_id)
                    and _switch_only_window_request(
                        state.task,
                        inventory=state.apps,
                        observation=state.observation,
                    )
                )
                if state.verified_action_count == 0 and not zero_action_switch:
                    return self._failure(
                        "通用桌面任务尚无任何经过本地验收的动作",
                        stage="verify_completion",
                        error_code="NO_VERIFIED_ACTIONS",
                    )
                exact_terminal_condition = expectation_is_terminal_user_condition(
                    decision.expectation,
                    state.task,
                    last_action=state.last_action,
                    last_action_target=state.last_action_target,
                )
                related_destination_terminal = bool(
                    state.related_window_navigation_pending
                    and state.related_window_id
                    and state.observation.local_window_id == state.related_window_id
                    and _related_destination_terminal_condition(
                        decision.expectation,
                        state.related_window_destination,
                    )
                )
                last_action_was_visual_point = bool(
                    state.last_action is not None
                    and state.last_action.type == DesktopActionType.CLICK
                    and state.last_action.x is not None
                    and state.last_action.y is not None
                )
                last_action_has_terminal_evidence = expectation_is_terminal_user_condition(
                    state.last_action_expectation,
                    state.task,
                    last_action=state.last_action,
                    last_action_target=state.last_action_target,
                )
                visual_terminal_evidence = bool(
                    visual_state_completion
                    and state.verified_action_count > 0
                    and state.last_action is not None
                    and state.last_verification is not None
                    and state.last_verification.verified
                    and (
                        last_action_was_visual_point
                        or last_action_has_terminal_evidence
                        or (
                            state.related_window_navigation_pending
                            and state.related_window_id
                            and state.observation.local_window_id
                            == state.related_window_id
                        )
                    )
                )
                terminal_step_credit = bool(
                    spoken_step_count > 0
                    and state.verified_user_step_count == spoken_step_count - 1
                    and (related_destination_terminal or visual_terminal_evidence)
                )
                effective_verified_user_steps = state.verified_user_step_count + int(
                    terminal_step_credit
                )
                if (
                    (
                        not unrestricted
                        or spoken_step_count > 1
                        or natural_search_step_count(state.task) > 0
                        or visual_state_completion
                        or related_destination_terminal
                    )
                    and effective_verified_user_steps != spoken_step_count
                ):
                    return self._failure(
                        "尚未按顺序完成用户明确要求的全部桌面步骤",
                        stage="verify_completion",
                        error_code="USER_STEPS_INCOMPLETE",
                    )
                if decision.app is not None and (
                    decision.app.strip().casefold() not in state.allowed_apps
                ):
                    return self._failure(
                        "规划器完成条件超出本次口述授权的应用范围",
                        stage="verify_completion",
                        error_code="COMPLETION_APP_SCOPE_MISMATCH",
                    )
                if (
                    state.verified_action_count > 0
                    and not visual_state_completion
                    and not last_action_was_visual_point
                    and not related_destination_terminal
                    and (
                        state.last_action_expectation is None
                        or decision.expectation != state.last_action_expectation
                    )
                ):
                    return self._failure(
                        "完成条件必须与最后一个已建立的任务后置条件完全一致",
                        stage="verify_completion",
                        error_code="COMPLETION_CONDITION_CHANGED",
                    )
                if unrestricted and spoken_step_count > 1:
                    terminal_condition_is_bound = (
                        zero_action_switch
                        or exact_terminal_condition
                        or related_destination_terminal
                        or visual_terminal_evidence
                    )
                else:
                    terminal_condition_is_bound = zero_action_switch or (
                        visual_terminal_evidence
                        or exact_terminal_condition
                        or related_destination_terminal
                        or self.safety.accepts_personal_terminal_condition(
                            decision.expectation,
                            user_text=state.task,
                            last_action=state.last_action,
                        )
                        or self.safety.accepts_unrestricted_terminal_condition(
                            decision.expectation,
                            user_text=state.task,
                        )
                    )
                if not terminal_condition_is_bound:
                    return self._failure(
                        "完成条件没有绑定到用户要求的最后一个正向动作",
                        stage="verify_completion",
                        error_code="COMPLETION_NOT_BOUND_TO_TASK",
                    )
                verified = self.verifier.verify_completion(
                    decision,
                    state.observation,
                    last_action_result=state.last_verification,
                )
                if not verified.verified:
                    return self._failure(
                        f"本地完成条件未成立：{verified.reason}",
                        stage="verify_completion",
                        error_code="COMPLETION_NOT_VERIFIED",
                        app=state.observation.app,
                        generation=state.observation.generation,
                    )
                return ComputerControlResult(
                    True,
                    f"LOCAL_VERIFIED_COMPLETION: {verified.reason}",
                )
        return self._failure(
            "桌面任务达到最大单步数，未满足本地完成条件",
            stage="verify_completion",
            error_code="MAX_STEPS_REACHED",
        )

    def _perform_action(
        self,
        state: _TaskState,
        action: DesktopAction,
        *,
        expectation: DesktopExpectation | None,
        counts_as_user_step: bool = True,
        confirmed_binding: DesktopConfirmation | None = None,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult | None:
        assert self.driver is not None and state.observation is not None
        planner_observation = state.observation
        instrumental_reveal = bool(
            self.safety.profile == DesktopSafetyProfile.LOCAL_UNRESTRICTED
            and _is_instrumental_reveal(action, expectation)
        )
        reveal_signature: tuple[str, ...] | None = None
        if expectation is None:
            return self._failure(
                "桌面动作缺少任务相关的本地后置条件",
                stage="plan",
                error_code="ACTION_POSTCONDITION_MISSING",
            )
        if self._cancelled(cancel_event):
            return self._cancelled_result(stage="execute")
        try:
            before = self.driver.observe(
                action.app,
                cancel_event=self._current_cancel,
            )
        except Exception as exc:
            return self._failure(
                f"执行前桌面观察失败：{_safe_exception_message(exc)}",
                stage="observe_driver",
                error_code="PRE_ACTION_OBSERVE_FAILED",
                exception_type=type(exc).__name__,
                app=action.app,
            )
        if (
            before.app.strip().casefold() not in state.allowed_apps
            or before.app.strip().casefold() != planner_observation.app.strip().casefold()
        ):
            return self._failure(
                "执行前桌面驱动返回了授权范围外的应用，已拒绝动作",
                stage="observe_driver",
                error_code="PRE_ACTION_APP_CHANGED",
                app=before.app,
                generation=before.generation,
            )
        inspection = self.safety.inspect_observation(before, user_text=state.task)
        if inspection.disposition == DesktopSafetyDisposition.BLOCK:
            return self._failure(
                f"执行前本地安全策略阻止读取界面：{inspection.reason}",
                stage="observe_safety",
                error_code="PRE_ACTION_OBSERVATION_BLOCKED",
                app=before.app,
                generation=before.generation,
            )
        semantic_target_survived_animation = bool(
            self.safety.profile == DesktopSafetyProfile.LOCAL_UNRESTRICTED
            and _fresh_semantic_target_is_still_bound(
                action,
                planner_observation,
                before,
            )
        )
        if (
            before.fingerprint != planner_observation.fingerprint
            and not semantic_target_survived_animation
        ):
            if (
                not planner_observation.local_window_id
                or not before.local_window_id
                or before.local_window_id != planner_observation.local_window_id
            ):
                return self._failure(
                    "执行前界面已经变化，无法证明仍是同一窗口，已拒绝使用过期规划",
                    stage="observe_driver",
                    error_code="STALE_WINDOW_CHANGED",
                    app=before.app,
                    generation=before.generation,
                )
            state.stale_replans += 1
            if state.stale_replans > _MAX_STALE_REPLANS:
                return self._failure(
                    "同一窗口在执行前反复变化，已停止继续规划",
                    stage="observe_driver",
                    error_code="UI_STATE_UNSTABLE",
                    app=before.app,
                    generation=before.generation,
                )
            state.observation = before
            state.history.append(
                "refreshed changed UI in the same local window; previous action was not executed"
            )
            return None
        rebound = replace(action, generation=before.generation)
        visual_point_signature = _visual_point_click_signature(rebound, before)
        if visual_point_signature is not None:
            if state.visual_point_click_count >= _MAX_VISUAL_POINT_CLICKS:
                return self._failure(
                    "本任务的视觉点选次数已达到上限，已停止继续猜测位置",
                    stage="action_safety",
                    error_code="VISUAL_POINT_CLICK_LIMIT_REACHED",
                    app=before.app,
                    generation=before.generation,
                )
            if (
                state.visual_point_region_counts.get(visual_point_signature, 0)
                >= _MAX_IDENTICAL_VISUAL_POINT_REGIONS
            ):
                return self._failure(
                    "规划器反复点选同一视觉区域，已停止循环",
                    stage="action_safety",
                    error_code="VISUAL_POINT_REGION_REPEAT_LIMIT_REACHED",
                    app=before.app,
                    generation=before.generation,
                )
        fresh_safety = self.safety.evaluate(
            rebound,
            before,
            user_text=state.task,
            expectation=expectation,
        )
        if confirmed_binding is None:
            if fresh_safety.disposition != DesktopSafetyDisposition.ALLOW:
                return self._failure(
                    "执行前本地安全分类不再允许该动作",
                    stage="action_safety",
                    error_code="FRESH_ACTION_NOT_ALLOWED",
                    app=before.app,
                    generation=before.generation,
                )
        else:
            if (
                fresh_safety.disposition != DesktopSafetyDisposition.CONFIRM
                or fresh_safety.confirmation is None
                or fresh_safety.confirmation.action_digest != confirmed_binding.action_digest
                or fresh_safety.confirmation.summary != confirmed_binding.summary
            ):
                return self._failure(
                    "确认前本地安全分类发生变化，已拒绝执行",
                    stage="action_safety",
                    error_code="CONFIRMATION_BINDING_CHANGED",
                    app=before.app,
                    generation=before.generation,
                )
        if instrumental_reveal:
            if state.instrumental_reveal_count >= _MAX_INSTRUMENTAL_REVEALS:
                return self._failure(
                    "为寻找目标执行的滚动或展开动作已达到总次数上限",
                    stage="action_safety",
                    error_code="INSTRUMENTAL_REVEAL_LIMIT_REACHED",
                    app=before.app,
                    generation=before.generation,
                )
            reveal_signature = _instrumental_reveal_signature(rebound, before)
            if (
                state.instrumental_reveal_action_counts.get(reveal_signature, 0)
                >= _MAX_IDENTICAL_INSTRUMENTAL_REVEALS
            ):
                return self._failure(
                    "同一个滚动或展开动作重复次数过多，已停止以避免循环",
                    stage="action_safety",
                    error_code="INSTRUMENTAL_REVEAL_REPEAT_LIMIT_REACHED",
                    app=before.app,
                    generation=before.generation,
                )
        already_true = (
            VerificationResult(False, "search submission requires an action transition")
            if expectation.kind == DesktopExpectationKind.SEARCH_SUBMITTED
            else self.verifier.verify_expectation(
                expectation,
                before,
                last_action_result=None,
            )
        )
        if already_true.verified:
            return self._failure(
                "动作后置条件在执行前已经成立，不能证明本次动作完成",
                stage="verify_action",
                error_code="POSTCONDITION_ALREADY_TRUE",
                app=before.app,
                generation=before.generation,
            )
        try:
            if self._cancelled(cancel_event):
                return self._cancelled_result(stage="execute")
            state.action_dispatched = True
            receipt = self.driver.execute(
                rebound,
                before,
                cancel_event=self._current_cancel,
            )
        except Exception as exc:
            return self._failure(
                f"桌面动作失败：{_safe_exception_message(exc)}",
                stage="execute",
                error_code="ACTION_EXECUTION_FAILED",
                exception_type=type(exc).__name__,
                app=before.app,
                generation=before.generation,
            )
        if self._cancelled(cancel_event):
            return self._cancelled_result(stage="execute")
        after: DesktopObservation | None = None
        reobserve_error: Exception | None = None
        for attempt, delay in enumerate((0.0, 0.15, 0.35)):
            if delay:
                self._sleep(delay)
            if self._cancelled(cancel_event):
                return self._cancelled_result(stage="reobserve")
            try:
                after = self.driver.observe(
                    rebound.app,
                    cancel_event=self._current_cancel,
                )
                break
            except Exception as exc:
                reobserve_error = exc
                if attempt == 2:
                    break
        if after is None:
            assert reobserve_error is not None
            return self._failure(
                f"动作后重新观察失败：{_safe_exception_message(reobserve_error)}",
                stage="reobserve",
                error_code="REOBSERVE_FAILED",
                exception_type=type(reobserve_error).__name__,
                app=rebound.app,
            )
        verified = self.verifier.verify_action(rebound, receipt, before, after)
        if not verified.verified:
            if (
                instrumental_reveal
                and verified.reason == "no observable application change followed the action"
            ):
                return self._failure(
                    "滚动或展开后界面没有任何可观察变化",
                    stage="verify_action",
                    error_code="INSTRUMENTAL_REVEAL_NO_PROGRESS",
                    app=after.app,
                    generation=after.generation,
                )
            return self._failure(
                f"动作后本地验收失败：{verified.reason}",
                stage="verify_action",
                error_code="ACTION_NOT_VERIFIED",
                app=after.app,
                generation=after.generation,
            )
        if (
            instrumental_reveal
            and after.fingerprint in state.instrumental_reveal_fingerprints
        ):
            return self._failure(
                "滚动或展开回到了本任务先前见过的界面，已停止循环",
                stage="verify_action",
                error_code="INSTRUMENTAL_REVEAL_LOOP_DETECTED",
                app=after.app,
                generation=after.generation,
            )
        target = next(
            (element for element in before.elements if element.index == rebound.element_index),
            None,
        )
        expected_result = (
            self.verifier.verify_search_submission(
                rebound,
                expectation,
                before,
                after,
            )
            if expectation.kind == DesktopExpectationKind.SEARCH_SUBMITTED
            else self.verifier.verify_expectation(
                expectation,
                after,
                last_action_result=verified,
            )
        )
        related_window_transition = bool(
            self.safety.profile == DesktopSafetyProfile.LOCAL_UNRESTRICTED
            and verified.verified
            and receipt.after_local_window_id
            and before.local_window_id
            and after.local_window_id == receipt.after_local_window_id
            and after.local_window_id != before.local_window_id
            and target is not None
            and target.addressable
            and target.enabled
            and not target.visual_ocr
            and rebound.type
            in {
                DesktopActionType.CLICK,
                DesktopActionType.PERFORM_SECONDARY_ACTION,
            }
        )
        disappearance_destination = (
            _related_result_destination(target.name, state.task)
            if (
                related_window_transition
                and expected_result.verified
                and expectation.kind == DesktopExpectationKind.TEXT_ABSENT
                and expectation.text is not None
                and target is not None
                and target.control_type.strip().casefold() == "button"
                and " ".join(expectation.text.split()).casefold()
                == " ".join(target.name.split()).casefold()
            )
            else None
        )
        related_window_transition_bridge = bool(
            related_window_transition
            and (not expected_result.verified or disappearance_destination is not None)
        )
        if related_window_transition_bridge:
            # The exact semantic control produced a driver-proven related HWND
            # transition, but the destination's rendered content may not yet
            # exist in UIA.  Treat only the transition as verified, then force
            # the planner to inspect the fresh destination frame; this never
            # turns the requested text condition into an automatic success.
            expected_result = VerificationResult(
                True,
                "exact semantic action opened one related application window; "
                "the fresh destination frame still requires planner verification",
                app=after.app,
                generation=after.generation,
                expectation_kind=DesktopExpectationKind.LAST_ACTION_VERIFIED,
            )
        elif not expected_result.verified:
            return self._failure(
                f"动作后任务条件未成立：{expected_result.reason}",
                stage="verify_action",
                error_code="POSTCONDITION_NOT_VERIFIED",
                app=after.app,
                generation=after.generation,
            )
        inspection = self.safety.inspect_observation(after, user_text=state.task)
        if inspection.disposition == DesktopSafetyDisposition.BLOCK:
            return self._failure(
                f"动作后界面不能发送给规划器：{inspection.reason}",
                stage="observe_safety",
                error_code="POST_ACTION_OBSERVATION_BLOCKED",
                app=after.app,
                generation=after.generation,
            )
        state.observation = after
        if instrumental_reveal:
            assert reveal_signature is not None
            state.instrumental_reveal_count += 1
            state.instrumental_reveal_action_counts[reveal_signature] = (
                state.instrumental_reveal_action_counts.get(reveal_signature, 0) + 1
            )
            state.instrumental_reveal_fingerprints.update(
                {before.fingerprint, after.fingerprint}
            )
        else:
            if visual_point_signature is not None:
                state.visual_point_click_count += 1
                state.visual_point_region_counts[visual_point_signature] = (
                    state.visual_point_region_counts.get(visual_point_signature, 0) + 1
                )
            state.last_verification = expected_result
            state.last_action_expectation = (
                None if related_window_transition_bridge else expectation
            )
            state.last_action = rebound
            state.last_action_target = target.name if target is not None else None
            state.related_window_navigation_pending = related_window_transition_bridge
            state.related_window_id = (
                after.local_window_id if related_window_transition_bridge else None
            )
            state.related_window_destination = (
                disappearance_destination if related_window_transition_bridge else None
            )
            if counts_as_user_step:
                state.verified_user_step_count += 1
            state.verified_action_count += 1
        state.stale_replans = 0
        if instrumental_reveal:
            state.history.append(
                "locally verified one instrumental reveal bridge; "
                "it did not complete a user step"
            )
        else:
            state.history.append(
                f"locally verified {rebound.type.value}: {expected_result.reason}"
            )
        self._trace(
            stage="verify_action",
            error_code=(
                "INSTRUMENTAL_REVEAL_VERIFIED"
                if instrumental_reveal
                else "ACTION_VERIFIED"
            ),
            safe_message=(
                "A bounded reveal bridge changed the local interface"
                if instrumental_reveal
                else "A dispatched desktop action passed its local postcondition"
            ),
            app=after.app,
            generation=after.generation,
        )
        return None

    def _dictation_failure(
        self,
        message: str,
        *,
        stage: str,
        error_code: str,
        exception_type: str | None = None,
        app: str | None = None,
        generation: int | None = None,
    ) -> ComputerControlResult:
        self._clear_dictation_context()
        self._trace(
            stage=stage,
            error_code=error_code,
            safe_message="Continuous dictation stopped because its local binding failed closed",
            app=app,
            generation=generation,
            level="error",
        )
        return self._failure(
            message,
            stage=stage,
            error_code=error_code,
            exception_type=exception_type,
            app=app,
            generation=generation,
        )

    def _dictation_cancelled_result(self) -> ComputerControlResult:
        self._clear_dictation_context()
        return self._cancelled_result(
            "听写输入已取消，已清除本地输入框绑定",
            stage="dictation",
        )

    def _run_bound_dictation(
        self,
        payload: str,
        binding: _DictationBinding,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult:
        """Type one queued over segment into the exact still-focused composer."""

        if self.driver is None:
            return self._dictation_failure(
                "听写绑定存在，但没有可用的本地桌面驱动",
                stage="dictation",
                error_code="DICTATION_DRIVER_NOT_CONFIGURED",
            )
        if self._cancelled(cancel_event):
            return self._dictation_cancelled_result()
        try:
            self.driver.start()
            before = self.driver.observe(
                binding.app,
                cancel_event=self._current_cancel,
            )
        except Exception as exc:
            return self._dictation_failure(
                "听写前无法重新观察已绑定的本地窗口",
                stage="dictation",
                error_code="DICTATION_REOBSERVE_FAILED",
                exception_type=type(exc).__name__,
                app=binding.app,
            )
        if self._cancelled(cancel_event):
            return self._dictation_cancelled_result()
        target = self._bound_dictation_element(before, binding)
        if target is None:
            return self._dictation_failure(
                "听写窗口、焦点或输入框身份已经变化，未输入任何文字",
                stage="dictation",
                error_code="DICTATION_BINDING_CHANGED",
                app=before.app,
                generation=before.generation,
            )
        try:
            action = DesktopAction(
                DesktopActionType.TYPE_TEXT,
                app=before.app,
                generation=before.generation,
                element_index=target.index,
                text=payload,
            )
            expectation = DesktopExpectation(
                DesktopExpectationKind.FOCUSED_CONTAINS,
                text=payload,
            )
        except ValueError:
            return self._dictation_failure(
                "本段听写原文超出单次可验证输入范围",
                stage="dictation",
                error_code="DICTATION_PAYLOAD_INVALID",
                app=before.app,
                generation=before.generation,
            )
        # This marker is local safety provenance. ``payload`` is the unmodified
        # user-authored segment and never passes through the desktop planner.
        safety_user_text = local_dictation_user_text(payload)
        safety_result = self.safety.evaluate(
            action,
            before,
            user_text=safety_user_text,
            expectation=expectation,
        )
        if safety_result.disposition != DesktopSafetyDisposition.ALLOW:
            return self._dictation_failure(
                f"本地安全策略阻止听写输入：{safety_result.reason}",
                stage="dictation",
                error_code="DICTATION_ACTION_BLOCKED",
                app=before.app,
                generation=before.generation,
            )
        if self._cancelled(cancel_event):
            return self._dictation_cancelled_result()
        try:
            receipt = self.driver.execute(
                action,
                before,
                cancel_event=self._current_cancel,
            )
        except Exception as exc:
            return self._dictation_failure(
                "听写文字未能交给本地桌面驱动",
                stage="dictation",
                error_code="DICTATION_EXECUTION_FAILED",
                exception_type=type(exc).__name__,
                app=before.app,
                generation=before.generation,
            )
        if self._cancelled(cancel_event):
            return self._dictation_cancelled_result()
        try:
            after = self.driver.observe(
                binding.app,
                cancel_event=self._current_cancel,
            )
        except Exception as exc:
            return self._dictation_failure(
                "听写后无法重新观察并核验输入框",
                stage="dictation",
                error_code="DICTATION_POST_OBSERVE_FAILED",
                exception_type=type(exc).__name__,
                app=before.app,
                generation=before.generation,
            )
        if self._cancelled(cancel_event):
            return self._dictation_cancelled_result()
        verified_action = self.verifier.verify_action(action, receipt, before, after)
        if not verified_action.verified:
            return self._dictation_failure(
                f"听写动作本地验收失败：{verified_action.reason}",
                stage="dictation",
                error_code="DICTATION_ACTION_NOT_VERIFIED",
                app=after.app,
                generation=after.generation,
            )
        if self._bound_dictation_element(after, binding) is None:
            return self._dictation_failure(
                "听写后输入框身份或焦点发生变化，已停止继续听写",
                stage="dictation",
                error_code="DICTATION_POST_BINDING_CHANGED",
                app=after.app,
                generation=after.generation,
            )
        verified_expectation = self.verifier.verify_expectation(
            expectation,
            after,
            last_action_result=verified_action,
        )
        if not verified_expectation.verified:
            return self._dictation_failure(
                f"听写原文本地验收失败：{verified_expectation.reason}",
                stage="dictation",
                error_code="DICTATION_TEXT_NOT_VERIFIED",
                app=after.app,
                generation=after.generation,
            )
        with self._lifecycle_lock:
            if self._closed or self._cancelled(cancel_event):
                self._dictation_binding = None
                return self._cancelled_result(
                    "听写验收期间收到取消，已清除输入框绑定",
                    stage="dictation",
                )
            self._trusted_app_context = binding.app
            self._trusted_window_id = binding.local_window_id
        self._trace(
            stage="dictation",
            error_code="DICTATION_TEXT_VERIFIED",
            safe_message="One exact user-authored dictation segment was typed and locally verified",
            app=after.app,
            generation=after.generation,
        )
        return ComputerControlResult(
            True,
            "LOCAL_VERIFIED_COMPLETION: 听写原文已逐字输入并通过本地输入框验收",
            app=after.app,
            generation=after.generation,
        )

    def _bind_current_trusted_composer(
        self,
        task: str,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult | None:
        """Bind a composer already reached by the preceding verified command."""

        if (
            self.safety.profile != DesktopSafetyProfile.LOCAL_UNRESTRICTED
            or not _dictation_only_utterance(task)
            or self.driver is None
        ):
            return None
        trusted_context = self._trusted_context()
        if trusted_context is None:
            return None
        if self._cancelled(cancel_event):
            return self._dictation_cancelled_result()
        trusted_app, trusted_window_id = trusted_context
        try:
            self.driver.start()
            observation = self.driver.observe(
                trusted_app,
                cancel_event=self._current_cancel,
            )
        except Exception as exc:
            self._clear_trusted_context()
            return self._dictation_failure(
                "开始听写前无法重新观察上一条已验证窗口",
                stage="dictation",
                error_code="DICTATION_CONTEXT_OBSERVE_FAILED",
                exception_type=type(exc).__name__,
                app=trusted_app,
            )
        if self._cancelled(cancel_event):
            return self._dictation_cancelled_result()
        if (
            observation.app.strip().casefold() != trusted_app
            or observation.local_window_id != trusted_window_id
        ):
            self._clear_trusted_context()
            return self._dictation_failure(
                "开始听写前上一条已验证窗口已经变化",
                stage="dictation",
                error_code="DICTATION_CONTEXT_WINDOW_CHANGED",
                app=observation.app,
                generation=observation.generation,
            )
        inspection = self.safety.inspect_observation(observation, user_text=task)
        if inspection.disposition == DesktopSafetyDisposition.BLOCK:
            self._clear_trusted_context()
            return self._dictation_failure(
                f"开始听写前本地安全策略阻止该界面：{inspection.reason}",
                stage="dictation",
                error_code="DICTATION_CONTEXT_BLOCKED",
                app=observation.app,
                generation=observation.generation,
            )
        if not self._bind_dictation_from_observation(observation, task=task):
            return self._dictation_failure(
                "上一条已验证窗口没有唯一且已聚焦的可编辑 composer",
                stage="dictation",
                error_code="DICTATION_TARGET_NOT_VERIFIED",
                app=observation.app,
                generation=observation.generation,
            )
        return ComputerControlResult(
            True,
            "LOCAL_VERIFIED_COMPLETION: 已从上一条已验证窗口绑定连续听写输入框",
            app=observation.app,
            generation=observation.generation,
        )

    def run(
        self,
        instruction: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ComputerControlResult:
        if not isinstance(instruction, str) or not instruction.strip():
            self._clear_dictation_context()
            return self._failure(
                "电脑控制指令为空",
                stage="runtime",
                error_code="EMPTY_INSTRUCTION",
            )
        # Preserve the queued transcript byte-for-byte for bound dictation.
        # Ordinary control commands are stripped only after mode dispatch.
        raw_instruction = instruction
        with self._execution_lock:
            with self._lifecycle_lock:
                if self._closed:
                    return self._failure(
                        "桌面控制器已经关闭",
                        stage="runtime",
                        error_code="CONTROLLER_CLOSED",
                    )
                if self._pending is not None:
                    return self._failure(
                        "上一动作仍在等待精确确认",
                        stage="runtime",
                        error_code="CONFIRMATION_PENDING",
                    )
                self._current_cancel = threading.Event()
            started_at = float(self._monotonic())
            prior_dictation_binding = self._dictation_context()
            control_from_dictation = False
            try:
                if prior_dictation_binding is not None and self._cancelled(cancel_event):
                    return self._dictation_cancelled_result()
                if prior_dictation_binding is not None:
                    if _dictation_exit_utterance(raw_instruction):
                        self._clear_dictation_context()
                        return ComputerControlResult(
                            True,
                            "LOCAL_VERIFIED_COMPLETION: 已退出连续听写并清除输入框绑定",
                        )
                    stripped_control = _strip_leading_control_prefix(
                        raw_instruction,
                        self._control_prefixes,
                    )
                    if stripped_control is None:
                        self._set_driver_task_context("active local dictation composer")
                        return self._run_bound_dictation(
                            raw_instruction,
                            prior_dictation_binding,
                            cancel_event=cancel_event,
                        )
                    if not stripped_control.strip():
                        self._clear_dictation_context()
                        return self._failure(
                            "听写中的电脑控制前缀后没有实际指令",
                            stage="runtime",
                            error_code="EMPTY_CONTROL_INSTRUCTION",
                        )
                    if _dictation_exit_utterance(stripped_control):
                        self._clear_dictation_context()
                        return ComputerControlResult(
                            True,
                            "LOCAL_VERIFIED_COMPLETION: 已退出连续听写并清除输入框绑定",
                        )
                    instruction = stripped_control.strip()
                    control_from_dictation = True
                    # Ordinary control is allowed to switch apps or focus. Drop
                    # the capability before planning and restore it only after a
                    # successful final observation proves the exact same target.
                    self._clear_dictation_context()
                else:
                    instruction = raw_instruction.strip()

                self._set_driver_task_context(instruction)
                current_composer_result = self._bind_current_trusted_composer(
                    instruction,
                    cancel_event=cancel_event,
                )
                if current_composer_result is not None:
                    return current_composer_result
                try:
                    native_result = self._run_native(
                        instruction,
                        cancel_event=cancel_event,
                    )
                except Exception as exc:
                    self._clear_dictation_context()
                    return self._failure(
                        "确定性本机路由发生内部错误",
                        stage="native_route",
                        error_code="NATIVE_ROUTE_INTERNAL_ERROR",
                        exception_type=type(exc).__name__,
                    )
                if native_result is not None:
                    if not native_result.success:
                        self._clear_dictation_context()
                    if (
                        native_result.success
                        and _explicit_dictation_intent(instruction)
                        and self.safety.profile
                        == DesktopSafetyProfile.LOCAL_UNRESTRICTED
                        and self._dictation_context() is None
                    ):
                        return self._dictation_failure(
                            "语音输入任务完成后未找到唯一且已聚焦的可编辑 composer",
                            stage="dictation",
                            error_code="DICTATION_TARGET_NOT_VERIFIED",
                        )
                    return native_result
                if failure := self._ensure_generic_components():
                    self._clear_dictation_context()
                    return failure
                assert self.driver is not None
                try:
                    self.driver.start()
                    inventory = self.driver.list_apps(cancel_event=self._current_cancel)
                    visible_apps = _visible_apps(inventory)
                except Exception as exc:
                    return self._failure(
                        f"本地桌面驱动不可用：{_safe_exception_message(exc)}",
                        stage="list_apps",
                        error_code="APP_INVENTORY_FAILED",
                        exception_type=type(exc).__name__,
                    )
                configured_profiles = getattr(self.driver, "profiles", {})
                configured_apps = (
                    tuple(configured_profiles) if isinstance(configured_profiles, dict) else ()
                )
                known_apps = tuple(dict.fromkeys((*visible_apps, *configured_apps, *_APP_ALIASES)))
                observation: DesktopObservation | None = None
                history: list[str] = []
                unrestricted = self.safety.profile == DesktopSafetyProfile.LOCAL_UNRESTRICTED
                if unrestricted:
                    # This is an explicit opt-in for one private machine.  The
                    # complete locally enumerated window set is the planner's
                    # scope; the user no longer needs to name a preconfigured
                    # application before each natural-language instruction.
                    if not visible_apps:
                        return self._failure(
                            "当前没有可供桌面规划器观察的可见窗口",
                            stage="list_apps",
                            error_code="NO_VISIBLE_WINDOWS",
                        )
                    allowed_apps = frozenset(visible_apps)
                    apps = inventory
                    history.append(
                        "local unrestricted mode exposes every freshly enumerated visible window"
                    )
                    explicit_apps = _explicitly_named_apps(instruction, known_apps)
                    unsupported_scopes = _unsupported_explicit_app_scopes(
                        instruction,
                        known_apps,
                    )
                    trusted_context = self._trusted_context()
                    if (
                        not explicit_apps
                        and not unsupported_scopes
                        and trusted_context is not None
                    ):
                        trusted_app, trusted_window_id = trusted_context
                        if trusted_app in visible_apps:
                            try:
                                candidate = self.driver.observe(
                                    trusted_app,
                                    cancel_event=self._current_cancel,
                                )
                            except Exception:
                                self._clear_trusted_context()
                            else:
                                inspection = self.safety.inspect_observation(
                                    candidate,
                                    user_text=instruction,
                                )
                                if (
                                    candidate.app.strip().casefold() == trusted_app
                                    and candidate.local_window_id == trusted_window_id
                                    and inspection.disposition
                                    != DesktopSafetyDisposition.BLOCK
                                ):
                                    observation = candidate
                                    history.append(
                                        "resumed the same locally verified app window as "
                                        "fresh initial planner context"
                                    )
                                else:
                                    self._clear_trusted_context()
                        else:
                            self._clear_trusted_context()
                else:
                    explicit_apps = _explicitly_named_apps(instruction, known_apps)
                    unsupported_scopes = _unsupported_explicit_app_scopes(
                        instruction,
                        known_apps,
                    )
                    if explicit_apps or unsupported_scopes:
                        # A newly spoken explicit scope must never fall back to a
                        # previously verified application after this command fails.
                        self._clear_trusted_context()
                    if unsupported_scopes:
                        return self._failure(
                            "本次口述明确指定了未配置的应用，不能继承上一应用",
                            stage="list_apps",
                            error_code="APP_SCOPE_UNSUPPORTED",
                        )
                    allowed_apps = explicit_apps.intersection(visible_apps)
                    if len(explicit_apps) > 1:
                        return self._failure(
                            "通用桌面任务必须指定唯一一个当前可见应用",
                            stage="list_apps",
                            error_code="APP_SCOPE_AMBIGUOUS",
                        )
                    if explicit_apps and not allowed_apps:
                        return self._failure(
                            "本次口述明确指定的应用当前不可见，不能继承上一应用",
                            stage="list_apps",
                            error_code="APP_SCOPE_NOT_VISIBLE",
                            app=next(iter(explicit_apps)),
                        )
                    if not allowed_apps:
                        trusted_context = self._trusted_context()
                        if trusted_context is None:
                            return self._failure(
                                "通用桌面任务必须在本次口述中明确且肯定地指定唯一一个当前可见应用",
                                stage="list_apps",
                                error_code="APP_SCOPE_REQUIRED",
                            )
                        trusted_app, trusted_window_id = trusted_context
                        if trusted_app not in visible_apps:
                            self._clear_trusted_context()
                            return self._failure(
                                "上一条已验证应用当前不可见，不能继承控制范围",
                                stage="list_apps",
                                error_code="SESSION_APP_NOT_VISIBLE",
                                app=trusted_app,
                            )
                        try:
                            observation = self.driver.observe(
                                trusted_app,
                                cancel_event=self._current_cancel,
                            )
                        except Exception as exc:
                            return self._failure(
                                f"继承应用前桌面观察失败：{_safe_exception_message(exc)}",
                                stage="observe_driver",
                                error_code="SESSION_CONTEXT_OBSERVE_FAILED",
                                exception_type=type(exc).__name__,
                                app=trusted_app,
                            )
                        if observation.app.strip().casefold() != trusted_app:
                            self._clear_trusted_context()
                            return self._failure(
                                "桌面驱动返回了与上一条已验证应用不同的应用",
                                stage="observe_driver",
                                error_code="SESSION_APP_CHANGED",
                                app=observation.app,
                                generation=observation.generation,
                            )
                        if (
                            not observation.local_window_id
                            or observation.local_window_id != trusted_window_id
                        ):
                            self._clear_trusted_context()
                            return self._failure(
                                "上一条已验证应用窗口已经变化，不能继承控制范围",
                                stage="observe_driver",
                                error_code="SESSION_WINDOW_CHANGED",
                                app=observation.app,
                                generation=observation.generation,
                            )
                        inspection = self.safety.inspect_observation(
                            observation,
                            user_text=instruction,
                        )
                        if inspection.disposition == DesktopSafetyDisposition.BLOCK:
                            return self._failure(
                                f"本地安全策略阻止继承该界面：{inspection.reason}",
                                stage="observe_safety",
                                error_code="SESSION_OBSERVATION_BLOCKED",
                                app=observation.app,
                                generation=observation.generation,
                            )
                        allowed_apps = frozenset({trusted_app})
                        history.append("resumed the same locally verified app window")
                    apps = json.dumps(
                        [{"app": app, "visible_window_count": 1} for app in sorted(allowed_apps)],
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                state = _TaskState(
                    task=instruction.strip(),
                    apps=apps,
                    allowed_apps=allowed_apps,
                    observation=observation,
                    history=history,
                    last_verification=None,
                    last_action_expectation=None,
                    last_action=None,
                    last_action_target=None,
                    steps=0,
                    verified_action_count=0,
                    verified_user_step_count=0,
                    remaining_seconds=self._remaining(started_at),
                )
                self._trace(
                    stage="list_apps",
                    error_code="APP_SCOPE_RESOLVED",
                    safe_message=(
                        "Every freshly enumerated visible window is available to the planner"
                        if unrestricted
                        else "The desktop application scope was resolved locally"
                    ),
                )
                result = self._drive(state, cancel_event=cancel_event)
                self._remember_trusted_context(state, result)
                if not result.success:
                    self._clear_dictation_context()
                    return result
                if control_from_dictation and prior_dictation_binding is not None:
                    if (
                        state.observation is None
                        or self._bound_dictation_element(
                            state.observation,
                            prior_dictation_binding,
                        )
                        is None
                    ):
                        self._clear_dictation_context()
                    else:
                        with self._lifecycle_lock:
                            if not self._closed and not self._cancelled(cancel_event):
                                self._dictation_binding = prior_dictation_binding
                if _explicit_dictation_intent(instruction) and (
                    state.observation is None
                    or not self._bind_dictation_from_observation(
                        state.observation,
                        task=instruction,
                    )
                ):
                    return self._dictation_failure(
                        "语音输入任务完成后未找到唯一且已聚焦的可编辑 composer",
                        stage="dictation",
                        error_code="DICTATION_TARGET_NOT_VERIFIED",
                        app=(
                            state.observation.app
                            if state.observation is not None
                            else None
                        ),
                        generation=(
                            state.observation.generation
                            if state.observation is not None
                            else None
                        ),
                    )
                return result
            finally:
                try:
                    self._set_driver_task_context(None)
                finally:
                    with self._lifecycle_lock:
                        self._current_cancel = None

    def execute(
        self,
        instruction: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ComputerControlResult:
        return self.run(instruction, cancel_event=cancel_event)

    def _confirm_native(
        self,
        pending: _PendingConfirmation,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult:
        assert pending.native_plan is not None
        assert pending.native_user_text is not None and pending.native_binding_digest is not None
        if self._cancelled(cancel_event):
            return self._cancelled_result(
                "确认后的本机动作已取消",
                stage="native_route",
            )
        try:
            prepared = self.native_router.executor.prepare_plan(clone_plan(pending.native_plan))
            evaluated = self.native_router.safety.evaluate(
                prepared,
                user_text=pending.native_user_text,
            )
            evaluated = clone_plan(evaluated)
            if self._cancelled(cancel_event):
                return self._cancelled_result(
                    "确认后的本机动作已取消",
                    stage="native_route",
                )
            if evaluated.risk != RiskLevel.CONFIRM:
                return self._failure(
                    "确认时本机安全分类已经变化",
                    stage="native_route",
                    error_code="NATIVE_CONFIRMATION_CLASSIFICATION_CHANGED",
                )
            if self._native_binding(evaluated) != pending.native_binding_digest:
                return self._failure(
                    "确认时本机目标身份或内容已经变化",
                    stage="native_route",
                    error_code="NATIVE_CONFIRMATION_BINDING_CHANGED",
                )
            if self._cancelled(cancel_event):
                return self._cancelled_result(
                    "确认后的本机动作已取消",
                    stage="native_route",
                )
            with guard_plan_paths(
                evaluated,
                pending.native_binding_digest,
            ):
                if self._cancelled(cancel_event):
                    return self._cancelled_result(
                        "确认后的本机动作已取消",
                        stage="native_route",
                    )
                results = tuple(self.native_router.executor.execute_plan(evaluated))
        except Exception as exc:
            return self._failure(
                f"确认后的本机动作失败：{type(exc).__name__}",
                stage="native_route",
                error_code="NATIVE_CONFIRMATION_EXECUTION_FAILED",
                exception_type=type(exc).__name__,
            )
        if self._cancelled(cancel_event):
            return self._cancelled_result(
                "确认后的本机动作已取消",
                stage="native_route",
            )
        if not NativeSkillRouter.execution_is_locally_verified(
            evaluated,
            results,
        ):
            return self._failure(
                "确认后的本机动作没有完成全部后置检查",
                stage="native_route",
                error_code="NATIVE_CONFIRMATION_NOT_VERIFIED",
            )
        if self._cancelled(cancel_event):
            self._clear_trusted_context()
            return self._cancelled_result(
                "确认后的本机动作在验证期间被取消",
                stage="native_route",
            )
        context_expected = bool(
            self.driver is not None
            and self.safety.profile == DesktopSafetyProfile.PERSONAL_TRUSTED
            and len(
                {
                    action.app.strip().casefold()
                    for action in evaluated.actions
                    if isinstance(action.app, str) and action.app.strip()
                }
            )
            == 1
        )
        context_refreshed = self._refresh_trusted_context_after_native(
            evaluated,
            user_text=pending.native_user_text,
            cancel_event=cancel_event,
        )
        return self._publish_native_success(
            "LOCAL_VERIFIED_COMPLETION: 已执行精确确认的本机动作",
            cancel_event=cancel_event,
            context_expected=context_expected,
            context_refreshed=context_refreshed,
        )

    def _confirm_desktop(
        self,
        pending: _PendingConfirmation,
        *,
        cancel_event: threading.Event | None,
    ) -> ComputerControlResult:
        assert self.driver is not None
        assert (
            pending.state is not None and pending.action is not None and pending.binding is not None
        )
        state = pending.state
        if state.observation is None:
            return self._failure(
                "确认动作丢失了原始观察",
                stage="runtime",
                error_code="CONFIRMATION_STATE_MISSING",
            )
        state.remaining_seconds = max(state.remaining_seconds, 0.1)
        failure = self._perform_action(
            state,
            pending.action,
            expectation=pending.action_expectation,
            counts_as_user_step=pending.counts_as_user_step,
            confirmed_binding=pending.binding,
            cancel_event=cancel_event,
        )
        if failure is not None:
            return failure
        result = self._drive(state, cancel_event=cancel_event)
        self._remember_trusted_context(state, result)
        return result

    def confirm(
        self,
        confirmation_id: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> ComputerControlResult:
        with self._execution_lock:
            with self._lifecycle_lock:
                if self._closed:
                    return self._failure(
                        "桌面控制器已经关闭",
                        stage="runtime",
                        error_code="CONTROLLER_CLOSED",
                    )
                pending = self._pending
                if pending is None or confirmation_id != pending.confirmation_id:
                    return self._failure(
                        "确认标识不匹配或已经使用",
                        stage="runtime",
                        error_code="CONFIRMATION_ID_INVALID",
                    )
                self._pending = None
                self._current_cancel = threading.Event()
            try:
                confirmation_task = (
                    pending.native_user_text
                    if pending.native_plan is not None
                    else pending.state.task
                    if pending.state is not None
                    else None
                )
                self._set_driver_task_context(confirmation_task)
                if float(self._monotonic()) > pending.expires_at:
                    return self._failure(
                        "确认已经超时",
                        stage="runtime",
                        error_code="CONFIRMATION_EXPIRED",
                    )
                if pending.native_plan is not None:
                    return self._confirm_native(pending, cancel_event=cancel_event)
                return self._confirm_desktop(pending, cancel_event=cancel_event)
            finally:
                try:
                    self._set_driver_task_context(None)
                finally:
                    with self._lifecycle_lock:
                        self._current_cancel = None

    def cancel(self) -> bool:
        with self._lifecycle_lock:
            event = self._current_cancel
            pending = self._pending
            self._pending = None
            self._dictation_binding = None
            if event is not None:
                event.set()
        driver_cancelled = self.driver.cancel() if self.driver is not None else False
        return bool(event is not None or pending is not None or driver_cancelled)

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._trusted_app_context = None
            self._trusted_window_id = None
            self._dictation_binding = None
        self.cancel()
        if self.driver is not None:
            self.driver.close()
