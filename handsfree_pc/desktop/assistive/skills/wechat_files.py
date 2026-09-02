"""Send a local file into the currently open WeChat conversation.

WeChat 4.x has no UIA surface, so the file travels through the clipboard as a
CF_HDROP list and is pasted into the composer, exactly like copying it from
Explorer and pressing Ctrl+V. Every step is anchored on OCR of the exact
window: the open chat's header must name the requested conversation before
anything is pasted, the composer is located relative to the 发送 button, the
button itself is clicked to send, and the file name must appear in the chat
pane afterwards or the result is reported as unverified.
"""

from __future__ import annotations

import time
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Event
from typing import Any

from ....windows.native import DesktopUnavailableError, ForegroundIntegrityBoundary
from ....windows.uia import PasswordFieldError
from ...protocol import DesktopAction, DesktopActionType, DesktopElement, DesktopObservation
from ...visual_ocr import SensitiveVisualSurfaceError
from ..models import TaskSpec
from ..policy import AssistivePolicy, PolicyDisposition
from .conversation import ConversationSkillResult, WeChatConversationSkill

SELF_TRANSFER_NAMES = ("文件传输助手", "filetransfer", "file transfer")
_HARD_BOUNDARIES = (
    DesktopUnavailableError,
    ForegroundIntegrityBoundary,
    PasswordFieldError,
    SensitiveVisualSurfaceError,
)
Regions = Sequence[tuple[DesktopElement, tuple[int, int, int, int]]]


def _normalize(value: object) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def is_self_transfer(conversation: str | None) -> bool:
    return _normalize(conversation) in {_normalize(name) for name in SELF_TRANSFER_NAMES}


class WeChatSendFileSkill:
    def __init__(
        self,
        native: Any,
        driver: Any,
        policy: AssistivePolicy,
        *,
        paste_settle: float = 1.2,
        send_settle: float = 1.5,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.native = native
        self.driver = driver
        self.policy = policy
        self.paste_settle = max(0.0, float(paste_settle))
        self.send_settle = max(0.0, float(send_settle))
        self._sleep = sleeper
        self._helper = WeChatConversationSkill(native, driver, policy, sleeper=sleeper)

    def _regions(self, observation: DesktopObservation) -> Regions:
        return self._helper._regions(observation)

    @staticmethod
    def send_button(
        regions: Regions, *, width: int, height: int
    ) -> tuple[int, int, int, int] | None:
        """The 发送 button sits bottom-right; the lowest such label wins."""

        found = [
            bbox
            for element, bbox in regions
            if _normalize(element.name) == "发送"
            and bbox[0] > width * 0.5
            and bbox[1] > height * 0.7
        ]
        return max(found, key=lambda item: item[1]) if found else None

    @classmethod
    def composer_point(cls, regions: Regions, *, width: int, height: int) -> tuple[int, int]:
        """Locate the composer from the 发送 button; fall back to the layout ratio."""

        bbox = cls.send_button(regions, width=width, height=height)
        if bbox is not None:
            return int(width * 0.62), int(bbox[1] - height * 0.09)
        return int(width * 0.62), int(height * 0.86)

    @staticmethod
    def file_visible_in_chat(regions: Regions, *, path: Path, width: int, height: int) -> bool:
        stem = _normalize(path.stem)
        probe = stem[:8] if len(stem) > 8 else stem
        if not probe:
            return False
        return any(
            probe in _normalize(element.name) and bbox[0] > width * 0.3 and bbox[3] < height * 0.85
            for element, bbox in regions
        )

    def _click_point(
        self,
        task: TaskSpec,
        observation: DesktopObservation,
        *,
        x: int,
        y: int,
        hwnd: int,
        cancel_event: Event | None,
        what: str,
    ) -> ConversationSkillResult | None:
        viewport = self._helper._viewport(observation)
        if viewport is None:
            return ConversationSkillResult(
                "retryable_failure",
                "WeChat window has no visual viewport to click",
                observation=observation,
            )
        action = DesktopAction(
            DesktopActionType.CLICK,
            app=observation.app,
            generation=observation.generation,
            element_index=viewport.index,
            x=x,
            y=y,
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
                f"WeChat did not accept the {what} click",
                observation=observation,
            )
        return None

    def _observe(
        self, app: str, hwnd: int, cancel_event: Event | None
    ) -> DesktopObservation | None:
        observation = self.driver.observe(app, cancel_event=cancel_event, capture_screenshot=True)
        if observation.local_window_id != f"hwnd:{hwnd}":
            return None
        return observation

    def _saved_clipboard(self) -> str | None:
        reader = getattr(self.native, "read_clipboard_text", None)
        if not callable(reader):
            return None
        try:
            return reader()
        except Exception:
            return None

    def _restore_clipboard(self, saved: str | None) -> None:
        writer = getattr(self.native, "set_clipboard_text", None)
        if saved is None or not callable(writer):
            return
        try:
            writer(saved)
        except Exception:
            return

    def send(
        self,
        task: TaskSpec,
        *,
        app: str,
        hwnd: int,
        path: Path,
        conversation: str | None,
        cancel_event: Event | None = None,
    ) -> ConversationSkillResult:
        try:
            if not path.is_file():
                return ConversationSkillResult(
                    "retryable_failure", f"File does not exist: {path.name}"
                )
            self.native.assert_interactive_desktop()
            self.native.assert_foreground(hwnd)
            observation = self._observe(app, hwnd, cancel_event)
            if observation is None:
                return ConversationSkillResult(
                    "retryable_failure",
                    "The WeChat window changed before the file was attached",
                )
            regions = self._regions(observation)
            width, height = self._helper._frame_size(observation, regions)
            if conversation and not self._helper.header_shows(
                regions, name=conversation, width=width, height=height
            ):
                # Never paste into whatever chat happens to be open.
                return ConversationSkillResult(
                    "retryable_failure",
                    f"The open WeChat chat is not “{conversation}”; nothing was pasted",
                    {"wrong_conversation": True},
                    observation=observation,
                )
            x, y = self.composer_point(regions, width=width, height=height)
            failure = self._click_point(
                task,
                observation,
                x=x,
                y=y,
                hwnd=hwnd,
                cancel_event=cancel_event,
                what="composer focus",
            )
            if failure is not None:
                return failure
            saved = self._saved_clipboard()
            self.native.copy_files_to_clipboard([path])
            self.native.assert_foreground(hwnd)
            self.native.send_hotkey("ctrl+v")
            self._sleep(self.paste_settle)
            # The paste handler has consumed the file list; give the user
            # back whatever text they had copied.
            self._restore_clipboard(saved)

            if task.forbid_submit:
                return ConversationSkillResult(
                    "succeeded",
                    "Attached the file to the WeChat composer without sending it",
                    {
                        "path": path.name,
                        "hwnd": hwnd,
                        "actions": 2,
                        "attached": True,
                        "sent": False,
                    },
                    observation=observation,
                )
            send_mode = self.policy.send_disposition(observation.app, observation.process_name)
            if not is_self_transfer(conversation) and send_mode != "auto":
                return ConversationSkillResult(
                    "policy_rejected",
                    "文件已附加到微信输入框，但该联系人的 send_policy 为 confirm，未自动发送",
                    {"attached": True, "path": path.name},
                    observation=observation,
                )

            pasted = self._observe(app, hwnd, cancel_event) or observation
            pasted_regions = self._regions(pasted)
            pasted_width, pasted_height = self._helper._frame_size(pasted, pasted_regions)
            button = self.send_button(pasted_regions, width=pasted_width, height=pasted_height)
            if button is not None:
                # Clicking 发送 does not depend on the Enter-to-send setting.
                failure = self._click_point(
                    task,
                    pasted,
                    x=int((button[0] + button[2]) // 2),
                    y=int((button[1] + button[3]) // 2),
                    hwnd=hwnd,
                    cancel_event=cancel_event,
                    what="发送 button",
                )
                if failure is not None:
                    return failure
            else:
                self.native.assert_foreground(hwnd)
                self.native.send_hotkey("enter")
            self._sleep(self.send_settle)

            verified = False
            after: DesktopObservation | None = None
            try:
                after = self._observe(app, hwnd, cancel_event)
                if after is not None:
                    after_regions = self._regions(after)
                    after_width, after_height = self._helper._frame_size(after, after_regions)
                    verified = self.file_visible_in_chat(
                        after_regions, path=path, width=after_width, height=after_height
                    )
            except _HARD_BOUNDARIES:
                raise
            except Exception:
                after = None
            details = {
                "path": path.name,
                "hwnd": hwnd,
                "actions": 3,
                "attached": True,
                "sent": True,
                "verified": verified,
                "via": "button" if button is not None else "enter",
            }
            if not verified:
                return ConversationSkillResult(
                    "unverified",
                    "The send was triggered, but the chat pane does not show the file yet",
                    details,
                    observation=after or pasted,
                )
            return ConversationSkillResult(
                "succeeded",
                "Pasted the file into the WeChat conversation and sent it",
                details,
                observation=after or pasted,
            )
        except _HARD_BOUNDARIES as exc:
            return ConversationSkillResult(
                "hard_failure",
                "A secure desktop, foreground boundary, or credential surface blocked sending",
                {"error_type": type(exc).__name__},
            )
        except Exception as exc:
            return ConversationSkillResult(
                "retryable_failure",
                "WeChat file sending failed through the local Windows adapter",
                {"error_type": type(exc).__name__, "error": " ".join(str(exc).split())[:200]},
            )


__all__ = ["SELF_TRANSFER_NAMES", "WeChatSendFileSkill", "is_self_transfer"]
