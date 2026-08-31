from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from collections.abc import Callable, Mapping
from typing import Any

from .config import Settings
from .desktop.protocol import (
    DesktopAction,
    DesktopActionType,
    DesktopElement,
    DesktopExpectation,
    DesktopExpectationKind,
    DesktopObservation,
    ElementPlane,
    element_plane,
    redact_credential_like_text,
)
from .desktop.safety import (
    DesktopSafetyDisposition,
    DesktopSafetyPolicy,
    observation_credential_summary,
)
from .desktop.verifier import DesktopVerifier
from .desktop.windows_uia import WindowsUiaDriver

SUPPORTED_APPS = frozenset({"claude", "codex"})
_SAFE_STATS = frozenset(
    {
        "descendants",
        "visible",
        "interesting",
        "retained",
        "retained_focused",
        "retained_selected",
        "retained_actionable",
        "omitted",
        "omitted_by_max_elements",
        "omitted_oversized_name",
        "omitted_oversized_identity",
        "omitted_invalid_metadata",
        "summarized_long_values",
        "bounded_surface_items",
        "bounded_surface_sha256",
        # Newer per-application observation profiles use these names. Keep
        # this reader forward-compatible while still applying a field allow-list.
        "total_element_count",
        "kept_control_count",
        "skipped_content_count",
        "truncated_content_count",
        "property_error_count",
        "elements_truncated",
        "total",
        "kept",
        "skipped",
        "property_errors",
        "omitted_by_profile",
        "omitted_by_content_limit",
        "skipped_long_content",
        "summarized_long_content",
    }
)
_PATH_RE = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\)[^\s,;]+")
_EMAIL_RE = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
_SECRET_FIELD_RE = re.compile(
    r"(?i)\b(?:password|passcode|api[ _-]?key|secret|access[ _-]?token|otp|2fa)\b"
    r"|密码|口令|密钥|验证码|令牌"
)
_SEARCH_FIELD_RE = re.compile(r"(?i)\b(?:search|find|filter)\b|搜索|查找|筛选")
_COMPOSER_HINT_RE = re.compile(
    r"(?i)\b(?:prompt|message|composer|chat|ask|reply|send a message)\b"
    r"|输入|消息|提问|回复|随心输入"
)
_STATIC_LABEL_RE = re.compile(
    r"(?ix)^(?:"
    r"menu|search|back|forward|hide\ sidebar|show\ sidebar|"
    r"chat(?:\ and\ cowork)?|cowork|code|design|new|new\ chat|new\ conversation|"
    r"projects?|artifacts?|customize|filter|prompt|message|ask\ claude|"
    r"type\ a\ message|send|add|cancel|close|copy|read\ aloud|"
    r"press\ and\ hold\ to\ record|dictation\ settings|"
    r"菜单|搜索|返回|前进|隐藏侧边栏|显示侧边栏|聊天|代码|设计|"
    r"新建|新对话|项目|提示词|消息|输入消息|输入提示词|随心输入|"
    r"发送|添加|取消|关闭|复制|朗读|语音输入|听写设置"
    r")$"
)


class AppDoctorFailure(RuntimeError):
    def __init__(self, error_code: str, safe_message: str) -> None:
        super().__init__(safe_message)
        self.error_code = error_code
        self.safe_message = safe_message

    def __str__(self) -> str:
        return self.safe_message


def _safe_label(value: str, *, maximum: int = 96) -> str:
    cleaned = " ".join(value.split())
    cleaned = redact_credential_like_text(cleaned) or ""
    cleaned = _PATH_RE.sub("<local-path>", cleaned)
    cleaned = _EMAIL_RE.sub("<email>", cleaned)
    if not cleaned:
        return "<unnamed>"
    if len(cleaned) <= maximum and _STATIC_LABEL_RE.fullmatch(cleaned):
        return cleaned
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:16]
    return f"<label chars={len(cleaned)} sha256={digest}>"


def _safe_uia_stats(observation: DesktopObservation) -> dict[str, Any]:
    try:
        header = json.loads(observation.accessibility_text.splitlines()[0])
        raw_stats = header.get("uia_stats", {})
    except (IndexError, TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw_stats, Mapping):
        return {}
    safe: dict[str, Any] = {}
    for key in _SAFE_STATS:
        value = raw_stats.get(key)
        if key == "bounded_surface_sha256" and (
            value is None or (isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value))
        ):
            safe[key] = value
            continue
        if type(value) in {int, bool} and (not isinstance(value, int) or value >= 0):
            safe[key] = value
    # Typed observation fields are authoritative when present. The JSON header
    # remains for backward-compatible diagnostics, not for safety decisions.
    if observation.total_element_count is not None:
        safe["total_element_count"] = observation.total_element_count
        safe["elements_truncated"] = observation.elements_truncated
        safe["skipped_content_count"] = observation.skipped_long_content_count
        safe["property_error_count"] = observation.property_error_count
    return safe


def safe_observation_report(
    observation: DesktopObservation,
    *,
    policy: DesktopSafetyPolicy,
    user_text: str = "",
    max_labels: int = 60,
) -> dict[str, Any]:
    """Build a content-free report suitable for terminal output and bug reports."""

    inspection = policy.inspect_observation(observation, user_text=user_text)
    type_counts = Counter(element.control_type for element in observation.elements)
    plane_counts = Counter(element_plane(element).value for element in observation.elements)
    uia_stats = _safe_uia_stats(observation)
    visible_controls: list[dict[str, Any]] = []
    for element in observation.elements:
        plane = element_plane(element)
        if plane == ElementPlane.CONTENT:
            continue
        visible_controls.append(
            {
                "index": element.index,
                "control_type": element.control_type,
                "plane": plane.value,
                "label": _safe_label(element.name),
                "focused": element.focused,
                "selected": element.selected,
                "enabled": element.enabled,
                "addressable": element.addressable,
            }
        )
    visible_controls.sort(
        key=lambda item: (
            item["focused"] is not True,
            item["selected"] is not True,
            item["control_type"].casefold() not in {"edit", "button", "tabitem"},
            int(item["index"]),
        )
    )
    total_elements = observation.total_element_count
    if total_elements is None:
        total_elements = uia_stats.get("total", uia_stats.get("descendants"))
    stats_are_valid = bool(
        isinstance(total_elements, int)
        and total_elements >= len(observation.elements)
        and uia_stats
    )
    observe_succeeded = bool(
        stats_are_valid
        and observation.local_window_id
        and any(
            element.addressable
            and element_plane(element) in {ElementPlane.CONTROL, ElementPlane.INPUT}
            for element in observation.elements
        )
    )
    return {
        "app": observation.app,
        "observe_succeeded": observe_succeeded,
        "generation": observation.generation,
        "observation_sha256": observation.fingerprint,
        "safety_profile": policy.profile.value,
        "safety_disposition": inspection.disposition.value,
        "safety_reason": inspection.reason,
        "uia_stats": uia_stats,
        "observation_stats": {
            "total_elements": total_elements,
            "kept_elements": uia_stats.get("kept", len(observation.elements)),
            "kept_controls": plane_counts[ElementPlane.CONTROL.value]
            + plane_counts[ElementPlane.INPUT.value],
            "skipped_elements": uia_stats.get("skipped", uia_stats.get("omitted")),
            "skipped_content": observation.skipped_long_content_count
            or uia_stats.get("skipped_long_content", 0)
            + uia_stats.get("omitted_by_content_limit", 0),
            "truncated_content": uia_stats.get("summarized_long_content", 0),
            "property_errors": uia_stats.get("property_errors", 0),
        },
        "element_count": len(observation.elements),
        "control_type_counts": dict(sorted(type_counts.items())),
        "plane_counts": dict(sorted(plane_counts.items())),
        "credential_findings": observation_credential_summary(observation),
        "safe_labels": visible_controls[:max_labels],
        "safe_labels_truncated": len(visible_controls) > max_labels,
        "safe_labels_omitted": max(0, len(visible_controls) - max_labels),
        "chat_content_included": False,
        "element_values_included": False,
        "automation_ids_included": False,
    }


def _composer_hints(settings: Settings, app: str) -> tuple[str, ...]:
    profile = settings.apps[app]
    configured = getattr(profile, "composer_names", ())
    if not configured:
        observation_profile = getattr(profile, "observation", None)
        configured = getattr(observation_profile, "composer_names", ())
    return tuple(item.casefold() for item in configured if isinstance(item, str) and item.strip())


def _composer_score(element: DesktopElement, *, hints: tuple[str, ...]) -> int | None:
    if (
        not element.enabled
        or not element.addressable
        or element.password
        or not element.composer
        or element_plane(element) != ElementPlane.INPUT
        or element.control_type.casefold() not in {"edit", "document"}
    ):
        return None
    identity = " ".join((element.name, element.automation_id or "")).strip()
    if _SECRET_FIELD_RE.search(identity):
        return None
    score = 100 if element.focused is True else 0
    normalized_name = " ".join(element.name.casefold().split())
    normalized_identity = " ".join(identity.casefold().split())
    configured_hint = bool(
        normalized_identity and any(hint in normalized_identity for hint in hints)
    )
    composer_hint = bool(_COMPOSER_HINT_RE.search(normalized_identity))
    # A generic Search/Find/Filter input is never a draft composer unless an
    # app profile explicitly names it. Likewise, an unfocused input needs a
    # positive composer identity; merely being the only Edit is insufficient.
    if _SEARCH_FIELD_RE.search(normalized_identity) and not configured_hint:
        return None
    if (
        (element.focused is not True or bool(normalized_name))
        and not configured_hint
        and not composer_hint
    ):
        return None
    if configured_hint:
        score += 40
    if composer_hint:
        score += 25
    if element.value is None or not element.value.strip() or element.value == element.name:
        score += 5
    return score


def select_composer(
    observation: DesktopObservation,
    *,
    hints: tuple[str, ...] = (),
) -> DesktopElement:
    scored = [
        (score, element)
        for element in observation.elements
        if (score := _composer_score(element, hints=hints)) is not None
    ]
    if not scored:
        raise AppDoctorFailure(
            "COMPOSER_NOT_FOUND",
            "No enabled non-password composer candidate was found",
        )
    highest = max(score for score, _ in scored)
    candidates = [element for score, element in scored if score == highest]
    if len(candidates) != 1:
        raise AppDoctorFailure(
            "COMPOSER_NOT_UNIQUE",
            "ComposerNotUnique: focus the intended empty composer and retry",
        )
    return candidates[0]


def _matching_composer(
    before: DesktopElement,
    after: DesktopObservation,
) -> DesktopElement:
    if before.local_identity:
        matches = [
            element
            for element in after.elements
            if element.local_identity == before.local_identity
            and element.control_type == before.control_type
            and element_plane(element) == ElementPlane.INPUT
            and element.addressable
            and element.composer
        ]
        if len(matches) == 1:
            return matches[0]
        raise AppDoctorFailure(
            "COMPOSER_IDENTITY_CHANGED",
            "The composer runtime identity changed before local verification",
        )
    if before.automation_id:
        matches = [
            element
            for element in after.elements
            if element.automation_id == before.automation_id
            and element.control_type == before.control_type
            and element.name == before.name
            and element_plane(element) == ElementPlane.INPUT
            and element.addressable
            and element.composer
        ]
        if len(matches) == 1:
            return matches[0]
        raise AppDoctorFailure(
            "COMPOSER_IDENTITY_CHANGED",
            "The composer automation identity changed before local verification",
        )
    matches = [
        element
        for element in after.elements
        if element.name == before.name
        and element.control_type == before.control_type
        and element_plane(element) == ElementPlane.INPUT
        and element.addressable
        and element.composer
    ]
    if len(matches) == 1:
        return matches[0]
    raise AppDoctorFailure(
        "COMPOSER_IDENTITY_CHANGED",
        "The composer identity changed before local verification",
    )


def _fresh_same_window(
    before: DesktopObservation,
    after: DesktopObservation,
) -> bool:
    return bool(
        after.app.casefold() == before.app.casefold()
        and after.generation > before.generation
        and after.captured_at >= before.captured_at
        and before.local_window_id
        and after.local_window_id
        and after.local_window_id == before.local_window_id
    )


def _focus_composer(
    driver: WindowsUiaDriver,
    observation: DesktopObservation,
    composer: DesktopElement,
    *,
    policy: DesktopSafetyPolicy,
) -> tuple[DesktopObservation, DesktopElement, bool]:
    if composer.focused is True:
        return observation, composer, True
    verifier = DesktopVerifier()
    current = observation
    current_composer = composer
    # SetFocus and a physical-click fallback are separate, freshly observed
    # actions. If SetFocus partially succeeds before raising, the re-observation
    # sees focus and prevents a second click. The fallback itself never uses
    # generic Invoke/Select/Toggle patterns, which can submit a custom composer.
    for action_type, action_name in (
        (DesktopActionType.PERFORM_SECONDARY_ACTION, "setfocus"),
        (DesktopActionType.PERFORM_SECONDARY_ACTION, "clickfocus"),
    ):
        inspection = policy.inspect_observation(
            current,
            user_text=f"In {current.app}, focus the verified composer",
        )
        if inspection.disposition == DesktopSafetyDisposition.BLOCK:
            raise AppDoctorFailure(
                "COMPOSER_FOCUS_SURFACE_BLOCKED",
                "The composer focus surface became prohibited or sensitive",
            )
        action = DesktopAction(
            action_type,
            app=current.app,
            generation=current.generation,
            element_index=current_composer.index,
            action_name=action_name,
        )
        try:
            receipt = driver.execute(action, current)
        except Exception:
            # An input failure may still have changed focus. A fresh observation
            # is the only safe basis for deciding whether a fallback is needed.
            previous = current
            current = driver.observe(current.app)
            if not _fresh_same_window(previous, current):
                raise AppDoctorFailure(
                    "COMPOSER_FOCUS_OBSERVATION_STALE",
                    "Composer focus could not be verified in a fresh observation "
                    "of the same window",
                ) from None
            current_composer = _matching_composer(
                current_composer,
                current,
            )
            inspection = policy.inspect_observation(
                current,
                user_text=f"In {current.app}, focus the verified composer",
            )
            if inspection.disposition == DesktopSafetyDisposition.BLOCK:
                raise AppDoctorFailure(
                    "COMPOSER_FOCUS_SURFACE_BLOCKED",
                    "The composer focus surface became prohibited or sensitive",
                ) from None
            if current_composer.focused is True:
                return current, current_composer, True
            continue
        after = driver.observe(current.app)
        if not _fresh_same_window(current, after):
            raise AppDoctorFailure(
                "COMPOSER_FOCUS_OBSERVATION_STALE",
                "Composer focus could not be verified in a fresh observation "
                "of the same window",
            )
        current_composer = _matching_composer(current_composer, after)
        inspection = policy.inspect_observation(
            after,
            user_text=f"In {after.app}, focus the verified composer",
        )
        if inspection.disposition == DesktopSafetyDisposition.BLOCK:
            raise AppDoctorFailure(
                "COMPOSER_FOCUS_SURFACE_BLOCKED",
                "The composer focus surface became prohibited or sensitive",
            )
        verification = verifier.verify_action(action, receipt, current, after)
        if verification.verified and current_composer.focused is True:
            return after, current_composer, True
        current = after
    raise AppDoctorFailure(
        "COMPOSER_FOCUS_FAILED",
        "The unique composer could not be focused and freshly verified",
    )


def run_app_doctor(
    settings: Settings,
    *,
    app: str,
    draft_smoke: bool,
    driver_factory: Callable[[Mapping[str, Any]], WindowsUiaDriver] = WindowsUiaDriver,
) -> dict[str, Any]:
    normalized_app = app.strip().casefold()
    if normalized_app not in SUPPORTED_APPS or normalized_app not in settings.apps:
        raise AppDoctorFailure(
            "APP_NOT_SUPPORTED",
            "app-doctor supports only configured Claude and Codex applications",
        )
    policy = DesktopSafetyPolicy(profile=settings.computer_control.safety_profile)
    driver = driver_factory(settings.apps)
    try:
        observation = driver.observe(normalized_app)
        report = safe_observation_report(observation, policy=policy)
        if not report["observe_succeeded"]:
            raise AppDoctorFailure(
                "OBSERVATION_INCOMPLETE",
                "The application observation had no trustworthy addressable controls or stats",
            )
        if report["safety_disposition"] == DesktopSafetyDisposition.BLOCK.value:
            raise AppDoctorFailure(
                "OBSERVATION_SAFETY_BLOCKED",
                "The observed application is a prohibited or sensitive surface",
            )
        report["mode"] = "draft-smoke" if draft_smoke else "observe-only"
        report["draft_smoke"] = {
            "requested": draft_smoke,
            "performed": False,
            "sent": False,
        }
        if not draft_smoke:
            return report
        if policy.profile.value != "personal_trusted":
            raise AppDoctorFailure(
                "PERSONAL_TRUSTED_REQUIRED",
                "Draft smoke requires the explicit personal_trusted local safety profile",
            )

        hints = _composer_hints(settings, normalized_app)
        composer = select_composer(observation, hints=hints)
        if not composer.composer:
            raise AppDoctorFailure(
                "COMPOSER_NOT_VERIFIED",
                "The candidate was not positively classified as an app composer",
            )
        if not composer.local_identity and not composer.automation_id:
            raise AppDoctorFailure(
                "COMPOSER_IDENTITY_UNSTABLE",
                "Draft smoke requires a stable local composer identity",
            )
        observation, composer, focus_verified = _focus_composer(
            driver,
            observation,
            composer,
            policy=policy,
        )
        if not composer.value_observed:
            raise AppDoctorFailure(
                "COMPOSER_VALUE_UNREADABLE",
                "The composer value was not readable, so an empty draft cannot be proven",
            )
        current_value = composer.value or ""
        if current_value.strip():
            raise AppDoctorFailure(
                "COMPOSER_NOT_EMPTY",
                "The composer already contains a draft; it was left unchanged",
            )

        token = f"HandsFreePC-DRAFT-{uuid.uuid4().hex[:10]}-中文"
        expectation = DesktopExpectation(
            DesktopExpectationKind.FOCUSED_CONTAINS,
            text=token,
        )
        action = DesktopAction(
            DesktopActionType.TYPE_TEXT,
            app=observation.app,
            generation=observation.generation,
            element_index=composer.index,
            text=token,
        )
        target_label = composer.name.strip() or "focused composer"
        safety = policy.evaluate(
            action,
            observation,
            user_text=f"In {normalized_app}, type {token} into {target_label}",
            expectation=expectation,
        )
        if safety.disposition != DesktopSafetyDisposition.ALLOW:
            raise AppDoctorFailure(
                "DRAFT_ACTION_SAFETY_BLOCKED",
                "The draft action was not automatically allowed by personal_trusted safety",
            )
        receipt = driver.execute(action, observation)
        after = driver.observe(normalized_app)
        if not _fresh_same_window(observation, after):
            raise AppDoctorFailure(
                "POST_DRAFT_OBSERVATION_STALE",
                "The post-draft observation was not fresh or from the same window",
            )
        after_inspection = policy.inspect_observation(
            after,
            user_text=f"In {normalized_app}, type exact draft text into the composer",
        )
        if after_inspection.disposition == DesktopSafetyDisposition.BLOCK:
            raise AppDoctorFailure(
                "POST_DRAFT_OBSERVATION_BLOCKED",
                "The fresh post-draft application surface became prohibited or sensitive",
            )
        after_composer = _matching_composer(composer, after)
        if (
            not after_composer.composer
            or not after_composer.addressable
            or after_composer.focused is not True
            or not after_composer.value_observed
        ):
            raise AppDoctorFailure(
                "POST_DRAFT_COMPOSER_CHANGED",
                "The fresh post-draft target is no longer the verified focused composer",
            )
        if after_composer.value != token:
            raise AppDoctorFailure(
                "POST_DRAFT_VALUE_MISMATCH",
                "The composer did not read back the exact random draft",
            )
        verifier = DesktopVerifier()
        action_verification = verifier.verify_action(action, receipt, observation, after)
        expectation_verification = verifier.verify_expectation(
            expectation,
            after,
            last_action_result=action_verification,
        )
        verified = action_verification.verified and expectation_verification.verified
        cleanup = getattr(driver, "clear_app_doctor_draft", None)
        if not callable(cleanup):
            raise AppDoctorFailure(
                "DRAFT_CLEANUP_UNAVAILABLE",
                "The desktop driver cannot safely remove its exact app-doctor draft",
            )
        try:
            cleanup(after, after_composer, expected_text=token)
            cleaned = driver.observe(normalized_app)
            if not _fresh_same_window(after, cleaned):
                raise AppDoctorFailure(
                    "DRAFT_CLEANUP_OBSERVATION_STALE",
                    "Draft cleanup was not verified in a fresh observation of the same window",
                )
            # Verify cleanup against the same stable composer identity that held
            # the random draft. Re-selecting the "best" empty composer here can
            # produce a false PASS if focus moved to a different blank field while
            # the original field still contains the token.
            cleaned_composer = _matching_composer(after_composer, cleaned)
            cleanup_verified = bool(
                cleaned_composer.focused is True
                and cleaned_composer.value_observed
                and not (cleaned_composer.value or "").strip()
            )
            if not cleanup_verified:
                raise AppDoctorFailure(
                    "DRAFT_CLEANUP_FAILED",
                    "The exact app-doctor draft was not verified as removed",
                )
        except AppDoctorFailure:
            raise
        except Exception as exc:
            raise AppDoctorFailure(
                "DRAFT_CLEANUP_FAILED",
                "The exact app-doctor draft could not be safely removed",
            ) from exc
        report["draft_smoke"] = {
            "requested": True,
            "performed": True,
            "sent": False,
            "focus_verified": focus_verified,
            "fresh_observation": after.generation > observation.generation,
            "action_verified": action_verification.verified,
            "expectation_verified": expectation_verification.verified,
            "verified": verified,
            "cleanup_verified": cleanup_verified,
            "draft_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "draft_chars": len(token),
            "composer": {
                "control_type": composer.control_type,
                "label": _safe_label(composer.name),
            },
        }
        if not verified:
            raise AppDoctorFailure(
                "DRAFT_LOCAL_VERIFICATION_FAILED",
                "The random draft was not verified in a fresh local UIA observation",
            )
        return report
    finally:
        driver.close()
