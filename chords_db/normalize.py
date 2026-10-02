"""Unicode handling and chord-token normalization."""
from __future__ import annotations

import re
import unicodedata
from typing import List, Optional, Tuple

# Roots match either case; canon_chord() upper-cases the result.
ROOT = r"[A-Ga-g](?:#|b)?"
# Longest alternatives first so 'm7b5' wins over 'm7' and 'm'.
QUALITY = (
    r"(?:maj9|m7b9|m7b5|m13|m11|m9|dim7|aug7|sus4|sus2|add9|maj7|m7|m6|7b9|dim|aug|sus|[0-9]+|m|6|5)?"
)
CHORD_TOKEN_RE = re.compile(rf"\b{ROOT}{QUALITY}(?:/{ROOT})?\b")

# ChordPro brackets sometimes carry directives or multi-word junk, not chords.
_NON_CHORD = re.compile(r"[\s{}:]")


def normalize_unicode(text: str) -> str:
    """NFC so combining marks (Khmer vowels, diacritics) compare equal."""
    return unicodedata.normalize("NFC", text)


def has_private_use(text: str) -> bool:
    """True if glyphs come from the Unicode Private Use Area (U+E000-F8FF).

    Sites that render lyrics through an icon font map real characters to PUA
    codepoints; text extracted from such pages is garbage -- flag it.
    """
    return any(0xE000 <= ord(ch) <= 0xF8FF for ch in text)


def canon_chord(token: str) -> str:
    """Canonical chord spelling: uppercase root, original quality, slash bass.

    'am' -> 'Am', 'eb7' -> 'Eb7', 'g/b' -> 'G/B', 'c#m7b5' -> 'C#m7b5'.
    Raises ValueError for anything that is not a chord token.
    """
    m = re.fullmatch(rf"({ROOT})({QUALITY})(?:/({ROOT}))?", token.strip())
    if not m:
        raise ValueError(f"not a chord token: {token!r}")
    root, quality, bass = m.groups()
    root = root[0].upper() + root[1:]
    if bass:
        bass = bass[0].upper() + bass[1:]
    return f"{root}{quality or ''}{('/' + bass) if bass else ''}"


# --- key detection ------------------------------------------------------

_PITCH_OF = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}

# Common spellings per root pitch class (sharps for sharp keys,
# flats for flat keys; pc 6 is spelled F#/F#m by convention).
_MAJOR_KEYS = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
_MINOR_KEYS = ["Cm", "C#m", "Dm", "Ebm", "Em", "Fm", "F#m", "Gm", "G#m", "Am", "Bbm", "Bm"]
_MAJOR_STEPS = (0, 2, 4, 5, 7, 9, 11)
_MINOR_STEPS = (0, 2, 3, 5, 7, 8, 10)


def _root_pitch(chord: str) -> int:
    """Pitch class (0-11) of a canonical chord's root: 'C#m7b5' -> 1."""
    root = chord[0]
    if len(chord) > 1 and chord[1] in "#b":
        root = chord[:2]
    accidental = 1 if root.endswith("#") else -1 if root.endswith("b") else 0
    return (_PITCH_OF[root[0]] + accidental) % 12


def detect_key(chords: List[str]) -> Optional[str]:
    """Guess a song's key from its chord roots.

    Every major and minor key is scored by how many of the used
    roots fit its scale; ties go to the key whose own root is used
    most, then to the lowest pitch class. Returns None when no
    chords are given. (Tempo cannot be recovered from sheet text;
    tempo_bpm is only ever taken from site metadata.)
    """
    if not chords:
        return None
    counts = [0] * 12
    for chord in chords:
        counts[_root_pitch(canon_chord(chord))] += 1
    best: Optional[Tuple[int, int, int, str]] = None
    for pc in range(12):
        for steps, names in ((_MAJOR_STEPS, _MAJOR_KEYS), (_MINOR_STEPS, _MINOR_KEYS)):
            scale = {(pc + i) % 12 for i in steps}
            score = sum(c for p, c in enumerate(counts) if p in scale)
            candidate = (score, counts[pc], -pc, names[pc])
            if best is None or candidate > best:
                best = candidate
    return best[3] if best else None


def extract_chordpro(line: str) -> Tuple[List[Tuple[str, int]], str]:
    """Split a ChordPro line like '[Am]wonder [F]ful' into (chords, plain text).

    Returns ([(chord, char_offset), ...], text_with_chords_removed). Bracketed
    content that is not a chord token is kept as plain text.
    """
    chords: List[Tuple[str, int]] = []
    text: List[str] = []
    i = 0
    while i < len(line):
        if line[i] == "[":
            close = line.find("]", i)
            if close == -1:
                text.append(line[i:])
                break
            token = line[i + 1 : close]
            if _NON_CHORD.search(token):
                text.append(token)  # directive or multi-word bracket: keep as text
            else:
                try:
                    chords.append((canon_chord(token), len("".join(text))))
                except ValueError:
                    text.append(token)
            i = close + 1
        else:
            text.append(line[i])
            i += 1
    return chords, normalize_unicode("".join(text))
