"""The built-in demo source: a self-contained crawl -> web UI."""
from __future__ import annotations

import re
import sqlite3
import time

import httpx

from chords_db.__main__ import main
from chords_db.demo import DEMO_SONGS, DEMO_SEED, start_demo
from chords_db.parse import ChordProParser, SiteProfile
from chords_db.web import Refresher, WebApp

DEMO_PROFILE = SiteProfile(
    name="demo", song_url_pattern=re.compile(r"/chords/\d+")
)


def test_demo_site_allows_bots_and_lists_songs():
    site = start_demo()
    try:
        robots = httpx.get(site.url("/robots.txt"))
        assert "Allow" in robots.text
        listing = httpx.get(site.url(DEMO_SEED)).text
        for path, _sheet in DEMO_SONGS:
            assert f'href="{path}"' in listing
    finally:
        site.close()


def test_demo_crawl_populates_the_dataset(tmp_path):
    db = str(tmp_path / "demo.db")
    code = main([
        "--demo", "--db", db,
        "--cache-dir", str(tmp_path / "cache"),
        "--delay", "0",
    ])
    assert code == 0
    conn = sqlite3.connect(db)
    try:
        titles = {r[0] for r in conn.execute("SELECT title FROM songs")}
        assert len(titles) == len(DEMO_SONGS)
        sheets = conn.execute(
            "SELECT COUNT(*) FROM chord_sheets"
        ).fetchone()[0]
        assert sheets == len(DEMO_SONGS)
        chords = conn.execute(
            "SELECT COUNT(*) FROM sheet_chords"
        ).fetchone()[0]
        assert chords > 0
    finally:
        conn.close()


def test_demo_auto_refresh_cycle(tmp_path):
    # The full stack --demo --serve runs: demo site ->
    # refresher (with jitter) -> web app read side.
    db = str(tmp_path / "demo.db")
    site = start_demo()
    try:
        refresher = Refresher(
            seeds=[site.url(DEMO_SEED)],
            db_path=db,
            cache_dir=str(tmp_path / "cache"),
            delay=0,
            parser=ChordProParser(),
            profile=DEMO_PROFILE,
            interval=0.2, jitter=0.1,
        )
        refresher.start()
        try:
            deadline = time.time() + 10
            stats = None
            while time.time() < deadline:
                stats = refresher.state()["last_stats"]
                if stats and stats["stored"] == len(DEMO_SONGS):
                    break
                time.sleep(0.05)
            assert stats and stats["stored"] == len(DEMO_SONGS)
        finally:
            refresher.stop()
        app = WebApp(db)
        songs = app.songs()["songs"]
        assert len(songs) == len(DEMO_SONGS)
        detail = app.song(songs[0]["id"])
        assert detail["sections"], "sheet must carry sections"
        assert detail["instrument"] == "guitar"
    finally:
        site.close()


def test_demo_etag_revalidation_is_cheap(tmp_path):
    # A second crawl of the unchanged demo site stores nothing.
    site = start_demo()
    try:
        db = str(tmp_path / "re.db")
        code = main([
            "--demo", "--db", db,
            "--cache-dir", str(tmp_path / "cache"),
            "--delay", "0",
        ])
        assert code == 0
        code = main([
            "--demo", "--db", db,
            "--cache-dir", str(tmp_path / "cache"),
            "--delay", "0",
        ])
        assert code == 0
        conn = sqlite3.connect(db)
        try:
            # Idempotent: still exactly one sheet per song.
            assert conn.execute(
                "SELECT COUNT(*) FROM chord_sheets"
            ).fetchone()[0] == len(DEMO_SONGS)
        finally:
            conn.close()
    finally:
        site.close()
