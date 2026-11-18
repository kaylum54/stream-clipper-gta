"""SQLite store. One connection per thread/task; WAL lets the recorder, analyser and API work at once."""
import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS streamers (
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL,            -- twitch | kick | youtube
  channel TEXT NOT NULL,
  url TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  live INTEGER NOT NULL DEFAULT 0,
  title TEXT, category TEXT, video_id TEXT,
  checked_at REAL, check_error TEXT,
  created_at REAL NOT NULL,
  UNIQUE(platform, channel)
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY,
  streamer_id INTEGER NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
  started_at REAL NOT NULL, ended_at REAL,
  dir TEXT NOT NULL, title TEXT,
  status TEXT NOT NULL DEFAULT 'recording',   -- recording | ended | failed
  error TEXT
);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  path TEXT NOT NULL UNIQUE,
  start_ts REAL NOT NULL, duration REAL,
  analyzed INTEGER NOT NULL DEFAULT 0,
  deleted INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS chat_events (
  session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  ts REAL NOT NULL,
  hype INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chat_events_session_ts ON chat_events(session_id, ts);
CREATE TABLE IF NOT EXISTS clips (
  id INTEGER PRIMARY KEY,
  session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
  streamer_id INTEGER REFERENCES streamers(id) ON DELETE SET NULL,
  streamer_name TEXT NOT NULL,
  platform TEXT NOT NULL,
  peak_ts REAL NOT NULL, start_ts REAL NOT NULL, end_ts REAL NOT NULL,
  score REAL NOT NULL,
  signals TEXT NOT NULL DEFAULT '{}',        -- json: per-signal scores and the score curve
  path TEXT NOT NULL, thumb TEXT,
  transcript TEXT, caption TEXT,
  status TEXT NOT NULL DEFAULT 'pending',     -- pending | posting | posted | failed | rejected
  created_at REAL NOT NULL, decided_at REAL
);
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY,
  clip_id INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
  pfm_post_id TEXT,
  accounts TEXT NOT NULL,                     -- json list of Post for Me account ids
  status TEXT NOT NULL,                       -- uploading | submitted | done | failed
  results TEXT,                               -- json from social-post-results
  error TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts REAL NOT NULL,
  level TEXT NOT NULL,                        -- info | warn | error
  kind TEXT NOT NULL,                         -- live | offline | record | chunk | clip | post | discord | config | error
  message TEXT NOT NULL,
  streamer_id INTEGER
);
CREATE INDEX IF NOT EXISTS clips_streamer ON clips(streamer_id);
CREATE INDEX IF NOT EXISTS clips_status_score ON clips(status, score);
CREATE INDEX IF NOT EXISTS chunks_session_start ON chunks(session_id, start_ts);
CREATE INDEX IF NOT EXISTS sessions_streamer_status ON sessions(streamer_id, status);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
"""

# columns added after the first release: (table, column, declaration). connect() adds any that an older database lacks.
MIGRATIONS = [("chunks", "attempts", "INTEGER NOT NULL DEFAULT 0")]


def connect(path) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    for table, col, decl in MIGRATIONS:
        if col not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    return conn


def log(conn, kind: str, message: str, level: str = "info", streamer_id=None) -> None:
    conn.execute("INSERT INTO events(ts, level, kind, message, streamer_id) VALUES (?,?,?,?,?)",
                 (time.time(), level, kind, message, streamer_id))


def rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]
