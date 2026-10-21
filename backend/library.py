"""解析の実行と結果の保存（JSON + DB の要約列）。"""
import json
from pathlib import Path

from .config import ANALYSIS_DIR, ANALYZER_VERSION
from .db import connect, now
from .analysis.pipeline import analyze_file


def analysis_path(track_id):
    return ANALYSIS_DIR / f"{track_id}.json"


def load_analysis(track_id):
    p = analysis_path(track_id)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def analyze_track(track_id, con=None, force=False, verbose=False):
    con = con or connect()
    row = con.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
    if row is None:
        raise KeyError(f"track {track_id} がありません")
    old = load_analysis(track_id)
    if old and not force and old.get("analyzer_version") == ANALYZER_VERSION:
        return old
    res = analyze_file(Path(row["file_path"]), verbose=verbose)
    res["track_id"] = track_id
    p = analysis_path(track_id)
    p.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    con.execute(
        "UPDATE tracks SET analyzed_at=?, bpm=?, key_name=?, camelot=?, duration=?, analysis_path=? WHERE id=?",
        (now(), res["tempo"]["bpm"], res["key"]["name"], res["key"]["camelot"],
         res["duration"], str(p), track_id))
    con.commit()
    return res
