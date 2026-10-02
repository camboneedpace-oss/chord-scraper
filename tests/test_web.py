"""Web server tests: local UI endpoints and the auto-refresher."""
from __future__ import annotations

import re
import threading
import time

import httpx
import pytest

from chords_db.__main__ import main
from chords_db.parse import ChordProParser, SiteProfile
from chords_db.store import Store
from chords_db.web import INDEX_HTML, Refresher, WebApp, _nonnegative

PRO_ARGS = ["--parser", "chordpro", "--url-pattern", r"/pro/\d+"]


def _crawl(site, tmp_path) -> str:
    """Crawl the fake site into a fresh db; return the db path."""
    db = str(tmp_path / "web.db")
    code = main([
        "--db", db,
        "--cache-dir", str(tmp_path / "cache"),
        "--delay", "0",
        *PRO_ARGS,
        site.url("/pro/"),
    ])
    assert code == 0
    return db


def _profile() -> SiteProfile:
    return SiteProfile(
        name="test", song_url_pattern=re.compile(r"/pro/\d+")
    )


@pytest.fixture
def server(site, tmp_path):
    """WebApp (no refresher) on an ephemeral port + an httpx client."""
    db = _crawl(site, tmp_path)
    app = WebApp(db)
    httpd = app.make_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
        yield client
    httpd.shutdown()
    httpd.server_close()


def test_index_serves_html(server):
    r = server.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "<!doctype html>" in r.text
    assert "/api/songs" in r.text  # the UI polls the API


def test_api_songs_lists_crawled_songs(server):
    songs = server.get("/api/songs").json()["songs"]
    assert {s["title"] for s in songs} == {"Morning Light", "Evening Rain"}
    assert all(s["artist"] == "Som Srey" for s in songs)


def test_api_songs_search_filters(server):
    songs = server.get("/api/songs", params={"q": "evening"}).json()["songs"]
    assert [s["title"] for s in songs] == ["Evening Rain"]
    assert server.get("/api/songs", params={"q": "nope"}).json()["songs"] == []


def test_api_song_detail_returns_sheet_with_chords(server):
    song_id = server.get("/api/songs").json()["songs"][0]["id"]
    r = server.get(f"/api/songs/{song_id}")
    assert r.status_code == 200
    sheet = r.json()
    assert sheet["title"] in {"Morning Light", "Evening Rain"}
    assert sheet["instrument"] == "guitar"
    assert sheet["format"] == "chordpro"
    sections = sheet["sections"]
    assert sections, "sheet must carry at least one section"
    first = sections[0]
    assert first["label"].lower().startswith("verse")
    chords = [c["chord"] for line in first["lines"] for c in line["chords"]]
    assert chords, "chord refs must survive the round-trip"
    # offsets point into the line text
    for line in first["lines"]:
        for c in line["chords"]:
            assert 0 <= c["offset"] <= len(line["text"])


def test_api_song_detail_404_for_unknown_id(server):
    r = server.get("/api/songs/999999")
    assert r.status_code == 404
    assert "error" in r.json()


def test_api_stats_reports_counts(server):
    payload = server.get("/api/stats").json()
    assert payload["counts"]["songs"] == 2
    assert payload["counts"]["chord_sheets"] == 2
    assert payload["counts"]["sheet_lines"] >= 3
    assert "refresh" not in payload  # no refresher configured


def test_unknown_route_404(server):
    assert server.get("/api/nope").status_code == 404


def test_refresh_endpoint_triggers_a_crawl(site, tmp_path):
    db = _crawl(site, tmp_path)
    refresher = Refresher(
        seeds=[site.url("/pro/")],
        db_path=db,
        cache_dir=str(tmp_path / "cache2"),
        delay=0,
        parser=ChordProParser(),
        profile=_profile(),
        interval=3600,  # long: only the manual trigger should run
    )
    app = WebApp(db, refresher=refresher)
    httpd = app.make_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            r = client.post("/api/refresh")
            assert r.status_code == 200
            assert r.json()["accepted"]

            # The pass runs in the background: poll until it lands.
            deadline = time.time() + 10
            refresh = None
            while time.time() < deadline:
                refresh = client.get("/api/stats").json()["refresh"]
                if refresh["last_stats"]:
                    break
                time.sleep(0.05)
            assert refresh["last_stats"]["fetched"] >= 1
            assert refresh["running"] is False
            # ETag revalidation: the second pass should not re-store.
            assert refresh["last_stats"]["stored"] == 0
    finally:
        httpd.shutdown()
        httpd.server_close()
        refresher.stop()


def test_refresh_endpoint_without_refresher_is_409(server):
    assert server.post("/api/refresh").status_code == 409


def test_refresher_loop_runs_on_interval(site, tmp_path):
    db = _crawl(site, tmp_path)
    refresher = Refresher(
        seeds=[site.url("/pro/")],
        db_path=db,
        cache_dir=str(tmp_path / "cache3"),
        delay=0,
        parser=ChordProParser(),
        profile=_profile(),
        interval=0.2,
    )
    refresher.start()
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            if refresher.state()["last_stats"]:
                break
            time.sleep(0.05)
        state = refresher.state()
        assert state["last_stats"]["fetched"] >= 1
        assert state["interval"] == 0.2
    finally:
        refresher.stop()
    assert refresher.state()["running"] is False


def test_refresher_requires_parser_and_profile(tmp_path):
    with pytest.raises(ValueError):
        Refresher(
            seeds=["http://example.test/"],
            db_path=str(tmp_path / "x.db"),
        )


def test_index_search_keyboard_shortcut(server):
    r = server.get("/")
    assert r.status_code == 200
    assert "keydown" in r.text
    assert '$("#q").focus()' in r.text  # "/" jumps to the search box


# -- INDEX_HTML regression pins for the UI bug fixes --------

def test_index_html_hidden_rule_beats_overlay_flex():
    # .overlay is display:flex, which would otherwise outrank
    # the UA's [hidden] rule -- hidden splash/settings panels
    # would still paint over the page
    assert "[hidden]{display:none !important}" in INDEX_HTML
    assert ".overlay{" in INDEX_HTML  # the rule it must outrank


def test_index_html_status_uses_real_characters():
    # #status is written with textContent, which does NOT decode
    # entities: its separators must be real characters, not the
    # &middot;/&plusmn; that the innerHTML templates may keep
    assert '$("#status").textContent' in INDEX_HTML
    assert "songs · " in INDEX_HTML  # real middle dot
    assert " ±" in INDEX_HTML       # real plus-minus
    assert "&plusmn;" not in INDEX_HTML


def test_index_html_layout_adapts_to_wrapped_header():
    # the header wraps on narrow screens, so the page
    # can't subtract a hardcoded header height -- main
    # must fill the leftover space flexibly
    assert "height:calc(100vh - 57px)" not in INDEX_HTML
    assert "display:flex;flex-direction:column" in INDEX_HTML
    assert "main{flex:1;min-height:0" in INDEX_HTML


def test_index_html_mobile_chord_rows_are_compact():
    # single-line chord rows so the 40vh phone sidebar
    # shows more of the vocabulary
    assert "#chords li{display:flex" in INDEX_HTML
    assert "#chords .a{flex:1}" in INDEX_HTML


def test_index_html_settings_form_swallows_enter():
    # Enter inside a settings input would submit the form and
    # reload the page -- the handler must swallow the submit
    assert '$("#set-form").addEventListener("submit"' in INDEX_HTML
    assert "e.preventDefault()" in INDEX_HTML


def test_served_index_equals_index_html(server):
    # the UI page is the static INDEX_HTML, served verbatim
    assert server.get("/").text == INDEX_HTML


def test_refresher_jitter_stays_in_bounds(tmp_path):
    refresher = Refresher(
        seeds=["http://example.test/"],
        db_path=str(tmp_path / "x.db"),
        parser=ChordProParser(),
        profile=_profile(),
        interval=10, jitter=5,
    )
    assert refresher.state()["jitter"] == 5
    for _ in range(50):
        delay = refresher._sleep_for()
        assert 10 <= delay <= 15


def test_refresher_without_jitter_uses_fixed_interval(tmp_path):
    refresher = Refresher(
        seeds=["http://example.test/"],
        db_path=str(tmp_path / "x.db"),
        parser=ChordProParser(),
        profile=_profile(),
        interval=10,
    )
    assert refresher._sleep_for() == 10
    assert refresher.state()["jitter"] == 0


# -- chord vocabulary -------------------------------------------------

def test_api_chords_lists_vocabulary(server):
    chords = server.get("/api/chords").json()["chords"]
    by_chord = {c["chord"]: c for c in chords}
    assert set(by_chord) == {"Am", "F", "C", "G", "Dm"}
    # G is the only chord shared by both songs
    assert by_chord["G"]["song_count"] == 2
    assert by_chord["G"]["occurrences"] == 2
    assert {s["title"] for s in by_chord["G"]["songs"]} == {
        "Morning Light", "Evening Rain"
    }
    # C occurs twice, but within a single song
    assert by_chord["C"]["song_count"] == 1
    assert by_chord["C"]["occurrences"] == 2
    # most-used chord first, then alphabetical
    assert [c["chord"] for c in chords] == ["G", "Am", "C", "Dm", "F"]


def test_api_chords_empty_for_fresh_db(tmp_path):
    db = str(tmp_path / "fresh.db")
    store = Store(db)
    store.close()
    app = WebApp(db)
    assert app.chords() == {"chords": []}


def test_index_has_chords_view_settings_and_splash(server):
    html = server.get("/").text
    assert 'data-view="chords"' in html      # vocabulary tab
    assert "/api/chords" in html
    assert 'id="settings-backdrop"' in html  # settings panel
    assert "/api/settings" in html
    assert 'id="splash-backdrop"' in html    # first-run splash
    assert "localStorage" in html              # splash shows once


# -- settings endpoint --------------------------------------------------

@pytest.fixture
def refresh_server(site, tmp_path):
    """WebApp with a configured (unstarted) Refresher."""
    db = _crawl(site, tmp_path)
    refresher = Refresher(
        seeds=[site.url("/pro/")],
        db_path=db,
        cache_dir=str(tmp_path / "rcache"),
        delay=0,
        parser=ChordProParser(),
        profile=_profile(),
        interval=3600, jitter=0,
    )
    app = WebApp(db, refresher=refresher)
    httpd = app.make_server("127.0.0.1", 0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}") as client:
            yield client, refresher
    finally:
        httpd.shutdown()
        httpd.server_close()
        refresher.stop()


def test_api_settings_updates_the_schedule(refresh_server):
    client, refresher = refresh_server
    r = client.post("/api/settings", json={"interval": 60, "jitter": 2.5})
    assert r.status_code == 200
    body = r.json()
    assert body["accepted"] is True
    assert body["refresh"]["interval"] == 60
    assert body["refresh"]["jitter"] == 2.5
    # reflected in the stats payload the UI polls
    refresh = client.get("/api/stats").json()["refresh"]
    assert refresh["interval"] == 60
    assert refresh["jitter"] == 2.5


def test_api_settings_partial_update(refresh_server):
    client, refresher = refresh_server
    refresher.configure(interval=100, jitter=5)
    r = client.post("/api/settings", json={"interval": 50})
    assert r.status_code == 200
    state = r.json()["refresh"]
    assert state["interval"] == 50
    assert state["jitter"] == 5  # untouched


def test_api_settings_rejects_invalid_values(refresh_server):
    client, _ = refresh_server
    for payload in (
        {"interval": 0},       # below the 1s floor
        {"jitter": -1},
        {"interval": "ten"},
        {"interval": True},
    ):
        r = client.post("/api/settings", json=payload)
        assert r.status_code == 400
        assert "error" in r.json()
    # a valid JSON body that is not an object
    r = client.post("/api/settings", content=b"[1,2]")
    assert r.status_code == 400


def test_api_settings_without_refresher_is_409(server):
    r = server.post("/api/settings", json={"interval": 5})
    assert r.status_code == 409


def test_refresher_configure_updates_state(tmp_path):
    refresher = Refresher(
        seeds=["http://example.test/"],
        db_path=str(tmp_path / "x.db"),
        parser=ChordProParser(),
        profile=_profile(),
        interval=30, jitter=0,
    )
    state = refresher.configure(interval=90, jitter=4)
    assert state == refresher.state()
    assert state["interval"] == 90
    assert state["jitter"] == 4
    refresher.configure(jitter=1)  # partial: interval kept
    assert refresher.state()["interval"] == 90
    assert refresher.state()["jitter"] == 1


def test_nonnegative_validator():
    assert _nonnegative(None, "x", 0) is None
    assert _nonnegative(5, "x", 1) == 5.0
    assert _nonnegative(0, "x", 0) == 0.0
    for bad in (True, "3", [], {}, float("nan"), float("inf"), -1):
        with pytest.raises(ValueError):
            _nonnegative(bad, "x", 0)
    with pytest.raises(ValueError):
        _nonnegative(0, "x", 1)
