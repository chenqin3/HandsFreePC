from __future__ import annotations

from enum import StrEnum


class FeedbackMode(StrEnum):
    OVERLAY = "overlay"
    VOICE = "voice"
    BOTH = "both"
    SILENT = "silent"
