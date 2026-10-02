"""Browser QA for the web UI (pytest marker: ``browser``).

Drives real Chrome through the three UI features -- the
first-run splash, the song list and live sheets, the Chords
vocabulary tab, and the auto-refresh settings panel -- against
a live in-process server (demo site + Refresher), so the full
UI path is exercised exactly as a user sees it.

Requires Playwright and a Chrome build; the module skips
itself when either is missing, so plain unit-test runs stay
dependency-free:

    pip install -e ".[browser]" && playwright install chrome
    python -m pytest tests/qa_browser.py -m browser -v
"""
from __future__ import annotations

import re
import threading
import time

import pytest

from chords_db.__main__ import main
from chords_db.demo import DEMO_SEED, start_demo
from chords_db.parse import ChordProParser, SiteProfile
from chords_db.web import Refresher, WebApp

pytestmark = pytest.mark.browser

playwright = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    """Real Chrome for the whole module; each test gets its
    own context, so localStorage (splash dismissal) is
    isolated per test."""
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome")
        except Exception as exc:  # no Chrome build available
            pytest.skip(f"Chrome unavailable: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    pg = context.new_page()
    pg.set_viewport_size({"width": 1200, "height": 900})
    yield pg
    context.close()


@pytest.fixture
def shots(tmp_path):
    d = tmp_path / "screenshots"
    d.mkdir()
    return d


@pytest.fixture
def server_url(tmp_path):
    """Crawl the bundled demo site and serve the UI in-process,
    with a long-interval Refresher (only the manual trigger
    should run)."""
    demo_site = start_demo()
    db = str(tmp_path / "qa.db")
    assert main([
        "--demo", "--db", db,
        "--cache-dir", str(tmp_path / "cache"),
        "--delay", "0",
    ]) == 0
    refresher = Refresher(
        seeds=[demo_site.url(DEMO_SEED)],
        db_path=db,
        cache_dir=str(tmp_path / "rcache"),
        delay=0,
        parser=ChordProParser(),
        profile=SiteProfile(
            name="qa", song_url_pattern=re.compile(r"/chords/\d+")
        ),
        interval=3600, jitter=0,
    )
    app = WebApp(db, refresher=refresher)
    httpd = app.make_server("127.0.0.1", 0)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    refresher.start()  # first pass runs immediately, like production
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        refresher.stop()
        httpd.shutdown()
        httpd.server_close()
        demo_site.close()


def _js_errors(page):
    out = []
    page.on("pageerror", lambda e: out.append(str(e)))
    return out


def test_first_run_splash(page, server_url, shots):
    page.goto(server_url + "/", wait_until="networkidle")
    errors = _js_errors(page)

    splash = page.locator("#splash-backdrop")
    assert splash.is_visible(), "splash must show on first run"
    # .card h2 is CSS-uppercased, so compare loosely
    text = splash.inner_text().lower()
    assert "everything stays local" in text
    assert "demo" in text
    assert "point it at your own source" in text
    assert "403" in text
    page.screenshot(path=str(shots / "splash.png"))

    page.locator("#splash-ok").click()
    assert splash.is_hidden(), "Start exploring closes the splash"
    assert page.evaluate(
        "localStorage.getItem('chord-scraper.seen')") == "1"

    page.reload(wait_until="networkidle")
    assert splash.is_hidden(), "splash must not reappear after reload"

    page.locator("#about").click()
    assert splash.is_visible(), "? reopens the splash"
    page.locator("#splash-ok").click()

    # the [hidden] CSS fix: a hidden overlay must not paint
    disp = page.locator("#settings-backdrop").evaluate(
        "el => getComputedStyle(el).display")
    assert disp == "none", f"hidden overlay computed {disp!r}"
    assert not errors


def test_song_list_and_sheet(page, server_url, shots):
    page.goto(server_url + "/", wait_until="networkidle")
    errors = _js_errors(page)
    page.locator("#splash-ok").click()  # first run: dismiss the splash

    view = page.locator("#views .tab.active").get_attribute("data-view")
    assert view == "songs", "Songs tab is the default view"
    titles = page.locator("#songs li[data-id] .t").all_inner_texts()
    assert len(titles) == 4, titles
    assert "Riverside Walk" in " ".join(titles)

    page.fill("#q", "palm")
    assert page.locator("#songs li[data-id]").count() == 1
    page.fill("#q", "")
    assert page.locator("#songs li[data-id]").count() == 4

    page.locator("#songs li[data-id]",
                 has_text="Riverside Walk").click()
    page.wait_for_selector("#sheet section.sheet")
    heads = [h.lower() for h in
             page.locator("#sheet section.sheet h2").all_inner_texts()]
    assert "verse 1" in heads and "chorus" in heads, heads
    chord_rows = page.locator("#sheet pre.chords").all_inner_texts()
    assert len(chord_rows) >= 4, f"{len(chord_rows)} chord rows"
    assert chord_rows[0].startswith("Am"), chord_rows[0]
    assert page.locator("#songs li.active").count() == 1
    page.screenshot(path=str(shots / "song-sheet.png"))
    assert not errors


def test_chords_vocabulary_tab(page, server_url, shots):
    page.goto(server_url + "/", wait_until="networkidle")
    errors = _js_errors(page)
    page.locator("#splash-ok").click()  # first run: dismiss the splash

    page.locator("#views .tab", has_text="Chords").click()
    page.wait_for_selector("#chords li[data-chord]")
    rows = page.locator("#chords li[data-chord]")
    assert rows.count() == 9, f"{rows.count()} chords"
    assert rows.first.locator(".c").inner_text() == "C"
    g = page.locator("#chords li[data-chord='G']")
    assert g.count() == 1
    gmeta = g.locator(".a").inner_text()
    assert "4 songs" in gmeta and "18 occurrences" in gmeta, gmeta
    page.screenshot(path=str(shots / "chords-tab.png"))

    g.click()
    page.wait_for_selector("#sheet .chord-name")
    assert page.locator("#sheet .chord-name").inner_text() == "G"
    assert "4 songs" in page.locator("#sheet .meta").inner_text()
    links = page.locator("#sheet .songlinks li")
    assert links.count() == 4
    links.first.click()
    page.wait_for_selector("#sheet section.sheet")
    assert page.locator("#sheet section.sheet").count() >= 1
    assert not errors


def test_settings_panel_changes_schedule_live(page, server_url, shots):
    page.goto(server_url + "/", wait_until="networkidle")
    errors = _js_errors(page)
    page.locator("#splash-ok").click()  # first run: dismiss the splash

    page.locator("#gear").click()
    settings = page.locator("#settings-backdrop")
    assert settings.is_visible(), "gear opens the settings panel"
    assert page.locator("#set-interval").input_value() == "3600"
    assert page.locator("#set-jitter").input_value() == "0"
    page.screenshot(path=str(shots / "settings.png"))

    page.fill("#set-interval", "120")
    page.fill("#set-jitter", "25")
    page.locator("#set-save").click()
    # the save handler is async: wait for the panel to close
    # and the status line to update
    page.wait_for_selector("#settings-backdrop", state="hidden")
    assert settings.is_hidden()
    st = page.evaluate("async () => (await fetch('/api/stats')).json()")
    assert st["refresh"]["interval"] == 120, st["refresh"]
    assert st["refresh"]["jitter"] == 25
    page.wait_for_function(
        "() => document.querySelector('#status').textContent"
        ".includes('auto every 120s')")
    assert "auto every 120s" in page.locator("#status").inner_text()

    # Enter must not submit the form and reload the page
    page.locator("#gear").click()
    page.locator("#set-jitter").fill("30")
    page.locator("#set-jitter").press("Enter")
    assert settings.is_visible(), "Enter must not close the panel"
    page.keyboard.press("Escape")
    assert settings.is_hidden(), "Escape closes the settings panel"

    # invalid input is rejected and the panel stays open
    page.locator("#gear").click()
    page.fill("#set-interval", "0")
    page.locator("#set-save").click()
    page.wait_for_function(
        "() => document.querySelector('#set-err').textContent !== ''")
    assert page.locator("#set-err").inner_text()
    assert settings.is_visible(), "panel stays open after a rejected save"
    page.screenshot(path=str(shots / "settings-error.png"))
    assert not errors


def test_narrow_viewport_layout(page, server_url, shots):
    # the mobile breakpoint must not push the page past
    # the viewport: the header wraps, so the layout can't
    # assume a fixed header height
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto(server_url + "/", wait_until="networkidle")
    errors = _js_errors(page)
    page.locator("#splash-ok").click()

    m = page.evaluate(
        "() => ({docH: document.documentElement.scrollHeight,"
        " vh: innerHeight, sw: document.documentElement.scrollWidth,"
        " vw: innerWidth})")
    assert m["docH"] <= m["vh"] + 1, m   # no vertical overflow
    assert m["sw"] <= m["vw"] + 1, m     # no horizontal overflow

    # controls stay big enough to tap
    tab = page.locator("#views .tab").first.bounding_box()
    assert tab["height"] >= 32, f"view tab {tab['height']}px tall"
    gear = page.locator("#gear").bounding_box()
    assert gear["height"] >= 32, f"gear button {gear['height']}px tall"

    # chord vocabulary rows go compact under the breakpoint
    page.locator("#views .tab", has_text="Chords").click()
    page.wait_for_selector("#chords li[data-chord]")
    h = page.locator("#chords li[data-chord]").first.bounding_box()["height"]
    assert h < 50, f"chord row {h}px tall on a phone"

    # the settings panel fits without scrolling the page
    page.locator("#gear").click()
    page.wait_for_selector("#settings-backdrop:not([hidden])")
    card = page.locator("#settings-backdrop .card").bounding_box()
    assert card["x"] >= 0 and card["x"] + card["width"] <= 376, card
    assert card["y"] >= 0 and card["y"] + card["height"] <= 668, card
    page.screenshot(path=str(shots / "narrow.png"))
    assert not errors


def test_refresh_now_recrawls_without_restart(page, server_url, shots):
    page.goto(server_url + "/", wait_until="networkidle")
    errors = _js_errors(page)
    page.locator("#splash-ok").click()

    def stats():
        return page.evaluate(
            "async () => (await fetch('/api/stats')).json()")

    before = stats()["refresh"]["last_run"]
    page.locator("#refresh").click()
    deadline = time.time() + 30
    refresh = stats()["refresh"]
    while time.time() < deadline:
        refresh = stats()["refresh"]
        if refresh["last_run"] != before and not refresh["running"]:
            break
        time.sleep(0.2)
    assert refresh["last_run"] != before, "Refresh now must re-crawl"
    assert refresh["last_stats"]["fetched"] >= 1
    assert refresh["last_stats"]["stored"] == 0, refresh["last_stats"]
    # the status line is rewritten by the UI's 1s stats
    # poll after the crawl finishes -- wait for it
    page.wait_for_function(
        "() => document.querySelector('#status').textContent"
        ".includes('refreshed')", timeout=15000)
    assert "refreshed" in page.locator("#status").inner_text()
    page.screenshot(path=str(shots / "after-refresh.png"))
    assert not errors
