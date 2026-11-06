"""シートの曲を YouTube から一括で取り込む。

構成入力の秒数は「ある特定の動画」で計ったものなので、別の動画（MVのドラマ部分付き、ライブ版など）を
取ると時刻がずれる。そこで検索結果の中から「シートの終了時刻と長さが最も近い動画」を選ぶ。
選んだ結果は data/sheet/downloads.csv に残すので、違っていたら video_id を書き換えて再実行できる。
"""
import csv
import re
from pathlib import Path

from .config import AUDIO_DIR
from .db import connect, get_or_create_set
from .ingest import _ydl_opts, _register
from .sheet import SHEET_DIR, load_cached

BAD_WORDS = re.compile(r"(live|ライブ|cover|カバー|歌ってみた|弾いてみた|叩いてみた|karaoke|カラオケ|"
                       r"instrumental|off vocal|short|切り抜き|remix|nightcore|slowed|sped up|piano)", re.I)
GOOD_WORDS = re.compile(r"(official|music video|mv|audio|公式)", re.I)
TOLERANCE = 4.0  # 秒: シートの長さとの許容差


def score_candidate(entry, target_sec):
    """小さいほど良い。長さが合わないものは除外（None）"""
    dur = entry.get("duration")
    title = entry.get("title") or ""
    if not dur:
        return None
    s = 0.0
    if target_sec:
        diff = abs(dur - target_sec)
        if diff > TOLERANCE:
            return None
        s += diff
    else:
        if not 100 <= dur <= 480:
            return None
    if BAD_WORDS.search(title):
        s += 50
    if GOOD_WORDS.search(title):
        s -= 2
    return s


def choose(entries, target_sec):
    scored = [(score_candidate(e, target_sec), e) for e in entries if e]
    scored = [(s, e) for s, e in scored if s is not None]
    if not scored:
        return None
    scored.sort(key=lambda x: x[0])
    return scored[0][1]


def _manual_overrides():
    p = SHEET_DIR / "downloads.csv"
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return {r["song"]: r for r in csv.DictReader(f)}


def fetch_all(only=None, dry_run=False, set_name="sheet", n_results=8, con=None, log=print):
    import yt_dlp
    structure, lst = load_cached()
    con = con or connect()
    set_id = get_or_create_set(con, set_name)
    overrides = _manual_overrides()
    rows = []
    search_opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "skip_download": True}

    names = list(lst["songs"])
    if only:
        names = [n for n in names if n in only]
    with yt_dlp.YoutubeDL(search_opts) as ys, yt_dlp.YoutubeDL(_ydl_opts(AUDIO_DIR, False)) as yd:
        for name in names:
            target = structure.get(name, {}).get("end")
            ov = overrides.get(name, {})
            vid = (ov.get("video_id") or "").strip()
            pick = None
            if vid:
                pick = {"id": vid, "title": ov.get("title", ""), "duration": float(ov.get("duration") or 0)}
            else:
                res = ys.extract_info(f"ytsearch{n_results}:{lst['songs'][name]}", download=False)
                pick = choose(res.get("entries") or [], target)
            if not pick:
                log(f"× {name}: 長さ{target}s に合う動画が見つかりません（downloads.csv に video_id を手で書いて再実行）")
                rows.append({"song": name, "video_id": "", "title": "", "duration": "",
                             "sheet_end": target or "", "diff": "", "status": "not_found"})
                continue
            diff = abs(pick["duration"] - target) if (target and pick.get("duration")) else ""
            log(f"○ {name}: {pick['title']} ({pick.get('duration')}s, シート {target}s)")
            status = "selected"
            existing = con.execute("SELECT file_path FROM tracks WHERE source_id=?", (f"yt:{pick['id']}",)).fetchone()
            if existing and Path(existing["file_path"]).exists():
                status = "already"
                con.execute("UPDATE tracks SET title=? WHERE source_id=?", (name, f"yt:{pick['id']}"))
                con.commit()
            elif not dry_run:
                info = yd.extract_info(f"https://www.youtube.com/watch?v={pick['id']}", download=True)
                path = AUDIO_DIR / f"{info['id']}.mp3"
                _register(con, title=name, artist=None, url=info.get("webpage_url"),
                          source_id=f"yt:{info['id']}", path=path, genres=["J-pop"],
                          set_id=set_id, duration=info.get("duration"))
                status = "downloaded"
            rows.append({"song": name, "video_id": pick["id"], "title": pick.get("title", ""),
                         "duration": pick.get("duration", ""), "sheet_end": target or "",
                         "diff": diff, "status": status})

    SHEET_DIR.mkdir(parents=True, exist_ok=True)
    with open(SHEET_DIR / "downloads.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["song", "video_id", "title", "duration", "sheet_end", "diff", "status"])
        w.writeheader()
        w.writerows(rows)
    return rows
