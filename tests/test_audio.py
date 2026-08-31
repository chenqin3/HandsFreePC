from __future__ import annotations

import json
import sys
import types

import numpy as np
import pytest

import handsfree_pc.audio as audio_module
from handsfree_pc.audio import (
    AudioError,
    ControlPhraseDetected,
    EndpointConfig,
    EnergyEndpointRecorder,
    FeedbackPending,
    LocalSpeechSession,
    MicrophoneSource,
    PhraseDetection,
    VoskWakeDetector,
    rms,
)


def test_rms_silence_and_signal() -> None:
    assert rms(np.zeros(160, dtype=np.float32)) == 0
    assert 0.49 < rms(np.full(160, 0.5, dtype=np.float32)) < 0.51


class SilenceSource:
    def read(self):
        return np.zeros(1600, dtype=np.float32)


def test_energy_endpoint_honors_shorter_state_timeout() -> None:
    recorder = EnergyEndpointRecorder(
        SilenceSource(),
        EndpointConfig(
            sample_rate=16000,
            trailing_silence_seconds=0.5,
            max_utterance_seconds=25,
            min_speech_seconds=0.25,
            energy_threshold=0.012,
            noise_multiplier=3,
        ),
    )

    with pytest.raises(AudioError, match="No complete utterance"):
        recorder.record(timeout_seconds=0.2)


def test_energy_endpoint_observer_can_interrupt_recording() -> None:
    recorder = EnergyEndpointRecorder(
        SilenceSource(),
        EndpointConfig(
            sample_rate=16000,
            trailing_silence_seconds=0.5,
            max_utterance_seconds=25,
            min_speech_seconds=0.25,
            energy_threshold=0.012,
            noise_multiplier=3,
        ),
    )

    def interrupt(_block) -> None:
        raise RuntimeError("stop")

    with pytest.raises(RuntimeError, match="stop"):
        recorder.record(timeout_seconds=1, block_observer=interrupt)


def test_local_speech_session_raises_on_stop_kws_during_utterance() -> None:
    class FakeEndpoint:
        def record(self, *, timeout_seconds, block_observer):
            assert timeout_seconds == 2
            block_observer(np.zeros(1600, dtype=np.float32))
            raise AssertionError("observer should interrupt")

    class FakeWake:
        def accept(self, _block):
            return "电脑停止"

    session = object.__new__(LocalSpeechSession)
    session.endpoint = FakeEndpoint()
    session.wake = FakeWake()

    with pytest.raises(ControlPhraseDetected, match="电脑停止"):
        session.listen_utterance(timeout_seconds=2, interrupt_phrases=["电脑停止"])


def test_local_speech_session_keeps_audio_when_prompt_marker_is_detected() -> None:
    expected_audio = np.ones(1600, dtype=np.float32)
    observed_block = np.arange(1600, dtype=np.float32)
    detector_blocks = []

    class FakeEndpoint:
        def record(self, *, timeout_seconds, block_observer):
            assert timeout_seconds == 2
            block_observer(observed_block)
            return expected_audio

    class FakeWake:
        def accept(self, block):
            detector_blocks.append(block)
            return None

    class FakeMarker:
        samples_seen = 0

        def accept_detection(self, block):
            detector_blocks.append(block)
            return PhraseDetection("over", 600, 1000)

    session = object.__new__(LocalSpeechSession)
    session.endpoint = FakeEndpoint()
    session.wake = FakeWake()
    session.marker = FakeMarker()

    audio = session.listen_utterance(
        timeout_seconds=2,
        interrupt_phrases=["电脑停止"],
        marker_phrases=["over"],
    )

    assert audio is expected_audio
    assert session.last_marker_phrase == "over"
    assert session.last_marker_events == (PhraseDetection("over", 600, 1000),)
    assert len(detector_blocks) == 2
    assert detector_blocks[0] is observed_block
    assert detector_blocks[1] is observed_block
    assert np.array_equal(session.last_marker_audio_segments[0], observed_block[:600])
    assert np.array_equal(session.last_marker_audio_segments[1], observed_block[1000:])


def test_local_speech_session_includes_marker_emitted_only_at_endpoint_flush() -> None:
    observed_block = np.arange(1600, dtype=np.float32)

    class FakeEndpoint:
        def record(self, *, timeout_seconds, block_observer):
            assert timeout_seconds == 2
            block_observer(observed_block)
            return observed_block

    class FakeWake:
        def accept(self, _block):
            return None

    class FinalOnlyMarker:
        samples_seen = 0

        def accept_detection(self, _block):
            return None

        def finalize_detection(self):
            return PhraseDetection("over", 500, 900)

    session = object.__new__(LocalSpeechSession)
    session.endpoint = FakeEndpoint()
    session.wake = FakeWake()
    session.marker = FinalOnlyMarker()

    session.listen_utterance(timeout_seconds=2, marker_phrases=["over"])

    assert session.last_marker_events == (PhraseDetection("over", 500, 900),)
    assert np.array_equal(session.last_marker_audio_segments[0], observed_block[:500])
    assert np.array_equal(session.last_marker_audio_segments[1], observed_block[900:])


def test_local_session_builds_independent_control_and_delimiter_detectors(
    settings, tmp_path, monkeypatch
) -> None:
    detectors = []

    class FakeDetector:
        def __init__(self, **kwargs):
            detectors.append(kwargs)

    monkeypatch.setattr(audio_module, "VoskWakeDetector", FakeDetector)
    monkeypatch.setattr(audio_module, "build_transcriber", lambda *_args, **_kwargs: object())
    settings.speech.vad["backend"] = "energy"

    LocalSpeechSession(
        settings.speech,
        base_dir=tmp_path,
        phrases=["开始语音操作"],
        marker_phrases=["over"],
    )

    assert len(detectors) == 2
    assert detectors[0]["phrases"] == ["开始语音操作"]
    assert detectors[0]["model_path"].name == "vosk-model-small-cn-0.22"
    assert detectors[1]["phrases"] == ["over"]
    assert detectors[1]["model_path"].name == "vosk-model-small-en-us-0.15"


def test_microphone_drain_clears_queue_and_pre_roll() -> None:
    source = MicrophoneSource(pre_roll_seconds=1.2, block_seconds=0.1)
    queued = np.full(160, 0.25, dtype=np.float32)
    pre_roll = np.full(160, 0.5, dtype=np.float32)
    source._queue.put_nowait(queued)
    source._ring.append(pre_roll)

    source.drain()

    assert source._queue.empty()
    assert source.pre_roll() == []


def test_local_session_wraps_native_transcriber_failure() -> None:
    class FailingTranscriber:
        def transcribe(self, _samples, _sample_rate):
            raise RuntimeError("native failure")

    session = object.__new__(LocalSpeechSession)
    session.settings = type("Settings", (), {"sample_rate": 16000})()
    session.transcriber = FailingTranscriber()

    with pytest.raises(AudioError, match="Local command transcription failed"):
        session.transcribe(np.zeros(160, dtype=np.float32))


def test_local_session_wraps_native_endpoint_failure() -> None:
    class FailingEndpoint:
        def record(self, **_kwargs):
            raise RuntimeError("native VAD failure")

    session = object.__new__(LocalSpeechSession)
    session.endpoint = FailingEndpoint()
    session.wake = object()

    with pytest.raises(AudioError, match="Endpoint processing failed"):
        session.listen_utterance(timeout_seconds=1)


def test_wait_for_phrase_yields_to_pending_spoken_feedback() -> None:
    class Source:
        def read(self):
            raise AssertionError("microphone must not be read while feedback is pending")

    session = object.__new__(LocalSpeechSession)
    session.source = Source()
    pending = __import__("threading").Event()
    pending.set()

    with pytest.raises(FeedbackPending):
        session.wait_for_phrase(feedback_event=pending)


class _FakeVoskRecognizer:
    def __init__(self, events):
        self.events = iter(events)
        self.current = None
        self.reset_count = 0

    def AcceptWaveform(self, _pcm):  # noqa: N802 - mirrors the Vosk API
        self.current = next(self.events)
        return self.current[0]

    def Result(self):  # noqa: N802 - mirrors the Vosk API
        if len(self.current) > 2:
            return json.dumps(self.current[2], ensure_ascii=False)
        return json.dumps({"text": self.current[1]}, ensure_ascii=False)

    def PartialResult(self):  # noqa: N802 - mirrors the Vosk API
        if len(self.current) > 2:
            return json.dumps(self.current[2], ensure_ascii=False)
        return json.dumps({"partial": self.current[1]}, ensure_ascii=False)

    def FinalResult(self):  # noqa: N802 - mirrors the Vosk API
        return json.dumps({"text": ""})

    def Reset(self):  # noqa: N802 - mirrors the Vosk API
        self.reset_count += 1


def _wake_detector(monkeypatch, events, times):
    recognizer = _FakeVoskRecognizer(events)
    fake_vosk = types.SimpleNamespace(
        SetLogLevel=lambda _level: None,
        Model=lambda _path: object(),
        KaldiRecognizer=lambda *_args: recognizer,
    )
    monkeypatch.setitem(sys.modules, "vosk", fake_vosk)
    clock = iter(times)
    detector = VoskWakeDetector(
        model_path=__file_path(),
        sample_rate=16000,
        phrases=["开始语音操作"],
        phrase_window_seconds=5.0,
        monotonic=lambda: next(clock),
    )
    return detector, recognizer


def __file_path():
    from pathlib import Path

    return Path(__file__)


def test_vosk_wake_accumulates_slow_final_chunks(monkeypatch) -> None:
    detector, recognizer = _wake_detector(
        monkeypatch,
        [(True, "开始"), (True, "语音"), (True, "操作")],
        [0.0, 1.5, 3.0],
    )
    block = np.zeros(160, dtype=np.float32)

    assert detector.accept(block) is None
    assert detector.accept(block) is None
    assert detector.accept(block) == "开始语音操作"
    assert recognizer.reset_count == 1


def test_vosk_wake_does_not_join_chunks_outside_window(monkeypatch) -> None:
    detector, _ = _wake_detector(
        monkeypatch,
        [(True, "开始"), (True, "语音"), (True, "操作")],
        [0.0, 6.0, 12.0],
    )
    block = np.zeros(160, dtype=np.float32)

    assert detector.accept(block) is None
    assert detector.accept(block) is None
    assert detector.accept(block) is None


def test_vosk_reset_clears_rolling_slow_phrase_state(monkeypatch) -> None:
    detector, recognizer = _wake_detector(
        monkeypatch,
        [(True, "开始"), (True, "语音操作")],
        [0.0, 1.0],
    )
    block = np.zeros(160, dtype=np.float32)

    assert detector.accept(block) is None
    detector.reset()
    assert detector.accept(block) is None
    assert recognizer.reset_count == 1


def test_vosk_grammar_unions_configured_and_runtime_control_phrases(monkeypatch) -> None:
    captured: list[list[str]] = []

    def recognizer_factory(_model, _sample_rate, grammar_json):
        captured.append(json.loads(grammar_json))
        return _FakeVoskRecognizer([])

    fake_vosk = types.SimpleNamespace(
        SetLogLevel=lambda _level: None,
        Model=lambda _path: object(),
        KaldiRecognizer=recognizer_factory,
    )
    monkeypatch.setitem(sys.modules, "vosk", fake_vosk)

    VoskWakeDetector(
        model_path=__file_path(),
        sample_rate=16000,
        phrases=["开始语音操作", "over"],
        grammar=["开始 语音 操作"],
    )

    assert captured == [["开始 语音 操作", "开始语音操作", "over", "[unk]"]]


def test_vosk_detection_carries_monotonic_word_sample_boundaries(monkeypatch) -> None:
    events = [
        (
            False,
            "over",
            {
                "partial": "over",
                "partial_result": [{"word": "over", "start": 0.1, "end": 0.36}],
            },
        ),
        (
            False,
            "over",
            {
                "partial": "over",
                "partial_result": [{"word": "over", "start": 0.0, "end": 0.25}],
            },
        ),
    ]
    recognizer = _FakeVoskRecognizer(events)
    fake_vosk = types.SimpleNamespace(
        SetLogLevel=lambda _level: None,
        Model=lambda _path: object(),
        KaldiRecognizer=lambda *_args: recognizer,
    )
    monkeypatch.setitem(sys.modules, "vosk", fake_vosk)
    detector = VoskWakeDetector(
        model_path=__file_path(),
        sample_rate=16000,
        phrases=["over"],
    )
    block = np.zeros(8000, dtype=np.float32)

    assert detector.accept_detection(block) == PhraseDetection("over", 1600, 5760)
    assert detector.accept_detection(block) == PhraseDetection("over", 8000, 12000)
    assert detector.samples_seen == 16000
    assert recognizer.reset_count == 2


def test_vosk_final_result_flushes_a_marker_with_absolute_sample_boundaries(
    monkeypatch,
) -> None:
    class FinalOnlyRecognizer(_FakeVoskRecognizer):
        def FinalResult(self):  # noqa: N802 - mirrors the Vosk API
            return json.dumps(
                {
                    "text": "over",
                    "result": [{"word": "over", "start": 0.09, "end": 0.57}],
                }
            )

    recognizer = FinalOnlyRecognizer([(False, "")])
    fake_vosk = types.SimpleNamespace(
        SetLogLevel=lambda _level: None,
        Model=lambda _path: object(),
        KaldiRecognizer=lambda *_args: recognizer,
    )
    monkeypatch.setitem(sys.modules, "vosk", fake_vosk)
    detector = VoskWakeDetector(
        model_path=__file_path(),
        sample_rate=16000,
        phrases=["over"],
    )

    assert detector.accept_detection(np.zeros(16000, dtype=np.float32)) is None
    assert detector.finalize_detection() == PhraseDetection("over", 1440, 9120)
    assert detector.samples_seen == 16000
    assert detector.last_detection == PhraseDetection("over", 1440, 9120)
    assert recognizer.reset_count == 1
