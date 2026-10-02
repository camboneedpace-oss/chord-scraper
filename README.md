# chord-scraper

[![Python package](https://github.com/camboneedpace-oss/chord-scraper/actions/workflows/python-package.yml/badge.svg)](https://github.com/camboneedpace-oss/chord-scraper/actions/workflows/python-package.yml)

Polite scraper for chord-sheet websites (e.g. khmerchords.com) that produces a
normalized, deduplicated SQLite dataset.

## Compliance gate — read before crawling

- The target site already returned **403** to automated fetches. That is a hard
  stop: check `robots.txt` and the Terms of Service, and look for an official
  API or data dump before scraping anything. The crawler enforces the
  mechanical parts — it honors `robots.txt` and aborts on 401/403 — but the
  ToS and licensing review is yours to make.
- Chord sheets and lyrics are copyrighted arrangements. Personal/academic use
  only — do not republish the dataset.

## Layout

```
chords_db/
  schema.sql        SQLite DDL (artists, songs, chord_sheets, sheet_lines,
                  sheet_chords, chord_vocab, crawl_log)
  models.py         Pydantic models for the normalized sheet
  normalize.py      Unicode (NFC) + chord-token canonicalization
  parse.py          SiteProfile + ChordProParser + HtmlSpanParser
  fetch.py          PoliteFetcher: robots.txt, per-host delay, retries, disk cache + ETag sidecar
  store.py          Idempotent SQLite upserts keyed on (source_id, checksum)
  pipeline.py       Crawler: frontier -> fetch (etag-revalidated) -> parse -> validate -> store
  demo.py           Built-in demo chord site (original sheets) + LocalSite test server
  web.py            Local web UI + JSON API over the dataset, and the background Refresher
  __main__.py       CLI: python -m chords_db <seeds...> (also a chord-scraper console script)
tests/
  conftest.py       Shared fixtures: local HTTP site standing in for a chord site
  test_fetch.py     PoliteFetcher unit tests
  test_parse.py     Normalization + parser unit tests (pytest or standalone)
  test_pipeline.py  End-to-end crawl -> store tests
  test_cli.py       CLI flags, exit codes, and a fake-site crawl through main()
  test_demo.py      Bundled demo site + LocalSite test-server tests
  test_web.py       Web API, Refresher, and settings validation tests
  qa_browser.py     Browser QA (pytest marker: browser) — real Chrome via Playwright
```

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -q

# Zero-config: everything local -- the bundled demo
# site plus the web UI with gentle auto-refresh
python -m chords_db

# Then, with a compliant data source:
python -m chords_db --delay 1.5 --max-pages 50 \
    --parser chordpro --url-pattern '/chords/\d+' \
    --db chords.db https://example.test/sitemap

# HTML sites: point the parser at the page markup
python -m chords_db --parser html \
    --title-selector 'h1.song-title' --artist-selector '.artist' \
    --db chords.db https://example.test/sitemap
```

The CLI prints the crawl stats as one JSON line and exits 0 on
success, 1 when the site refuses automated access (401/403), and
2 for a bad `--url-pattern`. `pip install -e .` also installs a
`chord-scraper` console script with the same interface. Useful
flags: `--cache-dir`, `--no-follow` (treat seeds as song pages
only), `-v`/`-vv` for log detail.

Re-crawls are cheap: successful pages are cached on disk alongside their
ETag, and each URL's last-seen ETag (also recorded in `crawl_log`) is
replayed as `If-None-Match` — a `304` answer skips parsing and storage
entirely (`not_modified` in the stats).

## Local web UI + auto-refresh

Serve the dataset locally with `--serve`:

```bash
python -m chords_db --serve --port 8000 \
    --refresh-interval 3600 \
    --parser chordpro --url-pattern '/chords/\d+' \
    --db chords.db https://example.test/sitemap
```

Then open http://127.0.0.1:8000 — a song list with live chord sheets
(chords rendered above the lyric lines), search, and a status line.
The page polls `/api/stats` every 10s and reloads the list whenever
the dataset changes; the **Refresh now** button triggers one crawl
immediately (`POST /api/refresh`). A **Chords** tab lists the whole
chord vocabulary — every chord, how many songs use it, and which
songs (click through to the sheet). The gear button opens a settings
panel to change the refresh interval/jitter on the fly (`POST
/api/settings`, no restart needed), and a first-run splash explains
local mode and how to point the crawler at a compliant source.

`--refresh-interval N` starts a background thread that re-crawls the
seeds every N seconds using the same polite pipeline (robots-checked,
throttled, ETag-revalidated — an unchanged site costs almost nothing).
`--refresh-seeds` overrides which URLs the refresher hits (comma-separated);
default it re-crawls the seeds given on the command line. The API:

- `GET /` — the HTML UI
- `GET /api/songs?q=...` — song list (filtered by title/artist)
- `GET /api/songs/<id>` — full sheet: metadata, sections, lines, chord offsets
- `GET /api/chords` — chord vocabulary: every chord with song counts and the songs using it
- `GET /api/stats` — dataset counts + refresher state
- `POST /api/refresh` — run one crawl pass now
- `POST /api/settings` — update the refresher's `interval` (>= 1s) and `jitter` (>= 0s)

**Bundled demo source:** with no arguments at all, the tool turns fully local
-- it crawls a small built-in site of original chord sheets (its `robots.txt`
allows bots), serves the web UI, and re-crawls gently (every 60s ± 10s):

```bash
python -m chords_db          # local web UI at http://127.0.0.1:8000
```

`--demo` alone crawls the same built-in site and exits; add `--serve` to
keep the UI up. `--refresh-jitter N` adds a random 0..N seconds to every
scheduled pass so crawlers on identical intervals don't all hit the source
at the same moment (default 10 in the zero-config local web). In the UI,
press `/` to jump to the search box (Escape leaves it, and closes any
open panel).

**Compliance:** auto-refresh re-requests the source site on a schedule,
so only point it at a source whose `robots.txt` and Terms of Service
allow automated access. khmerchords.com currently returns **403** to
automated fetches — the refresher will abort each cycle (and the UI
shows a BLOCKED badge) until that is resolved or an official API/dump
is available.

`Crawler.run()` returns counts for `fetched`, `stored`, `duplicate`,
`skipped`, `errors`, and `not_modified`.

## Browser QA

`tests/qa_browser.py` drives real Chrome through the web UI against a
live in-process server (demo site crawl + Refresher + WebApp), so the
splash, song sheets, Chords tab, and settings panel are exercised
exactly as a user sees them. It carries the `browser` pytest marker
and skips itself when Playwright or Chrome is missing, so plain unit
test runs stay dependency-free:

```bash
pip install -e ".[browser]" && playwright install chrome
python -m pytest tests/qa_browser.py -m browser -v
```

With Playwright installed it also runs as part of the full suite
(`python -m pytest tests/ -q`); without it, the module skips.

## Pre-commit

`pip install pre-commit && pre-commit install` runs the same
flake8 gate as CI on every commit, so undefined names and
syntax errors never reach a push.

## Extending to a new site

1. Fill a `SiteProfile` (URL pattern, container/span selectors, title/artist selectors).
2. If the site serves JSON or ChordPro text, use `ChordProParser`; for inline
   `<span class="chord">` markup use `HtmlSpanParser`. Absolutely-positioned
   chords (CSS top/left over a text layer) need a per-site coordinate mapper —
   not implemented; such pages are flagged via PUA detection instead.
3. Per-page metadata (title, artist, key) is passed via the `meta` dict —
   merge it from listing pages if detail pages lack it.
