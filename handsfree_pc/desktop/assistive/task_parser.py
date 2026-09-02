from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from pathlib import PureWindowsPath

from .models import Goal, GoalKind, TaskSpec

_APP_ALIASES = {
    "claude": "claude",
    "克劳德": "claude",
    "cloud": "claude",
    "codex": "codex",
    "科德克斯": "codex",
    "chatgpt": "codex",
    "chat gpt": "codex",
    "chrome": "chrome",
    "谷歌浏览器": "chrome",
    "浏览器": "chrome",
    "微信": "wechat",
    "wechat": "wechat",
    "explorer": "explorer",
    "资源管理器": "explorer",
    "文件资源管理器": "explorer",
}
_DEFAULT_PATH_ALIASES = {"桌面": "桌面", "文档": "文档", "下载": "下载"}
_CHAT_APPS = frozenset({"wechat", "claude", "codex"})
_WEBPAGE_ALIASES = {
    "google": "google.com",
    "谷歌": "google.com",
    "chatgpt": "chatgpt.com",
    "chat gpt": "chatgpt.com",
    "gpt": "chatgpt.com",
    "openai": "chatgpt.com",
    "claude": "claude.ai",
    "gemini": "gemini.google.com",
    "deepseek": "chat.deepseek.com",
    "kimi": "kimi.com",
    "百度": "baidu.com",
    "知乎": "zhihu.com",
    "bilibili": "bilibili.com",
    "b站": "bilibili.com",
    "youtube": "youtube.com",
    "github": "github.com",
    "微博": "weibo.com",
}
_FORBID_SUBMIT_PHRASES = ("不要发送", "别发送", "先不发", "不发送")
# "再"/"并" only separate clauses when a verb follows; inside a name such as
# "并购数据" they are ordinary characters.
_CLAUSE_SPLIT_RE = re.compile(
    r"\s*(?:然后|接着|随后|并且|(?:再|并)"
    r"(?=\s*(?:打开|进入|输入|键入|点击|发送|搜索|查找|找到|切换|问|开|新建|创建)))\s*"
)
# Only an explicitly *new* conversation is implied by loading a chat site's
# front page; "打开聊天" is a real follow-up clause and must survive.
_NEW_CHAT_RE = re.compile(
    r"^(?:(?:新建|创建)\s*(?:一个|个)?\s*(?:新的?)?|(?:开|开始|打开|建)?\s*(?:一个|个)?\s*新的?\s*)"
    r"(?:对话|聊天|会话)$|^new\s*chat$",
    re.IGNORECASE,
)
_FORBID_TAIL_RE = re.compile(
    r"[\s,，。;；!！(（]*(?:但是|但|然后|记得|注意|先|请)?\s*"
    r"(?:不要发送|别发送|先不发|不发送)"
    r"(?:了|出去|这条|消息|内容|它|哦|啊)?[\s,，。;；!！)）]*$"
)
_SEND_FILE_RE = re.compile(
    r"^(?:请)?(?:帮我|给我)?(?:把|将)\s*(?P<file>.+?)\s*(?:发送|发|传|分享)(?:到|给|至)\s*"
    r"(?P<target>.+)$"
)
_SEND_TARGET_RE = re.compile(
    r"^(?P<app>微信|wechat|weixin|claude|codex|chatgpt)\s*(?:的|里的|里面的|里|中的|中)?\s*"
    r"(?P<conversation>.*)$",
    re.IGNORECASE,
)
_SIDE_EFFECT_MARKERS = (
    "发送",
    "提交",
    "删除",
    "覆盖",
    "安装",
    "卸载",
    "上传",
    "分享",
    "支付",
    "转账",
)
_LEADING_NEGATION_RE = re.compile(r"^(?:请)?(?:不要|别|不用|不需要|无需)")
_INPUT_VERB_RE = re.compile(r"(?:输入|键入|问一下|问一句|问他|问它|提问|询问)(?!框|法)")
_INPUT_TAIL_ACTION_RE = re.compile(
    r"(?:[,，。;；]|\s)+(?:然后|接着|随后|再|并且|并|同时)\s*"
    r"(?:请|帮我|给我)?\s*(?:发送|提交|点击|按下|打开|关闭|删除|保存|上传|分享)"
)
# "去" is an activation verb only when an app name follows ("去chrome…");
# "去年的报表" must not lose its first character.
_GO_TO_APP = (
    r"去(?=\s*(?:"
    + "|".join(re.escape(alias) for alias in sorted(_APP_ALIASES, key=len, reverse=True))
    + r"))"
)
_COMPOUND_RE = re.compile(
    r"^(?:请)?(?:切换到|切到|激活|" + _GO_TO_APP + r")\s*(?P<app>.+?)\s*(?:然后\s*)?"
    r"(?P<verb>打开|进入)\s*(?P<target>.+)$",
    re.IGNORECASE,
)
_CONVERSATION_RE = re.compile(
    r"^(?:请)?在\s*(?P<app>.+?)\s*(?:里|中|内|里面|应用里|app里)\s*"
    r"(?:找到|找一?下?|打开|搜索|搜一?下?|切换到|切到|进入)\s*"
    r"(?:联系人|会话|对话|聊天|好友)?\s*(?:和|跟|与)?\s*(?P<conversation>.+)$",
    re.IGNORECASE,
)
# Chat-app + contact without the "在…里" frame: "打开微信 sensa",
# "微信联系人 sensa", "切换到微信 找 sensa", "微信 sensa".
_CHAT_APP_ALIASES = tuple(
    sorted(
        (alias for alias, family in _APP_ALIASES.items() if family in _CHAT_APPS),
        key=len,
        reverse=True,
    )
)
_CHAT_APP_TARGET_RE = re.compile(
    r"^(?:请)?(?:帮我|给我)?\s*(?:打开|切换到|切到|切换|进入|去)?\s*"
    r"(?P<app>" + "|".join(re.escape(alias) for alias in _CHAT_APP_ALIASES) + r")"
    r"\s*(?:里|中|内|里面|的|应用里|app里)?\s*"
    r"(?:联系人|会话|对话|聊天|好友)?\s*"
    r"(?:找到|找一?下?|打开|搜索|搜一?下?|切换到|切到|进入)?\s*"
    r"(?:和|跟|与)?\s*(?P<conversation>\S.*)$",
    re.IGNORECASE,
)
_APP_RE = re.compile(
    r"^(?:请)?(?:切换到|切到|激活|打开|启动|" + _GO_TO_APP + r")\s*(?P<app>.+)$",
    re.IGNORECASE,
)
_DIRECT_PATH_RE = re.compile(
    r"^(?:请)?(?:打开|进入|查看)\s*(?:路径\s*)?"
    r"(?P<path>(?:[a-zA-Z]:[\\/].*|\\\\[^\\/]+[\\/][^\\/]+.*))$",
    re.IGNORECASE,
)
_DRIVE_RE = re.compile(
    r"^(?:请)?(?:打开|进入|查看)\s*(?P<drive>[a-zA-Z])\s*盘$",
    re.IGNORECASE,
)


def _nfkc(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


# Claude Desktop keeps two conversation surfaces. Spoken qualifiers select one;
# an unqualified request means the Code surface (the user's default).
_CLAUDE_CHAT_MODE_RE = re.compile(
    r"(?:的)?\s*(?:chat\s*(?:and|&)\s*cowork|chatandcowork|cowork"
    r"|聊天(?:和|与)?协作|聊天模式|协作模式|对话模式)\s*(?:模式)?",
    re.IGNORECASE,
)
_CLAUDE_CODE_MODE_RE = re.compile(
    # "code" only counts as a surface name next to the app or a locative
    # ("Claude Code 里", "code 模式"), never inside a conversation title.
    r"(?:的)?\s*(?:(?<![a-z])code(?![a-z])|代码模式|编程模式|coding)\s*(?:模式)?"
    r"(?=\s*(?:里|中|下|的|模式|$))",
    re.IGNORECASE,
)


def _extract_claude_mode(text: str) -> tuple[str | None, str]:
    normalized = _nfkc(text)
    for mode, pattern in (("chat", _CLAUDE_CHAT_MODE_RE), ("code", _CLAUDE_CODE_MODE_RE)):
        match = pattern.search(normalized)
        if match is None:
            continue
        # "在 chat and cowork 里…" names the surface instead of the app.
        replacement = " " if "claude" in _key(normalized) else " Claude "
        cleaned = normalized[: match.start()] + replacement + normalized[match.end() :]
        return mode, re.sub(r"\s+", " ", cleaned).strip()
    return None, normalized


def _with_claude_mode(goal: Goal, mode: str) -> Goal:
    if goal.kind != GoalKind.CONVERSATION_SELECTED or goal.app not in (None, "claude"):
        return goal
    return Goal(goal.kind, goal.target, app=goal.app, mode=mode)


def _surface(value: str) -> str:
    normalized = _nfkc(value).strip()
    # Spoken punctuation is a clause separator, but punctuation inside an
    # explicit URL is data. Stash complete HTTP(S) tokens before normalizing so
    # query strings and fragments survive the one-pass task parse byte-for-byte.
    urls: list[str] = []

    def stash_url(match: re.Match[str]) -> str:
        urls.append(match.group(0))
        return f"\ue000{len(urls) - 1}\ue001"

    normalized = re.sub(
        r"https?://[^\s，。！？；、]+",
        stash_url,
        normalized,
        flags=re.IGNORECASE,
    )
    # Keep the ASCII colon because it is structural in drive paths and URLs.
    normalized = re.sub(r"[，。！？；、,：;!?]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    for index, url in enumerate(urls):
        normalized = normalized.replace(f"\ue000{index}\ue001", url)
    return normalized


def _key(value: str) -> str:
    return re.sub(r"\s+", "", _nfkc(value)).casefold()


def _clean_app(value: str) -> str:
    candidate = _nfkc(value).strip(" \t,，。;；")
    candidate = re.sub(r"^(?:桌面上的?|桌面里(?:的)?|桌面)", "", candidate)
    candidate = re.sub(r"(?:app|应用|窗口)$", "", candidate, flags=re.IGNORECASE).strip()
    return _APP_ALIASES.get(_key(candidate), candidate)


def _clean_conversation(value: str) -> str:
    candidate = _nfkc(value).strip(" \t,，。;；")
    candidate = re.sub(r"^(?:和|跟|与)\s*", "", candidate)
    candidate = re.sub(r"\s*的?(?:会话|对话|聊天|好友)$", "", candidate).strip()
    return candidate


def _looks_negated(surface: str) -> bool:
    return (
        _LEADING_NEGATION_RE.search(surface) is not None
        or re.search(
            r"(?:然后|并且|但|但是)\s*(?:不要|别|不用)\s*(?:打开|进入|切换|激活|找到)",
            surface,
        )
        is not None
    )


def _has_side_effect(raw_text: str, *, forbid_submit: bool) -> bool:
    compact = _key(raw_text)
    if forbid_submit:
        for phrase in _FORBID_SUBMIT_PHRASES:
            compact = compact.replace(_key(phrase), "")
    return any(marker in compact for marker in _SIDE_EFFECT_MARKERS)


class DeterministicTaskParser:
    """Parse the bounded PR1 command families into one immutable TaskSpec.

    Parsing is a single ordered pass over the complete utterance. Unknown or
    only partially recognized commands are kept intact as one ``free_form``
    goal so the assistive agent, rather than a prefix parser, handles them.
    """

    def __init__(self, *, path_aliases: Mapping[str, str] | None = None) -> None:
        aliases = dict(_DEFAULT_PATH_ALIASES)
        if path_aliases is not None:
            for spoken, resolved in path_aliases.items():
                if not isinstance(spoken, str) or not spoken.strip():
                    raise ValueError("path alias names must be non-empty strings")
                if not isinstance(resolved, str) or not resolved.strip():
                    raise ValueError("path alias targets must be non-empty strings")
                aliases[spoken] = resolved
        self._path_aliases = {_key(spoken): resolved for spoken, resolved in aliases.items()}

    def parse(self, text: str) -> TaskSpec:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("task text must be a non-empty string")
        raw_text = text
        mode, text = _extract_claude_mode(text)
        surface = _surface(text)

        parsed = self._parse_goals(text, surface, raw_text=raw_text)
        if mode is None:
            return parsed
        return TaskSpec(
            goals=tuple(_with_claude_mode(goal, mode) for goal in parsed.goals),
            forbid_submit=parsed.forbid_submit,
            side_effect=parsed.side_effect,
            raw_text=parsed.raw_text,
        )

    def _parse_goals(self, text: str, surface: str, *, raw_text: str) -> TaskSpec:
        sent = self._parse_send_file(surface, raw_text=raw_text)
        if sent is not None:
            return sent

        parsed_input = self._parse_input(text, raw_text=raw_text)
        if parsed_input is not None:
            return parsed_input

        if not _looks_negated(surface):
            conversation = self._parse_conversation(surface, raw_text=raw_text)
            if conversation is not None:
                return conversation

            compound = self._parse_compound(surface, raw_text=raw_text)
            if compound is not None:
                return compound

            chat_target = self._parse_chat_app_target(surface, raw_text=raw_text)
            if chat_target is not None:
                return chat_target

            path_goal = self._parse_path(surface)
            if path_goal is not None:
                return self._task(raw_text, (path_goal,))

            app_goal = self._parse_app(surface)
            if app_goal is not None:
                return self._task(raw_text, (app_goal,))

        forbid_submit = any(_key(phrase) in _key(text) for phrase in _FORBID_SUBMIT_PHRASES)
        return TaskSpec(
            goals=(Goal(GoalKind.FREE_FORM, raw_text),),
            forbid_submit=forbid_submit,
            side_effect=_has_side_effect(text, forbid_submit=forbid_submit),
            raw_text=raw_text,
        )

    @staticmethod
    def _task(raw_text: str, goals: tuple[Goal, ...]) -> TaskSpec:
        return TaskSpec(
            goals=goals,
            forbid_submit=False,
            side_effect=False,
            raw_text=raw_text,
        )

    def _parse_path(self, surface: str) -> Goal | None:
        direct = _DIRECT_PATH_RE.fullmatch(surface)
        if direct is not None:
            return Goal(GoalKind.PATH_OPEN, str(PureWindowsPath(direct.group("path"))))

        drive = _DRIVE_RE.fullmatch(surface)
        if drive is not None:
            return Goal(GoalKind.PATH_OPEN, f"{drive.group('drive').upper()}:\\")

        alias = re.fullmatch(r"(?:请)?(?:打开|进入|查看)\s*(?P<alias>.+)", surface)
        if alias is None:
            return None
        target = self._path_aliases.get(_key(alias.group("alias")))
        return Goal(GoalKind.PATH_OPEN, target) if target is not None else None

    @staticmethod
    def _parse_app(surface: str) -> Goal | None:
        if _key(surface) in {"打开资源管理器", "打开文件资源管理器", "打开explorer"}:
            return Goal(GoalKind.APP_FOREGROUND, "explorer")
        match = _APP_RE.fullmatch(surface)
        if match is None:
            return None
        candidate = match.group("app").strip()
        if not candidate or re.search(
            r"(?:打开|进入|找到|输入|键入|发送|提交|搜索|查找|然后|接着)", candidate
        ):
            # A second operation after the app name is not an app name; the
            # whole utterance stays intact for the planner.
            return None
        conversation = re.fullmatch(
            r"(?P<name>.+?)(?:的)?(?:聊天框|聊天窗口|会话|对话|聊天)", candidate
        )
        if conversation is not None:
            name = conversation.group("name").strip()
            app = _clean_app(name)
            if _key(app) in set(_APP_ALIASES.values()):
                # "切换到微信聊天" still names the app itself.
                return Goal(GoalKind.APP_FOREGROUND, app)
            if name:
                # "切换到周报生成聊天框" names a conversation inside whichever
                # chat app is in front; the controller resolves the app.
                return Goal(GoalKind.CONVERSATION_SELECTED, name)
        app = _clean_app(candidate)
        return Goal(GoalKind.APP_FOREGROUND, app) if app else None

    def _parse_send_file(self, surface: str, *, raw_text: str) -> TaskSpec | None:
        """"把<文件描述>发送到微信的<会话>" → open the chat, then hand it the file."""

        match = _SEND_FILE_RE.fullmatch(surface)
        if match is None:
            return None
        file_text = match.group("file").strip()
        target_text = match.group("target").strip()
        forbid_submit = any(_key(phrase) in _key(target_text) for phrase in _FORBID_SUBMIT_PHRASES)
        if forbid_submit:
            # "…发到微信文件传输助手但是不要发送": attach only, and the
            # negation is not part of the conversation name.
            target_text = _FORBID_TAIL_RE.sub("", target_text).strip()
        app: str | None = None
        conversation = target_text
        target = _SEND_TARGET_RE.fullmatch(target_text)
        if target is not None:
            app = _clean_app(target.group("app")) or None
            conversation = target.group("conversation")
        conversation = _clean_conversation(conversation)
        if not file_text or not conversation:
            return None
        if app is None and _key(conversation) == _key("文件传输助手"):
            # The file-transfer assistant exists only inside WeChat.
            app = "wechat"
        goals: list[Goal] = []
        if app:
            goals.append(Goal(GoalKind.APP_FOREGROUND, app))
        goals.append(Goal(GoalKind.CONVERSATION_SELECTED, conversation, app=app))
        goals.append(Goal(GoalKind.FILE_SENT, file_text, app=app))
        return TaskSpec(
            goals=tuple(goals),
            forbid_submit=forbid_submit,
            side_effect=not forbid_submit,
            raw_text=raw_text,
        )

    def _parse_compound(self, surface: str, *, raw_text: str) -> TaskSpec | None:
        match = _COMPOUND_RE.fullmatch(surface)
        if match is None:
            return None
        app = _clean_app(match.group("app"))
        target = match.group("target").strip()
        if not app or not target:
            return None
        first, *rest = [clause for clause in _CLAUSE_SPLIT_RE.split(target) if clause.strip()]
        goals: list[Goal] = [
            Goal(GoalKind.APP_FOREGROUND, app),
            self._goal_for_open_target(first.strip(), app=app),
        ]
        for clause in rest:
            clause = clause.strip()
            if _NEW_CHAT_RE.fullmatch(clause) and goals[-1].kind == GoalKind.URL_LOADED:
                # Loading a chat site's front page already starts a fresh
                # conversation; nothing else needs to happen for this clause.
                continue
            goals.append(Goal(GoalKind.FREE_FORM, clause, app=app))
        return self._task(raw_text, tuple(goals))

    def _goal_for_open_target(self, target: str, *, app: str) -> Goal:
        path = self._parse_path(f"打开 {target}")
        if path is not None:
            return path

        conversation = re.fullmatch(r"(?:会话|对话|聊天)\s*(?P<name>.+)", target)
        if conversation is not None:
            name = _clean_conversation(conversation.group("name"))
            if name:
                return Goal(GoalKind.CONVERSATION_SELECTED, name, app=app)

        url = target.strip()
        if re.match(r"https?://", url, re.IGNORECASE) or re.fullmatch(
            r"(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/\S*)?", url, re.IGNORECASE
        ):
            return Goal(GoalKind.URL_LOADED, url, app=app)
        webpage = re.fullmatch(r"(?P<name>.+?)(?:网页|网站|页面)", url)
        if webpage is not None and webpage.group("name").strip():
            name = webpage.group("name").strip()
            resolved = _WEBPAGE_ALIASES.get(_key(name), name)
            return Goal(GoalKind.URL_LOADED, resolved, app=app)

        if _key(app) in _CHAT_APPS:
            # "切换到微信 打开文件传输助手": inside a chat app, opening a
            # named thing means opening that conversation.
            name = _clean_conversation(target)
            if name:
                return Goal(GoalKind.CONVERSATION_SELECTED, name, app=app)

        return Goal(GoalKind.FREE_FORM, f"打开 {target}", app=app)

    @staticmethod
    def _parse_conversation(surface: str, *, raw_text: str) -> TaskSpec | None:
        match = _CONVERSATION_RE.fullmatch(surface)
        if match is None:
            return None
        app = _clean_app(match.group("app"))
        conversation = _clean_conversation(match.group("conversation"))
        if not app or not conversation:
            return None
        return DeterministicTaskParser._task(
            raw_text,
            (
                Goal(GoalKind.APP_FOREGROUND, app),
                Goal(GoalKind.CONVERSATION_SELECTED, conversation, app=app),
            ),
        )

    @staticmethod
    def _parse_chat_app_target(surface: str, *, raw_text: str) -> TaskSpec | None:
        """"打开微信 sensa" / "微信联系人 sensa" → open the chat, select the contact."""

        match = _CHAT_APP_TARGET_RE.fullmatch(surface)
        if match is None:
            return None
        app = _clean_app(match.group("app"))
        if _key(app) not in _CHAT_APPS:
            return None
        raw_conversation = match.group("conversation").strip()
        # A trailing operation is a separate command, not a contact name; leave
        # those to the compound/input/send-file parsers or the generic planner.
        if re.search(r"(?:打开|进入|输入|键入|发送|提交|新建|创建|然后|接着)", raw_conversation):
            return None
        conversation = _clean_conversation(raw_conversation)
        if not conversation or _key(conversation) in _APP_ALIASES:
            return None
        return DeterministicTaskParser._task(
            raw_text,
            (
                Goal(GoalKind.APP_FOREGROUND, app),
                Goal(GoalKind.CONVERSATION_SELECTED, conversation, app=app),
            ),
        )

    def _parse_input(self, text: str, *, raw_text: str | None = None) -> TaskSpec | None:
        raw_text = text if raw_text is None else raw_text
        normalized = _nfkc(text).strip()
        match = _INPUT_VERB_RE.search(normalized)
        if match is None:
            return None
        prefix = normalized[: match.start()]
        if re.search(r"(?:不要|别|不用|无需)\s*$", prefix):
            return None

        payload = normalized[match.end() :].lstrip(" \t:：,，")
        payload = re.sub(r"^(?:以下)?(?:内容|文字|文本)\s*[:：]?\s*", "", payload)
        forbid_submit = any(_key(phrase) in _key(normalized) for phrase in _FORBID_SUBMIT_PHRASES)
        if not forbid_submit and _INPUT_TAIL_ACTION_RE.search(payload):
            # A second positive operation is not dictated payload. Keep the
            # complete request for the generic planner instead of typing an
            # action phrase and falsely completing only the draft goal.
            return None
        if forbid_submit:
            payload = re.sub(
                r"[\s,，。;；!！(（]*(?:但是|但|然后|记得|注意|先|请)?\s*"
                r"(?:不要发送|别发送|先不发|不发送)"
                r"(?:了|出去|这条|消息|内容|它|哦|啊)?[\s,，。;；!！)）]*$",
                "",
                payload,
            ).rstrip()
        if not payload:
            return None

        app: str | None = None
        activate_app = False
        prefix_surface = _surface(prefix)
        # "切换到 Claude 然后在输入框输入…" carries the input location in the
        # prefix; it is not part of the app name.
        prefix_surface = re.sub(
            r"\s*(?:然后|接着|再)?\s*在?\s*(?:输入框|对话框|聊天框|消息框|编辑框)"
            r"(?:里|中|内|里面)?\s*$",
            "",
            prefix_surface,
        )
        activation_context = re.fullmatch(
            r"(?:请)?(?:切换到|切到|激活|" + _GO_TO_APP + r")\s*(?P<app>.+?)(?:\s*然后)?",
            prefix_surface,
            re.IGNORECASE,
        )
        if activation_context is not None and re.search(
            r"(?:打开|进入|找到|输入|键入|发送|提交|搜索|查找|然后|接着)",
            activation_context.group("app"),
        ):
            # "去chrome打开chatgpt网页然后…" is several operations, not one
            # app name; the multi-goal branch below handles it.
            activation_context = None
        if activation_context is not None:
            cleaned = _clean_app(activation_context.group("app"))
            app = cleaned or None
            activate_app = app is not None
        app_context = re.search(
            r"在\s*(?P<app>.+?)\s*(?:(?:里|中|内|里面)(?:的)?|的)"
            r"(?:输入框)?\s*$",
            prefix,
            re.IGNORECASE,
        )
        if app_context is not None:
            cleaned = _clean_app(app_context.group("app"))
            app = cleaned or None
        elif prefix_surface and not activate_app and re.search(
            r"(?:切换到|切到|激活|打开|进入|找到|" + _GO_TO_APP + r")", prefix_surface
        ):
            # The leading operations ("去chrome打开chatgpt网页然后开一个新对话")
            # become their own goals; the dictated text is typed afterwards
            # in the app they end in. An unparsable prefix keeps the whole
            # utterance for the planner rather than typing a suffix blindly.
            leading = self._parse_goals(prefix_surface, prefix_surface, raw_text=raw_text)
            if any(goal.kind == GoalKind.FREE_FORM for goal in leading.goals):
                return None
            app = next(
                (
                    goal.app or (goal.target if goal.kind == GoalKind.APP_FOREGROUND else None)
                    for goal in reversed(leading.goals)
                    if goal.app or goal.kind == GoalKind.APP_FOREGROUND
                ),
                None,
            )
            return TaskSpec(
                goals=(*leading.goals, Goal(GoalKind.INPUT_CONTAINS, payload, app=app)),
                forbid_submit=forbid_submit,
                side_effect=False,
                raw_text=raw_text,
            )
        goals = (Goal(GoalKind.INPUT_CONTAINS, payload, app=app),)
        if activate_app and app is not None:
            goals = (
                Goal(GoalKind.APP_FOREGROUND, app),
                Goal(GoalKind.INPUT_CONTAINS, payload, app=app),
            )
        return TaskSpec(
            goals=goals,
            forbid_submit=forbid_submit,
            side_effect=False,
            raw_text=raw_text,
        )


TaskParser = DeterministicTaskParser


def parse_task(text: str, *, path_aliases: Mapping[str, str] | None = None) -> TaskSpec:
    return DeterministicTaskParser(path_aliases=path_aliases).parse(text)
