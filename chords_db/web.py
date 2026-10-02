"""Local web server: browse the dataset, auto-refresh from the source.

`python -m chords_db --serve` runs a small stdlib web app over the
SQLite dataset: an HTML page listing songs (with live chord sheets)
and a JSON API. With --refresh-interval a background thread re-crawls
the source site on a schedule; each pass is a normal Crawler run --
robots-checked, throttled, ETag-revalidated -- so an unchanged site
answers 304 and costs almost nothing. The page polls /api/stats and
reloads the song list whenever the dataset changes.
"""
from __future__ import annotations

import json
import logging
import math
import random
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from .fetch import PoliteFetcher
from .parse import Parser, SiteProfile
from .pipeline import Crawler
from .store import Store

log = logging.getLogger("chords")

_COUNT_TABLES = (
    "artists", "songs", "chord_sheets", "sheet_lines",
    "sheet_chords", "chord_vocab",
)


class HttpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _nonnegative(value: Any, name: str, minimum: float) -> Optional[float]:
    """Validate a JSON number coming from the settings panel."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    if math.isnan(value) or math.isinf(value) or value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return float(value)


# ----------------------------------------------------------------------
# Background refresher
# ----------------------------------------------------------------------

class Refresher:
    """Re-crawls the source site on a fixed schedule in a daemon
    thread. Each pass builds its own fetcher/store (the crawler is
    single-threaded per pass) and runs the same pipeline as the CLI,
    so politeness rules and ETag revalidation always apply."""

    def __init__(
        self,
        seeds: List[str],
        db_path: str,
        cache_dir: str = ".cache",
        delay: float = 1.5,
        parser: Optional[Parser] = None,
        profile: Optional[SiteProfile] = None,
        interval: float = 3600.0,
        jitter: float = 0.0,
        follow_links: bool = True,
        max_pages: Optional[int] = None,
    ):
        if parser is None or profile is None:
            raise ValueError("Refresher needs a parser and a SiteProfile")
        self.seeds = list(seeds)
        self.db_path = db_path
        self.cache_dir = cache_dir
        self.delay = delay
        self.parser = parser
        self.profile = profile
        self.interval = interval
        self.jitter = jitter
        self.follow_links = follow_links
        self.max_pages = max_pages
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state: Dict[str, Any] = {
            "interval": interval, "jitter": jitter,
            "running": False,
            "last_run": None, "last_stats": None, "blocked": False,
        }

    # -- state ----------------------------------------------------------

    def state(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def configure(
        self,
        interval: Optional[float] = None,
        jitter: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Update the schedule; takes effect from the next
        scheduled pass (a wait already in progress finishes)."""
        if interval is not None:
            self.interval = float(interval)
        if jitter is not None:
            self.jitter = float(jitter)
        with self._lock:
            self._state["interval"] = self.interval
            self._state["jitter"] = self.jitter
        return self.state()

    # -- lifecycle ------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._loop, name="chord-refresher", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=self.delay + 5)

    def refresh_now(self) -> None:
        """Run one pass outside the schedule (used by POST /api/refresh).
        Never blocks the caller; overlapping runs are ignored."""
        threading.Thread(
            target=self.run_once, name="chord-refresh-once", daemon=True
        ).start()

    def _loop(self) -> None:
        self.run_once()
        while not self._stop_evt.wait(self._sleep_for()):
            self.run_once()

    def _sleep_for(self) -> float:
        """Scheduled wait: the fixed interval plus a random
        0..jitter, so crawlers on identical schedules don't all
        hit the source at the same moment."""
        if self.jitter > 0:
            return self.interval + random.uniform(0, self.jitter)
        return self.interval

    def run_once(self) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._state["running"]:
                return None
            self._state["running"] = True
        try:
            fetcher = PoliteFetcher(cache_dir=self.cache_dir, delay=self.delay)
            store = Store(self.db_path)
            try:
                crawler = Crawler(
                    fetcher, self.parser, store, self.profile,
                    seeds=self.seeds, follow_links=self.follow_links,
                )
                stats = crawler.run(max_pages=self.max_pages)
                stats["blocked"] = crawler.blocked
            finally:
                store.close()
                fetcher.close()
        except Exception as exc:  # the loop must never die on one bad pass
            stats = {"errors": 1, "blocked": False, "error": str(exc)}
        with self._lock:
            self._state.update(
                running=False, last_run=time.time(),
                last_stats=stats, blocked=bool(stats.get("blocked")),
            )
        log.info("refresh done: %s", stats)
        return stats


# ----------------------------------------------------------------------
# Read side of the dataset (per-request connections: the refresher
# writes from its own Store, so readers never hold a write lock)
# ----------------------------------------------------------------------

class WebApp:
    def __init__(self, db_path: str, refresher: Optional[Refresher] = None):
        self.db_path = db_path
        self.refresher = refresher
        self._httpd: Optional[_Server] = None

    def _read(self, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            return fn(conn)
        finally:
            conn.close()

    # -- API payloads ----------------------------------------------------

    def songs(self, q: str = "") -> Dict[str, Any]:
        def read(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
            like = f"%{q}%"
            rows = conn.execute(
                """
                SELECT s.id, s.title, s.title_khmer, a.name AS artist,
                       s.song_key, s.capo, s.updated_at
                FROM songs s JOIN artists a ON a.id = s.artist_id
                WHERE ? = ''
                   OR s.title LIKE ? OR a.name LIKE ? OR s.title_khmer LIKE ?
                ORDER BY s.title COLLATE NOCASE LIMIT 500
                """,
                (q, like, like, like),
            ).fetchall()
            return [dict(r) for r in rows]

        return {"songs": self._read(read)}

    def song(self, song_id: int) -> Dict[str, Any]:
        def read(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
            song = conn.execute(
                """
                SELECT s.id, s.source_id, s.title, s.title_khmer, s.language,
                       s.song_key, s.capo, s.tempo_bpm, s.tuning, s.tags,
                       s.raw_url, s.updated_at,
                       a.name AS artist, a.name_khmer AS artist_khmer
                FROM songs s JOIN artists a ON a.id = s.artist_id
                WHERE s.id = ?
                """,
                (song_id,),
            ).fetchone()
            if song is None:
                return None
            out = dict(song)
            out["tags"] = json.loads(song["tags"] or "[]")
            sheet = conn.execute(
                """
                SELECT id, instrument, format, quality_score
                FROM chord_sheets WHERE song_id = ?
                ORDER BY fetched_at DESC, id DESC LIMIT 1
                """,
                (song_id,),
            ).fetchone()
            if sheet is None:
                out.update(instrument=None, format=None,
                           quality_score=None, sections=[])
                return out
            out["instrument"] = sheet["instrument"]
            out["format"] = sheet["format"]
            out["quality_score"] = sheet["quality_score"]
            lines = conn.execute(
                """
                SELECT id, seq, section, label, text FROM sheet_lines
                WHERE sheet_id = ? ORDER BY seq
                """,
                (sheet["id"],),
            ).fetchall()
            chords = conn.execute(
                """
                SELECT line_id, chord, char_offset FROM sheet_chords
                WHERE line_id IN (SELECT id FROM sheet_lines WHERE sheet_id = ?)
                ORDER BY line_id, char_offset
                """,
                (sheet["id"],),
            ).fetchall()
            by_line: Dict[int, List[Dict[str, Any]]] = {}
            for ch in chords:
                by_line.setdefault(ch["line_id"], []).append(
                    {"chord": ch["chord"], "offset": ch["char_offset"]}
                )
            sections: List[Dict[str, Any]] = []
            for line in lines:
                if not sections or (
                    sections[-1]["type"] != line["section"]
                    or sections[-1]["label"] != line["label"]
                ):
                    sections.append(
                        {"type": line["section"], "label": line["label"],
                         "lines": []}
                    )
                sections[-1]["lines"].append(
                    {"text": line["text"], "chords": by_line.get(line["id"], [])}
                )
            out["sections"] = sections
            return out

        data = self._read(read)
        if data is None:
            raise HttpError(404, f"no song with id {song_id}")
        return data

    def chords(self) -> Dict[str, Any]:
        """Chord vocabulary: every chord in the dataset with the
        songs (and occurrence count) that use it."""
        def read(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
            summary: Dict[str, Dict[str, Any]] = {}
            for row in conn.execute(
                """
                SELECT sc.chord AS chord,
                       COUNT(DISTINCT s.id) AS song_count,
                       COUNT(*) AS occurrences
                FROM sheet_chords AS sc
                JOIN sheet_lines AS sl ON sl.id = sc.line_id
                JOIN chord_sheets AS cs ON cs.id = sl.sheet_id
                JOIN songs AS s ON s.id = cs.song_id
                GROUP BY sc.chord
                ORDER BY song_count DESC, chord
                """
            ):
                summary[row["chord"]] = {
                    "chord": row["chord"],
                    "song_count": row["song_count"],
                    "occurrences": row["occurrences"],
                    "songs": [],
                }
            for chord, song_id, title in conn.execute(
                """
                SELECT DISTINCT sc.chord, s.id, s.title
                FROM sheet_chords AS sc
                JOIN sheet_lines AS sl ON sl.id = sc.line_id
                JOIN chord_sheets AS cs ON cs.id = sl.sheet_id
                JOIN songs AS s ON s.id = cs.song_id
                ORDER BY sc.chord, s.title
                """
            ):
                summary[chord]["songs"].append(
                    {"id": song_id, "title": title}
                )
            return list(summary.values())
        return {"chords": self._read(read)}

    def stats_payload(self) -> Dict[str, Any]:
        def read(conn: sqlite3.Connection) -> Dict[str, int]:
            return {
                t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in _COUNT_TABLES
            }

        payload: Dict[str, Any] = {"counts": self._read(read)}
        if self.refresher is not None:
            payload["refresh"] = self.refresher.state()
        return payload

    # -- serving ----------------------------------------------------------

    def make_server(self, host: str = "127.0.0.1", port: int = 0) -> "_Server":
        return _Server((host, port), self)

    def serve(self, host: str = "127.0.0.1", port: int = 8000) -> None:
        self._httpd = self.make_server(host, port)
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        if self.refresher is not None:
            self.refresher.stop()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: Any, app: WebApp):
        self.app = app
        super().__init__(addr, _Handler)


class _Handler(BaseHTTPRequestHandler):
    server_version = "chord-scraper/1.0"

    def log_message(self, *args: object) -> None:
        pass  # keep the console to the crawl logs

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._html(INDEX_HTML)
            elif parsed.path == "/api/songs":
                q = parse_qs(parsed.query).get("q", [""])[0]
                self._json(self.server.app.songs(q))
            elif parsed.path == "/api/chords":
                self._json(self.server.app.chords())
            elif parsed.path.startswith("/api/songs/"):
                raw = parsed.path.rsplit("/", 1)[1]
                if not raw.isdigit():
                    raise HttpError(400, "song id must be an integer")
                self._json(self.server.app.song(int(raw)))
            elif parsed.path == "/api/stats":
                self._json(self.server.app.stats_payload())
            else:
                raise HttpError(404, "not found")
        except HttpError as exc:
            self._error(exc.code, exc.message)
        except Exception as exc:  # malformed request must not kill the server
            self._error(500, str(exc))

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/refresh":
            if self.server.app.refresher is None:
                self._error(
                    409, "no auto-refresh configured (use --refresh-interval)"
                )
                return
            self.server.app.refresher.refresh_now()
            self._json({"accepted": True})
        elif parsed.path == "/api/settings":
            self._post_settings()
        else:
            self._error(404, "not found")

    def _post_settings(self) -> None:
        """POST /api/settings: {interval, jitter} for the refresher."""
        app = self.server.app
        if app.refresher is None:
            self._error(
                409, "no auto-refresh configured (use --refresh-interval)"
            )
            return
        try:
            payload = self._json_body()
            interval = _nonnegative(payload.get("interval"), "interval", 1)
            jitter = _nonnegative(payload.get("jitter"), "jitter", 0)
        except (ValueError, TypeError) as exc:
            self._error(400, str(exc))
            return
        state = app.refresher.configure(interval=interval, jitter=jitter)
        self._json({"accepted": True, "refresh": state})

    def _json_body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            raise ValueError("bad Content-Length")
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("body must be a JSON object")
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    # -- response helpers ---------------------------------------------------

    def _html(self, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code: int, message: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        data = json.dumps({"error": message}).encode("utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# ----------------------------------------------------------------------
# Single-page UI: song list + live chord sheet, auto-refreshing
# ----------------------------------------------------------------------

INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>chord-scraper &middot; local chord sheets</title>
<style>
:root{--bg:#14161a;--fg:#e8e6e1;--dim:#9a978f;--accent:#e0a458;--line:#2a2d33}
*{box-sizing:border-box}
[hidden]{display:none !important}  /* .overlay's display:flex would otherwise win */
body{margin:0;font:15px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg);display:flex;flex-direction:column;height:100vh;height:100dvh}
header{display:flex;gap:12px;align-items:center;padding:14px 18px;border-bottom:1px solid var(--line);flex-wrap:wrap}
h1{font-size:17px;margin:0}
#q{flex:1;min-width:180px;padding:7px 10px;border-radius:8px;border:1px solid var(--line);background:#1c1f24;color:var(--fg)}
button{padding:7px 12px;border-radius:8px;border:1px solid var(--line);background:#23262c;color:var(--fg);cursor:pointer}
button:hover{border-color:var(--accent)}
#status{color:var(--dim);font-size:12.5px}
main{flex:1;min-height:0;display:grid;grid-template-columns:300px 1fr}
#sidebar{display:flex;flex-direction:column;min-height:0;border-right:1px solid var(--line)}
#views{display:flex;gap:6px;padding:8px 10px;border-bottom:1px solid var(--line)}
#views .tab{flex:1;padding:5px 0;font-size:12.5px}
#views .tab.active{background:var(--accent);color:#14161a;border-color:var(--accent);font-weight:600}
#songs,#chords{list-style:none;margin:0;padding:0;overflow-y:auto;flex:1}
#chords li{padding:10px 14px;border-bottom:1px solid var(--line);cursor:pointer}
#chords li:hover{background:#1c1f24}
#chords .c{font-weight:600}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:10;padding:18px}
.card{background:#1c1f24;border:1px solid var(--line);border-radius:12px;max-width:540px;width:100%;max-height:85vh;overflow-y:auto;padding:20px 22px}
.card h1{font-size:18px;margin:0 0 4px}
.card h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);margin:16px 0 6px}
.card p{margin:6px 0;font-size:14px}
.card code{background:#14161a;padding:2px 6px;border-radius:6px;font-size:12.5px;word-break:break-all}
.card .row{display:flex;gap:8px;margin-top:14px}
.card label{display:block;margin:10px 0;font-size:13.5px}
.card input[type=number]{width:110px;padding:6px 8px;border-radius:8px;border:1px solid var(--line);background:#14161a;color:var(--fg);margin-left:8px}
.err{color:#e06c5b;font-size:12.5px;min-height:1em}
.chord-name{font-size:26px;margin:4px 0 8px}
.songlinks{list-style:none;margin:0;padding:0}
.songlinks li{padding:8px 10px;border:1px solid var(--line);border-radius:8px;margin-bottom:6px;cursor:pointer}
.songlinks li:hover{border-color:var(--accent)}
.note{color:var(--dim);font-size:12.5px}
#songs li{padding:10px 14px;border-bottom:1px solid var(--line);cursor:pointer}
#songs li:hover,#songs li.active{background:#1c1f24}
#songs .t{font-weight:600}
#songs .a{color:var(--dim);font-size:12.5px}
#sheet{overflow-y:auto;padding:18px 22px}
.empty{color:var(--dim)}
.meta{color:var(--dim);font-size:12.5px;margin-bottom:12px}
.sheet h2{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--accent);margin:18px 0 6px}
pre.line{margin:0;font:13px/1.6 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
pre.chords{color:var(--accent)}
@media(max-width:760px){main{grid-template-columns:1fr}#sidebar{border-right:0;border-bottom:1px solid var(--line);max-height:40vh}#chords li{display:flex;align-items:baseline;gap:10px;padding:8px 12px}#chords .c{white-space:nowrap}#chords .a{flex:1}}
</style>
</head>
<body>
<header>
  <h1>Chord sheets</h1>
  <input id="q" type="search" placeholder="search title or artist&hellip;" autocomplete="off">
  <button id="refresh" title="re-crawl the source site now">Refresh now</button>
  <button id="gear" title="auto-refresh settings">&#9881;</button>
  <button id="about" title="about chord-scraper">?</button>
  <span id="status">loading&hellip;</span>
</header>
<main>
  <div id="sidebar">
    <nav id="views">
      <button class="tab active" data-view="songs">Songs</button>
      <button class="tab" data-view="chords">Chords</button>
    </nav>
    <ul id="songs"></ul>
    <ul id="chords" hidden></ul>
  </div>
  <article id="sheet"><p class="empty">pick a song from the list</p></article>
</main>
<div id="settings-backdrop" class="overlay" hidden>
  <div class="card" role="dialog" aria-label="auto-refresh settings">
    <h1>Auto-refresh</h1>
    <form id="set-form">
      <label>Interval (seconds)<input id="set-interval" type="number" min="1" step="1" required></label>
      <label>Jitter (seconds)<input id="set-jitter" type="number" min="0" step="0.5"></label>
      <p class="note">Takes effect from the next scheduled pass &mdash; a wait already in progress finishes first. Jitter spreads otherwise identical crawlers out so they don&rsquo;t all hit the source at the same moment. Interval minimum is 1s.</p>
      <div class="row">
        <button id="set-save" type="button">Save</button>
        <button id="set-cancel" type="button">Cancel</button>
      </div>
      <p id="set-err" class="err"></p>
    </form>
    <p id="set-note" hidden class="note">Auto-refresh is off for this run &mdash; restart with <code>--refresh-interval N</code> to enable it.</p>
  </div>
</div>
<div id="splash-backdrop" class="overlay" hidden>
  <div class="card" role="dialog" aria-label="about chord-scraper">
    <h1>chord-scraper</h1>
    <p class="note">local chord-sheet crawler + web UI</p>
    <h2>Everything stays local</h2>
    <p>The web UI, the SQLite dataset and the crawler all run on this machine. Nothing is uploaded anywhere; the only network traffic is to the chord source site itself.</p>
    <h2>The demo source</h2>
    <p>Started with no arguments, this app crawls a small built-in site of <em>original</em> chord sheets (its robots.txt allows bots) and re-crawls it gently in the background &mdash; so the UI has real data with no external site involved.</p>
    <h2>Point it at your own source</h2>
    <p>Restart with seed URLs, for example:</p>
    <p><code>python -m chords_db --serve --refresh-interval 3600 --url-pattern '/chords/\\d+' https://example.test/sitemap</code></p>
    <p>Only crawl sites whose robots.txt and Terms of Service allow automated access. khmerchords.com currently returns <strong>403</strong> to bots, so the crawler refuses it until an official API or data dump is available.</p>
    <div class="row"><button id="splash-ok">Start exploring</button></div>
  </div>
</div>
<script>
const $ = s => document.querySelector(s);
const api = (p, o) => fetch(p, o).then(r => { if (!r.ok) throw new Error(r.status); return r.json(); });
let songs = [], chords = [], selected = null, prevCounts = null;
let chordsLoaded = false, lastStats = null;
const SEEN_KEY = "chord-scraper.seen";

function esc(s){ return String(s ?? "").replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c])); }

async function loadSongs(){ songs = (await api("/api/songs")).songs; renderSongs(); }

function renderSongs(){
  const q = $("#q").value.trim().toLowerCase();
  const hits = songs.filter(s => !q || s.title.toLowerCase().includes(q) ||
    (s.artist || "").toLowerCase().includes(q) || (s.title_khmer || "").includes(q));
  $("#songs").innerHTML = hits.map(s =>
    `<li data-id="${s.id}" class="${selected === s.id ? "active" : ""}">` +
    `<div class="t">${esc(s.title)}${s.title_khmer ? ` <span class="a">${esc(s.title_khmer)}</span>` : ""}</div>` +
    `<div class="a">${esc(s.artist)}${s.song_key ? ` &middot; key ${esc(s.song_key)}` : ""}${s.capo ? ` &middot; capo ${s.capo}` : ""}</div></li>`
  ).join("") || `<li><div class="a">no matches</div></li>`;
  $("#songs").querySelectorAll("li[data-id]").forEach(li =>
    li.onclick = () => select(+li.dataset.id));
}

async function select(id){
  selected = id; renderSongs();
  renderSheet(await api("/api/songs/" + id));
}

function renderSheet(s){
  const meta = [s.artist, s.song_key && "key " + s.song_key, s.capo && "capo " + s.capo,
    s.tempo_bpm && s.tempo_bpm + " bpm", s.tuning && "tuning " + s.tuning]
    .filter(Boolean).join(" &middot; ");
  $("#sheet").innerHTML = `<div class="meta">${esc(meta)}</div>` +
    s.sections.map(sec =>
      `<section class="sheet"><h2>${esc(sec.label || sec.type)}</h2>` +
      sec.lines.map(line => {
        const row = chordRow(line.chords);
        return (row ? `<pre class="line chords">${esc(row)}</pre>` : "") +
               `<pre class="line">${esc(line.text) || " "}</pre>`;
      }).join("") + "</section>"
    ).join("");
}

// Lay chords above the lyric line at their character offsets,
// chordpro-style: one monospace row of chords over one row of text.
function chordRow(chords){
  if (!chords || !chords.length) return "";
  const cells = [];
  for (const c of chords)
    for (let i = 0; i < c.chord.length; i++) cells[c.offset + i] = c.chord[i];
  let out = "";
  for (let i = 0; i < cells.length; i++) out += cells[i] || " ";
  return out.replace(/\\s+$/, "");
}

// -- sidebar views: songs / chord vocabulary --------------------
function showView(name){
  document.querySelectorAll("#views .tab").forEach(t =>
    t.classList.toggle("active", t.dataset.view === name));
  $("#songs").hidden = name !== "songs";
  $("#chords").hidden = name !== "chords";
  if (name === "chords") loadChords();
}
document.querySelectorAll("#views .tab").forEach(t =>
  t.onclick = () => showView(t.dataset.view));

async function loadChords(){
  chords = (await api("/api/chords")).chords;
  chordsLoaded = true;
  $("#chords").innerHTML = chords.map(c =>
    `<li data-chord="${esc(c.chord)}">` +
    `<div class="c">${esc(c.chord)}</div>` +
    `<div class="a">${c.song_count} song${c.song_count === 1 ? "" : "s"} &middot; ` +
    `${c.occurrences} occurrence${c.occurrences === 1 ? "" : "s"}</div></li>`
  ).join("") || `<li><div class="a">no chords yet</div></li>`;
  $("#chords").querySelectorAll("li[data-chord]").forEach(li =>
    li.onclick = () => selectChord(li.dataset.chord));
}

function selectChord(name){
  const c = chords.find(x => x.chord === name);
  if (!c) return;
  $("#sheet").innerHTML =
    `<div class="chord-name">${esc(c.chord)}</div>` +
    `<div class="meta">${c.song_count} song${c.song_count === 1 ? "" : "s"} &middot; ` +
    `${c.occurrences} occurrence${c.occurrences === 1 ? "" : "s"}</div>` +
    `<ul class="songlinks">` +
    c.songs.map(s => `<li data-id="${s.id}">${esc(s.title)}</li>`).join("") +
    `</ul>`;
  $("#sheet").querySelectorAll(".songlinks li").forEach(li =>
    li.onclick = () => select(+li.dataset.id));
}

async function loadStats(){
  const st = await api("/api/stats");
  lastStats = st;
  const r = st.refresh;
  // textContent (not innerHTML), so use real characters
  // rather than HTML entities here.
  $("#status").textContent =
    `${st.counts.songs} songs · ${st.counts.chord_sheets} sheets` +
    (r ? (r.interval ? ` · auto every ${r.interval}s` + (r.jitter ? ` ±${r.jitter}s` : "") : "") +
      (r.last_run ? ` · refreshed ${new Date(r.last_run * 1000).toLocaleTimeString()}` : "") +
      (r.running ? " · crawling…" : "") +
      (r.blocked ? " · BLOCKED (401/403)" : "") : "");
  return st;
}

$("#q").oninput = renderSongs;
// "/" jumps to the search box; Escape leaves it
document.addEventListener("keydown", e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
  if (e.key === "/" && !typing) { e.preventDefault(); $("#q").focus(); }
  else if (e.key === "Escape" && document.activeElement === $("#q")) $("#q").blur();
  else if (e.key === "Escape")
    document.querySelectorAll(".overlay").forEach(ov => ov.hidden = true);
});
$("#refresh").onclick = async () => {
  try { await api("/api/refresh", {method: "POST"}); } catch (e) {}
  const t0 = Date.now();
  const poll = setInterval(async () => {
    const st = await loadStats();
    if (!st.refresh || !st.refresh.running || Date.now() - t0 > 120000) {
      clearInterval(poll); await loadSongs();
    }
  }, 1000);
};

// -- settings panel: change the refresh schedule live -----------
function openSettings(){
  const r = lastStats && lastStats.refresh;
  $("#set-note").hidden = !!r;
  $("#set-form").hidden = !r;
  if (r) {
    $("#set-interval").value = r.interval;
    $("#set-jitter").value = r.jitter;
  }
  $("#set-err").textContent = "";
  $("#settings-backdrop").hidden = false;
}
// Enter inside a settings input would submit the form and
// reload the page -- swallow it.
$("#set-form").addEventListener("submit", e => e.preventDefault());
$("#gear").onclick = openSettings;
$("#set-cancel").onclick = () => { $("#settings-backdrop").hidden = true; };
$("#set-save").onclick = async () => {
  const body = {
    interval: Number($("#set-interval").value),
    jitter: Number($("#set-jitter").value),
  };
  try {
    await api("/api/settings", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body),
    });
    await loadStats();
    $("#settings-backdrop").hidden = true;
  } catch (e) {
    $("#set-err").textContent =
      "Could not save (HTTP " + e.message + ") — interval must be at least 1s, jitter at least 0.";
  }
};

// -- splash: shown on first run, explains local mode ------------
function openSplash(){ $("#splash-backdrop").hidden = false; }
$("#about").onclick = openSplash;
$("#splash-ok").onclick = () => {
  $("#splash-backdrop").hidden = true;
  try { localStorage.setItem(SEEN_KEY, "1"); } catch (e) {}
};
// clicking the dimmed backdrop closes either panel
document.querySelectorAll(".overlay").forEach(ov =>
  ov.addEventListener("click", e => { if (e.target === ov) ov.hidden = true; }));

(async function init(){
  prevCounts = (await loadStats()).counts;
  await loadSongs();
  let seen = null;
  try { seen = localStorage.getItem(SEEN_KEY); } catch (e) {}
  if (!seen) openSplash();
  // auto-refresh the view whenever the background crawler changes the data
  setInterval(async () => {
    const st = await loadStats();
    if (prevCounts && JSON.stringify(st.counts) !== JSON.stringify(prevCounts)) {
      await loadSongs();
      chordsLoaded = false;  // the vocabulary may have changed too
    }
    prevCounts = st.counts;
  }, 10000);
})();
</script>
</body>
</html>
"""
