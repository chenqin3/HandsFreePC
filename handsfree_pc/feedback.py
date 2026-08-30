from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
from dataclasses import dataclass
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


@dataclass(slots=True)
class FeedbackEvent:
    text: str
    kind: str = "recognized"
    duration: float = 3.5


class Overlay:
    def __init__(self) -> None:
        self._queue: queue.Queue[FeedbackEvent | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="HandsFreePC-overlay", daemon=True)
        self._started = False

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def show(self, event: FeedbackEvent) -> None:
        self.start()
        self._queue.put(event)

    def close(self) -> None:
        if self._started:
            self._queue.put(None)

    def _run(self) -> None:  # pragma: no cover - visually smoke-tested
        import tkinter as tk

        root = tk.Tk()
        root.title("HandsFreePC Feedback")
        root.withdraw()
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-alpha", 0.94)
        screen_width = root.winfo_screenwidth()
        screen_height = root.winfo_screenheight()
        width = max(760, int(screen_width * 0.72))
        height = max(170, int(screen_height * 0.18))
        x = max(0, (screen_width - width) // 2)
        y = max(24, int(screen_height * 0.06))
        root.geometry(f"{width}x{height}+{x}+{y}")
        label = tk.Label(
            root,
            text="",
            font=("Microsoft YaHei UI", max(28, int(height * 0.20)), "bold"),
            wraplength=width - 80,
            justify="center",
            padx=36,
            pady=20,
        )
        label.pack(fill="both", expand=True)
        root.update_idletasks()
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
                    root.deiconify()
                    root.lift()
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
        self.speaking = threading.Event()

    def speak(self, text: str) -> None:
        with self._state_lock:
            if self._closed:
                return
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
    ) -> None:
        force_visible = (self.mode == FeedbackMode.SILENT and kind in {"confirm", "error"}) or (
            not allow_voice
        )
        if self.mode in {FeedbackMode.OVERLAY, FeedbackMode.BOTH} or force_visible:
            self.overlay.show(FeedbackEvent(text=text, kind=kind, duration=duration))
        if allow_voice and self.mode in {FeedbackMode.VOICE, FeedbackMode.BOTH}:
            self.speaker.speak(text)

    def close(self) -> None:
        self.overlay.close()
        self.speaker.close()
