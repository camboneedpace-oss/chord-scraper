"""Crawl orchestration: URL frontier -> polite fetch -> parse -> validate -> store."""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse, urldefrag

from .fetch import BlockedError, FetchResult, PoliteFetcher
from .parse import Parser, SiteProfile
from .store import Store

log = logging.getLogger("chords")

# href values may be double-quoted, single-quoted, or unquoted
# (browsers accept all three, so listings in the wild use all three).
_HREF_RE = re.compile(
    r"""href\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
    re.IGNORECASE,
)


class Crawler:
    """BFS over a site's song pages. Stops immediately on a BlockedError and
    skips anything robots.txt disallows."""

    def __init__(
        self,
        fetcher: PoliteFetcher,
        parser: Parser,
        store: Store,
        profile: SiteProfile,
        seeds: List[str],
        follow_links: bool = True,
    ):
        self.fetcher = fetcher
        self.parser = parser
        self.store = store
        self.profile = profile
        self._frontier: List[str] = list(seeds)
        self._seen = set(seeds)
        self._follow_links = follow_links
        # Set when a BlockedError aborts the crawl early
        # (the site answered 401/403); callers like the
        # CLI use it to exit non-zero.
        self.blocked = False

    def run(self, max_pages: Optional[int] = None) -> Dict[str, int]:
        stats = {
            "fetched": 0, "stored": 0, "duplicate": 0,
            "skipped": 0, "errors": 0, "not_modified": 0,
        }
        while self._frontier and (max_pages is None or stats["fetched"] < max_pages):
            url = self._frontier.pop(0)

            if not self.fetcher.robots_allows(url):
                log.warning("robots.txt disallows %s -- skipping", url)
                stats["skipped"] += 1
                continue

            try:
                result = self.fetcher.get(url, etag=self.store.get_etag(url))
            except BlockedError as exc:
                log.error("BLOCKED: %s -- aborting crawl", exc)
                self.blocked = True
                break
            except Exception as exc:  # transient network failure: log and move on
                stats["errors"] += 1
                log.warning("fetch failed for %s: %s", url, exc)
                continue
            stats["fetched"] += 1

            if result.status == 304:
                # The server confirms our copy is still current: the
                # page (and its links) cannot have changed, so skip
                # parsing and storage entirely.
                self.store.log_crawl(url, 304)
                stats["not_modified"] += 1
                log.info("unchanged %s -- etag match", url)
                continue

            self.store.log_crawl(
                url,
                result.status,
                content_hash=hashlib.sha256(
                    result.body.encode("utf-8")
                ).hexdigest(),
                etag=result.etag,
            )

            # Grow the frontier first: listing pages carry no lyric
            # lines, but they are how the song pages are discovered.
            if self._follow_links:
                for link in self._song_links(result.body, url):
                    if link not in self._seen:
                        self._seen.add(link)
                        self._frontier.append(link)

            try:
                sheet = self.parser.parse(result.body, {}, url, self.profile)
            except Exception as exc:
                stats["errors"] += 1
                log.warning("parse failed for %s: %s", url, exc)
                continue

            if not any(sec.lines for sec in sheet.sections):
                stats["skipped"] += 1
                log.warning("no lyric lines extracted from %s -- skipping", url)
                continue

            try:
                stored = self.store.upsert_sheet(sheet, self.parser.format)
            except Exception as exc:
                # One bad page must not take the whole crawl down.
                stats["errors"] += 1
                log.warning("store failed for %s: %s", url, exc)
                continue
            if stored:
                stats["stored"] += 1
                log.info("stored %s - %s", sheet.artist, sheet.title)
            else:
                stats["duplicate"] += 1
        return stats

    def _song_links(self, body: str, base_url: str) -> List[str]:
        host = urlparse(base_url).netloc
        links = []
        for m in _HREF_RE.finditer(body):
            href = m.group(1) or m.group(2) or m.group(3)
            if not href:
                continue
            # Fragments are client-side only: strip them so
            # '/chords/1#x' and '/chords/1' dedupe to one page.
            absolute, _ = urldefrag(urljoin(base_url, href))
            if urlparse(absolute).netloc == host and self.profile.song_url_pattern.search(absolute):
                links.append(absolute)
        return links
