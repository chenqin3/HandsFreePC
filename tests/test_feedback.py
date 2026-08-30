from __future__ import annotations

import threading
import time

import pytest

import handsfree_pc.feedback as feedback_module
from handsfree_pc.feedback import FeedbackController, SapiSpeaker
from handsfree_pc.models import FeedbackMode


class RecordingOverlay:
    def __init__(self) -> None:
        self.events = []

    def show(self, event) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


class RecordingSpeaker:
    def __init__(self) -> None:
        self.messages = []

    def speak(self, text: str) -> None:
        self.messages.append(text)

    def close(self) -> None:
        pass


def test_silent_mode_still_shows_confirmation_and_errors() -> None:
    controller = FeedbackController(FeedbackMode.SILENT)
    controller.overlay = RecordingOverlay()
    controller.speaker = RecordingSpeaker()

    controller.emit("ordinary", kind="recognized")
    controller.emit("confirm", kind="confirm")
    controller.emit("error", kind="error")

    assert [event.text for event in controller.overlay.events] == ["confirm", "error"]
    assert controller.speaker.messages == []


def test_native_voice_feedback_can_force_overlay_without_speaking() -> None:
    controller = FeedbackController(FeedbackMode.VOICE)
    controller.overlay = RecordingOverlay()
    controller.speaker = RecordingSpeaker()

    controller.emit("native voice ready", kind="success", allow_voice=False)

    assert [event.text for event in controller.overlay.events] == ["native voice ready"]
    assert controller.speaker.messages == []


def test_mode_switch_can_be_forced_to_overlay_only() -> None:
    controller = FeedbackController(FeedbackMode.VOICE)
    controller.overlay = RecordingOverlay()
    controller.speaker = RecordingSpeaker()

    controller.set_mode(FeedbackMode.BOTH, allow_voice=False)

    assert controller.mode == FeedbackMode.BOTH
    assert [event.text for event in controller.overlay.events] == ["已切换到 both 反馈"]
    assert controller.speaker.messages == []


def wait_until(predicate, *, timeout: float = 1.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        time.sleep(0.005)


class TrackingEvent:
    def __init__(self) -> None:
        self._event = threading.Event()
        self.transitions: list[str] = []

    def set(self) -> None:
        self.transitions.append("set")
        self._event.set()

    def clear(self) -> None:
        self.transitions.append("clear")
        self._event.clear()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


class FakePythonCom:
    def __init__(self, *, initialize_error: Exception | None = None) -> None:
        self.initialize_error = initialize_error
        self.initialized = 0
        self.uninitialized = 0

    def CoInitialize(self) -> None:
        if self.initialize_error is not None:
            raise self.initialize_error
        self.initialized += 1

    def CoUninitialize(self) -> None:
        self.uninitialized += 1


class FakeWin32Client:
    def __init__(self, voice=None, *, dispatch_error: Exception | None = None) -> None:
        self.voice = voice
        self.dispatch_error = dispatch_error

    def Dispatch(self, name: str):
        assert name == "SAPI.SpVoice"
        if self.dispatch_error is not None:
            raise self.dispatch_error
        return self.voice


class BlockingVoice:
    def __init__(self, count: int) -> None:
        self.started = [threading.Event() for _ in range(count)]
        self.release = [threading.Event() for _ in range(count)]
        self.messages: list[str] = []

    def Speak(self, text: str) -> None:
        index = len(self.messages)
        self.messages.append(text)
        self.started[index].set()
        assert self.release[index].wait(timeout=1)


def test_sapi_speaking_stays_set_across_all_queued_messages(monkeypatch) -> None:
    voice = BlockingVoice(2)
    pythoncom = FakePythonCom()
    client = FakeWin32Client(voice)
    monkeypatch.setattr(
        feedback_module,
        "_load_sapi_modules",
        lambda: (pythoncom, client),
    )
    speaker = SapiSpeaker()
    tracking = TrackingEvent()
    speaker.speaking = tracking

    speaker.speak("first")
    assert tracking.is_set()
    assert voice.started[0].wait(timeout=1)
    speaker.speak("second")
    voice.release[0].set()
    assert voice.started[1].wait(timeout=1)

    assert tracking.is_set()
    assert tracking.transitions == ["set"]

    voice.release[1].set()
    wait_until(lambda: not tracking.is_set())
    speaker.close()
    speaker._thread.join(timeout=1)

    assert tracking.transitions == ["set", "clear"]
    assert voice.messages == ["first", "second"]
    assert pythoncom.initialized == 1
    assert pythoncom.uninitialized == 1


class RaisingVoice:
    def Speak(self, _text: str) -> None:
        raise RuntimeError("worker failed")


@pytest.mark.parametrize("failure_point", ["import", "com", "dispatch", "worker"])
def test_sapi_failures_always_clear_speaking(monkeypatch, failure_point: str) -> None:
    pythoncom = FakePythonCom(
        initialize_error=RuntimeError("COM failed") if failure_point == "com" else None
    )
    client = FakeWin32Client(
        RaisingVoice() if failure_point == "worker" else BlockingVoice(1),
        dispatch_error=RuntimeError("dispatch failed") if failure_point == "dispatch" else None,
    )

    def load_modules():
        if failure_point == "import":
            raise ImportError("pywin32 missing")
        return pythoncom, client

    monkeypatch.setattr(feedback_module, "_load_sapi_modules", load_modules)
    speaker = SapiSpeaker()
    speaker.speak("queued")
    speaker._thread.join(timeout=1)

    assert not speaker._thread.is_alive()
    assert not speaker.speaking.is_set()
    assert speaker._pending == 0
    assert isinstance(speaker._last_error, Exception)
