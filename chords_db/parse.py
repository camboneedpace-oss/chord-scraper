"""Pluggable sheet parsers: ChordPro text and HTML span sheets."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Pattern, Protocol
from urllib.parse import urlparse

from .models import Instrument, LyricLine, ChordRef, Section, Sheet, SectionType
from .normalize import (
    canon_chord,
    detect_key,
    extract_chordpro,
    normalize_unicode,
)


@dataclass
class SiteProfile:
    """Per-site knobs so the parsers stay generic. Fill these per target site."""
    name: str
    song_url_pattern: Pattern = re.compile(r"/chords/\d+")
    sheet_container: str = ".lyrics, .chord-sheet, .sheet, .tab-content"
    chord_span: str = "span.chord, .chord, .chord-note"
    title_sel: Optional[str] = None
    artist_sel: Optional[str] = None
    title_khmer_sel: Optional[str] = None
    artist_khmer_sel: Optional[str] = None


class Parser(Protocol):
    format: str

    def parse(self, payload: str, meta: Dict[str, Any], url: str,
              profile: SiteProfile) -> Sheet: ...


SECTION_HEADER_RE = re.compile(
    r"^\s*[\[(]?\s*(intro|verse|chorus|bridge|outro|instrumental)"
    r"(?:\s+(\d+|[ivx]+))?\s*[\])]?\s*$",
    re.IGNORECASE,
)

# ChordPro metadata directives, e.g. '{title: Test Song}'.
_DIRECTIVE_RE = re.compile(r"^\{([^}:]+):\s*(.*?)\}\s*$")

# Directives that carry sheet metadata, mapped to the meta
# keys _sheet_base() reads. Everything else is skipped.
_DIRECTIVE_META = {
    "title": "title",
    "artist": "artist",
    "key": "song_key",
    "tempo": "tempo_bpm",
    "capo": "capo",
}

# ChordPro is plain text; a line carrying HTML tags means the payload
# is the wrong format (e.g. an HTML listing parsed as ChordPro).
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

_BLOCK_TAGS = {"div", "p", "li", "section", "article", "tr", "td", "blockquote", "pre"}


def _section(label: str) -> Section:
    m = SECTION_HEADER_RE.match(label)
    if not m:
        raise ValueError(f"not a section header: {label!r}")
    name = m.group(1).lower()
    num = f" {m.group(2)}" if m.group(2) else ""
    return Section(type=name, label=f"{name}{num}".strip())  # type: ignore[arg-type]


def _within(el: Any, container: Any) -> bool:
    """True if el is the container or one of its descendants."""
    node: Any = el
    while node is not None:
        if node is container:
            return True
        node = getattr(node, "parent", None)
    return False


def _meta_str(meta: Dict[str, Any], key: str) -> str:
    value = meta.get(key)
    return value.strip() if isinstance(value, str) else ""


def _source_id_from_url(url: str) -> str:
    """Last path segment of the URL, ignoring query string and
    fragment: '?x=1' and '#frag' must not leak into source_id or the
    same song would be stored once per variant."""
    path = urlparse(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or "index"


def _sheet_base(meta: Dict[str, Any], url: str, payload: str,
                sections: List[Section]) -> Sheet:
    source_id = _meta_str(meta, "source_id") or _source_id_from_url(url)
    # Site metadata wins; otherwise the key is inferred from
    # the chord roots actually used in the sheet. (Tempo
    # cannot be recovered from sheet text, so tempo_bpm only
    # ever comes from metadata.)
    used_chords = [
        chord.chord
        for section in sections
        for line in section.lines
        for chord in line.chords
    ]
    return Sheet(
        source_id=source_id,
        title=_meta_str(meta, "title") or "Untitled",
        artist=_meta_str(meta, "artist") or "Unknown",
        title_khmer=_meta_str(meta, "title_khmer") or None,
        artist_khmer=_meta_str(meta, "artist_khmer") or None,
        instrument=Instrument.coerce(_meta_str(meta, "instrument")),
        song_key=_meta_str(meta, "song_key") or detect_key(used_chords),
        capo=int(meta["capo"]) if str(meta.get("capo", "")).isdigit() else 0,
        tempo_bpm=int(meta["tempo_bpm"]) if str(meta.get("tempo_bpm", "")).isdigit() else None,
        tuning=_meta_str(meta, "tuning") or "EADGBE",
        tags=[t.strip() for t in _meta_str(meta, "tags").split(",") if t.strip()],
        sections=sections,
        raw_payload=payload,
        source_url=url,
    )


class ChordProParser:
    """Parses ChordPro text: '[Am]lyrics', '#'-prefixed or bare section headers,
    and '{...}' directive lines (skipped)."""

    format = "chordpro"

    def parse(self, payload: str, meta: Dict[str, Any], url: str,
              profile: SiteProfile) -> Sheet:
        meta = dict(meta)
        sections: List[Section] = []
        current = Section(type="other")
        sections.append(current)
        for raw in payload.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("{"):
                # '{title: ...}' / '{artist: ...}' / '{key: ...}' /
                # '{tempo: ...}' / '{capo: ...}' carry the sheet's own
                # metadata; every other directive is skipped.
                m = _DIRECTIVE_RE.match(line)
                if m:
                    name = m.group(1).strip().lower()
                    if name in _DIRECTIVE_META:
                        meta[_DIRECTIVE_META[name]] = m.group(2).strip()
                continue
            if _HTML_TAG_RE.search(line):
                continue  # markup, not a ChordPro lyric line
            try:
                current = _section(line.lstrip("#").strip())
            except ValueError:
                pass
            else:
                sections.append(current)
                continue
            chords, text = extract_chordpro(line)
            current.lines.append(
                LyricLine(
                    text=text,
                    chords=[ChordRef(chord=c, offset=o) for c, o in chords],
                )
            )
        # A section with no lines carries no data (e.g. the leading
        # 'other' bucket when the sheet opens with a header): drop it.
        sections = [s for s in sections if s.lines]
        return _sheet_base(meta, url, payload, sections)


def _select_text(soup: Any, selector: Optional[str]) -> Optional[str]:
    if not selector:
        return None
    el = soup.select_one(selector)
    return el.get_text(" ", strip=True) if el else None


class HtmlSpanParser:
    """Parses sheets whose chords are inline spans interleaved with lyric text,
    e.g. <p class="verse"><span class="chord">Am</span>lyrics ...</p>

    Line granularity = the innermost block element carrying chord spans, split
    at child block boundaries. Absolutely-positioned chords (CSS top/left over
    a text layer) are NOT handled: that needs a per-site coordinate mapper.
    Pages rendered through icon fonts are flagged via PUA detection instead.
    """

    format = "html"

    def parse(self, payload: str, meta: Dict[str, Any], url: str,
              profile: SiteProfile) -> Sheet:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(payload, "html.parser")
        container = soup.select_one(profile.sheet_container) or soup.body or soup

        title = _select_text(soup, profile.title_sel)
        artist = _select_text(soup, profile.artist_sel)
        title_khmer = _select_text(soup, profile.title_khmer_sel)
        artist_khmer = _select_text(soup, profile.artist_khmer_sel)
        if title:
            meta = {**meta, "title": title}
        if artist:
            meta = {**meta, "artist": artist}
        if title_khmer:
            meta = {**meta, "title_khmer": title_khmer}
        if artist_khmer:
            meta = {**meta, "artist_khmer": artist_khmer}

        chord_runs = {id(c) for c in container.select(profile.chord_span)}
        sections: List[Section] = []
        current = Section(type="other")
        sections.append(current)

        # Headings may sit outside the sheet container (layouts often
        # split the heading from the lyric block), so scan the whole
        # document for them; lyric lines come from the container only.
        for el in soup.find_all(True):
            if el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                try:
                    current = _section(el.get_text(" ", strip=True))
                except ValueError:
                    continue
                sections.append(current)
            elif self._is_line_element(el, chord_runs) and _within(el, container):
                current.lines.append(self._line(el, chord_runs))

        return _sheet_base(meta, url, payload, sections)

    def _is_line_element(self, el: Any, chord_runs: set) -> bool:
        """A block element is a lyric line when it carries chord spans but none
        of its child blocks do (i.e. it is the innermost such block)."""
        if el.name not in _BLOCK_TAGS:
            return False
        if not self._has_chord_descendant(el, chord_runs):
            return False
        for child in el.children:
            if getattr(child, "name", None) in _BLOCK_TAGS and self._has_chord_descendant(
                child, chord_runs
            ):
                return False
        return True

    def _has_chord_descendant(self, el: Any, chord_runs: set) -> bool:
        for desc in el.descendants:
            if getattr(desc, "name", None) is not None and id(desc) in chord_runs:
                return True
        return False

    def _line(self, el: Any, chord_runs: set) -> LyricLine:
        from bs4 import NavigableString

        parts: List[str] = []
        chords: List[ChordRef] = []
        taken: set = set()  # offsets already claimed on this line
        self._walk(el, parts, chords, chord_runs, in_chord=False, taken=taken)
        return LyricLine(
            text=normalize_unicode("".join(parts)),
            chords=chords,
        )

    def _walk(self, node: Any, parts: List[str], chords: List[ChordRef],
              chord_runs: set, in_chord: bool, taken: set) -> None:
        from bs4 import NavigableString

        for child in node.children:
            if isinstance(child, NavigableString):
                text = str(child)
                if in_chord:
                    for token in text.split():
                        try:
                            offset = len("".join(parts))
                            if offset in taken:
                                # Two chords stacked at one offset (adjacent
                                # spans, or several tokens in one span):
                                # the schema allows one chord per offset,
                                # so the first wins and the rest are dropped.
                                continue
                            taken.add(offset)
                            chords.append(
                                ChordRef(chord=canon_chord(token), offset=offset)
                            )
                        except ValueError:
                            pass  # non-chord junk inside a chord span: drop
                elif text:
                    parts.append(text)
            else:
                is_chord = id(child) in chord_runs
                self._walk(child, parts, chords, chord_runs, in_chord or is_chord, taken)
