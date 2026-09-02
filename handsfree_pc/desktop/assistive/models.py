from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..protocol import DesktopAction


class GoalKind(StrEnum):
    APP_FOREGROUND = "app_foreground"
    PATH_OPEN = "path_open"
    URL_LOADED = "url_loaded"
    CONVERSATION_SELECTED = "conversation_selected"
    INPUT_CONTAINS = "input_contains"
    TEXT_VISIBLE = "text_visible"
    ELEMENT_STATE = "element_state"
    MESSAGE_SENT = "message_sent"
    # A local file handed to a chat conversation (target = spoken file
    # description or resolved path; app = the chat app).
    FILE_SENT = "file_sent"
    FREE_FORM = "free_form"


class SkillStatus(StrEnum):
    COMPLETED = "completed"
    PROGRESS = "progress"
    RETRYABLE_FAILURE = "retryable_failure"
    MISS = "miss"
    NEEDS_CONFIRMATION = "needs_confirmation"
    BLOCKED = "blocked"


class ActionOutcome(StrEnum):
    COMPLETED = "completed"
    PROGRESS = "progress"
    NO_EFFECT = "no_effect"
    UNOBSERVABLE = "unobservable"


class AssistiveDecisionKind(StrEnum):
    OBSERVE = "observe"
    ACTION = "action"
    DONE = "done"
    FAIL = "fail"
    SCREENSHOT = "screenshot"


def _validated_text(value: object, *, label: str, allow_newlines: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    allowed_controls = {"\n", "\t"} if allow_newlines else set()
    if any(ord(character) < 32 and character not in allowed_controls for character in value):
        raise ValueError(f"{label} contains an unsupported control character")
    return value


@dataclass(frozen=True, slots=True)
class Goal:
    """One observable end state for an assistive task.

    ``target`` is deliberately generic: its interpretation is fixed by
    ``kind``. ``app`` optionally binds app-local goals such as a selected
    conversation or draft text without inventing a second goal language.
    """

    kind: GoalKind
    target: str
    app: str | None = None
    # App-specific surface the goal lives in, e.g. Claude Desktop's "code" vs
    # "chat" (Chat and Cowork) mode. None means the app's default.
    mode: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, GoalKind):
            raise ValueError("goal kind must be a GoalKind")
        target = _validated_text(self.target, label="goal target", allow_newlines=True).strip()
        object.__setattr__(self, "target", target)
        if self.app is not None:
            app = _validated_text(self.app, label="goal app").strip()
            object.__setattr__(self, "app", app)
        if self.mode is not None:
            mode = _validated_text(self.mode, label="goal mode").strip().casefold()
            object.__setattr__(self, "mode", mode)

    def to_dict(self) -> dict[str, str]:
        payload = {"kind": self.kind.value, "target": self.target}
        if self.app is not None:
            payload["app"] = self.app
        if self.mode is not None:
            payload["mode"] = self.mode
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Goal:
        if not isinstance(value, dict):
            raise ValueError("goal must be an object")
        unknown = set(value) - {"kind", "target", "app", "mode"}
        if unknown:
            raise ValueError(f"unknown goal fields: {sorted(unknown)}")
        try:
            kind = GoalKind(value["kind"])
            target = value["target"]
        except KeyError as exc:
            raise ValueError(f"missing goal field: {exc.args[0]}") from exc
        return cls(kind=kind, target=target, app=value.get("app"), mode=value.get("mode"))


@dataclass(frozen=True, slots=True)
class TaskSpec:
    goals: tuple[Goal, ...]
    forbid_submit: bool
    side_effect: bool
    raw_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.goals, tuple) or not self.goals:
            raise ValueError("task goals must be a non-empty tuple")
        if any(not isinstance(goal, Goal) for goal in self.goals):
            raise ValueError("task goals must contain only Goal values")
        if not isinstance(self.forbid_submit, bool):
            raise ValueError("forbid_submit must be a boolean")
        if not isinstance(self.side_effect, bool):
            raise ValueError("side_effect must be a boolean")
        _validated_text(self.raw_text, label="task raw_text", allow_newlines=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "goals": [goal.to_dict() for goal in self.goals],
            "forbid_submit": self.forbid_submit,
            "side_effect": self.side_effect,
            "raw_text": self.raw_text,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> TaskSpec:
        if not isinstance(value, dict):
            raise ValueError("task spec must be an object")
        unknown = set(value) - {"goals", "forbid_submit", "side_effect", "raw_text"}
        if unknown:
            raise ValueError(f"unknown task spec fields: {sorted(unknown)}")
        missing = {"goals", "forbid_submit", "side_effect", "raw_text"} - set(value)
        if missing:
            raise ValueError(f"missing task spec fields: {sorted(missing)}")
        goals = value["goals"]
        if not isinstance(goals, list):
            raise ValueError("serialized task goals must be an array")
        return cls(
            goals=tuple(Goal.from_dict(goal) for goal in goals),
            forbid_submit=value["forbid_submit"],
            side_effect=value["side_effect"],
            raw_text=value["raw_text"],
        )


@dataclass(frozen=True, slots=True)
class AssistiveDecision:
    """A planner decision without proof-v1's per-step expectation field."""

    kind: AssistiveDecisionKind
    reason: str
    app: str | None = None
    action: DesktopAction | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, AssistiveDecisionKind):
            raise ValueError("assistive decision kind is invalid")
        _validated_text(self.reason, label="assistive decision reason", allow_newlines=True)
        if self.app is not None:
            _validated_text(self.app, label="assistive decision app")
        if self.action is not None and not isinstance(self.action, DesktopAction):
            raise ValueError("assistive decision action must be a DesktopAction or null")
        if self.kind == AssistiveDecisionKind.ACTION and self.action is None:
            raise ValueError("action decision requires an action")
        if self.kind != AssistiveDecisionKind.ACTION and self.action is not None:
            raise ValueError("only an action decision may carry an action")
        if (
            self.app is not None
            and self.action is not None
            and self.app.casefold() != self.action.app.casefold()
        ):
            raise ValueError("assistive decision app must match its action app")
