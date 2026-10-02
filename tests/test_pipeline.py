"""End-to-end pipeline tests: crawl a local site, verify the store."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from chords_db.fetch import PoliteFetcher
from chords_db.parse import ChordProParser, HtmlSpanParser, SiteProfile
from chords_db.pipeline import Crawler
from chords_db.store import Store

from conftest import PRO_LISTING, PRO_SONG_1, PRO_SONG_2, ROBOTS_DISALLOW

HTML_PROFILE = SiteProfile(
    name="fake-html",
    song_url_pattern=re.compile(r"/chords/\d+"),
    title_sel="h1.song-title",
)
PRO_PROFILE = SiteProfile(
    name="fake-pro",
    song_url_pattern=re.compile(r"/pro/\d+"),
)
CP_CHORDS_PROFILE = SiteProfile(
    name="fake-cp-chords",
    song_url_pattern=re.compile(r"/chords/\d+"),
)


def _run_crawl(site, parser, profile, tmp_path, seeds=None, **fetcher_kwargs):
    """Crawl the fake site; return (stats, path to the sqlite db)."""
    db_path = str(tmp_path / "chords.db")
    with PoliteFetcher(
        cache_dir=str(tmp_path / "cache"), delay=0, **fetcher_kwargs
    ) as fetcher, Store(db_path) as store:
        crawler = Crawler(
            fetcher, parser, store, profile, seeds or [site.url("/chords/")]
        )
        stats = crawler.run()
    return stats, db_path


def test_html_crawl_end_to_end(site, tmp_path):
    stats, db = _run_crawl(site, HtmlSpanParser(), HTML_PROFILE, tmp_path)
    # listing page + both song pages fetched; listing has no lines
    assert stats == {
        "fetched": 3, "stored": 2, "duplicate": 0, "skipped": 1,
        "errors": 0, "not_modified": 0,
    }
    conn = sqlite3.connect(db)
    titles = {r[0] for r in conn.execute("SELECT title FROM songs")}
    assert titles == {"Song One", "Song Two"}  # from title_sel, not "Untitled"
    vocab = {r[0] for r in conn.execute("SELECT chord FROM chord_vocab")}
    assert vocab == {"Am", "F", "C", "G"}
    labels = {r[0] for r in conn.execute(
        "SELECT DISTINCT label FROM sheet_lines WHERE label IS NOT NULL"
    )}
    assert labels == {"verse 1", "chorus"}
    lines = conn.execute("SELECT COUNT(*) FROM sheet_lines").fetchone()[0]
    assert lines == 3  # two lines on song 101, one on song 102


def test_chordpro_crawl_captures_directive_metadata(site, tmp_path):
    stats, db = _run_crawl(
        site, ChordProParser(), PRO_PROFILE, tmp_path, seeds=[site.url("/pro/")]
    )
    assert stats["fetched"] == 3
    assert stats["stored"] == 2
    assert stats["errors"] == 0
    conn = sqlite3.connect(db)
    titles = dict(conn.execute("SELECT source_id, title FROM songs"))
    assert titles == {"201": "Morning Light", "202": "Evening Rain"}
    artists = {r[0] for r in conn.execute("SELECT name FROM artists")}
    assert artists == {"Som Srey"}  # from {artist: ...}, not "Unknown"


def test_recrawl_is_idempotent(site, tmp_path):
    seeds = [site.url("/pro/")]
    # Pages served without ETags: the re-crawl is answered
    # from the disk cache and re-parsed, so idempotence has
    # to come from the store's checksum dedup (the 304 path
    # is covered by test_etag_revalidation_skips_unchanged_pages).
    for path, ctype, body in (
        ("/pro/", "text/html", PRO_LISTING),
        ("/pro/201", "text/chordpro", PRO_SONG_1),
        ("/pro/202", "text/chordpro", PRO_SONG_2),
    ):
        site.set_route(path, ctype, body, send_etag=False)
    _run_crawl(site, ChordProParser(), PRO_PROFILE, tmp_path, seeds=seeds)
    stats, _ = _run_crawl(site, ChordProParser(), PRO_PROFILE, tmp_path, seeds=seeds)
    assert stats["stored"] == 0
    assert stats["duplicate"] == 2


def test_robots_disallow_is_respected(site, tmp_path):
    site.set_robots(ROBOTS_DISALLOW)
    stats, _ = _run_crawl(
        site, HtmlSpanParser(), HTML_PROFILE, tmp_path,
        seeds=[site.url("/chords/101")],
    )
    assert stats["fetched"] == 0
    assert stats["skipped"] == 1
    assert stats["stored"] == 0


def test_missing_robots_txt_allows_crawl(site, tmp_path):
    site.drop_route("/robots.txt")  # 404: no rules apply
    stats, _ = _run_crawl(
        site, ChordProParser(), PRO_PROFILE, tmp_path, seeds=[site.url("/pro/")]
    )
    assert stats["stored"] == 2
    assert stats["errors"] == 0


def test_blocked_403_aborts_crawl(site, tmp_path):
    for path in ("/chords/", "/chords/101", "/chords/102"):
        site.set_route(path, "text/html", "forbidden", status=403)
    stats, _ = _run_crawl(
        site, HtmlSpanParser(), HTML_PROFILE, tmp_path,
        seeds=[site.url("/chords/101")],
    )
    assert stats["fetched"] == 0
    assert stats["stored"] == 0


def test_robots_fetched_once_per_host(site, tmp_path):
    _run_crawl(site, HtmlSpanParser(), HTML_PROFILE, tmp_path)
    # three pages crawled, but robots.txt requested exactly once
    assert len(site.requests_for("/robots.txt")) == 1
    for ua in site.requests_for("/robots.txt") + site.requests_for("/chords/101"):
        assert ua.startswith("chord-scraper/")  # real UA, not urllib default


def test_link_normalization_and_dedup(site, tmp_path):
    # a messy listing: relative links, ./, query+fragment, an
    # off-site link, and the same page twice (once with a fragment)
    for n in (10, 11, 12, 14):
        site.set_route(
            f"/chords/{n}", "text/chordpro",
            f"{{title: Song {n}}}\n[Am]ok\n",
        )
    listing = (
        '<a href="chords/10">rel</a>'
        '<a href="./chords/11">dot</a>'
        f'<a href="{site.url("/chords/12")}?x=1#frag">query</a>'
        '<a href="http://other.test/chords/13">offsite</a>'
        f'<a href="{site.url("/chords/14")}">dup</a>'
        f'<a href="{site.url("/chords/14")}#top">dup-frag</a>'
    )
    # listing lives at the root so relative links resolve to /chords/N
    site.set_route("/", "text/html", listing)
    stats, db = _run_crawl(
        site, ChordProParser(), CP_CHORDS_PROFILE, tmp_path,
        seeds=[site.url("/")],
    )
    assert stats["stored"] == 4
    assert stats["errors"] == 0
    conn = sqlite3.connect(db)
    ids = {r[0] for r in conn.execute("SELECT source_id FROM songs")}
    assert ids == {"10", "11", "12", "14"}  # no '?x=1#frag' leak
    # the fragment variant deduped: /chords/14 fetched exactly once
    assert len(site.requests_for("/chords/14")) == 1
    # the off-site link was never requested
    assert not site.requests_for("/chords/13")


def test_store_pua_pages_score_low(tmp_path):
    from chords_db.normalize import has_private_use
    pua_payload = "{title: Icon Font}\n[Am]\ue000\ue001\n"
    sheet = ChordProParser().parse(
        pua_payload, {}, "http://x.test/pua/1", PRO_PROFILE
    )
    assert has_private_use(sheet.sections[0].lines[0].text)
    with Store(str(tmp_path / "pua.db")) as store:
        store.upsert_sheet(sheet, "chordpro")
        score = store._conn.execute(
            "SELECT quality_score FROM chord_sheets"
        ).fetchone()[0]
    assert score == 0.3


def test_store_song_update_creates_new_version(tmp_path):
    # same source_id, changed content: song row is updated,
    # a new sheet version is stored, old versions kept
    url = "http://x.test/pro/201"
    v1 = ChordProParser().parse(
        "{title: Morning Light}\n{artist: Som Srey}\n[Am]old", {}, url, PRO_PROFILE
    )
    v2 = ChordProParser().parse(
        "{title: Morning Light (Live)}\n{artist: Som Srey}\n[Am]new", {}, url, PRO_PROFILE
    )
    with Store(str(tmp_path / "ver.db")) as store:
        assert store.upsert_sheet(v1, "chordpro") is True
        assert store.upsert_sheet(v2, "chordpro") is True
        songs = store._conn.execute("SELECT title FROM songs").fetchall()
        sheets = store._conn.execute(
            "SELECT COUNT(*) FROM chord_sheets"
        ).fetchone()[0]
        texts = store._conn.execute(
            "SELECT text FROM sheet_lines ORDER BY seq"
        ).fetchall()
    assert [r[0] for r in songs] == ["Morning Light (Live)"]
    assert sheets == 2  # both versions retained
    assert [r[0] for r in texts] == ["old", "new"]


def test_store_round_trip_chord_offsets(tmp_path):
    sheet = ChordProParser().parse(
        PRO_SONG_1, {}, "http://x.test/pro/201", PRO_PROFILE
    )
    with Store(str(tmp_path / "rt.db")) as store:
        assert store.upsert_sheet(sheet, "chordpro") is True
        assert store.upsert_sheet(sheet, "chordpro") is False  # same checksum
        rows = store._conn.execute(
            "SELECT chord, char_offset FROM sheet_chords "
            "ORDER BY line_id, char_offset"
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [
        ("Am", 0), ("F", 4), ("C", 8),  # verse: "The sun rises"
        ("G", 0), ("C", 5),              # chorus: "Sing along"
    ]


def test_artist_slug_collision_keeps_artists_distinct(tmp_path):
    # 'AC/DC' and 'AC DC' slugify to the same value; they are
    # different artists and must not be merged into one row
    with Store(str(tmp_path / "slug.db")) as store:
        for n, artist in ((1, "AC/DC"), (2, "AC DC")):
            sheet = ChordProParser().parse(
                f"{{title: Song {n}}}\n{{artist: {artist}}}\n[Am]ok",
                {}, f"http://x.test/pro/{n}", PRO_PROFILE,
            )
            store.upsert_sheet(sheet, "chordpro")
        rows = store._conn.execute(
            "SELECT s.title, ar.name FROM songs s "
            "JOIN artists ar ON ar.id = s.artist_id ORDER BY s.title"
        ).fetchall()
        # same artist re-crawled with different casing: still one row
        sheet = ChordProParser().parse(
            "{title: Again}\n{artist: ac dc}\n[Am]ok",
            {}, "http://x.test/pro/3", PRO_PROFILE,
        )
        store.upsert_sheet(sheet, "chordpro")
        artists = store._conn.execute(
            "SELECT slug, name FROM artists ORDER BY slug"
        ).fetchall()
    assert [(r[0], r[1]) for r in rows] == [
        ("Song 1", "AC/DC"), ("Song 2", "AC DC"),
    ]
    assert [(r[0], r[1]) for r in artists] == [
        ("ac-dc", "AC/DC"), ("ac-dc-2", "AC DC"),
    ]


def test_tags_are_stripped(tmp_path):
    sheet = ChordProParser().parse(
        "[Am]x", {"tags": " rock , jazz "},
        "http://x.test/pro/1", PRO_PROFILE,
    )
    assert sheet.tags == ["rock", "jazz"]
    with Store(str(tmp_path / "tags.db")) as store:
        store.upsert_sheet(sheet, "chordpro")
        tags = store._conn.execute("SELECT tags FROM songs").fetchone()[0]
    assert json.loads(tags) == ["rock", "jazz"]


def test_crawl_log_records_etag_and_content_hash(site, tmp_path):
    db_path = str(tmp_path / "log.db")
    with PoliteFetcher(
        cache_dir=str(tmp_path / "cache"), delay=0
    ) as fetcher, Store(db_path) as store:
        crawler = Crawler(
            fetcher, ChordProParser(), store, PRO_PROFILE,
            seeds=[site.url("/pro/201")], follow_links=False,
        )
        crawler.run()
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT etag, content_hash FROM crawl_log WHERE url = ?",
        (site.url("/pro/201"),),
    ).fetchone()
    assert row[0] == '"/pro/201"'  # the site's ETag, not NULL
    assert row[1] == hashlib.sha256(PRO_SONG_1.encode("utf-8")).hexdigest()


def test_unquoted_href_listing(site, tmp_path):
    # listings in the wild use unquoted hrefs; browsers
    # accept them, so the crawler must too
    for n in (30, 31):
        site.set_route(
            f"/pro/{n}", "text/chordpro", f"{{title: Song {n}}}\n[Am]ok\n"
        )
    site.set_route(
        "/list", "text/html",
        '<a href=/pro/30>one</a><a href="/pro/31">two</a>',
    )
    stats, db = _run_crawl(
        site, ChordProParser(), PRO_PROFILE, tmp_path,
        seeds=[site.url("/list")],
    )
    assert stats["stored"] == 2
    assert stats["errors"] == 0


def test_etag_revalidation_skips_unchanged_pages(site, tmp_path):
    # first crawl stores the pages and their etags;
    # a re-crawl with a cold cache revalidates with
    # If-None-Match and the 304s skip parse + store
    db_path = str(tmp_path / "log.db")
    seeds = [site.url("/pro/")]
    with PoliteFetcher(
        cache_dir=str(tmp_path / "c1"), delay=0
    ) as fetcher, Store(db_path) as store:
        stats = Crawler(
            fetcher, ChordProParser(), store, PRO_PROFILE, seeds=seeds
        ).run()
    assert stats["stored"] == 2
    with PoliteFetcher(
        cache_dir=str(tmp_path / "c2"), delay=0
    ) as fetcher, Store(db_path) as store:
        stats = Crawler(
            fetcher, ChordProParser(), store, PRO_PROFILE, seeds=seeds
        ).run()
    assert stats == {
        "fetched": 1, "stored": 0, "duplicate": 0,
        "skipped": 0, "errors": 0, "not_modified": 1,
    }
    # the 304 re-log must not wipe the stored etag/hash
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT etag, content_hash FROM crawl_log WHERE url = ?",
        (site.url("/pro/"),),
    ).fetchone()
    assert row[0] == '"/pro/"'
    assert row[1]  # content hash from the original fetch survives


def test_store_failure_is_counted_not_fatal(site, tmp_path, monkeypatch):
    # a page that breaks storage must be logged as an error,
    # not take the whole crawl down
    def boom(sheet, sheet_format):
        raise sqlite3.IntegrityError("constraint violation")

    db_path = str(tmp_path / "err.db")
    with PoliteFetcher(
        cache_dir=str(tmp_path / "cache"), delay=0
    ) as fetcher, Store(db_path) as store:
        monkeypatch.setattr(store, "upsert_sheet", boom)
        crawler = Crawler(
            fetcher, ChordProParser(), store, PRO_PROFILE,
            seeds=[site.url("/pro/201")], follow_links=False,
        )
        stats = crawler.run()
    assert stats["fetched"] == 1
    assert stats["errors"] == 1
    assert stats["stored"] == 0
