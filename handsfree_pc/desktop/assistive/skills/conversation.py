"""Deterministic conversation switching for chat apps.

Two shapes cover the user's daily apps:

* ``PaletteConversationSkill`` — Claude Desktop and Codex Desktop open a search
  palette on Ctrl+K whose results are real UIA ``ListItem`` controls. Type the
  name, click the first matching entry.
* ``WeChatConversationSkill`` — WeChat 4.x exposes no UIA controls, but its
  search dropdown is legible to OCR. Type the name into the search box, locate
  the first entry under a real result section (功能/联系人/群聊/最常使用…),
  never the web-search suggestion rows, and click it through the frame-bound
  visual viewport.
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from threading import Event
from typing import Any, Literal

from ....windows.native import DesktopUnavailableError, ForegroundIntegrityBoundary
from ....windows.uia import PasswordFieldError
from ...protocol import (
    DesktopAction,
    DesktopActionType,
    DesktopElement,
    DesktopElementAction,
    DesktopObservation,
)
from ...visual_ocr import SensitiveVisualSurfaceError
from ..models import TaskSpec
from ..policy import AssistivePolicy, PolicyDisposition

ConversationSkillStatus = Literal[
    "succeeded",
    "unverified",
    "retryable_failure",
    "policy_rejected",
    "hard_failure",
]

PALETTE_HOTKEYS: dict[str, str] = {"claude": "ctrl+k", "codex": "ctrl+k"}
# Claude Desktop's surface toggle; "Code, working" is the same button while a
# session runs, so labels are matched as prefixes up to a comma.
CLAUDE_MODE_BUTTONS: dict[str, tuple[str, ...]] = {
    "code": ("Code",),
    "chat": ("Chat and Cowork",),
}
CLAUDE_DEFAULT_MODE = "code"
# Sidebar rows carry a status word before the conversation name.
SIDEBAR_STATUS_PREFIXES = (
    "idle",
    "running",
    "working",
    "markasunread",
    "unread",
    "needsattention",
    "waiting",
)
# Sidebar buttons that are chrome or per-row menus rather than conversations.
SIDEBAR_EXCLUDED_PREFIXES = (
    "moreoptionsfor",
    "newsessionin",
    "newchat",
    "newtask",
    "viewall",
    "filter",
    "pinned",
    "hidesidebar",
    "dispatch",
    "design(",
    "chatsandtasks",
    "quicktask",
    "code,",
    "置顶聊天",
    "归档聊天",
    "新对话",
    "新聊天",
)
SIDEBAR_EXCLUDED_EXACT = frozenset(
    {
        "new",
        "search",
        "back",
        "forward",
        "menu",
        "close",
        "artifacts",
        "projects",
        "customize",
        "scheduled",
        "recents",
        "actions",
        "design",
        "code",
        "chatandcowork",
        "最小化",
        "恢复",
        "关闭",
        "新对话",
    }
)
# Palette rows that create or run something rather than open an existing chat.
PALETTE_ACTION_PREFIXES = (
    "newchat",
    "newtask",
    "newsession",
    "newproject",
    "startachat",
    "startnewchat",
    "新聊天",
    "新任务",
    "新会话",
    "新对话",
    "新建",
    "创建",
    "打开文件夹",
    "搜索文件",
    "常规",
    "导入",
)
WECHAT_SEARCH_HOTKEY = "ctrl+f"
# Section headers in WeChat's search dropdown, in the order they are trusted.
# The web-search suggestion block is never a conversation.
WECHAT_RESULT_SECTIONS = ("最常使用", "联系人", "群聊", "功能", "公众号", "聊天记录")
WECHAT_SKIPPED_SECTIONS = ("搜索网络结果",)


def _normalize(value: object) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def _mentions(candidate: str, name: str) -> bool:
    wanted = _normalize(name)
    return bool(wanted) and wanted in _normalize(candidate)


@dataclass(frozen=True, slots=True)
class ConversationSkillResult:
    status: ConversationSkillStatus
    message: str
    details: Mapping[str, object] = field(default_factory=dict)
    observation: DesktopObservation | None = None


def _boundary_result(exc: BaseException) -> ConversationSkillResult:
    return ConversationSkillResult(
        "hard_failure",
        "A secure desktop, foreground boundary, or credential surface blocked the switch",
        {"error_type": type(exc).__name__},
    )


def _transient_result(exc: BaseException, what: str) -> ConversationSkillResult:
    return ConversationSkillResult(
        "retryable_failure",
        f"{what} failed through the local Windows adapter",
        {"error_type": type(exc).__name__, "error": " ".join(str(exc).split())[:200]},
    )


_HARD_BOUNDARIES = (
    DesktopUnavailableError,
    ForegroundIntegrityBoundary,
    PasswordFieldError,
    SensitiveVisualSurfaceError,
)


class PaletteConversationSkill:
    """Sidebar-first, Ctrl+K-palette-second navigation for Electron chat apps.

    Claude Desktop has two surfaces (Code sessions vs Chat and Cowork chats)
    behind a toggle at the top of its sidebar; ``mode`` selects one before the
    conversation is looked up. The sidebar is tried first because the palette
    lists sessions across surfaces and a name alone cannot tell them apart.
    """

    def __init__(
        self,
        native: Any,
        driver: Any,
        policy: AssistivePolicy,
        *,
        hotkey: str = "ctrl+k",
        mode: str | None = None,
        mode_buttons: Mapping[str, Sequence[str]] | None = None,
        open_settle: float = 0.5,
        typed_settle: float = 0.7,
        mode_settle: float = 0.8,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.native = native
        self.driver = driver
        self.policy = policy
        self.hotkey = hotkey
        self.mode = (mode or "").strip().casefold() or None
        self.mode_buttons = {
            key: tuple(labels) for key, labels in (mode_buttons or {}).items()
        }
        self.open_settle = max(0.0, float(open_settle))
        self.typed_settle = max(0.0, float(typed_settle))
        self.mode_settle = max(0.0, float(mode_settle))
        self._sleep = sleeper

    def _mode_button(self, observation: DesktopObservation, mode: str) -> DesktopElement | None:
        labels = tuple(_normalize(label) for label in self.mode_buttons.get(mode, ()))
        if not labels:
            return None
        for element in observation.elements:
            if element.control_type.casefold() != "button" or not element.enabled:
                continue
            candidate = _normalize(element.name)
            # "Code, working" while a session runs still names the Code surface.
            if any(candidate == label or candidate.startswith(label + ",") for label in labels):
                return element
        return None

    def _click(
        self,
        task: TaskSpec,
        observation: DesktopObservation,
        element: DesktopElement,
        *,
        hwnd: int,
        cancel_event: Event | None,
    ) -> ConversationSkillResult | None:
        """Policy-check and execute one click; None means it went through."""

        action = DesktopAction(
            DesktopActionType.CLICK,
            app=observation.app,
            generation=observation.generation,
            element_index=element.index,
        )
        decision = self.policy.evaluate(task, action, observation)
        if decision.disposition != PolicyDisposition.AUTO:
            return ConversationSkillResult(
                "hard_failure" if decision.hard_block else "policy_rejected",
                decision.reason,
                observation=observation,
            )
        self.native.assert_foreground(hwnd)
        receipt = self.driver.execute(action, observation, cancel_event=cancel_event)
        if not receipt.accepted:
            return ConversationSkillResult(
                "retryable_failure",
                f"The entry {element.name!r} did not accept the click",
                observation=observation,
            )
        return None

    def _ensure_mode(
        self,
        task: TaskSpec,
        observation: DesktopObservation,
        *,
        app: str,
        hwnd: int,
        cancel_event: Event | None,
    ) -> tuple[DesktopObservation, ConversationSkillResult | None]:
        if self.mode is None or not self.mode_buttons:
            return observation, None
        button = self._mode_button(observation, self.mode)
        if button is None or button.selected is True:
            return observation, None
        failure = self._click(task, observation, button, hwnd=hwnd, cancel_event=cancel_event)
        if failure is not None:
            return observation, failure
        self._sleep(self.mode_settle)
        refreshed = self.driver.observe(app, cancel_event=cancel_event)
        return refreshed, None

    @staticmethod
    def _sidebar_entries(observation: DesktopObservation, name: str) -> list[DesktopElement]:
        matches: list[DesktopElement] = []
        exact: list[DesktopElement] = []
        for element in observation.elements:
            if element.control_type.casefold() not in {"button", "listitem", "link", "treeitem"}:
                continue
            if not element.enabled or not element.addressable:
                continue
            actions = set(element.supported_actions or ())
            if DesktopElementAction.EXPAND in actions or DesktopElementAction.COLLAPSE in actions:
                continue
            clickable = {DesktopElementAction.CLICK, DesktopElementAction.INVOKE}
            if not actions & clickable:
                continue
            label = _normalize(element.name)
            if label in SIDEBAR_EXCLUDED_EXACT or any(
                label.startswith(prefix) for prefix in SIDEBAR_EXCLUDED_PREFIXES
            ):
                continue
            if label.endswith(",renamechat"):
                continue
            stripped = label
            for prefix in SIDEBAR_STATUS_PREFIXES:
                if stripped.startswith(prefix):
                    stripped = stripped[len(prefix) :]
                    break
            if not _mentions(stripped, name):
                continue
            matches.append(element)
            if stripped == _normalize(name):
                exact.append(element)
        return exact or matches

    @staticmethod
    def _is_action_entry(item_name: str, query: str) -> bool:
        """Quick actions echo the typed text ("New chat“项目周报”"); never open those.

        Clicking one would create a fresh chat and send the query as a message
        instead of switching to the existing conversation.
        """

        normalized = _normalize(item_name)
        if any(normalized.startswith(prefix) for prefix in PALETTE_ACTION_PREFIXES):
            return True
        quoted = (f"“{query}”", f"\"{query}\"", f"'{query}'", f"「{query}」")
        return any(marker in item_name for marker in quoted)

    @classmethod
    def _matching_items(cls, observation: DesktopObservation, name: str) -> list[DesktopElement]:
        items = [
            element
            for element in observation.elements
            if element.control_type.casefold() == "listitem"
            and element.enabled
            and element.addressable
            and _mentions(element.name, name)
            and not cls._is_action_entry(element.name, name)
        ]
        exact = [element for element in items if _normalize(element.name) == _normalize(name)]
        return exact or items

    def _dismiss(self) -> None:
        try:
            self.native.send_hotkey("escape")
        except Exception:
            return

    def open(
        self,
        task: TaskSpec,
        *,
        app: str,
        name: str,
        hwnd: int,
        cancel_event: Event | None = None,
    ) -> ConversationSkillResult:
        try:
            self.native.assert_interactive_desktop()
            self.native.assert_foreground(hwnd)
            current = self.driver.observe(app, cancel_event=cancel_event)
            if current.local_window_id != f"hwnd:{hwnd}":
                return ConversationSkillResult(
                    "retryable_failure",
                    "The chat window changed before the conversation switch",
                    observation=current,
                )
            current, failure = self._ensure_mode(
                task, current, app=app, hwnd=hwnd, cancel_event=cancel_event
            )
            if failure is not None:
                return failure
            sidebar = self._sidebar_entries(current, name)
            if sidebar:
                target = sidebar[0]
                failure = self._click(task, current, target, hwnd=hwnd, cancel_event=cancel_event)
                if failure is not None:
                    return failure
                return ConversationSkillResult(
                    "succeeded",
                    "Opened the conversation from the app sidebar",
                    {"item": target.name, "hwnd": hwnd, "actions": 1, "via": "sidebar"},
                    observation=current,
                )
            self.native.assert_foreground(hwnd)
            self.native.send_hotkey(self.hotkey)
            self._sleep(self.open_settle)
            self.native.assert_foreground(hwnd)
            self.native.send_text(name)
            self._sleep(self.typed_settle)
            if cancel_event is not None and cancel_event.is_set():
                self._dismiss()
                return ConversationSkillResult("retryable_failure", "Conversation switch cancelled")
            observation = self.driver.observe(app, cancel_event=cancel_event)
            if observation.local_window_id != f"hwnd:{hwnd}":
                self._dismiss()
                return ConversationSkillResult(
                    "retryable_failure",
                    "The chat window changed while its search palette was open",
                    observation=observation,
                )
            matches = self._matching_items(observation, name)
            if not matches:
                self._dismiss()
                return ConversationSkillResult(
                    "retryable_failure",
                    "No palette entry matched the requested conversation",
                    {
                        "listed": sum(
                            1 for e in observation.elements if e.control_type == "ListItem"
                        )
                    },
                    observation=observation,
                )
            target = matches[0]
            failure = self._click(task, observation, target, hwnd=hwnd, cancel_event=cancel_event)
            if failure is not None:
                self._dismiss()
                return failure
            return ConversationSkillResult(
                "succeeded",
                "Opened the conversation from the app's search palette",
                {"item": target.name, "hwnd": hwnd, "actions": 3, "via": "palette"},
                observation=observation,
            )
        except _HARD_BOUNDARIES as exc:
            return _boundary_result(exc)
        except Exception as exc:
            self._dismiss()
            return _transient_result(exc, "Palette conversation switch")


class WeChatConversationSkill:
    """Search-driven conversation switch for WeChat's render-only window."""

    def __init__(
        self,
        native: Any,
        driver: Any,
        policy: AssistivePolicy,
        *,
        hotkey: str = WECHAT_SEARCH_HOTKEY,
        open_settle: float = 0.4,
        typed_settle: float = 0.9,
        click_settle: float = 0.6,
        results_polls: int = 4,
        results_poll_interval: float = 0.7,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.results_polls = max(1, int(results_polls))
        self.results_poll_interval = max(0.0, float(results_poll_interval))
        self.native = native
        self.driver = driver
        self.policy = policy
        self.hotkey = hotkey
        self.open_settle = max(0.0, float(open_settle))
        self.typed_settle = max(0.0, float(typed_settle))
        self.click_settle = max(0.0, float(click_settle))
        self._sleep = sleeper

    def _regions(
        self,
        observation: DesktopObservation,
    ) -> list[tuple[DesktopElement, tuple[int, int, int, int]]]:
        read_bbox = getattr(self.driver, "visual_region_bbox", None)
        if not callable(read_bbox):
            return []
        regions: list[tuple[DesktopElement, tuple[int, int, int, int]]] = []
        for element in observation.elements:
            if element.control_type != "VisualText":
                continue
            bbox = read_bbox(observation, element)
            if bbox is not None:
                regions.append((element, tuple(int(v) for v in bbox)))
        return regions

    @staticmethod
    def _viewport(observation: DesktopObservation) -> DesktopElement | None:
        return next(
            (e for e in observation.elements if e.control_type == "VisualViewport"),
            None,
        )

    @staticmethod
    def pick_result(
        regions: Sequence[tuple[DesktopElement, tuple[int, int, int, int]]],
        *,
        name: str,
        width: int,
    ) -> tuple[DesktopElement, tuple[int, int, int, int]] | None:
        """Choose the first entry under the first real result section.

        The dropdown occupies the left part of the window. Web-search
        suggestions come first and repeat the query verbatim, so a plain
        text match is not enough: the entry must sit below a trusted section
        header such as 功能 or 联系人.
        """

        column = [item for item in regions if item[1][0] < width * 0.45]
        headers = [
            item
            for item in column
            if _normalize(item[0].name) in {_normalize(h) for h in WECHAT_RESULT_SECTIONS}
        ]
        if not headers:
            return None
        header = min(headers, key=lambda item: item[1][1])
        header_bottom = header[1][3]
        below = sorted(
            (item for item in column if item[1][1] >= header_bottom and item is not header),
            key=lambda item: item[1][1],
        )
        if not below:
            return None
        named = [item for item in below if _mentions(item[0].name, name)]
        return named[0] if named else below[0]

    @staticmethod
    def header_shows(
        regions: Sequence[tuple[DesktopElement, tuple[int, int, int, int]]],
        *,
        name: str,
        width: int,
        height: int,
    ) -> bool:
        return any(
            _mentions(element.name, name) and bbox[0] > width * 0.35 and bbox[3] < height * 0.15
            for element, bbox in regions
        )

    def open(
        self,
        task: TaskSpec,
        *,
        app: str,
        name: str,
        hwnd: int,
        cancel_event: Event | None = None,
    ) -> ConversationSkillResult:
        try:
            self.native.assert_interactive_desktop()
            self.native.assert_foreground(hwnd)
            # WeChat's search dropdown loads over the network and its layout is
            # unstable; skip it entirely when the requested chat is already the
            # open one, which the title header proves.
            current = self.driver.observe(app, cancel_event=cancel_event, capture_screenshot=True)
            if current.local_window_id == f"hwnd:{hwnd}":
                current_regions = self._regions(current)
                width, height = self._frame_size(current, current_regions)
                if self.header_shows(current_regions, name=name, width=width, height=height):
                    return ConversationSkillResult(
                        "succeeded",
                        "The requested WeChat conversation is already open",
                        {"item": name, "hwnd": hwnd, "actions": 0, "already_open": True},
                        observation=current,
                    )
            self.native.assert_foreground(hwnd)
            self.native.send_hotkey(self.hotkey)
            self._sleep(self.open_settle)
            self.native.assert_foreground(hwnd)
            self.native.send_text(name)
            self._sleep(self.typed_settle)
            if cancel_event is not None and cancel_event.is_set():
                return ConversationSkillResult("retryable_failure", "Conversation switch cancelled")
            # The dropdown loads over the network; poll until a result section
            # is legible instead of trusting one fixed delay.
            chosen = None
            viewport = None
            regions = []
            width = height = 0
            for attempt in range(self.results_polls):
                observation = self.driver.observe(
                    app, cancel_event=cancel_event, capture_screenshot=True
                )
                if observation.local_window_id != f"hwnd:{hwnd}":
                    return ConversationSkillResult(
                        "retryable_failure",
                        "The WeChat window changed while its search was open",
                        observation=observation,
                    )
                viewport = self._viewport(observation)
                regions = list(self._regions(observation))
                if viewport is not None and regions:
                    width, height = self._frame_size(observation, regions)
                    chosen = self.pick_result(regions, name=name, width=width)
                    if chosen is not None:
                        break
                if attempt + 1 < self.results_polls:
                    self._sleep(self.results_poll_interval)
            if viewport is None or not regions:
                return ConversationSkillResult(
                    "retryable_failure",
                    "WeChat search results were not legible (no OCR regions or viewport)",
                    {"regions": len(regions)},
                    observation=observation,
                )
            if chosen is None:
                return ConversationSkillResult(
                    "retryable_failure",
                    "No result section entry was found under the WeChat search box",
                    {"regions": len(regions)},
                    observation=observation,
                )
            element, bbox = chosen
            action = DesktopAction(
                DesktopActionType.CLICK,
                app=observation.app,
                generation=observation.generation,
                element_index=viewport.index,
                x=int((bbox[0] + bbox[2]) // 2),
                y=int((bbox[1] + bbox[3]) // 2),
            )
            decision = self.policy.evaluate(task, action, observation)
            if decision.disposition != PolicyDisposition.AUTO:
                return ConversationSkillResult(
                    "hard_failure" if decision.hard_block else "policy_rejected",
                    decision.reason,
                    observation=observation,
                )
            self.native.assert_foreground(hwnd)
            receipt = self.driver.execute(action, observation, cancel_event=cancel_event)
            if not receipt.accepted:
                return ConversationSkillResult(
                    "retryable_failure",
                    "The WeChat search result did not accept the click",
                    observation=observation,
                )
            self._sleep(self.click_settle)
            verified = False
            after: DesktopObservation | None = None
            try:
                after = self.driver.observe(app, cancel_event=cancel_event, capture_screenshot=True)
                after_regions = self._regions(after)
                after_width, after_height = self._frame_size(after, after_regions)
                verified = self.header_shows(
                    after_regions, name=name, width=after_width, height=after_height
                )
            except _HARD_BOUNDARIES:
                raise
            except Exception:
                after = None
            return ConversationSkillResult(
                "succeeded",
                "Opened the conversation from WeChat search results",
                {
                    "item": element.name,
                    "hwnd": hwnd,
                    "actions": 3,
                    "verified": verified,
                },
                observation=after or observation,
            )
        except _HARD_BOUNDARIES as exc:
            return _boundary_result(exc)
        except Exception as exc:
            return _transient_result(exc, "WeChat conversation switch")

    @staticmethod
    def _frame_size(
        observation: DesktopObservation,
        regions: Sequence[tuple[DesktopElement, tuple[int, int, int, int]]],
    ) -> tuple[int, int]:
        size = getattr(observation, "screenshot_size", None)
        if isinstance(size, tuple) and len(size) == 2:
            return int(size[0]), int(size[1])
        png = observation.screenshot_png
        if png:
            try:
                from ...visual_ocr import _png_dimensions

                return _png_dimensions(png)
            except Exception:
                pass
        width = max((bbox[2] for _e, bbox in regions), default=1)
        height = max((bbox[3] for _e, bbox in regions), default=1)
        return width, height


__all__ = [
    "CLAUDE_DEFAULT_MODE",
    "CLAUDE_MODE_BUTTONS",
    "PALETTE_HOTKEYS",
    "WECHAT_RESULT_SECTIONS",
    "WECHAT_SKIPPED_SECTIONS",
    "ConversationSkillResult",
    "PaletteConversationSkill",
    "WeChatConversationSkill",
]
