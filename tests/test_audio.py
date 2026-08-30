from __future__ import annotations

import numpy as np
import pytest

from handsfree_pc.audio import (
    AudioError,
    ControlPhraseDetected,
    EndpointConfig,
    EnergyEndpointRecorder,
    LocalSpeechSession,
    MicrophoneSource,
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
