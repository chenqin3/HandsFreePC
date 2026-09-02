from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum

from ..protocol import (
    DesktopAction,
    DesktopActionType,
    DesktopElement,
    DesktopObservation,
    ElementPlane,
    element_plane,
)
from . import policy_terms as _terms
from .browser_identity import is_verified_browser_address
from .models import TaskSpec


class PolicyDisposition(StrEnum):
    """Local authorization result for exactly one bound desktop action."""

    AUTO = "auto"
    CONFIRM = "confirm"
    BLOCK = "block"


class ConfirmationKind(StrEnum):
    SEND = "send"
    DELETE = "delete"
    INSTALL = "install"
    UPLOAD = "upload"
    CLOSE = "close"


_CONFIRMATION_PHRASES: dict[ConfirmationKind, str] = {
    ConfirmationKind.SEND: "确认发送",
    ConfirmationKind.DELETE: "确认删除",
    ConfirmationKind.INSTALL: "确认安装",
    ConfirmationKind.UPLOAD: "确认上传",
    ConfirmationKind.CLOSE: "确认关闭",
}


def _normalize(value: str | None) -> str:
    if not value:
        return ""
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _contains_any(value: str, terms: tuple[str, ...]) -> bool:
    normalized = _normalize(value)
    return any(_normalize(term) in normalized for term in terms)


def _compact_phrase(value: str) -> str:
    return re.sub(r"[\s，。！？；、,:：;.!?]+", "", value).casefold()


def _action_target(action: DesktopAction, observation: DesktopObservation) -> DesktopElement | None:
    if action.element_index is None:
        return None
    return next((item for item in observation.elements if item.index == action.element_index), None)


def _target_text(target: DesktopElement | None) -> str:
    if target is None:
        return ""
    values = (target.name, target.automation_id, target.control_type)
    return " ".join(value for value in values if value)


def _action_semantic_text(action: DesktopAction) -> str:
    """Return operation metadata; user-authored text/value remain inert payloads."""
    return " ".join(
        value
        for value in (
            action.action_name,
            action.key,
        )
        if value
    )


def _app_family(app: str) -> str:
    normalized = _normalize(app)
    for family, aliases in {
        "claude": ("claude", "克劳德"),
        "codex": ("codex", "chatgpt", "chat gpt", "代码助手"),
        "wechat": ("wechat", "weixin", "微信"),
    }.items():
        if any(alias in normalized for alias in aliases):
            return family
    return normalized


def _looks_like_submit(
    action: DesktopAction,
    target: DesktopElement | None,
    observation: DesktopObservation,
) -> bool:
    if action.type in {DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE}:
        return False
    combined = f"{_target_text(target)} {_action_semantic_text(action)}"
    if _contains_any(combined, _terms.SEND_TERMS):
        return True
    if action.type == DesktopActionType.PRESS_KEY and _normalize(action.key) in {
        "enter",
        "return",
    }:
        if target is None:
            return True
        # Enter in the browser omnibox navigates or searches; it never sends a
        # message, so neither a URL nor a search phrase is a side effect. The
        # bar's identity does not depend on whether the planning observation
        # already showed it focused.
        if is_verified_browser_address(target, observation.process_name):
            return False
        return bool(target.composer or element_plane(target) == ElementPlane.INPUT)
    return False


def _hard_block_reason(
    action: DesktopAction | None,
    observation: DesktopObservation,
    target: DesktopElement | None,
    *,
    whole_window: bool = False,
) -> str | None:
    if whole_window and any(
        element.password or element.secret_labeled or element.high_credential
        for element in observation.elements
    ):
        return "密码、认证或凭据界面禁止自动操作"
    if target is not None and (target.password or target.secret_labeled or target.high_credential):
        return "密码、认证或凭据控件禁止自动操作"
    app = _normalize(observation.app)
    process_name = _normalize(observation.process_name)
    title = _normalize(observation.window_title)
    process_hint = process_name or app.split("-", 1)[0]
    if process_hint in _terms.TERMINAL_APPS or any(
        name in process_name or name in app for name in _terms.TERMINAL_APPS
    ):
        return "终端或 Shell 在 assistive_v1 中保持阻断"
    opaque_process = not process_name or process_name in {
        "applicationframehost.exe",
        "openconsole.exe",
    }
    browser_title = title.endswith(
        (" - google chrome", " - microsoft edge", " - mozilla firefox")
    )
    terminal_title = any(
        title == prefix
        or title.startswith(f"{prefix} ")
        or title.startswith(f"{prefix} -")
        or title.startswith(f"{prefix}:")
        for prefix in _terms.TERMINAL_TITLE_PREFIXES
    )
    if opaque_process and terminal_title and not browser_title:
        return "终端或 Shell 窗口在 assistive_v1 中保持阻断"
    if target is not None:
        target_name = _normalize(target.name)
        automation_id = _normalize(target.automation_id)
        if target_name in _terms.TERMINAL_CONTROL_NAMES or any(
            marker in automation_id for marker in _terms.TERMINAL_AUTOMATION_MARKERS
        ):
            return "嵌入式终端或 Shell 控件在 assistive_v1 中保持阻断"
    combined = " ".join(
        (
            title,
            _target_text(target),
            _action_semantic_text(action) if action is not None else "",
        )
    )
    if _contains_any(combined, _terms.HARD_BLOCK_TERMS):
        return "密码、认证、支付、UAC 或安全/隐私界面禁止操作"
    if observation.high_credential_count and (
        whole_window
        or action is None
        or target is None
        or target.visual_ocr
        or action.type in {DesktopActionType.DRAG}
    ):
        return "当前窗口包含高置信凭据，坐标或整窗视觉动作被阻断"
    return None


@dataclass(frozen=True, slots=True)
class ActionBinding:
    confirmation_id: str
    phrase: str
    kind: ConfirmationKind
    app: str
    local_window_id: str
    generation: int
    element_index: str | None
    element_identity: str | None
    action_digest: str
    summary: str
    created_at: float
    expires_at: float

    def matches(
        self, action: DesktopAction, observation: DesktopObservation, *, now: float
    ) -> bool:
        if now > self.expires_at:
            return False
        if observation.app != self.app:
            return False
        if observation.local_window_id != self.local_window_id:
            return False
        if observation.generation != self.generation:
            return False
        if action.element_index != self.element_index:
            return False
        target = _action_target(action, observation)
        if (target.local_identity if target is not None else None) != self.element_identity:
            return False
        return self.action_digest == _action_digest(action, observation)


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    disposition: PolicyDisposition
    reason: str
    binding: ActionBinding | None = None
    hard_block: bool = False


def _action_digest(action: DesktopAction, observation: DesktopObservation) -> str:
    target = _action_target(action, observation)
    payload = {
        "app": observation.app,
        "window": observation.local_window_id,
        "generation": observation.generation,
        "type": action.type.value,
        "element_index": action.element_index,
        "element_identity": target.local_identity if target is not None else None,
        "x": action.x,
        "y": action.y,
        "click_count": action.click_count,
        "mouse_button": action.mouse_button,
        "direction": action.direction,
        "pages": action.pages,
        "action_name": action.action_name,
        "text": action.text,
        "key": action.key,
        "value": action.value,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class AssistivePolicy:
    """Auto-run navigation/drafts; confirm side effects; block hard boundaries."""

    def __init__(
        self,
        *,
        send_policy: Mapping[str, str] | None = None,
        confirmation_timeout_seconds: float = 15.0,
        monotonic: object = time.monotonic,
    ) -> None:
        if confirmation_timeout_seconds <= 0:
            raise ValueError("confirmation_timeout_seconds must be positive")
        merged = dict(_terms.DEFAULT_SEND_POLICY)
        for raw_app, raw_mode in (send_policy or {}).items():
            app = _normalize(str(raw_app))
            mode = _normalize(str(raw_mode))
            if not app:
                raise ValueError("send_policy app names must be non-empty")
            if mode not in {"auto", "confirm"}:
                raise ValueError("send_policy values must be auto or confirm")
            merged[app] = mode
        self.send_policy = merged
        self.confirmation_timeout_seconds = float(confirmation_timeout_seconds)
        self._monotonic = monotonic

    def send_disposition(self, app: str, process_name: str | None = None) -> str:
        """Public view of the configured send policy ("auto" or "confirm") for an app."""

        return self._send_mode(app, process_name)

    def _send_mode(self, app: str, process_name: str | None = None) -> str:
        identity = " ".join(value for value in (app, process_name) if value)
        family = _app_family(identity)
        if family in self.send_policy:
            return self.send_policy[family]
        normalized = _normalize(identity)
        for configured, mode in self.send_policy.items():
            if configured and configured in normalized:
                return mode
        return "confirm"

    @staticmethod
    def _confirmation_kind(
        action: DesktopAction,
        observation: DesktopObservation,
        target: DesktopElement | None,
    ) -> ConfirmationKind | None:
        # Typing and SetValue only create an unsent draft.  Words inside the
        # payload are not action semantics and never trigger a side-effect
        # confirmation by themselves.
        if action.type in {DesktopActionType.TYPE_TEXT, DesktopActionType.SET_VALUE}:
            return None
        combined = " ".join(
            (
                _target_text(target),
                _action_semantic_text(action),
                observation.window_title or "",
            )
        )
        if _contains_any(combined, _terms.DELETE_TERMS):
            return ConfirmationKind.DELETE
        if _contains_any(combined, _terms.INSTALL_TERMS) or _contains_any(
            combined,
            _terms.EXECUTABLE_TERMS,
        ):
            return ConfirmationKind.INSTALL
        if _contains_any(combined, _terms.UPLOAD_TERMS):
            return ConfirmationKind.UPLOAD
        if _contains_any(combined, _terms.CLOSE_TERMS) and _contains_any(
            observation.accessibility_text,
            _terms.UNSAVED_TERMS,
        ):
            return ConfirmationKind.CLOSE
        if _looks_like_submit(action, target, observation):
            return ConfirmationKind.SEND
        return None

    @staticmethod
    def _summary(
        kind: ConfirmationKind,
        action: DesktopAction,
        observation: DesktopObservation,
        target: DesktopElement | None,
    ) -> str:
        labels = {
            ConfirmationKind.SEND: "发送或提交当前内容",
            ConfirmationKind.DELETE: "删除、覆盖或丢弃已有内容",
            ConfirmationKind.INSTALL: "安装、卸载或首次执行程序",
            ConfirmationKind.UPLOAD: "上传、分享或附加文件",
            ConfirmationKind.CLOSE: "关闭含未保存内容的窗口",
        }
        target_name = (target.name.strip() if target is not None else "")[:80]
        window = (observation.window_title or observation.app).strip()[:80]
        location = f"窗口“{window}”"
        if target_name:
            location += f"中的“{target_name}”"
        return f"{labels[kind]}，目标为{location}"

    def evaluate(
        self,
        task: TaskSpec,
        action: DesktopAction,
        observation: DesktopObservation,
    ) -> PolicyDecision:
        if action.app != observation.app or action.generation != observation.generation:
            return PolicyDecision(
                PolicyDisposition.BLOCK,
                "动作未绑定到当前观察的应用和代次",
            )
        if not observation.local_window_id:
            return PolicyDecision(
                PolicyDisposition.BLOCK,
                "动作缺少确切顶层窗口绑定",
            )
        target = _action_target(action, observation)
        if action.element_index is not None and target is None:
            return PolicyDecision(
                PolicyDisposition.BLOCK,
                "动作目标不在当前可见控件快照中",
            )
        if target is not None and (not target.enabled or not target.addressable):
            return PolicyDecision(
                PolicyDisposition.BLOCK,
                "动作目标当前不可用或不可精确寻址",
            )
        if reason := _hard_block_reason(action, observation, target):
            return PolicyDecision(PolicyDisposition.BLOCK, reason, hard_block=True)

        is_submit = _looks_like_submit(action, target, observation)
        if task.forbid_submit and is_submit:
            return PolicyDecision(
                PolicyDisposition.BLOCK,
                "本任务明确要求不要发送；forbid_submit 拒绝一切发送或提交动作",
            )

        kind = self._confirmation_kind(action, observation, target)
        if kind is None:
            return PolicyDecision(
                PolicyDisposition.AUTO,
                "普通导航、滚动、搜索或未发送草稿输入可自动执行",
            )
        if kind == ConfirmationKind.SEND and self._send_mode(
            observation.app,
            observation.process_name,
        ) == "auto":
            return PolicyDecision(
                PolicyDisposition.AUTO,
                "该 AI 助手应用的 send_policy 为 auto",
            )

        now = float(self._monotonic())
        phrase = _CONFIRMATION_PHRASES[kind]
        digest = _action_digest(action, observation)
        binding = ActionBinding(
            confirmation_id=digest[:24],
            phrase=phrase,
            kind=kind,
            app=observation.app,
            local_window_id=observation.local_window_id,
            generation=observation.generation,
            element_index=action.element_index,
            element_identity=target.local_identity if target is not None else None,
            action_digest=digest,
            summary=self._summary(kind, action, observation, target),
            created_at=now,
            expires_at=now + self.confirmation_timeout_seconds,
        )
        return PolicyDecision(
            PolicyDisposition.CONFIRM,
            f"{binding.summary}；请说“{phrase}”",
            binding=binding,
        )

    def authorize_browser_address_focus(
        self, observation: DesktopObservation, element_index: str
    ) -> PolicyDecision:
        """Authorize only Ctrl+L bound to exact, non-sensitive browser chrome."""

        if not observation.local_window_id:
            return PolicyDecision(PolicyDisposition.BLOCK, "动作缺少确切顶层窗口绑定")
        target = next((item for item in observation.elements if item.index == element_index), None)
        if target is None or not is_verified_browser_address(
            target,
            observation.process_name,
        ):
            return PolicyDecision(PolicyDisposition.BLOCK, "未绑定到确切浏览器地址栏")
        if reason := _hard_block_reason(
            None,
            observation,
            target,
            whole_window=True,
        ):
            return PolicyDecision(PolicyDisposition.BLOCK, reason, hard_block=True)
        return PolicyDecision(PolicyDisposition.AUTO, "确切浏览器地址栏可接收导航快捷键")

    def confirm(
        self,
        binding: ActionBinding,
        phrase: str,
        action: DesktopAction,
        observation: DesktopObservation,
    ) -> PolicyDecision:
        now = float(self._monotonic())
        if now > binding.expires_at:
            return PolicyDecision(PolicyDisposition.BLOCK, "动作确认已超过 15 秒并失效")
        if _compact_phrase(phrase) != _compact_phrase(binding.phrase):
            return PolicyDecision(
                PolicyDisposition.CONFIRM,
                f"确认短语不匹配；请完整说“{binding.phrase}”",
                binding=binding,
            )
        if not binding.matches(action, observation, now=now):
            return PolicyDecision(
                PolicyDisposition.BLOCK,
                "窗口、控件或动作在确认前发生变化，原确认绑定已失效",
            )
        return PolicyDecision(PolicyDisposition.AUTO, "确切动作已获得一次性口头确认")

    def refresh_binding(self, binding: ActionBinding) -> ActionBinding:
        """Start the 15-second window when feedback is actually delivered."""

        now = float(self._monotonic())
        return replace(
            binding,
            created_at=now,
            expires_at=now + self.confirmation_timeout_seconds,
        )

    def binding_expired(self, binding: ActionBinding) -> bool:
        """Return whether a one-shot confirmation capability has expired."""

        return float(self._monotonic()) > binding.expires_at


__all__ = [
    "ActionBinding",
    "AssistivePolicy",
    "ConfirmationKind",
    "PolicyDecision",
    "PolicyDisposition",
]
