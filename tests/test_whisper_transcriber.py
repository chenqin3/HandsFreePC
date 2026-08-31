from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np

import handsfree_pc.audio as audio_module
from handsfree_pc.audio import FasterWhisperTranscriber, build_transcriber


def test_faster_whisper_passes_command_bias_options(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class FakeWhisperModel:
        def __init__(self, model, **kwargs):
            observed["model"] = model
            observed["model_kwargs"] = kwargs

        def transcribe(self, samples, **kwargs):
            observed["samples"] = samples
            observed["transcribe_kwargs"] = kwargs
            segments = [
                SimpleNamespace(text=" 切换到 Claude"),
                SimpleNamespace(text=" 打开 Chat"),
            ]
            return segments, None

    monkeypatch.setitem(
        sys.modules,
        "faster_whisper",
        types.SimpleNamespace(WhisperModel=FakeWhisperModel),
    )
    samples = np.ones(160, dtype=np.float32)
    transcriber = FasterWhisperTranscriber(
        "large-v3-turbo",
        device="cuda",
        compute_type="float16",
        language="zh",
        beam_size=7,
        initial_prompt="命令可能包含 Claude 和 Codex。",
        hotwords="Claude Codex Chat and Cowork",
    )

    assert transcriber.transcribe(samples, 16000) == "切换到 Claude 打开 Chat"
    assert observed["model"] == "large-v3-turbo"
    assert observed["model_kwargs"] == {"device": "cuda", "compute_type": "float16"}
    assert observed["samples"] is samples
    assert observed["transcribe_kwargs"] == {
        "language": "zh",
        "beam_size": 7,
        "vad_filter": False,
        "condition_on_previous_text": False,
        "initial_prompt": "命令可能包含 Claude 和 Codex。",
        "hotwords": "Claude Codex Chat and Cowork",
    }


def test_build_transcriber_normalizes_auto_language_and_hotword_list(monkeypatch) -> None:
    observed: dict[str, object] = {}

    class FakeTranscriber:
        def __init__(self, model, **kwargs):
            observed["model"] = model
            observed.update(kwargs)

    monkeypatch.setattr(audio_module, "FasterWhisperTranscriber", FakeTranscriber)
    settings = SimpleNamespace(
        command={
            "backend": "faster-whisper",
            "model": "large-v3-turbo",
            "device": "cuda",
            "compute_type": "float16",
            "language": "auto",
            "beam_size": 5,
            "initial_prompt": "语音控制命令。",
            "hotwords": ["Claude", "Chat and Cowork", "Codex"],
        },
        fallback={"backend": "none"},
    )

    result = build_transcriber(settings, base_dir=Path.cwd())

    assert isinstance(result, FakeTranscriber)
    assert observed == {
        "model": "large-v3-turbo",
        "device": "cuda",
        "compute_type": "float16",
        "language": "auto",
        "beam_size": 5,
        "initial_prompt": "语音控制命令。",
        "hotwords": "Claude Chat and Cowork Codex",
    }
