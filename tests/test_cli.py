"""CLI tests: flags, exit codes, and a fake-site crawl through main()."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from chords_db.__main__ import build_parser, main

from conftest import ROBOTS_DISALLOW

PRO_ARGS = ["--parser", "chordpro", "--url-pattern", r"/pro/\d+"]


def _run(site, capsys, tmp_path, *args):
    """Invoke the CLI against the fake site; return (exit, out, db_path)."""
    db = str(tmp_path / "cli.db")
    code = main([
        "--db", db,
        "--cache-dir", str(tmp_path / "cache"),
        "--delay", "0",
        *args,
    ])
    captured = capsys.readouterr()
    return code, captured, db


def _stats(captured):
    return json.loads(captured.out.strip().splitlines()[-1])


def test_cli_crawl_reports_stats_and_stores(site, capsys, tmp_path):
    code, captured, db = _run(
        site, capsys, tmp_path, *PRO_ARGS, site.url("/pro/")
    )
    assert code == 0
    stats = _stats(captured)
    assert stats["fetched"] == 3
    assert stats["stored"] == 2
    assert stats["errors"] == 0
    conn = sqlite3.connect(db)
    titles = {r[0] for r in conn.execute("SELECT title FROM songs")}
    assert titles == {"Morning Light", "Evening Rain"}


def test_cli_html_parser_flag(site, capsys, tmp_path):
    code, captured, db = _run(
        site, capsys, tmp_path,
        "--parser", "html",
        "--title-selector", "h1.song-title",
        site.url("/chords/"),
    )
    assert code == 0
    assert _stats(captured)["stored"] == 2
    conn = sqlite3.connect(db)
    assert {r[0] for r in conn.execute("SELECT title FROM songs")} == {
        "Song One", "Song Two",
    }


def test_cli_no_follow_stops_at_seed(site, capsys, tmp_path):
    code, captured, _ = _run(
        site, capsys, tmp_path, *PRO_ARGS, "--no-follow",
        site.url("/pro/"),
    )
    assert code == 0
    stats = _stats(captured)
    assert stats["fetched"] == 1  # the listing only
    assert stats["stored"] == 0   # listings carry no lyric lines


def test_cli_max_pages_caps_fetch(site, capsys, tmp_path):
    code, captured, _ = _run(
        site, capsys, tmp_path, *PRO_ARGS,
        "--max-pages", "1", site.url("/pro/"),
    )
    assert code == 0
    assert _stats(captured)["fetched"] == 1


def test_cli_verbose_flags_run_quietly(site, capsys, tmp_path):
    # each verbosity level gets its own cache + db: a shared
    # cache would revalidate to 304 on the second pass
    for i, verbosity in enumerate(("-v", "-vv")):
        run = tmp_path / f"run{i}"
        run.mkdir()
        code = main([
            "--db", str(run / "cli.db"),
            "--cache-dir", str(run / "cache"),
            "--delay", "0",
            *PRO_ARGS, verbosity,
            site.url("/pro/201"),
        ])
        captured = capsys.readouterr()
        assert code == 0
        assert _stats(captured)["stored"] == 1


def test_cli_blocked_403_exits_nonzero(site, capsys, tmp_path):
    for path in ("/pro/", "/pro/201", "/pro/202"):
        site.set_route(path, "text/html", "forbidden", status=403)
    code, captured, _ = _run(
        site, capsys, tmp_path, *PRO_ARGS, site.url("/pro/201"),
    )
    assert code == 1
    assert "refused automated access" in captured.err


def test_cli_invalid_url_pattern_exits_2(capsys, tmp_path):
    code = main([
        "--db", str(tmp_path / "x.db"),
        "--cache-dir", str(tmp_path / "cache"),
        "--delay", "0", "--url-pattern", "[",
        "http://example.test/",
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert "invalid --url-pattern" in captured.err


def test_cli_robots_disallow_exits_zero_with_skips(site, capsys, tmp_path):
    site.set_robots(ROBOTS_DISALLOW)
    code, captured, _ = _run(
        site, capsys, tmp_path, *PRO_ARGS, site.url("/pro/201"),
    )
    assert code == 0
    stats = _stats(captured)
    assert stats["fetched"] == 0
    assert stats["skipped"] == 1


def test_module_help_smoke():
    # python -m chords_db --help must work from the project root
    root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-m", "chords_db", "--help"],
        cwd=root, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    assert "usage" in proc.stdout


def test_console_script_entry_point_registered():
    # pyproject must expose the chord-scraper console script
    pyproject = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    assert 'chord-scraper = "chords_db.__main__:main"' in pyproject


def test_cli_demo_flag_crawls_builtin_site(capsys, tmp_path):
    db = str(tmp_path / "demo.db")
    code = main([
        "--demo", "--db", db,
        "--cache-dir", str(tmp_path / "cache"),
        "--delay", "0",
    ])
    captured = capsys.readouterr()
    assert code == 0
    assert _stats(captured)["stored"] >= 4


def test_cli_refresh_jitter_flag_parses():
    args = build_parser([
        "--serve", "--refresh-interval", "60",
        "--refresh-jitter", "7.5", "http://example.test/",
    ])
    assert args.refresh_interval == 60
    assert args.refresh_jitter == 7.5


def test_cli_defaults_to_local_web():
    # bare invocation: bundled demo site + local web UI
    args = build_parser([])
    assert args.local_web is True
    assert args.demo is True
    assert args.serve is True
    assert args.seeds == []

def test_cli_explicit_demo_stays_crawl_only():
    args = build_parser(["--demo"])
    assert args.local_web is False
    assert args.serve is False

def test_cli_seeds_disable_local_web_defaults():
    args = build_parser(["http://example.test/"])
    assert args.local_web is False
    assert args.demo is False
    assert args.serve is False
