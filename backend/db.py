"""SQLite: セット(約10曲の単位)と曲のメタ情報。"""
import sqlite3
from datetime import datetime, timezone
from .config import DB_PATH, GENRES

SCHEMA = """
CREATE TABLE IF NOT EXISTS sets(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tracks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT,
  artist TEXT,
  source_url TEXT,
  source_id TEXT UNIQUE,
  file_path TEXT NOT NULL,
  genre TEXT NOT NULL DEFAULT '',      -- カンマ区切り（複数可）
  set_id INTEGER REFERENCES sets(id),
  duration REAL,
  added_at TEXT NOT NULL,
  analyzed_at TEXT,
  bpm REAL,
  key_name TEXT,
  camelot TEXT,
  analysis_path TEXT
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=DB_PATH):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def normalize_genres(genres):
    """入力ジャンルを GENRES の正式名に揃える（大文字小文字・表記ゆれを吸収）。"""
    if isinstance(genres, str):
        genres = [g for g in genres.replace("、", ",").split(",")]
    table = {g.lower().replace("-", "").replace(" ", ""): g for g in GENRES}
    out = []
    for g in genres:
        key = g.strip().lower().replace("-", "").replace(" ", "")
        if not key:
            continue
        if key not in table:
            raise ValueError(f"未知のジャンル: {g!r}（使えるのは {', '.join(GENRES)}）")
        if table[key] not in out:
            out.append(table[key])
    return out


def get_or_create_set(con, name):
    row = con.execute("SELECT id FROM sets WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = con.execute("INSERT INTO sets(name, created_at) VALUES (?,?)", (name, now()))
    con.commit()
    return cur.lastrowid
