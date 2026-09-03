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
from .normalize import compact_text, phrase_in_text

# The ring buffer keeps this much audio so the wake transcript can start just
# before the detected phrase instead of at its tail.
_WAKE_HISTORY_SECONDS = 4.0
_WAKE_HISTORY_MARGIN_SECONDS = 0.25


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


class FeedbackPending(AudioError):
    """Wake the microphone loop so queued spoken feedback can play at a safe boundary."""


@dataclass(frozen=True, slots=True)
class PhraseDetection:
    """A local KWS match tied to one monotonic microphone-sample interval."""

    phrase: str
    start_sample: int
    end_sample: int


@dataclass(frozen=True, slots=True)
class _TimedToken:
    text: str
    start_sample: int
    end_sample: int


def rms(samples: Any) -> float:
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - checked by doctor
        raise AudioError("numpy is required for audio") from exc
    array = np.asarray(samples, dtype=np.float32)
    if array.size == 0:
        return 0.0
    return float(math.sqrt(float(np.mean(array * array))))


def has_transcribable_energy(
    samples: Any,
    *,
    sample_rate: int,
    energy_threshold: float,
    min_speech_seconds: float,
) -> bool:
    """Reject obvious silence without acting as a second full speech detector.

    Marker splitting can leave a non-empty suffix containing only endpoint padding.
    Neural ASR models may hallucinate a short utterance for that padding, so check
    short-window energy before transcription.  The gate intentionally uses only a
    small fraction of the endpoint threshold and caps its duration requirement to
    preserve quiet speech and short words.
    """
    try:
        import numpy as np
    except ImportError as exc:  # pragma: no cover - checked by doctor
        raise AudioError("numpy is required for audio") from exc

    array = np.asarray(samples, dtype=np.float32).reshape(-1)
    if array.size == 0 or sample_rate <= 0:
        return False

    frame_size = max(1, int(round(sample_rate * 0.02)))
    # The endpoint threshold is tuned for deciding when an utterance starts, which
    # is too strict for a post-marker fragment.  Five percent still separates the
    # observed near-zero padding from genuinely quiet microphone speech.
    frame_threshold = max(float(energy_threshold) * 0.05, 1e-5)
    required_seconds = min(max(float(min_speech_seconds), 0.0), 0.06)
    required_frames = max(1, math.ceil(required_seconds * sample_rate / frame_size))
    active_frames = 0
    for start in range(0, int(array.size), frame_size):
        frame = array[start : start + frame_size]
        if rms(frame) < frame_threshold:
            continue
        active_frames += 1
        if active_frames >= required_frames:
            return True
    return False


class MicrophoneSource:
    """A bounded, in-memory microphone stream. It never writes audio to disk."""

    # Optional hook consulted on every block read; returning True aborts the
    # current listen so the runtime can act (e.g. release the mic for a meeting).
    interrupt_check: Any = None

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        device: int | str | None = None,
        block_seconds: float = 0.1,
        pre_roll_seconds: float = 1.2,
        history_seconds: float | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self.block_size = max(160, int(sample_rate * block_seconds))
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=100)
        # The ring always holds the pre-roll; a longer history lets the wake path
        # hand the transcriber the whole detected phrase, not just its tail.
        self.pre_roll_blocks = max(1, math.ceil(pre_roll_seconds / block_seconds))
        history_blocks = max(
            self.pre_roll_blocks, math.ceil((history_seconds or 0.0) / block_seconds)
        )
        self._ring: deque[Any] = deque(maxlen=history_blocks)
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
        check = self.interrupt_check
        if check is not None and check():
            raise AudioError("No complete utterance detected: microphone guard interrupt")
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty as exc:
            raise AudioError("No microphone audio arrived; check the input device") from exc

    def pre_roll(self, *, samples: int | None = None) -> list[Any]:
        """The newest audio: the configured pre-roll, or at least ``samples`` samples."""

        with self._buffer_lock:
            blocks = list(self._ring)
        wanted = self.pre_roll_blocks
        if samples is not None:
            wanted = max(wanted, math.ceil(max(0, int(samples)) / self.block_size))
        return blocks[-wanted:]

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
        phrase_window_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
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
        self.phrase_window_seconds = phrase_window_seconds
        self._monotonic = monotonic
        self._rolling_finals: list[tuple[float, str, tuple[_TimedToken, ...]]] = []
        self._np: Any = None
        self._samples_seen = 0
        self._recognizer_base_sample = 0
        self.last_detection: PhraseDetection | None = None
        grammar_items: list[str] = []
        seen_grammar: set[str] = set()
        seen_compact_grammar: set[str] = set()
        for raw_phrase in [*(grammar or ()), *phrases]:
            phrase = str(raw_phrase).strip()
            key = phrase.casefold()
            compact_key = compact_text(phrase).casefold()
            if phrase and key not in seen_grammar and compact_key not in seen_compact_grammar:
                grammar_items.append(phrase)
                seen_grammar.add(key)
                seen_compact_grammar.add(compact_key)
        grammar_json = json.dumps([*grammar_items, "[unk]"], ensure_ascii=False)
        self._recognizer = vosk.KaldiRecognizer(
            vosk.Model(str(model_path)), sample_rate, grammar_json
        )
        # Word intervals let the delimiter path separate audio before and after
        # the keyword.  Older/fake recognizers remain usable through the
        # block-interval fallback in ``accept_detection``.
        if hasattr(self._recognizer, "SetWords"):
            self._recognizer.SetWords(True)
        if hasattr(self._recognizer, "SetPartialWords"):
            self._recognizer.SetPartialWords(True)

    @property
    def samples_seen(self) -> int:
        return self._samples_seen

    def accept(self, samples: Any) -> str | None:
        detection = self.accept_detection(samples)
        return detection.phrase if detection is not None else None

    def accept_detection(self, samples: Any) -> PhraseDetection | None:
        if self._np is None:
            import numpy as np

            self._np = np
        array = self._np.asarray(samples, dtype=self._np.float32).reshape(-1)
        block_start = self._samples_seen
        block_end = block_start + int(array.size)
        self._samples_seen = block_end
        pcm = (self._np.clip(array, -1, 1) * 32767).astype(self._np.int16).tobytes()
        is_final = self._recognizer.AcceptWaveform(pcm)
        raw = self._recognizer.Result() if is_final else self._recognizer.PartialResult()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return self._detection_from_payload(
            payload,
            is_final=bool(is_final),
            block_start=block_start,
            block_end=block_end,
        )

    def finalize_detection(self) -> PhraseDetection | None:
        """Flush a phrase that Vosk only emits after the current endpoint closes."""

        try:
            payload = json.loads(self._recognizer.FinalResult())
        except (AttributeError, json.JSONDecodeError):
            self.reset()
            return None
        detection = self._detection_from_payload(
            payload,
            is_final=True,
            block_start=self._recognizer_base_sample,
            block_end=self._samples_seen,
        )
        if detection is None:
            self.reset()
        return detection

    def _detection_from_payload(
        self,
        payload: dict[str, Any],
        *,
        is_final: bool,
        block_start: int,
        block_end: int,
    ) -> PhraseDetection | None:
        text = payload.get("text") or payload.get("partial") or ""
        token_key = "result" if is_final else "partial_result"
        tokens = self._timed_tokens(
            payload.get(token_key),
            fallback_text=str(text),
            block_start=block_start,
            block_end=block_end,
        )
        now = self._monotonic()
        cutoff = now - self.phrase_window_seconds
        self._rolling_finals = [
            (seen_at, value, final_tokens)
            for seen_at, value, final_tokens in self._rolling_finals
            if seen_at >= cutoff
        ]
        if is_final and text:
            self._rolling_finals.append((now, str(text), tokens))
            partial = ""
            partial_tokens: tuple[_TimedToken, ...] = ()
        else:
            partial = str(text)
            partial_tokens = tokens
        rolling_text = " ".join([*(value for _, value, _ in self._rolling_finals), partial]).strip()
        matched = phrase_in_text(rolling_text, self.phrases)
        if matched:
            rolling_tokens = (
                tuple(
                    token for _, _, final_tokens in self._rolling_finals for token in final_tokens
                )
                + partial_tokens
            )
            span = self._phrase_sample_span(rolling_tokens, matched)
            if span is None:
                span = (block_start, block_end)
            detection = PhraseDetection(matched, span[0], span[1])
            self.reset()
            self.last_detection = detection
            return detection
        return None

    def _timed_tokens(
        self,
        raw_tokens: Any,
        *,
        fallback_text: str,
        block_start: int,
        block_end: int,
    ) -> tuple[_TimedToken, ...]:
        result: list[_TimedToken] = []
        if isinstance(raw_tokens, list):
            for item in raw_tokens:
                if not isinstance(item, dict):
                    continue
                word = str(item.get("word", "")).strip()
                try:
                    start = self._recognizer_base_sample + round(
                        float(item["start"]) * self.sample_rate
                    )
                    end = self._recognizer_base_sample + round(
                        float(item["end"]) * self.sample_rate
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if word and end > start:
                    result.append(_TimedToken(word, max(0, start), max(0, end)))
        if not result and fallback_text:
            result.append(_TimedToken(fallback_text, block_start, block_end))
        return tuple(result)

    @staticmethod
    def _phrase_sample_span(tokens: tuple[_TimedToken, ...], phrase: str) -> tuple[int, int] | None:
        compact_parts: list[str] = []
        token_by_character: list[int] = []
        for index, token in enumerate(tokens):
            value = compact_text(token.text)
            compact_parts.append(value)
            token_by_character.extend([index] * len(value))
        haystack = "".join(compact_parts)
        needle = compact_text(phrase)
        offset = haystack.find(needle)
        if offset < 0 or not needle:
            return None
        first = tokens[token_by_character[offset]]
        last = tokens[token_by_character[offset + len(needle) - 1]]
        return first.start_sample, last.end_sample

    def reset(self) -> None:
        self._recognizer.Reset()
        self._rolling_finals.clear()
        self._recognizer_base_sample = self._samples_seen
        self.last_detection = None


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
    def __init__(
        self,
        model: str,
        *,
        device: str = "auto",
        compute_type: str = "auto",
        language: str | None = "zh",
        beam_size: int = 5,
        initial_prompt: str | None = None,
        hotwords: str | None = None,
    ) -> None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise AudioError("Install HandsFreePC with the whisper extra") from exc
        self._model = WhisperModel(model, device=device, compute_type=compute_type)
        normalized_language = str(language or "").strip().lower()
        self._language = None if normalized_language in {"", "auto", "none"} else language
        self._beam_size = beam_size
        self._initial_prompt = initial_prompt or None
        self._hotwords = hotwords or None

    def transcribe(self, samples: Any, sample_rate: int) -> str:
        del sample_rate  # faster-whisper accepts 16 kHz float arrays directly.
        segments, _ = self._model.transcribe(
            samples,
            language=self._language,
            beam_size=self._beam_size,
            vad_filter=False,
            condition_on_previous_text=False,
            initial_prompt=self._initial_prompt,
            hotwords=self._hotwords,
        )
        return "".join(segment.text for segment in segments).strip()


def _whisper_text_option(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        text = " ".join(str(item).strip() for item in value if str(item).strip())
    else:
        text = str(value).strip()
    return text or None


def _build_faster_whisper(options: dict[str, Any]) -> FasterWhisperTranscriber:
    return FasterWhisperTranscriber(
        str(options.get("model", "large-v3-turbo")),
        device=str(options.get("device", "auto")),
        compute_type=str(options.get("compute_type", "auto")),
        language=options.get("language", "zh"),
        beam_size=int(options.get("beam_size", 5)),
        initial_prompt=_whisper_text_option(options.get("initial_prompt")),
        hotwords=_whisper_text_option(options.get("hotwords")),
    )


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
        primary = _build_faster_whisper(command)
    else:
        raise AudioError(f"Unsupported command ASR backend: {backend}")

    fallback = settings.fallback
    if str(fallback.get("backend", "")).lower() != "faster-whisper":
        return primary
    return FallbackTranscriber(
        primary,
        lambda: _build_faster_whisper(fallback),
    )


def _resolve_model_path(value: str, base_dir: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base_dir / path).resolve()


class LocalSpeechSession:
    """High-level half-duplex speech session used by the runtime state machine."""

    def __init__(
        self,
        settings: SpeechSettings,
        *,
        base_dir: Path,
        phrases: list[str],
        marker_phrases: list[str] | None = None,
    ) -> None:
        self.settings = settings
        self.source = MicrophoneSource(
            sample_rate=settings.sample_rate,
            device=settings.input_device,
            pre_roll_seconds=settings.pre_roll_seconds,
            history_seconds=_WAKE_HISTORY_SECONDS,
        )
        wake_path = _resolve_model_path(str(settings.wake["model_path"]), base_dir)
        self.wake = VoskWakeDetector(
            model_path=wake_path,
            sample_rate=settings.sample_rate,
            phrases=phrases,
            grammar=[str(item) for item in settings.wake.get("grammar", phrases)],
            phrase_window_seconds=float(settings.wake.get("phrase_window_seconds", 5.0)),
        )
        marker_values = list(marker_phrases or ())
        delimiter = settings.delimiter
        self.marker = (
            VoskWakeDetector(
                model_path=_resolve_model_path(str(delimiter["model_path"]), base_dir),
                sample_rate=settings.sample_rate,
                phrases=marker_values,
                grammar=[str(item) for item in delimiter.get("grammar", marker_values)],
                phrase_window_seconds=float(delimiter.get("phrase_window_seconds", 2.0)),
            )
            if marker_values
            else None
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
        self.last_marker_phrase: str | None = None
        self.last_marker_events: tuple[PhraseDetection, ...] = ()
        self.last_marker_audio_segments: tuple[Any, ...] = ()
        self.last_marker_segment_transcribed: tuple[bool, ...] = ()

    def __enter__(self) -> LocalSpeechSession:
        self.source.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if getattr(self, "_microphone_paused", False):
            return
        self.source.__exit__(exc_type, exc, traceback)

    def pause_microphone(self) -> None:
        """Release the capture device but keep every loaded model in memory."""

        if getattr(self, "_microphone_paused", False):
            return
        self.source.__exit__(None, None, None)
        self._microphone_paused = True

    def resume_microphone(self) -> None:
        """Re-open the capture device after ``pause_microphone``."""

        if not getattr(self, "_microphone_paused", False):
            return
        self.source.__enter__()
        self._microphone_paused = False

    @property
    def microphone_paused(self) -> bool:
        return bool(getattr(self, "_microphone_paused", False))

    def wait_for_phrase(
        self,
        *,
        stop_event: threading.Event | None = None,
        feedback_event: threading.Event | None = None,
    ) -> tuple[str, Any]:
        try:
            while stop_event is None or not stop_event.is_set():
                if feedback_event is not None and feedback_event.is_set():
                    raise FeedbackPending("Spoken feedback is pending")
                block = self.source.read()
                if feedback_event is not None and feedback_event.is_set():
                    raise FeedbackPending("Spoken feedback is pending")
                self.endpoint.observe_noise(block)
                detection = self._detect_control_phrase(block)
                if detection is None:
                    continue
                matched, history = detection
                pre_roll = (
                    self.source.pre_roll(samples=history)
                    if history is not None
                    else self.source.pre_roll()
                )
                audio = self.endpoint.record(pre_roll)
                return matched, audio
            raise AudioError("Speech session stopped")
        except AudioError:
            raise
        except Exception as exc:
            raise AudioError("Wake phrase or endpoint processing failed") from exc

    def _detect_control_phrase(self, block: Any) -> tuple[str, int | None] | None:
        """Run the spotter; also report how far back the phrase started, in samples."""

        accept_detection = getattr(self.wake, "accept_detection", None)
        if not callable(accept_detection):
            matched = self.wake.accept(block)
            return (matched, None) if matched else None
        detection = accept_detection(block)
        if detection is None:
            return None
        samples_seen = getattr(self.wake, "samples_seen", None)
        if not isinstance(samples_seen, int):
            return detection.phrase, None
        margin = int(_WAKE_HISTORY_MARGIN_SECONDS * self.settings.sample_rate)
        return detection.phrase, max(0, samples_seen - detection.start_sample) + margin

    def listen_utterance(
        self,
        *,
        timeout_seconds: float | None = None,
        interrupt_phrases: Iterable[str] = (),
        marker_phrases: Iterable[str] = (),
    ) -> Any:
        interrupts = tuple(interrupt_phrases)
        markers = tuple(marker_phrases)
        self.last_marker_phrase = None
        self.last_marker_events = ()
        self.last_marker_audio_segments = ()
        self.last_marker_segment_transcribed = ()
        marker_detector = getattr(self, "marker", None)
        if markers and marker_detector is None:
            raise AudioError("Prompt delimiter detector is not initialized")
        captured_blocks: list[Any] = []
        marker_events: list[PhraseDetection] = []
        capture_origin = int(getattr(marker_detector, "samples_seen", 0))
        captured_sample_count = 0

        def observe(block: Any) -> None:
            nonlocal captured_sample_count
            block_size = int(getattr(block, "size", len(block)))
            block_start = capture_origin + captured_sample_count
            block_end = block_start + block_size
            captured_sample_count += block_size
            captured_blocks.append(block)
            if (
                interrupts
                and (matched := self.wake.accept(block))
                and phrase_in_text(matched, interrupts)
            ):
                raise ControlPhraseDetected(matched)
            if markers and marker_detector is not None:
                if hasattr(marker_detector, "accept_detection"):
                    detection = marker_detector.accept_detection(block)
                else:  # Compatibility with simple injected/test detectors.
                    marker = marker_detector.accept(block)
                    detection = PhraseDetection(marker, block_start, block_end) if marker else None
                if detection is None or not phrase_in_text(detection.phrase, list(markers)):
                    return
                # A prompt delimiter is a marker, not an emergency interrupt.
                # Continue recording so command audio before the marker is not lost.
                marker_events.append(detection)

        try:
            audio = self.endpoint.record(
                timeout_seconds=timeout_seconds,
                block_observer=observe if interrupts or markers else None,
            )
            if (
                markers
                and marker_detector is not None
                and hasattr(marker_detector, "finalize_detection")
            ):
                final_detection = marker_detector.finalize_detection()
                if final_detection is not None and phrase_in_text(
                    final_detection.phrase, list(markers)
                ):
                    marker_events.append(final_detection)
            if marker_events:
                self._store_marker_capture(
                    captured_blocks,
                    marker_events,
                    capture_origin=capture_origin,
                )
            return audio
        except (AudioError, ControlPhraseDetected):
            raise
        except Exception as exc:
            raise AudioError("Endpoint processing failed") from exc

    def _store_marker_capture(
        self,
        captured_blocks: list[Any],
        marker_events: list[PhraseDetection],
        *,
        capture_origin: int,
    ) -> None:
        import numpy as np

        captured = (
            np.concatenate(
                [np.asarray(block, dtype=np.float32).reshape(-1) for block in captured_blocks]
            )
            if captured_blocks
            else np.empty(0, dtype=np.float32)
        )
        ordered = sorted(marker_events, key=lambda item: (item.start_sample, item.end_sample))
        segments: list[Any] = []
        cursor = 0
        total_samples = int(captured.size)
        retained_events: list[PhraseDetection] = []
        for event in ordered:
            marker_start = min(
                total_samples,
                max(cursor, event.start_sample - capture_origin),
            )
            marker_end = min(
                total_samples,
                max(marker_start, event.end_sample - capture_origin),
            )
            if marker_end <= cursor:
                continue
            segments.append(captured[cursor:marker_start].copy())
            cursor = marker_end
            retained_events.append(event)
        if not retained_events:
            return
        segments.append(captured[cursor:].copy())
        self.last_marker_events = tuple(retained_events)
        self.last_marker_phrase = retained_events[-1].phrase
        self.last_marker_audio_segments = tuple(segments)

    def transcribe(self, samples: Any) -> str:
        try:
            return self.transcriber.transcribe(samples, self.settings.sample_rate)
        except AudioError:
            raise
        except Exception as exc:
            raise TranscriptionError("Local command transcription failed") from exc

    def transcribe_marked_segments(self) -> list[str]:
        if not self.last_marker_events:
            raise AudioError("No prompt delimiter boundary is available")
        transcripts: list[str] = []
        transcribed: list[bool] = []
        for samples in self.last_marker_audio_segments:
            has_energy = has_transcribable_energy(
                samples,
                sample_rate=self.settings.sample_rate,
                energy_threshold=self.settings.energy_threshold,
                min_speech_seconds=self.settings.min_speech_seconds,
            )
            transcripts.append(self.transcribe(samples) if has_energy else "")
            transcribed.append(has_energy)
        self.last_marker_segment_transcribed = tuple(transcribed)
        return transcripts

    def reset_control_detector(self) -> None:
        self.wake.reset()
        if self.marker is not None:
            self.marker.reset()


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
