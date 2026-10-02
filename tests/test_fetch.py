"""Tests for the politeness layer (PoliteFetcher)."""
from __future__ import annotations

import time

import pytest

from chords_db.fetch import BlockedError, PoliteFetcher


def test_throttle_enforces_minimum_delay(tmp_path):
    with PoliteFetcher(cache_dir=str(tmp_path), delay=0.3) as fetcher:
        start = time.monotonic()
        fetcher._throttle("http://a.test/1")
        fetcher._throttle("http://a.test/2")  # same host: must wait
        elapsed = time.monotonic() - start
    assert elapsed >= 0.3


def test_throttle_is_per_host(tmp_path):
    with PoliteFetcher(cache_dir=str(tmp_path), delay=0.3) as fetcher:
        start = time.monotonic()
        fetcher._throttle("http://a.test/1")
        fetcher._throttle("http://b.test/1")  # different host: no wait
        elapsed = time.monotonic() - start
    assert elapsed < 0.3


def test_blocked_error_on_403(site, tmp_path):
    site.set_route("/chords/101", "text/html", "nope", status=403)
    with PoliteFetcher(cache_dir=str(tmp_path), delay=0) as fetcher:
        with pytest.raises(BlockedError):
            fetcher.get(site.url("/chords/101"))


def test_disk_cache_revalidates_second_fetch(site, tmp_path):
    cache = str(tmp_path / "cache")
    with PoliteFetcher(cache_dir=cache, delay=0) as fetcher:
        first = fetcher.get(site.url("/chords/101"))
        second = fetcher.get(site.url("/chords/101"))
    assert first.cached is False
    assert second.cached is True
    # the warm cache revalidated: 304, no body re-downloaded
    assert second.status == 304
    assert second.etag == '"/chords/101"'
    assert len(site.requests_for("/chords/101")) == 2


def test_etag_is_persisted_beside_cache_body(site, tmp_path):
    cache = tmp_path / "cache"
    with PoliteFetcher(cache_dir=str(cache), delay=0) as fetcher:
        first = fetcher.get(site.url("/chords/101"))
    digest = __import__("hashlib").sha256(
        site.url("/chords/101").encode()
    ).hexdigest()
    # the body and its ETag live in sibling sidecar files
    assert (cache / (digest + ".html")).read_text() == first.body
    assert (cache / (digest + ".etag")).read_text() == '"/chords/101"'


def test_warm_cache_detects_changed_content(site, tmp_path):
    cache = str(tmp_path / "cache")
    with PoliteFetcher(cache_dir=cache, delay=0) as fetcher:
        first = fetcher.get(site.url("/chords/101"))
        assert first.body  # original content cached
    # the site publishes revised content under a new ETag
    site.set_route("/chords/101", "text/html", "<html>revised</html>",
                   etag='"/chords/101-v2"')
    with PoliteFetcher(cache_dir=cache, delay=0) as fetcher:
        second = fetcher.get(site.url("/chords/101"))
    assert second.cached is False
    assert second.status == 200
    assert second.body == "<html>revised</html>"
    assert second.etag == '"/chords/101-v2"'
    # the cache was refreshed: the next re-crawl revalidates
    # against the new ETag and gets a 304
    with PoliteFetcher(cache_dir=cache, delay=0) as fetcher:
        third = fetcher.get(site.url("/chords/101"))
    assert third.cached is True
    assert third.status == 304


def test_cache_without_etag_serves_without_network(site, tmp_path):
    # a site that sends no ETag: the disk cache still works,
    # but there is nothing to revalidate against
    site.set_route("/chords/101", "text/html", "no etag here",
                   send_etag=False)
    cache = str(tmp_path / "cache")
    with PoliteFetcher(cache_dir=cache, delay=0) as fetcher:
        first = fetcher.get(site.url("/chords/101"))
        second = fetcher.get(site.url("/chords/101"))
    assert first.cached is False
    assert first.etag is None
    assert second.cached is True
    assert second.body == "no etag here"
    # served straight from disk: only one request ever hit the site
    assert len(site.requests_for("/chords/101")) == 1


def test_conditional_request_gets_304(site, tmp_path):
    with PoliteFetcher(cache_dir=str(tmp_path / "c1"), delay=0) as fetcher:
        first = fetcher.get(site.url("/chords/101"))
        assert first.etag == '"/chords/101"'
    # cold cache + stored etag: the site answers 304
    with PoliteFetcher(cache_dir=str(tmp_path / "c2"), delay=0) as fetcher:
        second = fetcher.get(site.url("/chords/101"), etag=first.etag)
    assert second.status == 304
    assert second.body == ""
    assert second.cached is True
    # two requests hit the site, but only one carried a body
    assert len(site.requests_for("/chords/101")) == 2


def test_no_etag_means_no_conditional_request(site, tmp_path):
    with PoliteFetcher(cache_dir=str(tmp_path / "c1"), delay=0) as fetcher:
        first = fetcher.get(site.url("/chords/101"))
    # cold cache, no etag passed: full 200 response again
    with PoliteFetcher(cache_dir=str(tmp_path / "c2"), delay=0) as fetcher:
        again = fetcher.get(site.url("/chords/101"))
    assert first.status == again.status == 200
    assert again.body == first.body
    assert again.cached is False


def test_robots_allows_honors_rules(site, tmp_path):
    with PoliteFetcher(cache_dir=str(tmp_path), delay=0) as fetcher:
        assert fetcher.robots_allows(site.url("/chords/101")) is True
    site.set_robots("User-agent: *\nDisallow: /chords/\n")
    with PoliteFetcher(cache_dir=str(tmp_path), delay=0) as fetcher:
        assert fetcher.robots_allows(site.url("/chords/101")) is False
        assert fetcher.robots_allows(site.url("/pro/201")) is True
