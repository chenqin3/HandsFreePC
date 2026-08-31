from __future__ import annotations

import json
from pathlib import Path

from handsfree_pc.transcripts import (
    TranscriptJournal,
    default_transcript_path,
    tail_transcripts,
)


def test_default_transcript_path_is_independent_per_user_path(tmp_path: Path) -> None:
    path = default_transcript_path({"LOCALAPPDATA": str(tmp_path)})

    assert path == tmp_path / "HandsFreePC" / "transcripts" / "asr-transcripts.jsonl"


def test_transcript_journal_preserves_raw_utf8_text_and_segment_metadata(
    tmp_path: Path,
) -> None:
    path = tmp_path / "asr.jsonl"
    journal = TranscriptJournal(path)
    raw = "  切换到 Claude，打开 Chat and Cowork\n第二行  "
    try:
        journal.record(
            source="marker_segment",
            text=raw,
            session_id="session-1",
            segment_index=0,
            segment_count=2,
            transcribed=False,
            skip_reason="silence_energy_gate",
        )
    finally:
        journal.close()

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["text"] == raw
    assert saved["source"] == "marker_segment"
    assert saved["session_id"] == "session-1"
    assert saved["segment_index"] == 0
    assert saved["segment_count"] == 2
    assert saved["transcribed"] is False
    assert saved["skip_reason"] == "silence_energy_gate"
    assert tail_transcripts(path, limit=1) == [saved]


def test_transcript_journal_rotates_independently(tmp_path: Path) -> None:
    path = tmp_path / "asr.jsonl"
    journal = TranscriptJournal(path)
    journal._handler.maxBytes = 160
    try:
        journal.record(source="command_utterance", text="甲" * 80)
        journal.record(source="command_utterance", text="乙" * 80)
    finally:
        journal.close()

    assert Path(f"{path}.1").is_file()
    entries = tail_transcripts(path, limit=2)
    assert [entry["text"] for entry in entries] == ["甲" * 80, "乙" * 80]
