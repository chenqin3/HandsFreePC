from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .protocol import (
    ActionReceipt,
    DesktopAction,
    DesktopActionType,
    DesktopDecision,
    DesktopDecisionKind,
    DesktopExpectation,
    DesktopExpectationKind,
    DesktopObservation,
    ElementPlane,
    element_plane,
)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    """A local verification result; driver or planner prose is never evidence."""

    verified: bool
    reason: str
    app: str | None = None
    generation: int | None = None
    expectation_kind: DesktopExpectationKind | None = None
    expectation_text: str | None = None


def _same_app(left: str, right: str) -> bool:
    return left.strip().casefold() == right.strip().casefold()


def _result(
    verified: bool,
    reason: str,
    observation: DesktopObservation | None = None,
    *,
    expectation: DesktopExpectation | None = None,
) -> VerificationResult:
    return VerificationResult(
        verified=verified,
        reason=reason,
        app=observation.app if observation is not None else None,
        generation=observation.generation if observation is not None else None,
        expectation_kind=expectation.kind if expectation is not None else None,
        expectation_text=expectation.text if expectation is not None else None,
    )


def _fresh_observation_failure(
    before: DesktopObservation,
    after: DesktopObservation,
) -> str | None:
    if not _same_app(before.app, after.app):
        return "post-action observation belongs to a different application"
    if after.generation <= before.generation:
        return "post-action observation is not a fresh generation"
    if after.captured_at < before.captured_at:
        return "post-action observation predates the action observation"
    if (before.local_window_id or after.local_window_id) and (
        not before.local_window_id
        or not after.local_window_id
        or before.local_window_id != after.local_window_id
    ):
        return "post-action observation belongs to a different local application window"
    if "\ufffd" in after.accessibility_text:
        return "post-action accessibility text contains damaged Unicode"
    return None


def _rendered_text_candidates(text: str) -> tuple[str, ...]:
    # Open Computer Use renders multiline UIA values using literal escape sequences.
    escaped = text.replace("\r", "\\r").replace("\n", "\\n")
    return (text,) if escaped == text else (text, escaped)


def _element_by_index(observation: DesktopObservation, index: str | None):
    if index is None:
        return None
    matches = [element for element in observation.elements if element.index == index]
    return matches[0] if len(matches) == 1 else None


def _matching_element_after(target, after: DesktopObservation):
    if target is None:
        return None
    if target.local_identity:
        matches = [
            element
            for element in after.elements
            if element.local_identity == target.local_identity
            and element.control_type == target.control_type
        ]
    elif target.automation_id:
        matches = [
            element
            for element in after.elements
            if element.automation_id == target.automation_id
            and element.control_type == target.control_type
            and element.name == target.name
        ]
    else:
        matches = [
            element
            for element in after.elements
            if element.name == target.name and element.control_type == target.control_type
        ]
    return matches[0] if len(matches) == 1 else None


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _contains_bounded_text(candidate: str, expected: str) -> bool:
    haystack = _normalized_text(candidate)
    needle = _normalized_text(expected)
    if not needle:
        return False
    if haystack == needle:
        return True
    return bool(re.search(rf"(?<![\w-]){re.escape(needle)}(?![\w-])", haystack))


def _observation_contains_text(observation: DesktopObservation, expected: str) -> bool:
    # Structured element names/values are authoritative when present. Raw UIA text is only a
    # bounded fallback for drivers that cannot expose structured elements.
    if observation.elements:
        return any(
            _contains_bounded_text(candidate, expected)
            for element in observation.elements
            for candidate in (element.name, element.value or "")
            if candidate
        )
    return _contains_bounded_text(observation.accessibility_text, expected)


_SEARCH_INPUT_RE = re.compile(r"(?i)\b(?:search|find|address\s+and\s+search)\b|搜索|查找|地址栏")


def _is_search_input(element) -> bool:
    if element is None or element_plane(element) != ElementPlane.INPUT:
        return False
    identity = "\n".join((element.name, element.automation_id or ""))
    return _SEARCH_INPUT_RE.search(identity) is not None


def _search_result_semantics(observation: DesktopObservation) -> tuple[object, ...]:
    """Return stable local result evidence while ignoring focus-only changes."""

    return (
        observation.window_title or "",
        tuple(
            (
                element.local_identity,
                element.automation_id,
                element.name,
                element.control_type,
                element.value,
                element.selected,
                element.enabled,
                element.addressable,
            )
            for element in observation.elements
        ),
    )


class DesktopVerifier:
    """Fail-closed checks over immutable before/after desktop observations."""

    def verify_action(
        self,
        action: DesktopAction,
        receipt: ActionReceipt,
        before: DesktopObservation,
        after: DesktopObservation,
    ) -> VerificationResult:
        if not receipt.accepted:
            return _result(False, "desktop driver did not accept the action", after)
        if receipt.action != action:
            return _result(False, "desktop driver receipt describes a different action", after)
        if receipt.before_generation != before.generation:
            return _result(False, "desktop driver receipt used a different generation", after)
        if action.generation != before.generation or not _same_app(action.app, before.app):
            return _result(False, "desktop action is not bound to its observation", after)
        if failure := _fresh_observation_failure(before, after):
            return _result(False, failure, after)
        if before.fingerprint == after.fingerprint:
            return _result(False, "no observable application change followed the action", after)

        if action.type in {DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE}:
            before_target = _element_by_index(before, action.element_index)
            after_target = _matching_element_after(before_target, after)
            if before_target is None or after_target is None:
                return _result(
                    False,
                    "the exact editable target is absent or ambiguous after input",
                    after,
                )
            if before_target.password or after_target.password:
                return _result(False, "password targets cannot be verified or controlled", after)
            if (
                not before_target.addressable
                or not after_target.addressable
                or element_plane(before_target) != ElementPlane.INPUT
                or element_plane(after_target) != ElementPlane.INPUT
                or before_target.editable is False
                or after_target.editable is False
            ):
                return _result(
                    False,
                    "the exact target is no longer a verified editable input",
                    after,
                )
            if action.type == DesktopActionType.TYPE_TEXT:
                if before_target.focused is not True or after_target.focused is not True:
                    return _result(False, "text input target did not retain verified focus", after)
                expected = action.text
                if (
                    expected is None
                    or after_target.value is None
                    or not any(
                        candidate in after_target.value
                        for candidate in _rendered_text_candidates(expected)
                    )
                    or after_target.value == before_target.value
                ):
                    return _result(
                        False,
                        "the exact text is not present in the changed target value",
                        after,
                    )
            elif (
                action.value is None
                or after_target.value != action.value
                or after_target.value == before_target.value
            ):
                return _result(
                    False,
                    "the exact replacement value is not present on the target element",
                    after,
                )

        return _result(True, "fresh local state verifies the action result", after)

    def verify_search_submission(
        self,
        action: DesktopAction,
        expectation: DesktopExpectation,
        before: DesktopObservation,
        after: DesktopObservation,
    ) -> VerificationResult:
        """Verify Enter submitted an exact query and produced non-focus result state."""

        query = expectation.text or ""
        key = (action.key or "").strip().casefold()
        before_target = _element_by_index(before, action.element_index)
        after_target = _matching_element_after(before_target, after)
        if (
            expectation.kind != DesktopExpectationKind.SEARCH_SUBMITTED
            or action.type != DesktopActionType.PRESS_KEY
            or key not in {"enter", "return"}
            or not query
            or not _is_search_input(before_target)
            or before_target.focused is not True
            or before_target.value is None
            or not any(
                candidate == before_target.value
                for candidate in _rendered_text_candidates(query)
            )
        ):
            return _result(
                False,
                "search submission is not bound to the focused exact query",
                after,
            )
        target_value_changed = bool(
            after_target is not None and after_target.value != before_target.value
        )
        if (
            not target_value_changed
            and _search_result_semantics(after) == _search_result_semantics(before)
        ):
            return _result(
                False,
                "search Enter produced no non-focus title, address, or UI result change",
                after,
            )
        return _result(
            True,
            "fresh local state verifies the exact search submission",
            after,
            expectation=expectation,
        )

    def verify_expectation(
        self,
        expectation: DesktopExpectation,
        observation: DesktopObservation,
        *,
        last_action_result: VerificationResult | None = None,
    ) -> VerificationResult:
        if "\ufffd" in observation.accessibility_text:
            return _result(False, "accessibility text contains damaged Unicode", observation)

        if expectation.kind == DesktopExpectationKind.APP_VISIBLE:
            return _result(True, "the requested application has a current observation", observation)

        if expectation.kind == DesktopExpectationKind.LAST_ACTION_VERIFIED:
            if (
                last_action_result is None
                or not last_action_result.verified
                or last_action_result.app is None
                or not _same_app(last_action_result.app, observation.app)
                or last_action_result.generation != observation.generation
            ):
                return _result(
                    False,
                    "no locally verified action is bound to this observation",
                    observation,
                )
            return _result(True, "the last action has matching local verification", observation)

        if expectation.kind == DesktopExpectationKind.SEARCH_SUBMITTED:
            if (
                last_action_result is None
                or not last_action_result.verified
                or last_action_result.expectation_kind != DesktopExpectationKind.SEARCH_SUBMITTED
                or _normalized_text(last_action_result.expectation_text or "")
                != _normalized_text(expectation.text or "")
                or last_action_result.app is None
                or not _same_app(last_action_result.app, observation.app)
                or last_action_result.generation is None
                or last_action_result.generation > observation.generation
            ):
                return _result(
                    False,
                    "no locally verified exact search submission is bound to this observation",
                    observation,
                )
            return _result(
                True,
                "the exact search submission has matching local transition evidence",
                observation,
                expectation=expectation,
            )

        expected = expectation.text
        if expected is None:
            return _result(False, "expectation has no local comparison text", observation)

        if expectation.kind == DesktopExpectationKind.TEXT_PRESENT:
            passed = _observation_contains_text(observation, expected)
            reason = (
                "expected text is present in accessibility state"
                if passed
                else "expected text is absent from accessibility state"
            )
            return _result(passed, reason, observation)

        if expectation.kind == DesktopExpectationKind.TEXT_ABSENT:
            passed = not _observation_contains_text(observation, expected)
            reason = (
                "expected-absent text is not in accessibility state"
                if passed
                else "expected-absent text remains in accessibility state"
            )
            return _result(passed, reason, observation)

        if expectation.kind == DesktopExpectationKind.FOCUSED_CONTAINS:
            focused_elements = [
                element for element in observation.elements if element.focused is True
            ]
            passed = len(focused_elements) == 1 and (
                _normalized_text(focused_elements[0].name) == _normalized_text(expected)
                or any(
                    candidate in (focused_elements[0].value or "")
                    for candidate in _rendered_text_candidates(expected)
                )
            )
            reason = (
                "the unique structured focused element contains the expected text"
                if passed
                else "the structured focused element is absent, ambiguous, or does not match"
            )
            return _result(passed, reason, observation)

        if expectation.kind == DesktopExpectationKind.ELEMENT_SELECTED:
            normalized = " ".join(expected.casefold().split())
            matches = [
                element
                for element in observation.elements
                if " ".join(element.name.casefold().split()) == normalized
            ]
            passed = len(matches) == 1 and matches[0].selected is True
            reason = (
                "exactly one requested element is locally selected"
                if passed
                else "the requested element is absent, ambiguous, or not selected"
            )
            return _result(passed, reason, observation)

        return _result(False, "unsupported desktop expectation", observation)

    def verify_completion(
        self,
        decision: DesktopDecision,
        observation: DesktopObservation,
        *,
        last_action_result: VerificationResult | None = None,
    ) -> VerificationResult:
        """Verify planner ``done`` against local state instead of trusting its prose."""

        if decision.kind != DesktopDecisionKind.DONE or decision.expectation is None:
            return _result(
                False,
                "planner did not provide a verifiable completion condition",
                observation,
            )
        if decision.app is not None and not _same_app(decision.app, observation.app):
            return _result(
                False,
                "completion observation belongs to a different application",
                observation,
            )
        return self.verify_expectation(
            decision.expectation,
            observation,
            last_action_result=last_action_result,
        )
