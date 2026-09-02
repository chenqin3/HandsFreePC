from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .models import FeedbackMode

_COLORS = {
    "armed": ("#0B1220", "#8FA3BF"),
    "listening": ("#092B1B", "#7EF5B2"),
    "recognized": ("#102447", "#A9CDFF"),
    "executing": ("#2F2408", "#FFE38A"),
    "confirm": ("#3A1807", "#FFB277"),
    "success": ("#0D321E", "#98F5BC"),
    "error": ("#3A1015", "#FF9BA5"),
}


_PAD_X = 36
_PAD_Y = 20
_MIN_WIDTH = 360


def fit_overlay_size(
    required_width: int,
    required_height: int,
    *,
    max_width: int,
    min_width: int = _MIN_WIDTH,
) -> tuple[int, int]:
    """Size the overlay window to its text: never wider than max_width, never
    narrower than min_width, and exactly as tall as the wrapped text needs."""

    width = min(max_width, max(min_width, int(required_width)))
    height = max(1, int(required_height))
    return width, height


@dataclass(slots=True)
class FeedbackEvent:
    text: str
    kind: str = "recognized"
    duration: float = 3.5
    delivered: threading.Event = field(default_factory=threading.Event, repr=False)


class Overlay:
    def __init__(self) -> None:
        self._queue: queue.Queue[FeedbackEvent | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="HandsFreePC-overlay", daemon=True)
        self._started = False
        self._closed = False
        self._last_error: Exception | None = None
        self._state_lock = threading.Lock()

    def start(self) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Overlay is closed")
            if self._started:
                return
            self._thread.start()
            self._started = True

    def show(
        self,
        event: FeedbackEvent,
        *,
        wait_for_delivery: bool = False,
        timeout: float = 2.0,
    ) -> bool:
        with self._state_lock:
            if self._closed or self._last_error is not None:
                return False
        try:
            self.start()
        except Exception as exc:
            with self._state_lock:
                self._last_error = exc
            return False
        self._queue.put(event)
        if not wait_for_delivery:
            return True
        if not event.delivered.wait(timeout):
            return False
        with self._state_lock:
            return self._last_error is None

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            started = self._started
        if started:
            self._queue.put(None)

    def _run(self) -> None:  # pragma: no cover - visually smoke-tested
        try:
            self._run_overlay()
        except Exception as exc:
            with self._state_lock:
                self._last_error = exc
        finally:
            while True:
                try:
                    pending = self._queue.get_nowait()
                except queue.Empty:
                    break
                if pending is not None:
                    pending.delivered.set()

    def _run_overlay(self) -> None:  # pragma: no cover - visually smoke-tested
        import tkinter as tk

        root = tk.Tk()
        root.title("HandsFreePC Feedback")
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.94)
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        # The box fits its text: a short status line gets a compact box, a long
        # error report grows, and nothing is wider than about 72% of the screen.
        max_width = max(760, int(screen_width * 0.72))
        font_size = max(28, int(max(170, int(screen_height * 0.18)) * 0.20))
        y = max(24, int(screen_height * 0.06))
        label = tk.Label(
            root,
            text="",
            font=("Microsoft YaHei UI", font_size, "bold"),
            wraplength=max_width - 2 * _PAD_X,
            justify="center",
            padx=_PAD_X,
            pady=_PAD_Y,
        )
        label.pack(fill="both", expand=True)

        def fit_to_text() -> None:
            root.update_idletasks()
            width, height = fit_overlay_size(
                label.winfo_reqwidth(), label.winfo_reqheight(), max_width=max_width
            )
            x = max(0, (screen_width - width) // 2)
            root.geometry(f"{width}x{height}+{x}+{y}")

        fit_to_text()
        self._make_no_activate_click_through(root)
        hide_at = 0.0

        def poll() -> None:
            nonlocal hide_at
            try:
                while True:
                    event = self._queue.get_nowait()
                    if event is None:
                        root.destroy()
                        return
                    background, foreground = _COLORS.get(event.kind, _COLORS["recognized"])
                    label.configure(text=event.text, bg=background, fg=foreground)
                    root.configure(bg=background)
                    fit_to_text()
                    root.deiconify()
                    root.lift()
                    root.update_idletasks()
                    event.delivered.set()
                    hide_at = time.monotonic() + event.duration if event.duration > 0 else 0.0
            except queue.Empty:
                pass
            if hide_at and time.monotonic() >= hide_at:
                root.withdraw()
                hide_at = 0.0
            root.after(60, poll)

        root.after(10, poll)
        root.mainloop()

    @staticmethod
    def _make_no_activate_click_through(root: Any) -> None:
        if os.name != "nt":
            return
        root.update_idletasks()
        hwnd = root.winfo_id()
        get_window_long = ctypes.windll.user32.GetWindowLongW
        set_window_long = ctypes.windll.user32.SetWindowLongW
        style = get_window_long(hwnd, -20)
        ws_ex_toolwindow = 0x00000080
        ws_ex_transparent = 0x00000020
        ws_ex_noactivate = 0x08000000
        set_window_long(hwnd, -20, style | ws_ex_toolwindow | ws_ex_transparent | ws_ex_noactivate)


def _load_sapi_modules() -> tuple[Any, Any]:
    import pythoncom
    import win32com.client

    return pythoncom, win32com.client


class SapiSpeaker:
    def __init__(self) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="HandsFreePC-tts", daemon=True)
        self._started = False
        self._closed = False
        self._pending = 0
        self._state_lock = threading.Lock()
        self._last_error: Exception | None = None
        self._generation = 0
        self.speaking = threading.Event()

    @property
    def generation(self) -> int:
        with self._state_lock:
            return self._generation

    @property
    def last_error(self) -> Exception | None:
        with self._state_lock:
            return self._last_error

    def speak(self, text: str) -> bool:
        with self._state_lock:
            if self._closed:
                return False
            self._generation += 1
            if self._pending == 0:
                self.speaking.set()
            self._pending += 1
            self._queue.put(text)
            if not self._started:
                self._started = True
                try:
                    self._thread.start()
                except Exception as exc:
                    self._last_error = exc
                    self._closed = True
                    self._discard_pending_locked()
                    return False
            return True

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            if self._started:
                self._queue.put(None)
            elif self.speaking.is_set():
                self.speaking.clear()

    def _complete_one(self) -> None:
        with self._state_lock:
            if self._pending > 0:
                self._pending -= 1
            if self._pending == 0:
                self.speaking.clear()

    def _discard_pending_locked(self) -> None:
        was_speaking = self._pending > 0 or self.speaking.is_set()
        self._pending = 0
        if was_speaking:
            self.speaking.clear()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _discard_pending(self) -> None:
        with self._state_lock:
            self._discard_pending_locked()

    def _run(self) -> None:  # pragma: no cover - real SAPI is covered by fake-object tests
        pythoncom: Any | None = None
        com_initialized = False
        try:
            pythoncom, win32_client = _load_sapi_modules()
            pythoncom.CoInitialize()
            com_initialized = True
            voice = win32_client.Dispatch("SAPI.SpVoice")
            while True:
                text = self._queue.get()
                if text is None:
                    return
                try:
                    voice.Speak(text)
                except Exception as exc:
                    # Publish the failure before clearing ``speaking`` so the microphone owner
                    # cannot mistake a failed SAPI call for delivered confirmation feedback.
                    with self._state_lock:
                        self._last_error = exc
                    raise
                finally:
                    self._complete_one()
        except Exception as exc:
            self._last_error = exc
        finally:
            if com_initialized and pythoncom is not None:
                try:
                    pythoncom.CoUninitialize()
                except Exception as exc:
                    if self._last_error is None:
                        self._last_error = exc
            with self._state_lock:
                self._closed = True
            self._discard_pending()


class FeedbackController:
    def __init__(self, mode: FeedbackMode = FeedbackMode.OVERLAY) -> None:
        self.mode = mode
        self.overlay = Overlay()
        self.speaker = SapiSpeaker()

    def set_mode(self, mode: FeedbackMode, *, allow_voice: bool = True) -> None:
        self.mode = mode
        self.emit(
            f"已切换到 {mode.value} 反馈",
            kind="success",
            allow_voice=allow_voice,
        )

    def emit(
        self,
        text: str,
        *,
        kind: str = "recognized",
        duration: float = 3.5,
        allow_voice: bool = True,
        force_visible_when_voice_blocked: bool = True,
    ) -> bool:
        force_visible = (self.mode == FeedbackMode.SILENT and kind in {"confirm", "error"}) or (
            not allow_voice and force_visible_when_voice_blocked
        )
        delivered = True
        if self.mode in {FeedbackMode.OVERLAY, FeedbackMode.BOTH} or force_visible:
            delivered = self.overlay.show(
                FeedbackEvent(text=text, kind=kind, duration=duration),
                wait_for_delivery=kind == "confirm",
            )
        if allow_voice and self.mode in {FeedbackMode.VOICE, FeedbackMode.BOTH}:
            delivered = self.speaker.speak(text) is not False and delivered
        return delivered

    def close(self) -> None:
        self.overlay.close()
        self.speaker.close()
