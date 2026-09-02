from __future__ import annotations

import itertools

from handsfree_pc.mic_guard import MicrophoneGuard, MicrophoneUse, _filetime_to_unix


def _uses(*items: tuple[str, float, float | None]) -> list[MicrophoneUse]:
    return [MicrophoneUse(label, start, stop) for label, start, stop in items]


def test_filetime_conversion_matches_the_unix_epoch() -> None:
    assert _filetime_to_unix(116444736000000000) == 0.0
    assert _filetime_to_unix(0) is None
    assert _filetime_to_unix("x") is None


def test_guard_reports_another_app_that_is_capturing_now() -> None:
    now = 1_000_000.0
    guard = MicrophoneGuard(
        reader=lambda: _uses(
            ("C:\\Program Files\\Zoom\\Zoom.exe", now - 30, None),
            ("C:\\Program Files\\Chrome\\chrome.exe", now - 3600, now - 3500),
        ),
        clock=lambda: now,
        own_executables=[],
    )

    assert guard.busy_apps() == ["Zoom.exe"]
    assert guard.busy_app() == "Zoom.exe"


def test_guard_ignores_its_own_interpreter_and_configured_names() -> None:
    now = 1_000_000.0
    guard = MicrophoneGuard(
        reader=lambda: _uses(
            ("C:\\Users\\me\\HandsFreePC\\.venv\\Scripts\\python.exe", now - 5, None),
            ("C:\\Program Files (x86)\\SogouInput\\SGSmartAssistant.exe", now - 5, None),
        ),
        clock=lambda: now,
        own_executables=["C:/Users/me/HandsFreePC/.venv/Scripts/python.exe"],
        ignore=["SGSmartAssistant.exe"],
    )

    assert guard.busy_apps() == []
    assert guard.busy_app() is None


def test_guard_forgets_an_ancient_unterminated_use() -> None:
    now = 1_000_000.0
    guard = MicrophoneGuard(
        reader=lambda: _uses(("Zoom.exe", now - 3 * 24 * 3600, None)),
        clock=lambda: now,
        own_executables=[],
        recent_hours=12,
    )

    assert guard.busy_apps() == []


def test_guard_caches_between_polls_and_can_be_disabled() -> None:
    now = 1_000_000.0
    calls = itertools.count()
    ticks = iter([0.0, 1.0, 5.0])

    def reader():
        next(calls)
        return _uses(("MicrosoftTeams", now - 5, None))

    guard = MicrophoneGuard(
        reader=reader,
        clock=lambda: now,
        monotonic=lambda: next(ticks),
        poll_seconds=3.0,
        own_executables=[],
    )

    assert guard.busy_app() == "MicrosoftTeams"
    assert guard.busy_app() == "MicrosoftTeams"  # 1.0 s later: cached
    assert guard.busy_app() == "MicrosoftTeams"  # 5.0 s later: re-read
    assert next(calls) == 2

    disabled = MicrophoneGuard(enabled=False, reader=reader, own_executables=[])
    assert disabled.busy_app() is None


def test_speech_session_pause_and_resume_touch_only_the_source() -> None:
    from handsfree_pc.audio import LocalSpeechSession

    class FakeSource:
        def __init__(self) -> None:
            self.events: list[str] = []

        def __enter__(self):
            self.events.append("open")
            return self

        def __exit__(self, *_args):
            self.events.append("close")

    session = LocalSpeechSession.__new__(LocalSpeechSession)
    session.source = FakeSource()  # type: ignore[assignment]

    session.pause_microphone()
    session.pause_microphone()
    assert session.microphone_paused is True
    session.resume_microphone()
    session.resume_microphone()
    assert session.microphone_paused is False
    assert session.source.events == ["close", "open"]
    # A paused session does not close the device twice on exit.
    session.pause_microphone()
    session.__exit__(None, None, None)
    assert session.source.events == ["close", "open", "close"]


def test_microphone_source_read_aborts_when_the_guard_interrupts() -> None:
    import queue

    from handsfree_pc.audio import AudioError, MicrophoneSource

    source = MicrophoneSource.__new__(MicrophoneSource)
    source._queue = queue.Queue()  # type: ignore[attr-defined]
    source._queue.put(b"block")  # type: ignore[attr-defined]
    source.interrupt_check = lambda: True

    try:
        source.read(timeout=0.01)
    except AudioError as exc:
        assert "microphone guard" in str(exc)
    else:  # pragma: no cover - the read must not succeed
        raise AssertionError("read did not abort")

    source.interrupt_check = None
    assert source.read(timeout=0.01) == b"block"


def test_default_own_executables_include_the_base_interpreter(monkeypatch) -> None:
    import sys

    monkeypatch.setattr(sys, "executable", r"C:\proj\.venv\Scripts\python.exe")
    monkeypatch.setattr(sys, "_base_executable", r"C:\Python312\python.exe", raising=False)
    guard = MicrophoneGuard(reader=lambda: [], clock=lambda: 0.0)

    assert guard._ignored(r"C:\Python312\python.exe")
    assert guard._ignored(r"C:\Python312\pythonw.exe")
    assert guard._ignored(r"C:\proj\.venv\Scripts\python.exe")
    assert not guard._ignored(r"C:\Program Files\Zoom\Zoom.exe")
