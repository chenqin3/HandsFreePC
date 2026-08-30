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

_APP_NAMES_BY_CANONICAL = {
    canonical: tuple(alias for alias, value in _APP_ALIASES.items() if value == canonical)
    for canonical in frozenset(_APP_ALIASES.values())
}

_FEEDBACK_FULL_UTTERANCE_PATTERNS = (
    r"(?:切换到|切换成|使用|开启)?(?:屏幕反馈|大字模式|遮罩反馈)",
    r"(?:切换到|切换成|使用|开启)?(?:语音反馈|声音反馈|说话反馈)",
    r"(?:切换到|切换成|使用|开启)?(?:静默模式|安静模式|不要反馈)",
    r"(?:大字和语音)(?:两种)?(?:都开|同时开启)?",
    r"(?:两种反馈|两种都开)",
)

_PATH_UNCONSUMED_OPERATION_MARKERS = (
    "打开",
    "然后",
    "接着",
    "随后",
    "再",
    "同时",
    "并且",
    "最大化",
    "最小化",
    "保存",
    "归档",
    "退出账户",
    "退出登录",
    "登出",
    "注销",
    "创建",
    "新建",
    "点击",
    "滚动",
    "拖动",
    "勾选",
    "填写",
    "搜索",
    "输入",
    "上传",
    "回复",
    "发布",
    "关闭",
    "删除",
    "重命名",
    "复制",
    "移动",
    "发送",
    "提交",
    "安装",
    "maximize",
    "minimize",
    "save",
    "archive",
    "signout",
    "logout",
    "create",
    "click",
    "scroll",
    "drag",
    "check",
    "fill",
    "search",
    "type",
    "upload",
    "reply",
    "publish",
    "close",
    "delete",
    "rename",
    "copy",
    "move",
    "send",
    "submit",
    "install",
)

_APP_ENTITY_OPERATION_MARKERS = tuple(
    marker
    for marker in _PATH_UNCONSUMED_OPERATION_MARKERS
    if marker not in {"打开", "然后", "接着", "随后", "再", "同时", "并且"}
)

_APP_SURFACE_PATTERN = r"(?<![a-z0-9_-])(chat|code|cowork)(?![a-z0-9_-])"


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


def _detect_app_surface(compact: str) -> str | None:
    """Return an explicitly requested application surface.

    The ASCII boundaries are important: without them, ``code`` would match the
    first four letters of ``codex`` and every ordinary Codex activation would
    incorrectly become a request for the Code tab.
    """

    labelled = re.search(
        rf"{_APP_SURFACE_PATTERN}(?:选项卡|标签页|tab)",
        compact,
        re.IGNORECASE,
    )
    if labelled:
        return labelled.group(1).lower()
    navigated = re.search(
        rf"(?:切换到|切换至|进入|打开|到)(?:其中的)?{_APP_SURFACE_PATTERN}",
        compact,
        re.IGNORECASE,
    )
    return navigated.group(1).lower() if navigated else None


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

    def covers_full_text(self, text: str, plan: Plan | None = None) -> bool:
        """Return whether ``plan`` consumes the complete normalized utterance.

        ``parse`` intentionally remains a cheap candidate detector.  Callers
        that execute a deterministic plan must additionally use this anchored,
        fail-closed check so an application or path prefix cannot hide a second
        unsupported request.
        """

        compact = compact_text(text)
        candidate = plan if plan is not None else self.parse(text)
        if not compact or candidate is None:
            return False

        actions = candidate.actions
        action_types = tuple(action.type for action in actions)
        if action_types == (ActionType.SET_FEEDBACK_MODE,):
            return any(
                re.fullmatch(pattern, compact) is not None
                for pattern in _FEEDBACK_FULL_UTTERANCE_PATTERNS
            )
        if action_types == (ActionType.PAUSE,):
            if actions[0].mode == "dictation":
                return compact in {"退出听写", "结束听写", "停止听写"}
            return compact in {"暂停语音操作", "暂停监听", "先暂停"}
        if action_types == (ActionType.RESUME,):
            return compact in {"恢复语音操作", "恢复监听", "继续监听"}
        if action_types == (ActionType.SEND_PROMPT,):
            return compact in {
                "发送提示",
                "发送prompt",
                "提交提示",
                "电脑发送提示",
                "电脑发送prompt",
                "电脑提交提示",
            }
        if not actions and candidate.risk == RiskLevel.BLOCKED:
            return compact in {"开始听写", "进入听写", "打开语音输入", "语音输入"}
        if action_types == (ActionType.OPEN_PATH,):
            return self._covers_full_path_text(text, compact)
        if actions and actions[0].type == ActionType.ACTIVATE_APP:
            return self._covers_full_app_text(compact, actions)
        return False

    @staticmethod
    def _consume_prefix(value: str, pattern: str) -> str | None:
        match = re.match(pattern, value, re.IGNORECASE)
        if match is None:
            return None
        return value[match.end() :]

    @staticmethod
    def _covers_full_path_text(text: str, compact: str) -> bool:
        """Recognize one anchored path-opening utterance and no later clause."""

        direct = re.fullmatch(
            r"\s*(?:打开|进入|查看)\s*[a-z]:[\\/](?P<tail>[^，。；;!?]+?)\s*",
            text,
            re.IGNORECASE,
        )
        if direct is not None:
            tail = compact_text(direct.group("tail"))
            return not any(marker in tail for marker in _PATH_UNCONSUMED_OPERATION_MARKERS)
        spoken = re.fullmatch(
            r"(?:打开|进入|查看)(?:桌面|文档|下载|[a-z]盘)"
            r"(?:上的|下的|里的|中的|上|下|里|的)?(?P<tail>.*)",
            compact,
            re.IGNORECASE,
        )
        if spoken is None:
            return False
        tail = spoken.group("tail")
        return not any(marker in tail for marker in _PATH_UNCONSUMED_OPERATION_MARKERS)

    @classmethod
    def _covers_full_app_text(cls, compact: str, actions: list[Action]) -> bool:
        """Consume an allow-listed app command one planned action at a time."""

        app = actions[0].app
        aliases = _APP_NAMES_BY_CANONICAL.get(app or "")
        if not aliases:
            return False
        app_pattern = "(?:" + "|".join(re.escape(compact_text(alias)) for alias in aliases) + ")"
        remainder = cls._consume_prefix(
            compact,
            r"^(?:打开|启动|进入|切换到|切换至|切换)"
            r"(?:桌面上的?|桌面里(?:的)?|桌面)?" + app_pattern + r"(?:app|应用)?",
        )
        if remainder is None:
            return False

        for action in actions[1:]:
            if action.app != app:
                return False
            if action.type == ActionType.OPEN_CONVERSATION:
                if not action.project or not action.conversation:
                    return False
                if any(
                    marker in compact_text(value)
                    for value in (action.project, action.conversation)
                    for marker in _APP_ENTITY_OPERATION_MARKERS
                ):
                    return False
                pattern = (
                    r"^(?:并|然后)?(?:打开|进入)?(?:其中的)?"
                    + re.escape(compact_text(action.project))
                    + r"项目(?:下|里|中的|下面的)?(?:的)?"
                    + re.escape(compact_text(action.conversation))
                    + r"(?:对话|任务)"
                )
            elif action.type == ActionType.OPEN_MODE:
                mode = compact_text(action.mode or "")
                if not mode:
                    return False
                if any(marker in mode for marker in _APP_ENTITY_OPERATION_MARKERS):
                    return False
                if action.tab:
                    pattern = (
                        r"^(?:并|然后)?(?:到|至|进入|打开|切换到|切换至)(?:其中的)?"
                        + re.escape(compact_text(action.tab))
                        + r"(?:选项卡|标签页|tab)(?:里面|中)?"
                        r"(?:并|然后)?(?:开启|创建|打开)(?:一个)?" + re.escape(mode)
                    )
                elif mode in {"chat", "code", "cowork"}:
                    pattern = (
                        r"^(?:并|然后)?(?:的|到|至|进入|打开|切换到|切换至)?(?:其中的)?"
                        + re.escape(mode)
                        + r"(?:选项卡|标签页|tab)(?:里面|中)?"
                    )
                else:
                    pattern = r"^(?:并|然后)?(?:开启|创建|打开)(?:一个)?" + re.escape(mode)
            elif action.type == ActionType.ENTER_DICTATION:
                pattern = (
                    r"^(?:并|然后)?(?:(?:接下来)?我会)?"
                    r"(?:打开|开启|开始|使用)?(?:语音输入|听写)"
                )
            elif action.type == ActionType.START_NATIVE_VOICE:
                pattern = r"^(?:并|然后)?(?:使用|打开|开启|开始)?(?:应用内语音|原生语音)"
            else:
                return False
            consumed = cls._consume_prefix(remainder, pattern)
            if consumed is None:
                return False
            remainder = consumed
        return remainder == ""

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

        requested_surface = _detect_app_surface(compact)
        if app == "claude":
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
                            tab=requested_surface,
                            mode=mode,
                        )
                    )
            elif requested_surface:
                actions.append(Action(ActionType.OPEN_MODE, app=app, mode=requested_surface))
        elif requested_surface:
            actions.append(Action(ActionType.OPEN_MODE, app=app, mode=requested_surface))

        native_voice = any(token in compact for token in ("应用内语音", "原生语音"))
        wants_voice = any(token in compact for token in ("语音输入", "开始听写", "开启语音"))
        if native_voice:
            actions.append(Action(ActionType.START_NATIVE_VOICE, app=app))
        elif wants_voice:
            actions.append(Action(ActionType.ENTER_DICTATION, app=app))

        target = conversation or project or app
        return Plan(f"打开 {app} 中的 {target}", actions)
