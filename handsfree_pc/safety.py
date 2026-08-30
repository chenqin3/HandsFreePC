from __future__ import annotations

from dataclasses import replace
from pathlib import Path, PureWindowsPath

from .config import ExecutionSettings
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
_PLANNER_FORBIDDEN_ACTIONS = {ActionType.TYPE_TEXT, ActionType.SEND_PROMPT}


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

        if plan.source in _CLOUD_PLANNER_SOURCES and any(
            action.type in _PLANNER_FORBIDDEN_ACTIONS for action in plan.actions
        ):
            return replace(
                plan,
                risk=RiskLevel.BLOCKED,
                summary="云规划器不得生成或提交输入文本",
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
