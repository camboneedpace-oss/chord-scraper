"""Unit tests for normalization and both parsers. Runs under pytest or standalone."""
from __future__ import annotations

from chords_db.normalize import canon_chord, extract_chordpro, has_private_use
from chords_db.parse import ChordProParser, HtmlSpanParser, SiteProfile

CHORDPRO_SAMPLE = """{title: Test Song}
# Verse 1
[Am]This [F]is a [C]test [G]line
[Am]Second line stays [Em]here
# Chorus
[C]Oh [G]hhhh
"""

HTML_SAMPLE = """
<html><body>
<h2>Verse 1</h2>
<div class="lyrics">
<p class="verse"><span class="chord">Am</span>This is a <span class="chord">F</span>test line</p>
<p class="verse"><span class="chord">C</span>Second <span class="chord">G</span>line</p>
</div>
</body></html>
"""


def test_canon_chord():
    assert canon_chord("am") == "Am"
    assert canon_chord("eb7") == "Eb7"
    assert canon_chord("g/b") == "G/B"
    assert canon_chord("c#m7b5") == "C#m7b5"
    for bad in ("H", "am/xyz", "", "noise"):
        try:
            canon_chord(bad)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {bad!r}")


def test_extract_chordpro():
    chords, text = extract_chordpro("[Am]wonder [F]ful")
    assert chords == [("Am", 0), ("F", 7)]
    assert text == "wonder ful"
    # bracketed non-chord content is preserved as text
    chords, text = extract_chordpro("[x]plain")
    assert chords == []
    assert text == "xplain"


def test_chordpro_parser():
    sheet = ChordProParser().parse(
        CHORDPRO_SAMPLE, {}, "https://x.test/chords/1", SiteProfile(name="t")
    )
    assert sheet.source_id == "1"
    assert [s.type for s in sheet.sections] == ["verse", "chorus"]
    verse = sheet.sections[0]
    assert verse.label == "verse 1"
    first = verse.lines[0]
    assert first.text == "This is a test line"
    assert [(c.chord, c.offset) for c in first.chords] == [
        ("Am", 0), ("F", 5), ("C", 10), ("G", 15),
    ]


def test_html_parser():
    sheet = HtmlSpanParser().parse(
        HTML_SAMPLE, {"title": "T", "artist": "A"}, "https://x.test/chords/9",
        SiteProfile(name="t"),
    )
    assert sheet.title == "T"
    assert sheet.artist == "A"
    assert [s.type for s in sheet.sections] == ["other", "verse"]
    verse = sheet.sections[1]
    assert [(c.chord, c.offset) for c in verse.lines[0].chords] == [("Am", 0), ("F", 10)]
    assert verse.lines[0].text == "This is a test line"
    assert [(c.chord, c.offset) for c in verse.lines[1].chords] == [("C", 0), ("G", 7)]
    assert verse.lines[1].text == "Second line"


def test_source_id_ignores_query_and_fragment():
    # '?x=1' and '#frag' must not leak into source_id, or the
    # same song would be stored once per URL variant
    sheet = ChordProParser().parse(
        "[Am]ok", {}, "https://x.test/chords/1?x=1#frag", SiteProfile(name="t")
    )
    assert sheet.source_id == "1"
    sheet = ChordProParser().parse(
        "[Am]ok", {}, "https://x.test/", SiteProfile(name="t")
    )
    assert sheet.source_id == "index"


def test_meta_junk_falls_back_safely():
    # junk capo/tempo must not crash the parse
    sheet = ChordProParser().parse(
        "[Am]ok", {"capo": "abc", "tempo_bpm": "fast"},
        "https://x.test/chords/1", SiteProfile(name="t"),
    )
    assert sheet.capo == 0
    assert sheet.tempo_bpm is None


def test_html_khmer_selectors():
    html = """<html><body>
    <h1 class="en">Song</h1><h1 class="km">
ចមររឞន</h1>
    <div class="lyrics"><p><span class="chord">Am</span>ok</p></div>
    </body></html>"""
    profile = SiteProfile(
        name="t", title_sel="h1.en", title_khmer_sel="h1.km",
    )
    sheet = HtmlSpanParser().parse(html, {}, "https://x.test/1", profile)
    assert sheet.title == "Song"
    assert sheet.title_khmer == "ចមររឞន"


def test_chordpro_skips_html_markup():
    # a ChordPro parser pointed at an HTML page must not store
    # markup as lyrics
    sheet = ChordProParser().parse(
        "<html><body>\n<li><a href='/x/1'>Song</a></li>\n</body></html>\n",
        {}, "https://x.test/listing", SiteProfile(name="t"),
    )
    assert all(not s.lines for s in sheet.sections)


def test_pua_detection():
    assert not has_private_use("សូមរីករាយ")  # ordinary Khmer text
    assert has_private_use("")      # icon-font glyphs


def test_canon_chord_extended_qualities():
    # jazz extensions must parse as chords, not become lyric text
    for tok in ("Am7b9", "Cm9", "Cm11", "Cm13", "Dm9", "Fm7b9"):
        assert canon_chord(tok) == tok
    chords, text = extract_chordpro("[Am7b9]moves")
    assert chords == [("Am7b9", 0)]
    assert text == "moves"


def test_html_parser_adjacent_chord_spans():
    # adjacent chord spans stack two chords at one offset; the
    # schema allows one chord per offset, so the first wins
    html = """<html><body><div class="lyrics">
    <p><span class="chord">Am</span><span class="chord">F</span>x</p>
    </div></body></html>"""
    sheet = HtmlSpanParser().parse(html, {}, "https://x.test/1", SiteProfile(name="t"))
    line = sheet.sections[0].lines[0]
    assert [(c.chord, c.offset) for c in line.chords] == [("Am", 0)]


def test_html_parser_multi_token_chord_span():
    # several tokens inside one chord span share an offset;
    # first token wins, storage must not raise
    html = """<html><body><div class="lyrics">
    <p><span class="chord">Am F</span>x</p>
    </div></body></html>"""
    sheet = HtmlSpanParser().parse(html, {}, "https://x.test/1", SiteProfile(name="t"))
    line = sheet.sections[0].lines[0]
    assert [(c.chord, c.offset) for c in line.chords] == [("Am", 0)]


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    raise SystemExit(1 if failures else 0)
