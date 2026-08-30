from __future__ import annotations

import re
from pathlib import PureWindowsPath

from .models import Action, ActionType, FeedbackMode, Plan, RiskLevel
from .normalize import compact_text, normalize_text

_APP_ALIASES = {
    "codex": "codex",
    "科德克斯": "codex",
    "代码助手": "codex",
    "claude": "claude",
    "克劳德": "claude",
}


def _detect_app(text: str) -> str | None:
    lower = normalize_text(text)
    compact = compact_text(text)
    for alias, canonical in _APP_ALIASES.items():
        if alias in lower or compact_text(alias) in compact:
            return canonical
    return None


def _clean_entity(value: str) -> str:
    result = compact_text(value)
    for prefix in ("打开其中的", "打开", "进入", "切换到", "其中的", "一个"):
        if result.startswith(prefix):
            result = result[len(prefix) :]
    return result.strip("的")


def _normalize_spoken_path_tail(value: str) -> str:
    tail = re.sub(r"^(?:打开|进入)", "", value)
    tail = re.sub(r"文件夹(?:里|下|中的|的)", r"\\", tail)
    tail = re.sub(r"(?:里面|下面|其中)(?:的)?", r"\\", tail)
    tail = re.sub(r"(?:然后)?打开", r"\\", tail)
    tail = re.sub(r"\\的", r"\\", tail)
    return re.sub(r"[\\/]+", r"\\", tail).strip("\\")


def _spoken_path(text: str) -> str | None:
    direct = re.search(r"(?i)([a-z]):[\\/]([^，。；;!?]*)", text)
    if direct:
        return str(PureWindowsPath(f"{direct.group(1).upper()}:\\{direct.group(2).strip()}"))

    compact = compact_text(text)
    alias_match = re.search(
        r"(?:打开|进入|查看)?(桌面|文档|下载)(?:上的|下的|里的|中的|上|下|里|的)?(.+)?",
        compact,
    )
    if alias_match:
        alias = alias_match.group(1)
        tail = _normalize_spoken_path_tail(alias_match.group(2) or "")
        return alias if not tail else f"{alias}\\{tail}"

    drive_match = re.search(r"(?i)(?:打开|进入|查看)?([a-z])盘(?:的)?(.+)?", compact)
    if not drive_match:
        return None
    drive = drive_match.group(1).upper()
    tail = _normalize_spoken_path_tail(drive_match.group(2) or "")
    return f"{drive}:\\{tail}" if tail else f"{drive}:\\"


class DeterministicIntentParser:
    """Parse high-confidence commands; ambiguous language is left to a planner."""

    def parse(self, text: str) -> Plan | None:
        compact = compact_text(text)
        if not compact:
            return None

        feedback = self._parse_feedback(compact)
        if feedback:
            return feedback

        if any(token in compact for token in ("暂停语音操作", "暂停监听", "先暂停")):
            return Plan("暂停语音控制", [Action(ActionType.PAUSE)])
        if any(token in compact for token in ("恢复语音操作", "恢复监听", "继续监听")):
            return Plan("恢复语音控制", [Action(ActionType.RESUME)])

        if any(token in compact for token in ("退出听写", "结束听写", "停止听写")):
            return Plan("退出听写", [Action(ActionType.PAUSE, mode="dictation")])
        # Submission carries consent. It must be the entire control command,
        # never a substring of prose or a negation such as "不要发送提示".
        send_command = compact
        if send_command.startswith("电脑"):
            send_command = send_command[len("电脑") :]
        if send_command in {"发送提示", "发送prompt", "提交提示"}:
            return Plan("发送当前提示", [Action(ActionType.SEND_PROMPT)])

        if compact in {"开始听写", "进入听写", "打开语音输入", "语音输入"}:
            return Plan(
                "请在口令中明确指定已配置的 Codex 或 Claude 应用",
                [],
                risk=RiskLevel.BLOCKED,
            )

        app_plan = self._parse_app(text)
        if app_plan:
            return app_plan

        path = _spoken_path(text)
        if path and any(token in compact for token in ("打开", "进入", "查看")):
            return Plan(f"打开 {path}", [Action(ActionType.OPEN_PATH, path=path)])

        return None

    @staticmethod
    def _parse_feedback(compact: str) -> Plan | None:
        if any(token in compact for token in ("静默模式", "安静模式", "不要反馈")):
            mode = FeedbackMode.SILENT
        elif any(token in compact for token in ("两种反馈", "两种都开", "大字和语音")):
            mode = FeedbackMode.BOTH
        elif any(token in compact for token in ("语音反馈", "声音反馈", "说话反馈")):
            mode = FeedbackMode.VOICE
        elif any(token in compact for token in ("屏幕反馈", "大字模式", "遮罩反馈")):
            mode = FeedbackMode.OVERLAY
        else:
            return None
        return Plan(
            f"切换到 {mode.value} 反馈",
            [Action(ActionType.SET_FEEDBACK_MODE, feedback_mode=mode)],
        )

    def _parse_app(self, text: str) -> Plan | None:
        app = _detect_app(text)
        if app is None:
            return None
        compact = compact_text(text)
        actions = [Action(ActionType.ACTIVATE_APP, app=app)]

        project: str | None = None
        conversation: str | None = None
        match = re.search(
            r"(?:打开|进入)?(?:其中的)?(.+?)项目(?:下|里|中的|下面的)?(?:的)?(.+?)(?:对话|任务)",
            compact,
        )
        if match:
            project = _clean_entity(match.group(1))
            project = re.sub(r"^.*?(?:codex|科德克斯|claude|克劳德)(?:app)?", "", project)
            project = re.sub(r"^(?:app)?(?:打开其中的|打开|进入|其中的)", "", project)
            conversation = _clean_entity(match.group(2))
            actions.append(
                Action(
                    ActionType.OPEN_CONVERSATION,
                    app=app,
                    project=project or None,
                    conversation=conversation,
                )
            )

        if app == "claude":
            tab_match = re.search(r"(chat|code)(?:选项卡|标签页|tab)?", compact, re.IGNORECASE)
            mode_match = re.search(r"(?:开启|创建)(?:一个)?([a-zA-Z][a-zA-Z0-9_-]*)", compact)
            if mode_match is None:
                mode_match = re.search(r"打开一个([a-zA-Z][a-zA-Z0-9_-]*)", compact)
            if mode_match:
                mode = mode_match.group(1).lower()
                if mode not in {"claude", "app", "chat", "code"}:
                    actions.append(
                        Action(
                            ActionType.OPEN_MODE,
                            app=app,
                            tab=tab_match.group(1).lower() if tab_match else None,
                            mode=mode,
                        )
                    )

        native_voice = any(token in compact for token in ("应用内语音", "原生语音"))
        wants_voice = any(token in compact for token in ("语音输入", "开始听写", "开启语音"))
        if native_voice:
            actions.append(Action(ActionType.START_NATIVE_VOICE, app=app))
        elif wants_voice:
            actions.append(Action(ActionType.ENTER_DICTATION, app=app))

        target = conversation or project or app
        return Plan(f"打开 {app} 中的 {target}", actions)
