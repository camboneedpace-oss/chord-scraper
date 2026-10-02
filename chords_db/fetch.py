"""Politeness-aware HTTP layer: robots.txt, per-host delay, retries, disk cache."""
from __future__ import annotations

import hashlib
import time
import urllib.robotparser
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

import httpx


class BlockedError(RuntimeError):
    """The site refused automated access (401/403). A hard stop, never a retry."""


@dataclass
class FetchResult:
    url: str
    status: int
    body: str
    cached: bool = False
    etag: Optional[str] = None


class PoliteFetcher:
    """Context-managed fetcher. Caches successful responses on disk so
    re-crawls never re-download a page body, throttles per host, and
    raises BlockedError (not a retry) when the site refuses automated
    access.

    Each cached body is stored next to a sidecar file holding the
    response's ETag, so even a fully-cached re-crawl revalidates with
    If-None-Match: a 304 confirms the cached copy is still current,
    while a 200 replaces the cache with the changed content.
    """

    def __init__(
        self,
        cache_dir: str = ".cache",
        delay: float = 1.5,
        timeout: float = 20.0,
        max_retries: int = 3,
        user_agent: str = "chord-scraper/1.0 (+research dataset; contact: ops@example.com)",
    ):
        self._client = httpx.Client(
            timeout=timeout, follow_redirects=True, headers={"User-Agent": user_agent}
        )
        self._delay = delay
        self._max_retries = max_retries
        self._cache = Path(cache_dir)
        self._cache.mkdir(parents=True, exist_ok=True)
        self._last_request: dict = {}
        # robots.txt is fetched once per host, then cached in memory.
        self._robots: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}

    def __enter__(self) -> "PoliteFetcher":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def robots_allows(self, url: str) -> bool:
        """Check robots.txt for the target host. The file is fetched
        once per host through the same client (throttled, real UA),
        then cached. An unreadable or refused robots.txt is treated
        as disallowed (fail closed); a 404 means no rules apply."""
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        if host not in self._robots:
            self._robots[host] = self._load_robots(host)
        rp = self._robots[host]
        if rp is None:
            return False
        return rp.can_fetch(self._client.headers["User-Agent"], url)

    def _load_robots(
        self, host: str
    ) -> Optional[urllib.robotparser.RobotFileParser]:
        robots_url = f"{host}/robots.txt"
        self._throttle(robots_url)
        try:
            resp = self._client.get(robots_url)
        except httpx.HTTPError:
            return None  # network failure: fail closed
        if resp.status_code in (401, 403):
            return None  # site refuses automated access: fail closed
        rp = urllib.robotparser.RobotFileParser(robots_url)
        if resp.status_code == 404:
            rp.allow_all = True  # no robots.txt: nothing is disallowed
            return rp
        if resp.status_code != 200:
            return None
        rp.parse(resp.text.splitlines())
        return rp

    def get(self, url: str, etag: Optional[str] = None) -> FetchResult:
        """Fetch url. When `etag` is given it is sent as If-None-Match;
        the site may answer 304, meaning the copy we already hold is
        still current (the response carries no body)."""
        cache_file, etag_file = self._cache_files(url)
        cached_body: Optional[str] = None
        if cache_file.exists():
            cached_body = cache_file.read_text(encoding="utf-8")
            if etag is None and etag_file.exists():
                # The ETag is persisted beside the cached body, so a
                # fully-cached re-crawl still revalidates instead of
                # silently serving a page the server has changed.
                etag = etag_file.read_text(encoding="utf-8") or None
            if etag is None:
                # Nothing to revalidate against (the site sends no
                # ETag, or this is a legacy cache without a sidecar):
                # the cached copy is the best available, serve it.
                return FetchResult(
                    url=url, status=200, body=cached_body, cached=True
                )

        self._throttle(url)
        headers = {"If-None-Match": etag} if etag else None
        last_status = None
        for attempt in range(self._max_retries):
            resp = self._client.get(url, headers=headers)
            last_status = resp.status_code
            if resp.status_code == 200:
                cache_file.write_text(resp.text, encoding="utf-8")
                self._write_etag(etag_file, resp.headers.get("etag"))
                return FetchResult(
                    url=url, status=200, body=resp.text, etag=resp.headers.get("etag")
                )
            if resp.status_code == 304:
                # Unchanged since our etag: nothing to parse or store.
                return FetchResult(
                    url=url, status=304, body="", cached=True, etag=etag
                )
            if resp.status_code in (401, 403):
                raise BlockedError(
                    f"{resp.status_code} from {url}: site refuses automated access -- "
                    "stop crawling, check the ToS, and look for an official API"
                )
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("retry-after", 5.0)) * (attempt + 1))
            elif resp.status_code >= 500:
                time.sleep(2 ** attempt)
            else:
                resp.raise_for_status()
        raise RuntimeError(
            f"giving up on {url} after {self._max_retries} retries (last status {last_status})"
        )

    def _cache_files(self, url: str) -> tuple:
        digest = hashlib.sha256(url.encode()).hexdigest()
        return (
            self._cache / (digest + ".html"),
            self._cache / (digest + ".etag"),
        )

    @staticmethod
    def _write_etag(etag_file: Path, etag: Optional[str]) -> None:
        if etag:
            etag_file.write_text(etag, encoding="utf-8")
        elif etag_file.exists():
            # The server stopped sending an ETag: drop the sidecar so
            # later re-crawls serve the cache instead of revalidating
            # against a stale token.
            etag_file.unlink()

    def _throttle(self, url: str) -> None:
        host = httpx.URL(url).host or ""
        elapsed = time.monotonic() - self._last_request.get(host, 0.0)
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)
        self._last_request[host] = time.monotonic()
