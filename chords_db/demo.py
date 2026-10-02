"""Built-in demo chord source: a self-contained local site.

`python -m chords_db --demo` serves a handful of original chord
sheets from a tiny in-process HTTP site and crawls them through
the normal pipeline, so the local web UI has real data to show
without pointing at any external website. Every sheet here is
original material written for this project, and the site's
robots.txt explicitly allows automated access.

`LocalSite` is also the test-server machinery shared with the
pytest fixtures: path -> (content-type, body[, status,
send_etag, etag]), with ETag revalidation (304) support.
"""
from __future__ import annotations

import re
import socketserver
import threading
from http.server import BaseHTTPRequestHandler
from typing import Dict, List, Optional, Tuple

ROBOTS_ALLOW = "User-agent: *\nAllow: /\n"

# Seed listing page for the demo site.
DEMO_SEED = "/chords/"

# (path, ChordPro sheet) -- original lyrics written for this demo.
DEMO_SONGS: List[Tuple[str, str]] = [
    (
        "/chords/1",
        """{title: Riverside Walk}
{artist: The Mekong Trio}
[Verse 1]
[Am]Walking [F]down by the [C]river,
[G]watching the [Em]fishing boats [Dm]glide
[Verse 2]
[Am]Monsoon [F]clouds [C]gather,
[G]rain on the [Em]water, [Dm]riding the tide
[Chorus]
[G]Sing it [C]soft and [G]slow,
[Am]let the [F]evening [C]flow
""",
    ),
    (
        "/chords/2",
        """{title: Palm Shade}
{artist: The Mekong Trio}
[Verse 1]
[Dm]Resting [G]under the [C]palm trees,
[Am]counting the [Bm]kites that [F]fly
[Verse 2]
[Dm]Children [G]laugh in the [C]shade,
[Am]chasing the [Bm]dragonflies [F]by
[Chorus]
[G]Hum a [C]tune for the [G]sun,
[Dm]till the [G]day is [C]done
""",
    ),
    (
        "/chords/3",
        """{title: Moonlit Road}
{artist: Sophea Chan}
[Verse 1]
[Em]Lanterns [C]line the old [G]road home,
[D]shadows [A]dance on the [Bm]wall
[Verse 2]
[Em]Crickets [C]sing in the [G]warm night,
[D]whispering [A]as the [Bm]stars fall
[Chorus]
[C]Walk with [G]me through the [Em]moonlit [D]lane,
[A]every [Bm]step a [C]refrain
""",
    ),
    (
        "/chords/4",
        """{title: Rice Field Dawn}
{artist: Sophea Chan}
[Verse 1]
[F]Misty [C]dawn on the [G]rice fields,
[Am]mist on the [Dm]water, [G]bright
[Verse 2]
[F]Buffalo [C]drums in the [G]valley,
[Am]morning [Dm]breaks [G]white
[Chorus]
[C]Sing the [G]day into [F]flower,
[Am]greet the [Dm]rising [G]light
""",
    ),
]

_TITLE_RE = re.compile(r"^\{title:\s*(.*?)\}\s*$", re.MULTILINE)


def demo_listing() -> str:
    items = []
    for path, sheet in DEMO_SONGS:
        m = _TITLE_RE.search(sheet)
        title = m.group(1) if m else path.strip("/")
        items.append(f'  <li><a href="{path}">{title}</a></li>')
    return "<html><body><ul>\n" + "\n".join(items) + "\n</ul></body></html>\n"


def demo_routes() -> Dict[str, Tuple]:
    routes: Dict[str, Tuple] = {
        "/robots.txt": ("text/plain", ROBOTS_ALLOW),
        DEMO_SEED: ("text/html", demo_listing()),
    }
    for path, sheet in DEMO_SONGS:
        routes[path] = ("text/chordpro", sheet)
    return routes


class LocalSite:
    """A tiny local HTTP site standing in for a chord website.

    Routes map to `(content-type, body)` or the extended
    `(content-type, body, status, send_etag, etag)`; conditional
    GETs with a matching If-None-Match are answered with 304.
    """

    def __init__(
        self,
        routes: Optional[Dict[str, Tuple]] = None,
        host: str = "127.0.0.1",
        port: int = 0,
    ):
        self.routes: Dict[str, Tuple] = dict(routes or {})
        self.requests: List[Tuple[Optional[str], str]] = []
        site = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?")[0]
                site.requests.append((self.headers.get("User-Agent"), path))
                entry = site.routes.get(path)
                # Defaults for a dropped route (404): never send
                # an ETag, so a 404 cannot be revalidated against.
                send_etag = False
                etag = None
                if entry is None:
                    status, ctype, body = 404, "text/plain", "not found"
                else:
                    ctype, body = entry[0], entry[1]
                    status = entry[2] if len(entry) > 2 else 200
                    # A 4th tuple element suppresses the ETag header,
                    # standing in for sites that send none.
                    send_etag = len(entry) < 4 or entry[3]
                    etag = entry[4] if len(entry) > 4 else f'"{path}"'
                    # Honor conditional GETs: an If-None-Match that
                    # matches this route's etag means the client's
                    # copy is current.
                    if (
                        status == 200
                        and send_etag
                        and self.headers.get("If-None-Match") == etag
                    ):
                        status, body = 304, ""
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                if send_etag:
                    self.send_header("ETag", etag)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass  # keep the console to the crawl logs

        server_cls = type(
            "LocalSiteServer",
            (socketserver.ThreadingTCPServer,),
            {"daemon_threads": True, "allow_reuse_address": True},
        )
        self._server = server_cls((host, port), _Handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    # -- API (mirrors the old FakeSite test helper) ------------------

    def url(self, path: str) -> str:
        return self.base + path

    def set_route(
        self, path: str, ctype: str, body: str, status: int = 200,
        send_etag: bool = True, etag: Optional[str] = None,
    ) -> None:
        # etag=None stands for the default '"{path}"'; a route
        # with send_etag=False suppresses the header entirely.
        self.routes[path] = (
            ctype, body, status, send_etag,
            etag if etag is not None else f'"{path}"',
        )

    def drop_route(self, path: str) -> None:
        self.routes.pop(path, None)

    def set_robots(self, rules: str) -> None:
        self.set_route("/robots.txt", "text/plain", rules)

    def requests_for(self, path: str) -> List[Optional[str]]:
        return [ua for ua, p in self.requests if p == path]

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


def start_demo(host: str = "127.0.0.1", port: int = 0) -> LocalSite:
    """Start the demo chord site; call .close() when done."""
    return LocalSite(demo_routes(), host=host, port=port)
