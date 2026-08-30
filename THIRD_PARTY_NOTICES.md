# Third-party model notices

HandsFreePC 源代码使用根目录 `LICENSE` 中的 MIT License。默认模型**不包含在本 Git 仓库中**；`handsfreepc download-models` 会直接从各上游下载，在 staging 中完成归档 SHA-256、预期权重、许可文件和来源说明核验后再替换目标目录。每个完整模型目录包含来源、下载时间、归档 SHA-256、许可链接和可获取的许可文本；已有目录也只有这些元数据齐全时才会跳过。

本文件是方便用户识别义务的摘要，不替代上游许可原文。若你重新打包或分发模型权重，应自行复核分发时生效的完整条款。

## Vosk small Chinese model 0.22

- Model name: `vosk-model-small-cn-0.22`
- Upstream / author: Alpha Cephei, Vosk
- Purpose in HandsFreePC: local limited-grammar wake and stop phrase recognition
- Source: https://alphacephei.com/vosk/models/vosk-model-small-cn-0.22.zip
- Model listing and license: https://alphacephei.com/vosk/models
- License: Apache License 2.0
- License text used by the downloader: https://raw.githubusercontent.com/alphacep/vosk-api/v0.3.45/COPYING

The upstream Vosk model list identifies `vosk-model-small-cn-0.22` as Apache-2.0. If redistributed, include the Apache-2.0 license and retain applicable copyright, attribution and notice material as required by that license.

## SenseVoiceSmall INT8 model

- Model name retained by HandsFreePC: `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17`
- Original model: SenseVoiceSmall
- Upstream / authors: Alibaba DAMO Academy / FunASR / FunAudioLLM; ONNX package distributed for sherpa-onnx by k2-fsa
- Purpose in HandsFreePC: local command transcription for Chinese, English, Japanese, Korean and Cantonese
- Package source: https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2
- Upstream model documentation: https://k2-fsa.github.io/sherpa/onnx/sense-voice/pretrained.html
- Governing model terms: FunASR Model Open Source License Agreement 1.1
- License text used by the downloader: https://raw.githubusercontent.com/modelscope/FunASR/main/MODEL_LICENSE

Required attribution for use, copying, modification or sharing:

> SenseVoiceSmall by Alibaba DAMO Academy / FunASR, packaged as `sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17` for sherpa-onnx.

The FunASR Model License requires users to attribute the source and author information and to retain relevant model names. It also contains responsibility, conduct, termination and revision terms. Do not describe these weights merely as “MIT” or “Apache-2.0”; the model weights and the surrounding runtime code are separate licensed works. Recheck the official model license before redistribution because its revision clause allows later updates.

## Silero VAD v6.2.1 ONNX

- Model / file: Silero VAD v6.2.1, `silero_vad.onnx`
- Copyright: Copyright (c) 2020-present Silero Team
- Purpose in HandsFreePC: local voice activity detection and utterance endpointing
- Source: https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.1/src/silero_vad/data/silero_vad.onnx
- License: MIT License
- License text used by the downloader: https://raw.githubusercontent.com/snakers4/silero-vad/v6.2.1/LICENSE

The MIT License requires the copyright notice and permission notice to be included in copies or substantial portions of the software. The model installer saves the upstream `LICENSE` beside the downloaded ONNX file.

## Runtime packages

HandsFreePC also depends on separately distributed Python packages such as Vosk, sherpa-onnx, PyYAML, psutil, sounddevice, NumPy, pywin32 and pywinauto. Those packages remain under their own licenses as published in their distributions. A binary redistributor should generate and review a complete dependency license inventory for the exact locked build rather than treating this model notice as exhaustive.

The default installer does **not** install the optional [faster-whisper](https://github.com/SYSTRAN/faster-whisper) extra. `install.ps1 -WithWhisper` installs its Python package and transitive dependencies, but does not install model weights. Constructing `WhisperModel("large-v3-turbo")` resolves to the separately hosted [mobiuslabsgmbh/faster-whisper-large-v3-turbo](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo) repository and may download/cache those weights during an explicit preload or the first exception fallback.

These optional weights are not one of HandsFreePC's three default model assets. They are outside `handsfreepc download-models`, its pinned SHA-256 list, and its per-model `HANDSFREEPC_MODEL_SOURCE.txt` / license-copy workflow. Review and retain the exact code, repository and model terms that apply when you download or redistribute them; do not infer that the default-model notices above cover this optional cache.
