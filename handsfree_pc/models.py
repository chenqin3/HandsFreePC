from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class ActionType(StrEnum):
    OPEN_PATH = "open_path"
    ACTIVATE_APP = "activate_app"
    OPEN_CONVERSATION = "open_conversation"
    OPEN_MODE = "open_mode"
    ENTER_DICTATION = "enter_dictation"
    START_NATIVE_VOICE = "start_native_voice"
    SET_FEEDBACK_MODE = "set_feedback_mode"
    TYPE_TEXT = "type_text"
    SEND_PROMPT = "send_prompt"
    PAUSE = "pause"
    RESUME = "resume"
    WAIT = "wait"


class RiskLevel(StrEnum):
    SAFE = "safe"
    CONFIRM = "confirm"
    BLOCKED = "blocked"


class FeedbackMode(StrEnum):
    OVERLAY = "overlay"
    VOICE = "voice"
    BOTH = "both"
    SILENT = "silent"


class RuntimeState(StrEnum):
    ARMED = "armed"
    AWAKE = "awake"
    EXECUTING = "executing"
    DICTATION = "dictation"
    CONFIRMING = "confirming"
    PAUSED = "paused"
    STOPPED = "stopped"


@dataclass(slots=True)
class Action:
    type: ActionType
    path: str | None = None
    app: str | None = None
    project: str | None = None
    conversation: str | None = None
    tab: str | None = None
    mode: str | None = None
    text: str | None = None
    feedback_mode: FeedbackMode | None = None
    seconds: float | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Action:
        if not isinstance(value, dict):
            raise ValueError("Each action must be an object")
        allowed = {
            "type",
            "path",
            "app",
            "project",
            "conversation",
            "tab",
            "mode",
            "text",
            "feedback_mode",
            "seconds",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"Unknown action fields: {sorted(unknown)}")
        action_type = ActionType(value["type"])
        feedback = value.get("feedback_mode")
        action = cls(
            type=action_type,
            path=value.get("path"),
            app=value.get("app"),
            project=value.get("project"),
            conversation=value.get("conversation"),
            tab=value.get("tab"),
            mode=value.get("mode"),
            text=value.get("text"),
            feedback_mode=FeedbackMode(feedback) if feedback else None,
            seconds=value.get("seconds"),
        )
        action.validate()
        return action

    def validate(self) -> None:
        if not isinstance(self.type, ActionType):
            raise ValueError("Action type must be an ActionType")
        if self.feedback_mode is not None and not isinstance(self.feedback_mode, FeedbackMode):
            raise ValueError("feedback_mode must be a FeedbackMode or null")
        if self.seconds is not None and (
            isinstance(self.seconds, bool) or not isinstance(self.seconds, int | float)
        ):
            raise ValueError("seconds must be a number or null")
        requirements: dict[ActionType, tuple[str, ...]] = {
            ActionType.OPEN_PATH: ("path",),
            ActionType.ACTIVATE_APP: ("app",),
            ActionType.OPEN_CONVERSATION: ("app", "conversation"),
            ActionType.OPEN_MODE: ("app", "mode"),
            ActionType.ENTER_DICTATION: ("app",),
            ActionType.START_NATIVE_VOICE: ("app",),
            ActionType.SET_FEEDBACK_MODE: ("feedback_mode",),
            ActionType.TYPE_TEXT: ("text",),
        }
        for field_name in requirements.get(self.type, ()):
            if getattr(self, field_name) in (None, ""):
                raise ValueError(f"Action {self.type} requires {field_name}")
        if self.type == ActionType.WAIT and (self.seconds is None or not 0 <= self.seconds <= 10):
            raise ValueError("wait seconds must be between 0 and 10")
        for value in (
            self.path,
            self.app,
            self.project,
            self.conversation,
            self.tab,
            self.mode,
            self.text,
        ):
            if value is not None and not isinstance(value, str):
                raise ValueError("Action text fields must be strings or null")
            if isinstance(value, str) and "\x00" in value:
                raise ValueError("NUL characters are not allowed")
            if isinstance(value, str) and any(
                unicodedata.category(char).startswith("C") for char in value
            ):
                raise ValueError("Action text fields cannot contain control characters")
        if self.text is not None and len(self.text) > 2000:
            raise ValueError("Action text exceeds 2000 characters")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.type.value}
        for name in (
            "path",
            "app",
            "project",
            "conversation",
            "tab",
            "mode",
            "text",
            "seconds",
        ):
            value = getattr(self, name)
            if value is not None:
                result[name] = value
        if self.feedback_mode is not None:
            result["feedback_mode"] = self.feedback_mode.value
        return result


@dataclass(slots=True)
class Plan:
    summary: str
    actions: list[Action] = field(default_factory=list)
    risk: RiskLevel = RiskLevel.SAFE
    source: str = "deterministic"

    def validate(self) -> None:
        if not isinstance(self.summary, str):
            raise ValueError("Plan summary must be a string")
        if len(self.summary) > 200:
            raise ValueError("Plan summary exceeds 200 characters")
        if any(unicodedata.category(char).startswith("C") for char in self.summary):
            raise ValueError("Plan summary cannot contain control characters")
        if not isinstance(self.source, str):
            raise ValueError("Plan source must be a string")
        if not isinstance(self.risk, RiskLevel):
            raise ValueError("Plan risk must be a RiskLevel")
        if not isinstance(self.actions, list):
            raise ValueError("Plan actions must be a list")
        if len(self.actions) > 8:
            raise ValueError("Plan exceeds the 8 action limit")
        if not self.actions and self.risk != RiskLevel.BLOCKED:
            raise ValueError("A safe or confirm plan must contain at least one action")
        for action in self.actions:
            if not isinstance(action, Action):
                raise ValueError("Plan actions must contain Action objects")
            action.validate()

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, source: str = "llm") -> Plan:
        if not isinstance(value, dict):
            raise ValueError("Plan must be an object")
        missing = {"summary", "actions", "risk"} - set(value)
        if missing:
            raise ValueError(f"Missing required plan fields: {sorted(missing)}")
        unknown = set(value) - {"summary", "actions", "risk"}
        if unknown:
            raise ValueError(f"Unknown plan fields: {sorted(unknown)}")
        summary = value.get("summary", "")
        if not isinstance(summary, str):
            raise ValueError("Plan summary must be a string")
        if len(summary) > 200:
            raise ValueError("Plan summary exceeds 200 characters")
        if any(unicodedata.category(char).startswith("C") for char in summary):
            raise ValueError("Plan summary cannot contain control characters")
        raw_actions = value.get("actions", [])
        if not isinstance(raw_actions, list):
            raise ValueError("Plan actions must be an array")
        if len(raw_actions) > 8:
            raise ValueError("Plan exceeds the 8 action limit")
        risk = RiskLevel(value.get("risk", RiskLevel.SAFE))
        if not raw_actions and risk != RiskLevel.BLOCKED:
            raise ValueError("A safe or confirm plan must contain at least one action")
        actions = [Action.from_dict(item) for item in raw_actions]
        plan = cls(summary=summary, actions=actions, risk=risk, source=source)
        plan.validate()
        return plan

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "actions": [action.to_dict() for action in self.actions],
            "risk": self.risk.value,
        }


def clone_plan(plan: Plan) -> Plan:
    """Return a validated canonical deep copy with no shared ``Action`` objects."""

    plan.validate()
    return Plan.from_dict(plan.to_dict(), source=plan.source)


@dataclass(slots=True)
class ExecutionResult:
    success: bool
    message: str
    action: Action | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
