"""Shared fixtures: a local HTTP site that stands in for a chord site.

The crawler is exercised against a real socket (robots.txt, listings,
song pages) so the full fetch -> parse -> store path runs in tests.
The server machinery lives in chords_db.demo.LocalSite; this module
contributes the fake chord-site content.
"""
from __future__ import annotations

import pytest

from chords_db.demo import LocalSite

ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"
ROBOTS_DISALLOW = "User-agent: *\nDisallow: /\n"

HTML_LISTING = """<html><body><ul>
<li><a href="/chords/101">Song One</a></li>
<li><a href="/chords/102">Song Two</a></li>
</ul></body></html>"""

HTML_SONG_1 = """<html><body>
<h1 class="song-title">Song One</h1>
<div class="lyrics">
<h2>Verse 1</h2>
<p><span class="chord">Am</span>the <span class="chord">F</span>sun</p>
<h2>Chorus</h2>
<p><span class="chord">C</span>sing it</p>
</div></body></html>"""

HTML_SONG_2 = """<html><body>
<h1 class="song-title">Song Two</h1>
<div class="lyrics">
<h2>Verse 1</h2>
<p><span class="chord">G</span>hello <span class="chord">C</span>world</p>
</div></body></html>"""

PRO_LISTING = """<html><body><ul>
<li><a href="/pro/201">Morning Light</a></li>
<li><a href="/pro/202">Evening Rain</a></li>
</ul></body></html>"""

PRO_SONG_1 = """{title: Morning Light}
{artist: Som Srey}
[Verse 1]
[Am]The [F]sun [C]rises
[Chorus]
[G]Sing [C]along
"""

PRO_SONG_2 = """{title: Evening Rain}
{artist: Som Srey}
[Verse 1]
[Dm]Soft [G]drops fall
"""


@pytest.fixture
def site():
    site = LocalSite({
        "/robots.txt": ("text/plain", ROBOTS_ALLOW),
        "/chords/": ("text/html", HTML_LISTING),
        "/chords/101": ("text/html", HTML_SONG_1),
        "/chords/102": ("text/html", HTML_SONG_2),
        "/pro/": ("text/html", PRO_LISTING),
        "/pro/201": ("text/chordpro", PRO_SONG_1),
        "/pro/202": ("text/chordpro", PRO_SONG_2),
    })
    try:
        yield site
    finally:
        site.close()
