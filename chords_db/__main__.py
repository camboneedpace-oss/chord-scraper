"""Command-line entry point: python -m chords_db <seeds...>"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from typing import List, Optional

from .demo import DEMO_SEED, start_demo
from .fetch import PoliteFetcher
from .parse import ChordProParser, HtmlSpanParser, SiteProfile
from .pipeline import Crawler
from .store import Store
from .web import Refresher, WebApp

log = logging.getLogger("chords")

PARSERS = {
    "chordpro": ChordProParser,
    "html": HtmlSpanParser,
}


def build_parser(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m chords_db",
        description=(
            "Polite chord-sheet crawler: seeds -> normalized "
            "SQLite dataset. Honors robots.txt and aborts on 401/403."
        ),
    )
    parser.add_argument(
        "seeds", nargs="*", metavar="seed",
        help="seed URL(s): listing pages or song pages "
             "(not needed with --demo)",
    )
    parser.add_argument(
        "--db", default="chords.db",
        help="SQLite dataset path (default: chords.db)",
    )
    parser.add_argument(
        "--cache-dir", default=".cache",
        help="disk cache for fetched pages (default: .cache)",
    )
    parser.add_argument(
        "--delay", type=float, default=1.5,
        help="seconds between requests to one host (default: 1.5)",
    )
    parser.add_argument(
        "--max-pages", type=int, default=None,
        help="stop after this many fetched pages",
    )
    parser.add_argument(
        "--parser", choices=sorted(PARSERS), default="chordpro",
        help="sheet format to parse (default: chordpro)",
    )
    parser.add_argument(
        "--url-pattern", default=r"/chords/\d+",
        help=r"regex matching song URLs (default: /chords/\d+)",
    )
    parser.add_argument(
        "--no-follow", action="store_true",
        help="do not discover song pages from links in seeds",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="crawl the built-in demo site (original chord sheets, "
             "robots.txt allows bots) instead of external seeds",
    )
    parser.add_argument(
        "--serve", action="store_true",
        help="after the crawl, serve a local web UI over the dataset "
             "(the default when no seeds are given)",
    )
    parser.add_argument(
        "--host", default="127.0.0.1",
        help="bind address for --serve (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port", type=int, default=8000,
        help="port for --serve (default: 8000)",
    )
    parser.add_argument(
        "--refresh-interval", type=float, default=None,
        help="seconds between automatic re-crawls of the seeds "
             "while serving (0 = off; default: 60 for the "
             "zero-config local web, else 0; requires --serve)",
    )
    parser.add_argument(
        "--refresh-jitter", type=float, default=None,
        help="random 0..N extra seconds added to each scheduled "
             "pass so identical crawlers don't stampede the "
             "source (default: 10 for the zero-config local "
             "web, else 0)",
    )
    parser.add_argument(
        "--refresh-seeds", default=None,
        help="comma-separated seeds for auto-refresh "
             "(default: the seeds given on the command line)",
    )
    parser.add_argument(
        "--title-selector", default=None,
        help="CSS selector for the song title (HTML parser)",
    )
    parser.add_argument(
        "--artist-selector", default=None,
        help="CSS selector for the artist (HTML parser)",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="log detail: -v info, -vv debug",
    )
    args = parser.parse_args(argv)
    # Zero-config local web: with nothing given, crawl the bundled
    # demo site and serve the UI over it (with a gentle refresh).
    args.local_web = not args.seeds and not args.demo
    if args.local_web:
        args.demo = True
        args.serve = True
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser(argv)
    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")
    try:
        song_url_pattern = re.compile(args.url_pattern)
    except re.error as exc:
        print(f"error: invalid --url-pattern: {exc}", file=sys.stderr)
        return 2

    profile = SiteProfile(
        name="cli",
        song_url_pattern=song_url_pattern,
        title_sel=args.title_selector,
        artist_sel=args.artist_selector,
    )
    # Resolve refresh defaults: the zero-config local web turns
    # auto-refresh on gently; explicit runs keep it off unless asked.
    interval = args.refresh_interval
    jitter = args.refresh_jitter
    if args.local_web:
        interval = 60 if interval is None else interval
        jitter = 10 if jitter is None else jitter
    else:
        interval = 0 if interval is None else interval
        jitter = 0.0 if jitter is None else jitter
    # The demo site must outlive the crawl and the serve loop.
    demo_site = None
    try:
        if args.demo:
            demo_site = start_demo()
            seeds = [demo_site.url(DEMO_SEED)]
            if args.local_web:
                print(
                    "no seeds given -- crawling the bundled "
                    "local demo site",
                    file=sys.stderr,
                )
        else:
            seeds = list(args.seeds)
        with PoliteFetcher(
            cache_dir=args.cache_dir, delay=args.delay
        ) as fetcher, Store(args.db) as store:
            crawler = Crawler(
                fetcher,
                PARSERS[args.parser](),
                store,
                profile,
                seeds=seeds,
                follow_links=not args.no_follow,
            )
            stats = crawler.run(max_pages=args.max_pages)
        # Always report the counts; the exit code carries the outcome.
        print(json.dumps(stats, sort_keys=True))
        if crawler.blocked:
            print(
                "error: site refused automated access (401/403) -- "
                "check robots.txt and the Terms of Service before crawling",
                file=sys.stderr,
            )
            return 1

        if args.serve:
            refresher = None
            if interval > 0:
                refresh_seeds = (
                    [s.strip() for s in args.refresh_seeds.split(",") if s.strip()]
                    if args.refresh_seeds else seeds
                )
                refresher = Refresher(
                    seeds=refresh_seeds,
                    db_path=args.db,
                    cache_dir=args.cache_dir,
                    delay=args.delay,
                    parser=PARSERS[args.parser](),
                    profile=profile,
                    interval=interval,
                    jitter=jitter,
                    follow_links=not args.no_follow,
                )
            app = WebApp(args.db, refresher=refresher)
            if refresher is not None:
                refresher.start()
            print(
                f"serving http://{args.host}:{args.port} -- Ctrl-C to stop",
                file=sys.stderr,
            )
            try:
                app.serve(args.host, args.port)
            except KeyboardInterrupt:
                pass
            finally:
                app.shutdown()
    finally:
        if demo_site is not None:
            demo_site.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
