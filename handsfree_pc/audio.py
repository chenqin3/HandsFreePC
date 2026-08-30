from __future__ import annotations

import json
import math
import queue
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import SpeechSettings
from .normalize import phrase_in_text


class AudioError(RuntimeError):
    pass


class ModelMissingError(AudioError):
    pass


class TranscriptionError(AudioError):
    pass


class ControlPhraseDetected(AudioError):
    def __init__(self, phrase: str) -> None:
        super().__init__(f"Control phrase detected: {phrase}")
        self.phrase = phrase


def rms(samples: Any) -> float:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - checked by doctor
        raise AudioError("numpy is required for audio") from exc
    array = np.asarray(samples, dtype=np.float32)
    if array.size == 0:
        return 0.0
    return float(math.sqrt(float(np.mean(array * array))))


class MicrophoneSource:
    """A bounded, in-memory microphone stream. It never writes audio to disk."""

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        device: int | str | None = None,
        block_seconds: float = 0.1,
        pre_roll_seconds: float = 1.2,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self.block_size = max(160, int(sample_rate * block_seconds))
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=100)
        self._ring: deque[Any] = deque(maxlen=max(1, math.ceil(pre_roll_seconds / block_seconds)))
        self._buffer_lock = threading.Lock()
        self._stream: Any = None
        self.dropped_blocks = 0

    def __enter__(self) -> MicrophoneSource:
        try:
            import numpy as np
            import sounddevice as sd
        except ImportError as exc:
            raise AudioError("Install HandsFreePC with the audio extra") from exc

        def callback(indata: Any, frames: int, callback_time: Any, status: Any) -> None:
            del frames, callback_time
            block = np.asarray(indata[:, 0], dtype=np.float32).copy()
            with self._buffer_lock:
                self._ring.append(block)
                if status:
                    self.dropped_blocks += 1
                try:
                    self._queue.put_nowait(block)
                except queue.Full:
                    self.dropped_blocks += 1
                    with suppress(queue.Empty):
                        self._queue.get_nowait()
                    with suppress(queue.Full):
                        self._queue.put_nowait(block)

        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            device=self.device,
            channels=1,
            dtype="float32",
            blocksize=self.block_size,
            callback=callback,
        )
        self._stream.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def read(self, timeout: float = 1.0) -> Any:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise AudioError("No microphone audio arrived; check the input device") from exc

    def pre_roll(self) -> list[Any]:
        with self._buffer_lock:
            return list(self._ring)

    def drain(self) -> None:
        with self._buffer_lock:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
            self._ring.clear()


class VoskWakeDetector:
    def __init__(
        self,
        *,
        model_path: Path,
        sample_rate: int,
        phrases: list[str],
        grammar: list[str] | None = None,
    ) -> None:
        if not model_path.exists():
            raise ModelMissingError(f"Vosk wake model not found: {model_path}")
        try:
            import vosk
        except ImportError as exc:
            raise AudioError("vosk is required for the wake backend") from exc
        vosk.SetLogLevel(-1)
        self.sample_rate = sample_rate
        self.phrases = phrases
        self._np: Any = None
        grammar_json = json.dumps([*(grammar or phrases), "[unk]"], ensure_ascii=False)
        self._recognizer = vosk.KaldiRecognizer(
            vosk.Model(str(model_path)), sample_rate, grammar_json
        )

    def accept(self, samples: Any) -> str | None:
        if self._np is None:
            import numpy as np

            self._np = np
        pcm = (self._np.clip(samples, -1, 1) * 32767).astype(self._np.int16).tobytes()
        is_final = self._recognizer.AcceptWaveform(pcm)
        raw = self._recognizer.Result() if is_final else self._recognizer.PartialResult()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        text = payload.get("text") or payload.get("partial") or ""
        matched = phrase_in_text(text, self.phrases)
        if matched:
            self._recognizer.Reset()
        return matched


@dataclass(slots=True)
class EndpointConfig:
    sample_rate: int
    trailing_silence_seconds: float
    max_utterance_seconds: float
    min_speech_seconds: float
    energy_threshold: float
    noise_multiplier: float


class EnergyEndpointRecorder:
    """Adaptive energy endpointing with pre-roll; intended to be replaceable by Silero VAD."""

    def __init__(self, source: MicrophoneSource, config: EndpointConfig) -> None:
        self.source = source
        self.config = config
        self.noise_floor = config.energy_threshold / max(config.noise_multiplier, 1)

    @property
    def threshold(self) -> float:
        return max(self.config.energy_threshold, self.noise_floor * self.config.noise_multiplier)

    def observe_noise(self, block: Any) -> None:
        level = rms(block)
        if level < self.threshold:
            self.noise_floor = 0.98 * self.noise_floor + 0.02 * level

    def record(
        self,
        initial_blocks: Iterable[Any] = (),
        *,
        timeout_seconds: float | None = None,
        block_observer: Callable[[Any], None] | None = None,
    ) -> Any:
        import numpy as np

        blocks = [np.asarray(block, dtype=np.float32) for block in initial_blocks]
        speech_seconds = (
            sum(len(block) for block in blocks if rms(block) >= self.threshold)
            / self.config.sample_rate
        )
        total_seconds = sum(len(block) for block in blocks) / self.config.sample_rate
        speech_started = speech_seconds > 0
        silence_seconds = 0.0
        recording_limit = self.config.max_utterance_seconds
        if timeout_seconds is not None:
            recording_limit = min(recording_limit, max(0.0, timeout_seconds))
        while total_seconds < recording_limit:
            block = self.source.read()
            if block_observer is not None:
                block_observer(block)
            blocks.append(block)
            duration = len(block) / self.config.sample_rate
            total_seconds += duration
            level = rms(block)
            if level >= self.threshold:
                speech_started = True
                speech_seconds += duration
                silence_seconds = 0.0
            elif speech_started:
                silence_seconds += duration
            else:
                self.observe_noise(block)
            if (
                speech_started
                and speech_seconds >= self.config.min_speech_seconds
                and silence_seconds >= self.config.trailing_silence_seconds
            ):
                break
        if not blocks or speech_seconds < self.config.min_speech_seconds:
            raise AudioError("No complete utterance detected")
        return np.concatenate(blocks).astype(np.float32, copy=False)


class SileroEndpointRecorder:
    """Silero VAD v6 endpointing through sherpa-onnx's small ONNX runtime."""

    def __init__(
        self,
        source: MicrophoneSource,
        *,
        model_path: Path,
        sample_rate: int = 16000,
        threshold: float = 0.5,
        min_silence_duration: float = 0.5,
        min_speech_duration: float = 0.15,
        max_speech_duration: float = 30.0,
        window_size: int = 512,
    ) -> None:
        if not model_path.exists():
            raise ModelMissingError(f"Silero VAD model not found: {model_path}")
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise AudioError("sherpa-onnx is required for Silero VAD") from exc
        config = sherpa_onnx.VadModelConfig()
        config.silero_vad.model = str(model_path)
        config.silero_vad.threshold = threshold
        config.silero_vad.min_silence_duration = min_silence_duration
        config.silero_vad.min_speech_duration = min_speech_duration
        config.silero_vad.max_speech_duration = max_speech_duration
        config.silero_vad.window_size = window_size
        config.sample_rate = sample_rate
        config.num_threads = 1
        self.source = source
        self.sample_rate = sample_rate
        self.window_size = window_size
        self.max_speech_duration = max_speech_duration
        self._vad = sherpa_onnx.VoiceActivityDetector(
            config, buffer_size_in_seconds=max(45, int(max_speech_duration + 5))
        )

    def observe_noise(self, block: Any) -> None:
        del block

    def record(
        self,
        initial_blocks: Iterable[Any] = (),
        *,
        timeout_seconds: float | None = None,
        block_observer: Callable[[Any], None] | None = None,
    ) -> Any:
        import numpy as np

        self._vad.reset()
        pending = (
            np.concatenate(
                [np.asarray(item, dtype=np.float32).reshape(-1) for item in initial_blocks]
            )
            if initial_blocks
            else np.empty(0, dtype=np.float32)
        )
        started_at = time.monotonic()
        recording_limit = self.max_speech_duration + 2
        if timeout_seconds is not None:
            recording_limit = min(recording_limit, max(0.0, timeout_seconds))
        while time.monotonic() - started_at <= recording_limit:
            while pending.size >= self.window_size:
                self._vad.accept_waveform(pending[: self.window_size])
                pending = pending[self.window_size :]
                if not self._vad.empty():
                    segment = self._vad.front
                    samples = np.asarray(segment.samples, dtype=np.float32).copy()
                    self._vad.pop()
                    return samples
            block = self.source.read()
            if block_observer is not None:
                block_observer(block)
            pending = np.concatenate((pending, np.asarray(block, dtype=np.float32).reshape(-1)))
        if pending.size:
            padded = np.pad(pending, (0, max(0, self.window_size - pending.size)))
            self._vad.accept_waveform(padded[: self.window_size])
        self._vad.flush()
        if not self._vad.empty():
            segment = self._vad.front
            samples = np.asarray(segment.samples, dtype=np.float32).copy()
            self._vad.pop()
            return samples
        raise AudioError("No complete utterance detected")


class Transcriber:
    def transcribe(self, samples: Any, sample_rate: int) -> str:
        raise NotImplementedError


class SenseVoiceTranscriber(Transcriber):
    def __init__(
        self,
        model_path: Path,
        *,
        language: str = "auto",
        use_itn: bool = True,
        num_threads: int = 4,
        provider: str = "cpu",
    ) -> None:
        if not model_path.exists():
            raise ModelMissingError(f"SenseVoice model not found: {model_path}")
        try:
            import sherpa_onnx
        except ImportError as exc:
            raise AudioError("sherpa-onnx is required for SenseVoice") from exc
        model_file = next(
            (
                candidate
                for candidate in (
                    model_path / "model.int8.onnx",
                    model_path / "model.onnx",
                )
                if candidate.exists()
            ),
            None,
        )
        tokens = model_path / "tokens.txt"
        if model_file is None or not tokens.exists():
            raise ModelMissingError(
                "SenseVoice directory must contain model.int8.onnx "
                f"(or model.onnx) and tokens.txt: {model_path}"
            )
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_file),
            tokens=str(tokens),
            num_threads=num_threads,
            use_itn=use_itn,
            debug=False,
            provider=provider,
            language=language,
        )

    def transcribe(self, samples: Any, sample_rate: int) -> str:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(sample_rate, samples)
        self._recognizer.decode_stream(stream)
        return str(stream.result.text).strip()


class FasterWhisperTranscriber(Transcriber):
    def __init__(self, model: str, *, device: str = "auto", compute_type: str = "auto") -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise AudioError("Install HandsFreePC with the whisper extra") from exc
        self._model = WhisperModel(model, device=device, compute_type=compute_type)

    def transcribe(self, samples: Any, sample_rate: int) -> str:
        del sample_rate  # faster-whisper accepts 16 kHz float arrays directly.
        segments, _ = self._model.transcribe(
            samples,
            language="zh",
            beam_size=5,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        return "".join(segment.text for segment in segments).strip()


class FallbackTranscriber(Transcriber):
    def __init__(self, primary: Transcriber, fallback_factory: Any | None = None) -> None:
        self.primary = primary
        self.fallback_factory = fallback_factory
        self._fallback: Transcriber | None = None

    def transcribe(self, samples: Any, sample_rate: int) -> str:
        try:
            return self.primary.transcribe(samples, sample_rate)
        except Exception as exc:
            if self.fallback_factory is None:
                raise TranscriptionError(str(exc)) from exc
            if self._fallback is None:
                self._fallback = self.fallback_factory()
            return self._fallback.transcribe(samples, sample_rate)


def build_transcriber(settings: SpeechSettings, *, base_dir: Path) -> Transcriber:
    command = settings.command
    backend = str(command.get("backend", "sensevoice")).lower()
    if backend == "sensevoice":
        primary: Transcriber = SenseVoiceTranscriber(
            _resolve_model_path(str(command["model_path"]), base_dir),
            language=str(command.get("language", "auto")),
            use_itn=command.get("use_itn", True),
            num_threads=int(command.get("num_threads", 4)),
            provider=str(command.get("provider", "cpu")),
        )
    elif backend == "faster-whisper":
        primary = FasterWhisperTranscriber(
            str(command.get("model", "large-v3-turbo")),
            device=str(command.get("device", "auto")),
            compute_type=str(command.get("compute_type", "auto")),
        )
    else:
        raise AudioError(f"Unsupported command ASR backend: {backend}")

    fallback = settings.fallback
    if str(fallback.get("backend", "")).lower() != "faster-whisper":
        return primary
    return FallbackTranscriber(
        primary,
        lambda: FasterWhisperTranscriber(
            str(fallback.get("model", "large-v3-turbo")),
            device=str(fallback.get("device", "auto")),
            compute_type=str(fallback.get("compute_type", "auto")),
        ),
    )


def _resolve_model_path(value: str, base_dir: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base_dir / path).resolve()


class LocalSpeechSession:
    """High-level half-duplex speech session used by the runtime state machine."""

    def __init__(self, settings: SpeechSettings, *, base_dir: Path, phrases: list[str]) -> None:
        self.settings = settings
        self.source = MicrophoneSource(
            sample_rate=settings.sample_rate,
            device=settings.input_device,
            pre_roll_seconds=settings.pre_roll_seconds,
        )
        wake_path = _resolve_model_path(str(settings.wake["model_path"]), base_dir)
        self.wake = VoskWakeDetector(
            model_path=wake_path,
            sample_rate=settings.sample_rate,
            phrases=phrases,
            grammar=[str(item) for item in settings.wake.get("grammar", phrases)],
        )
        vad = settings.vad
        if str(vad.get("backend", "silero")).lower() == "silero":
            self.endpoint: Any = SileroEndpointRecorder(
                self.source,
                model_path=_resolve_model_path(str(vad["model_path"]), base_dir),
                sample_rate=settings.sample_rate,
                threshold=float(vad.get("threshold", 0.5)),
                min_silence_duration=float(vad.get("min_silence_duration", 0.5)),
                min_speech_duration=float(vad.get("min_speech_duration", 0.15)),
                max_speech_duration=float(vad.get("max_speech_duration", 30.0)),
                window_size=int(vad.get("window_size", 512)),
            )
        else:
            self.endpoint = EnergyEndpointRecorder(
                self.source,
                EndpointConfig(
                    sample_rate=settings.sample_rate,
                    trailing_silence_seconds=settings.trailing_silence_seconds,
                    max_utterance_seconds=settings.max_utterance_seconds,
                    min_speech_seconds=settings.min_speech_seconds,
                    energy_threshold=settings.energy_threshold,
                    noise_multiplier=settings.noise_multiplier,
                ),
            )
        self.transcriber = build_transcriber(settings, base_dir=base_dir)

    def __enter__(self) -> LocalSpeechSession:
        self.source.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.source.__exit__(exc_type, exc, traceback)

    def wait_for_phrase(self, *, stop_event: threading.Event | None = None) -> tuple[str, Any]:
        try:
            while stop_event is None or not stop_event.is_set():
                block = self.source.read()
                self.endpoint.observe_noise(block)
                if matched := self.wake.accept(block):
                    audio = self.endpoint.record(self.source.pre_roll())
                    return matched, audio
            raise AudioError("Speech session stopped")
        except AudioError:
            raise
        except Exception as exc:
            raise AudioError("Wake phrase or endpoint processing failed") from exc

    def listen_utterance(
        self,
        *,
        timeout_seconds: float | None = None,
        interrupt_phrases: Iterable[str] = (),
    ) -> Any:
        phrases = tuple(interrupt_phrases)

        def observe(block: Any) -> None:
            if (
                phrases
                and (matched := self.wake.accept(block))
                and phrase_in_text(matched, phrases)
            ):
                raise ControlPhraseDetected(matched)

        try:
            return self.endpoint.record(
                timeout_seconds=timeout_seconds,
                block_observer=observe if phrases else None,
            )
        except (AudioError, ControlPhraseDetected):
            raise
        except Exception as exc:
            raise AudioError("Endpoint processing failed") from exc

    def transcribe(self, samples: Any) -> str:
        try:
            return self.transcriber.transcribe(samples, self.settings.sample_rate)
        except AudioError:
            raise
        except Exception as exc:
            raise TranscriptionError("Local command transcription failed") from exc


def list_audio_devices() -> list[dict[str, Any]]:
    try:
        import sounddevice as sd
    except ImportError as exc:
        raise AudioError("sounddevice is not installed") from exc
    devices = sd.query_devices()
    result: list[dict[str, Any]] = []
    for index, device in enumerate(devices):
        if int(device.get("max_input_channels", 0)) > 0:
            result.append(
                {
                    "index": index,
                    "name": str(device["name"]),
                    "channels": int(device["max_input_channels"]),
                    "default_sample_rate": float(device["default_samplerate"]),
                }
            )
    return result
