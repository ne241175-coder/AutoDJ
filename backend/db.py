"""SQLite: セット(約10曲の単位)と曲のメタ情報。"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .config import AUDIO_DIR, ANALYSIS_DIR, DB_PATH, GENRES

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


_relocated = set()


def relocate(con):
    """フォルダごと移動したとき（例: デスクトップ → ~/dev）に、DB に入っている古い絶対パスを
    いまの data/audio・data/analysis に付け替える。見つからないファイルだけを直す"""
    fixed = 0
    for r in con.execute("SELECT id, file_path, analysis_path FROM tracks").fetchall():
        upd = {}
        fp = Path(r["file_path"] or "")
        if r["file_path"] and not fp.exists() and (AUDIO_DIR / fp.name).exists():
            upd["file_path"] = str(AUDIO_DIR / fp.name)
        ap = r["analysis_path"]
        if ap and not Path(ap).exists() and (ANALYSIS_DIR / Path(ap).name).exists():
            upd["analysis_path"] = str(ANALYSIS_DIR / Path(ap).name)
        if upd:
            con.execute(f"UPDATE tracks SET {', '.join(k + '=?' for k in upd)} WHERE id=?", (*upd.values(), r["id"]))
            fixed += 1
    if fixed:
        con.commit()
    return fixed


def connect(path=DB_PATH):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    if str(path) not in _relocated:      # プロセスごとに最初の1回だけ確認する
        _relocated.add(str(path))
        relocate(con)
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
