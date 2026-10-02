CREATE TABLE IF NOT EXISTS artists (
    id         INTEGER PRIMARY KEY,
    slug       TEXT UNIQUE NOT NULL,
    name       TEXT NOT NULL,        -- romanized
    name_khmer TEXT                  -- Khmer script, when present
);

CREATE TABLE IF NOT EXISTS songs (
    id          INTEGER PRIMARY KEY,
    source_id   TEXT UNIQUE NOT NULL,  -- the site's own id, e.g. '6949195'
    artist_id   INTEGER REFERENCES artists(id),
    title       TEXT NOT NULL,
    title_khmer TEXT,
    language    TEXT DEFAULT 'km',
    song_key    TEXT,                  -- detected key, e.g. 'Am'
    capo        INTEGER DEFAULT 0,
    tempo_bpm   INTEGER,
    tuning      TEXT DEFAULT 'EADGBE',
    tags        TEXT,                  -- JSON array: ['rock','90s']
    raw_url     TEXT NOT NULL,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chord_sheets (
    id            INTEGER PRIMARY KEY,
    song_id       INTEGER NOT NULL REFERENCES songs(id) ON DELETE CASCADE,
    instrument    TEXT NOT NULL CHECK(instrument IN ('guitar','ukulele','bass','piano')),
    format        TEXT NOT NULL CHECK(format IN ('chordpro','lines','html')),
    raw_payload   TEXT NOT NULL,       -- exact scraped text, kept for audit
    checksum      TEXT NOT NULL,       -- sha256 of normalized content
    quality_score REAL,                -- 0..1; PUA-font pages score low
    fetched_at    TEXT DEFAULT (datetime('now')),
    UNIQUE(song_id, instrument, checksum)
);

-- Normalized, line-aligned representation: the queryable core.
CREATE TABLE IF NOT EXISTS sheet_lines (
    id       INTEGER PRIMARY KEY,
    sheet_id INTEGER NOT NULL REFERENCES chord_sheets(id) ON DELETE CASCADE,
    seq      INTEGER NOT NULL,        -- line order across the whole sheet
    section  TEXT,                    -- intro/verse/chorus/bridge/outro/instrumental/other
    label    TEXT,                    -- e.g. 'Verse 2'
    text     TEXT,                    -- lyrics; may be empty for instrumental
    UNIQUE(sheet_id, seq)
);

CREATE TABLE IF NOT EXISTS sheet_chords (
    id          INTEGER PRIMARY KEY,
    line_id     INTEGER NOT NULL REFERENCES sheet_lines(id) ON DELETE CASCADE,
    chord       TEXT NOT NULL,         -- canonical: 'C#m7b5', 'G/B'
    char_offset INTEGER NOT NULL,      -- index into sheet_lines.text
    UNIQUE(line_id, char_offset)
);

CREATE TABLE IF NOT EXISTS chord_vocab (
    chord TEXT PRIMARY KEY             -- canonical spelling after normalization
);

CREATE TABLE IF NOT EXISTS crawl_log (
    url          TEXT PRIMARY KEY,
    http_status  INTEGER,
    fetched_at   TEXT,
    etag         TEXT,
    content_hash TEXT,
    attempts     INTEGER DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_sheet_chords_chord ON sheet_chords(chord);
CREATE INDEX IF NOT EXISTS idx_songs_artist ON songs(artist_id);
