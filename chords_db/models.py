"""Core data models for the chord-sheet dataset (pydantic v2)."""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

SectionType = Literal["intro", "verse", "chorus", "bridge", "outro", "instrumental", "other"]


class Instrument(str, Enum):
    GUITAR = "guitar"
    UKULELE = "ukulele"
    BASS = "bass"
    PIANO = "piano"

    @classmethod
    def coerce(cls, value: Optional[str]) -> "Instrument":
        """Best-effort instrument lookup; unknown values fall back to guitar."""
        if value is None:
            return cls.GUITAR
        try:
            return cls(value.strip().lower())
        except ValueError:
            return cls.GUITAR


class ChordRef(BaseModel):
    """One chord fretted at a character offset within a lyric line."""
    chord: str
    offset: int = Field(ge=0)


class LyricLine(BaseModel):
    text: str = ""
    chords: List[ChordRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _offsets_in_bounds(self) -> "LyricLine":
        for c in self.chords:
            if c.offset > len(self.text):
                raise ValueError(
                    f"chord {c.chord!r} offset {c.offset} beyond line length {len(self.text)}"
                )
        return self


class Section(BaseModel):
    type: SectionType
    label: Optional[str] = None
    lines: List[LyricLine] = Field(default_factory=list)


class Sheet(BaseModel):
    """A fully normalized chord sheet, ready for storage or interchange."""
    source_id: str
    title: str
    artist: str
    title_khmer: Optional[str] = None
    artist_khmer: Optional[str] = None
    language: str = "km"
    instrument: Instrument
    song_key: Optional[str] = None
    capo: int = 0
    tempo_bpm: Optional[int] = None
    tuning: str = "EADGBE"
    tags: List[str] = Field(default_factory=list)
    sections: List[Section]
    raw_payload: str
    source_url: str

    def content_checksum(self) -> str:
        """Stable digest of the normalized content (excludes raw payload/URL)."""
        core = self.model_dump(exclude={"raw_payload", "source_url"})
        blob = json.dumps(core, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
