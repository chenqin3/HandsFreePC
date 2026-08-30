from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PureWindowsPath

from .config import ExecutionSettings
from .desktop.safety import (
    affirmatively_authorized_action_reference,
    affirmatively_authorized_app_scope,
)
from .models import Action, ActionType, Plan, RiskLevel
from .normalize import compact_text
from .paths import is_disallowed_path_text

# Files outside this deliberately narrow set require confirmation. This is
# safer than trying to enumerate every Windows executable/indirect handler.
# Macro-enabled and legacy active-document formats are intentionally absent.
_SAFE_OPEN_SUFFIXES = {
    ".bmp",
    ".csv",
    ".gif",
    ".jpeg",
    ".jpg",
    ".json",
    ".m4a",
    ".md",
    ".mkv",
    ".mov",
    ".mp3",
    ".mp4",
    ".pdf",
    ".png",
    ".svg",
    ".tsv",
    ".txt",
    ".wav",
    ".webp",
    ".xlsx",
    ".yaml",
    ".yml",
}

_CONFIRM_ACTIONS = {ActionType.START_NATIVE_VOICE}
_CLOUD_PLANNER_SOURCES = {"codex", "claude", "llm"}
_CLOUD_PLANNER_UI_ACTIONS = {
    ActionType.ACTIVATE_APP,
    ActionType.OPEN_CONVERSATION,
    ActionType.OPEN_MODE,
    ActionType.ENTER_DICTATION,
    ActionType.START_NATIVE_VOICE,
}
_DICTATION_AUTHORITY_TERMS = ("听写", "语音输入", "dictation", "voice input")
_NATIVE_VOICE_AUTHORITY_TERMS = (
    "应用内语音",
    "语音输入",
    "麦克风",
    "in-app voice",
    "voice input",
    "microphone",
)


class SafetyPolicy:
    def __init__(self, settings: ExecutionSettings, *, max_actions: int = 8) -> None:
        self.settings = settings
        self.max_actions = max_actions

    def evaluate(
        self, plan: Plan, *, user_text: str = "", explicit_submission: bool = False
    ) -> Plan:
        try:
            plan.validate()
        except ValueError:
            return replace(plan, risk=RiskLevel.BLOCKED, summary="计划字段未通过本地校验")
        # Local checks may raise risk, but never lower a level supplied by an
        # earlier planner or target-resolution pass.
        risk = plan.risk
        if risk == RiskLevel.BLOCKED:
            return plan
        if len(plan.actions) > self.max_actions:
            return replace(plan, risk=RiskLevel.BLOCKED, summary="动作数量超过安全上限")

        compact = compact_text(user_text)
        if any(compact_text(keyword) in compact for keyword in self.settings.blocked_keywords):
            return replace(plan, risk=RiskLevel.BLOCKED, summary="命令包含首版明确禁止的高风险操作")

        if plan.source in _CLOUD_PLANNER_SOURCES:
            if any(action.type not in _CLOUD_PLANNER_UI_ACTIONS for action in plan.actions):
                return replace(
                    plan,
                    risk=RiskLevel.BLOCKED,
                    summary="旧云规划器仅可执行原句精确授权的应用内导航",
                )
            if any(
                not self._cloud_ui_action_is_authorized(action, user_text)
                for action in plan.actions
            ):
                return replace(
                    plan,
                    risk=RiskLevel.BLOCKED,
                    summary="云规划动作未被原句肯定且精确授权",
                )

        native_voice_indices = [
            index
            for index, action in enumerate(plan.actions)
            if action.type == ActionType.START_NATIVE_VOICE
        ]
        if native_voice_indices and (
            len(native_voice_indices) != 1
            or native_voice_indices[0] != len(plan.actions) - 1
            or any(action.type == ActionType.SET_FEEDBACK_MODE for action in plan.actions)
        ):
            return replace(
                plan,
                risk=RiskLevel.BLOCKED,
                summary="应用内语音必须是计划最后且唯一的模式切换动作",
            )

        for action in plan.actions:
            try:
                action.validate()
            except ValueError:
                return replace(plan, risk=RiskLevel.BLOCKED, summary="动作参数未通过校验")
            action_risk = self._action_risk(action, explicit_submission=explicit_submission)
            if action_risk == RiskLevel.BLOCKED:
                return replace(plan, risk=RiskLevel.BLOCKED)
            if action_risk == RiskLevel.CONFIRM and risk == RiskLevel.SAFE:
                risk = RiskLevel.CONFIRM
        return replace(plan, risk=risk)

    @staticmethod
    def _cloud_ui_action_is_authorized(action: Action, user_text: str) -> bool:
        app = (action.app or "").strip()
        if not app or app.casefold() == "current":
            return False
        if not affirmatively_authorized_app_scope(app, user_text):
            return False
        for value in (action.project, action.conversation, action.tab, action.mode):
            if value and not affirmatively_authorized_action_reference(value, user_text):
                return False
        if action.type == ActionType.ENTER_DICTATION:
            return any(
                affirmatively_authorized_action_reference(term, user_text)
                for term in _DICTATION_AUTHORITY_TERMS
            )
        if action.type == ActionType.START_NATIVE_VOICE:
            return any(
                affirmatively_authorized_action_reference(term, user_text)
                for term in _NATIVE_VOICE_AUTHORITY_TERMS
            )
        return True

    @staticmethod
    def _action_risk(action: Action, *, explicit_submission: bool) -> RiskLevel:
        if action.type in _CONFIRM_ACTIONS:
            return RiskLevel.CONFIRM
        if action.type == ActionType.SEND_PROMPT:
            return RiskLevel.SAFE if explicit_submission else RiskLevel.CONFIRM
        if action.type == ActionType.ENTER_DICTATION and (action.app or "").casefold() == "current":
            return RiskLevel.BLOCKED
        if action.type == ActionType.OPEN_PATH and action.path:
            if is_disallowed_path_text(action.path):
                return RiskLevel.BLOCKED
            suffix = PureWindowsPath(action.path).suffix.lower()
            path = Path(action.path)
            if path.exists() and path.is_dir():
                return RiskLevel.SAFE
            if suffix and suffix not in _SAFE_OPEN_SUFFIXES:
                return RiskLevel.CONFIRM
            # Once resolution proves that an extensionless target is a file,
            # treat it as unknown rather than assuming it is a directory.
            if not suffix and path.exists() and path.is_file():
                return RiskLevel.CONFIRM
        return RiskLevel.SAFE
