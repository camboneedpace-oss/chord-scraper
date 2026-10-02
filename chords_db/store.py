"""SQLite-backed store: idempotent upserts keyed on (source_id, checksum)."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Dict, Optional

from .models import Sheet
from .normalize import has_private_use

_SCHEMA = Path(__file__).with_name("schema.sql").read_text()


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "unknown"


def _json(values: list) -> str:
    return json.dumps(values, ensure_ascii=False)


class Store:
    def __init__(self, db_path: str = "chords.db"):
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        # The web server reads while the refresher writes: WAL plus
        # a busy timeout keeps those concurrent accesses smooth.
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def upsert_sheet(self, sheet: Sheet, sheet_format: str) -> bool:
        """Insert song + sheet + lines + chord refs.

        Returns True if stored, False when an identical checksum already exists
        (makes re-crawls idempotent).
        """
        checksum = sheet.content_checksum()
        dup = self._conn.execute(
            """
            SELECT 1 FROM chord_sheets cs
            JOIN songs s ON s.id = cs.song_id
            WHERE s.source_id = ? AND cs.checksum = ?
            """,
            (sheet.source_id, checksum),
        ).fetchone()
        if dup:
            return False

        artist_id = self._artist_id(sheet.artist, sheet.artist_khmer)
        row = self._conn.execute(
            "SELECT id FROM songs WHERE source_id = ?", (sheet.source_id,)
        ).fetchone()
        if row:
            song_id = row["id"]
            self._conn.execute(
                """UPDATE songs SET artist_id=?, title=?, title_khmer=?, language=?,
                   song_key=?, capo=?, tempo_bpm=?, tuning=?, tags=?, raw_url=?,
                   updated_at=datetime('now') WHERE id=?""",
                (artist_id, sheet.title, sheet.title_khmer, sheet.language, sheet.song_key,
                 sheet.capo, sheet.tempo_bpm, sheet.tuning, _json(sheet.tags),
                 sheet.source_url, song_id),
            )
        else:
            song_id = self._conn.execute(
                """INSERT INTO songs (source_id, artist_id, title, title_khmer, language,
                   song_key, capo, tempo_bpm, tuning, tags, raw_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (sheet.source_id, artist_id, sheet.title, sheet.title_khmer, sheet.language,
                 sheet.song_key, sheet.capo, sheet.tempo_bpm, sheet.tuning,
                 _json(sheet.tags), sheet.source_url),
            ).lastrowid

        sheet_id = self._conn.execute(
            """INSERT INTO chord_sheets (song_id, instrument, format, raw_payload,
               checksum, quality_score)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (song_id, sheet.instrument.value, sheet_format, sheet.raw_payload,
             checksum, self._quality(sheet)),
        ).lastrowid

        seq = 0
        for section in sheet.sections:
            for line in section.lines:
                seq += 1
                line_id = self._conn.execute(
                    """INSERT INTO sheet_lines (sheet_id, seq, section, label, text)
                       VALUES (?, ?, ?, ?, ?)""",
                    (sheet_id, seq, section.type, section.label, line.text),
                ).lastrowid
                for chord in line.chords:
                    # OR IGNORE: a parser upstream may still produce a
                    # duplicate offset; storage must never crash on it.
                    self._conn.execute(
                        "INSERT OR IGNORE INTO sheet_chords (line_id, chord, char_offset) VALUES (?, ?, ?)",
                        (line_id, chord.chord, chord.offset),
                    )
                    self._conn.execute(
                        "INSERT OR IGNORE INTO chord_vocab (chord) VALUES (?)", (chord.chord,)
                    )
        self._conn.commit()
        return True

    def log_crawl(self, url: str, status: int, content_hash: Optional[str] = None,
                  etag: Optional[str] = None) -> None:
        # COALESCE: a 304 re-log carries no hash/etag and must not
        # erase the values recorded when the page was first fetched.
        self._conn.execute(
            """INSERT INTO crawl_log (url, http_status, fetched_at, content_hash, etag, attempts)
               VALUES (?, ?, datetime('now'), ?, ?, 1)
               ON CONFLICT(url) DO UPDATE SET
                 http_status = excluded.http_status,
                 fetched_at  = excluded.fetched_at,
                 content_hash = COALESCE(excluded.content_hash, crawl_log.content_hash),
                 etag        = COALESCE(excluded.etag, crawl_log.etag),
                 attempts    = crawl_log.attempts + 1""",
            (url, status, content_hash, etag),
        )
        self._conn.commit()

    def get_etag(self, url: str) -> Optional[str]:
        """The last-seen ETag for url, if any (sent as If-None-Match)."""
        row = self._conn.execute(
            "SELECT etag FROM crawl_log WHERE url = ?", (url,)
        ).fetchone()
        return row["etag"] if row else None

    def stats(self) -> Dict[str, int]:
        tables = ("artists", "songs", "chord_sheets", "sheet_lines", "sheet_chords", "chord_vocab")
        return {
            t: self._conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in tables
        }

    def _artist_id(self, name: str, name_khmer: Optional[str]) -> int:
        # Artist identity is the name, case-insensitive: a re-crawl of
        # the same artist with different capitalization reuses the row.
        row = self._conn.execute(
            "SELECT id FROM artists WHERE lower(name) = lower(?)", (name,)
        ).fetchone()
        if row:
            return row["id"]
        # Distinct artists can slugify to the same value ('AC/DC' vs
        # 'AC DC' both -> 'ac-dc'): uniquify the slug so the UNIQUE
        # constraint never merges or rejects them.
        slug = _slug(name)
        candidate = slug
        n = 2
        while self._conn.execute(
            "SELECT 1 FROM artists WHERE slug = ?", (candidate,)
        ).fetchone():
            candidate = f"{slug}-{n}"
            n += 1
        return self._conn.execute(
            "INSERT INTO artists (slug, name, name_khmer) VALUES (?, ?, ?)",
            (candidate, name, name_khmer),
        ).lastrowid

    @staticmethod
    def _quality(sheet: Sheet) -> float:
        """PUA-font pages keep their data but score low: extraction is unreliable."""
        texts = [line.text for section in sheet.sections for line in section.lines]
        if not texts:
            return 0.0
        if any(has_private_use(t) for t in texts):
            return 0.3
        return 1.0
