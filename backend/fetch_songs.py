"""シートの曲を YouTube から一括で取り込む。

構成入力の秒数は「ある特定の動画」で計ったものなので、別の動画（MVのドラマ部分付き、ライブ版など）を
取ると時刻がずれる。そこで検索結果の中から「シートの終了時刻と長さが最も近い動画」を選ぶ。
選んだ結果は data/sheet/downloads.csv に残す。違っていたら、その行の video_id を正しい動画のIDに、
status を manual に書き換えて再実行する（YouTube の URL の v= の後ろがID）。
"""
import csv
import re
from pathlib import Path

from .config import AUDIO_DIR
from .db import connect, get_or_create_set
from .ingest import _ydl_opts, _register
from .sheet import SHEET_DIR, load_cached

BAD_WORDS = re.compile(r"(live|ライブ|cover|カバー|歌ってみた|弾いてみた|叩いてみた|踊ってみた|karaoke|カラオケ|"
                       r"instrumental|off vocal|short|切り抜き|remix|nightcore|slowed|sped up|piano|replica|"
                       r"dance practice|dance ver|mirrored|staff ver|movie edit|tv size|tv ver|8d|reaction|"
                       r"原曲歌手|[+-]\s?\d\s?key|キー|ガイドメロディ|オルゴール|music box|from [a-z0-9]|オーケストラ|acoustic|"
                       r"令和ver|re-?record|self cover|セルフカバー)", re.I)
GOOD_WORDS = re.compile(r"(official|music video|公式|provided to youtube)", re.I)
TOLERANCE = 4.0      # 秒: シートの長さとの許容差
MIN_DUR_NO_TARGET = 150  # シートに長さがない曲は、短すぎる同名曲を避けるため 2:30 以上に限る


def is_topic(entry):
    """YouTube の自動生成チャンネル（「アーティスト - Topic」）= アルバム音源そのもの"""
    ch = (entry.get("channel") or entry.get("uploader") or "")
    return ch.endswith(" - Topic") or ch.endswith("- Topic")


def artist_match(entry, artist):
    """タイトルかチャンネル名にアーティスト名が含まれるか（空白・大文字小文字は無視）"""
    if not artist:
        return False
    hay = ((entry.get("title") or "") + " " + (entry.get("channel") or entry.get("uploader") or "")).lower().replace(" ", "")
    return artist.lower().replace(" ", "") in hay


def _norm(x):
    return re.sub(r"[\s　・!！?？「」『』【】\[\]()（）/／\-–—~〜'\".,、。]", "", x or "").lower()


def name_match(entry, song):
    """動画タイトルに曲名（'フィギュア/SixTONES' なら 'フィギュア'）が含まれるか"""
    key = _norm(re.split(r"[/／]", song)[0])
    return bool(key) and key in _norm(entry.get("title"))


def score_candidate(entry, target_sec, artist="", song=""):
    """小さいほど良い。候補にしないものは None。
    - 除外語（カバー・カラオケ・ライブ等）を含むものは候補にしない
    - シートに長さがあれば ±4秒以内のものだけ（同じ音源である可能性が高い）
    - 長さがなければ、同名の別曲を避けるためアーティスト名の一致を重視する"""
    dur = entry.get("duration")
    title = entry.get("title") or ""
    if not dur or BAD_WORDS.search(title):
        return None
    match = artist_match(entry, artist)
    # 長さが偶然一致した別の曲（例: 完全感覚Dreamer → Kagerou）を避けるため、
    # 曲名かアーティスト名のどちらかがタイトル／チャンネル名に入っていることを必須にする
    if song and not (match or name_match(entry, song)):
        return None
    s = 0.0
    if target_sec:
        diff = abs(dur - target_sec)
        if diff > TOLERANCE:
            return None
        s += diff
        if is_topic(entry):
            s -= 5          # 長さが一致した Topic = アルバム音源そのもの
    else:
        if not MIN_DUR_NO_TARGET <= dur <= 480:
            return None
        if is_topic(entry):
            s += -5 if match else 3   # 同名の別アーティストの Topic を避ける
    if match:
        s -= 3
    # 曲名がタイトルに入っていれば大きく加点、入っていなければ減点。
    # 同じアーティストの別の曲で長さが偶然一致したもの（完全感覚Dreamer → Kagerou）を避ける。
    # Topic はローマ字・英語表記（Sugar Song and Bitter Step など）が多いので減点を小さく
    if song:
        if name_match(entry, song):
            s -= 6
        else:
            s += 2 if is_topic(entry) else 4
    if GOOD_WORDS.search(title):
        s -= 2
    return s


def choose(entries, target_sec, artist="", song=""):
    seen, scored = set(), []
    for e in entries:
        if not e or e.get("id") in seen:
            continue
        seen.add(e.get("id"))
        sc = score_candidate(e, target_sec, artist, song)
        if sc is not None:
            scored.append((sc, e))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0])
    return scored[0][1]


def clean_query(q):
    """'ハレンチ(ちゃんみな) ちゃんみな' → 'ハレンチ ちゃんみな'、'/' は空白に"""
    q = re.sub(r"[（(][^）)]*[）)]", " ", q).replace("/", " ")
    words, out = q.split(), []
    for w in words:
        if w not in out:
            out.append(w)
    return " ".join(out)


def _manual_overrides():
    p = SHEET_DIR / "downloads.csv"
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return {r["song"]: r for r in csv.DictReader(f)}


def fetch_all(only=None, dry_run=False, set_name="sheet", n_results=12, con=None, log=print, reselect=False):
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
            # 手で書いた video_id（status=manual や not_found の行）と、取り込み済みの動画だけを尊重する。
            # dry-run で選んだだけの行（selected）は毎回選び直す
            vid = (ov.get("video_id") or "").strip()
            keep = ("manual", "not_found") if reselect else ("manual", "not_found", "downloaded", "already")
            if ov.get("status") not in keep:
                vid = ""
            pick = None
            if vid:
                # 手動指定: タイトルと長さを取りに行って表示する（取れなくてもそのまま使う）
                try:
                    info = ys.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)
                    pick = {"id": vid, "title": info.get("title", ""), "duration": float(info.get("duration") or 0),
                            "channel": info.get("channel", "")}
                except Exception:
                    pick = {"id": vid, "title": "(手動指定)", "duration": 0.0}
                pick["title"] = f"{pick['title']} [手動指定]"
            else:
                q = clean_query(lst["songs"][name])
                title_words = clean_query(name).split()
                artist = " ".join(w for w in q.split() if w not in title_words)
                entries = []
                # 通常検索 ＋ アルバム音源（Topic チャンネル）狙いの検索
                for query in (q, f"{q} provided to YouTube"):
                    res = ys.extract_info(f"ytsearch{n_results}:{query}", download=False)
                    entries += res.get("entries") or []
                pick = choose(entries, target, artist, name)
            if not pick:
                want = f"長さ {target:.0f}秒 ±{TOLERANCE:.0f}" if target else "条件"
                log(f"× {name}: {want} に合う動画が見つかりません（downloads.csv に video_id を手で書いて再実行）")
                rows.append({"song": name, "video_id": "", "title": "", "duration": "",
                             "sheet_end": target or "", "diff": "", "status": "not_found"})
                continue
            diff = abs(pick["duration"] - target) if (target and pick.get("duration")) else ""
            status_manual = bool(vid)
            sheet_len = f"シート {target:.0f}秒" if target else "シートに長さなし"
            topic = " [Topic=アルバム音源]" if is_topic(pick) else ""
            if vid:
                log(f"○ {name}: 手動指定の動画 https://youtu.be/{vid}（長さはダウンロード時に確認）")
            else:
                log(f"○ {name}: {pick['title']}{topic} ({pick.get('duration')}秒, {sheet_len})")
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


def video_id_from(url_or_id):
    """https://youtu.be/XXXX / https://www.youtube.com/watch?v=XXXX / XXXX → XXXX"""
    m = re.search(r"(?:v=|youtu\.be/|shorts/)([A-Za-z0-9_-]{11})", url_or_id)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url_or_id.strip()):
        return url_or_id.strip()
    raise ValueError(f"YouTube の URL か動画IDとして読めません: {url_or_id}")


def set_manual(song, url_or_id):
    """downloads.csv のその曲の行を「手動指定」にする（次の sheet-fetch でこの動画を使う）"""
    _, lst = load_cached()
    if song not in lst["songs"]:
        near = [n for n in lst["songs"] if song in n or n in song]
        raise ValueError(f"シートに「{song}」がありません" + (f"（候補: {', '.join(near)}）" if near else ""))
    vid = video_id_from(url_or_id)
    p = SHEET_DIR / "downloads.csv"
    fields = ["song", "video_id", "title", "duration", "sheet_end", "diff", "status"]
    rows = list(csv.DictReader(open(p, encoding="utf-8"))) if p.exists() else []
    for r in rows:
        if r["song"] == song:
            r.update(video_id=vid, title="(手動指定)", duration="", diff="", status="manual")
            break
    else:
        rows.append({"song": song, "video_id": vid, "title": "(手動指定)", "duration": "",
                     "sheet_end": "", "diff": "", "status": "manual"})
    SHEET_DIR.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return vid

