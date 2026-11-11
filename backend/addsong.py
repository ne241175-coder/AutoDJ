"""画面（☰ → 曲を追加）から YouTube の曲を足す。

URL を受け取ったら、裏で ダウンロード → DB に登録 → 解析 → （指定があれば）ルートに追加 まで進める。
1曲ずつ順番に処理し、進み具合は jobs() で画面に返す。

DB に入るもの
  tracks         曲（シートの曲と同じ「sheet」セットに入れるので、analyze --set sheet などもそのまま使える）
  routes         画面で足した「1曲目 → 2曲目 → 3曲目」
  downloads.csv  この曲はこの動画（status=manual。あとで sheet-fetch しても選び直さない）

注意: YouTube からのダウンロードは個人利用の範囲で、著作権と YouTube の規約に従って行うこと。
"""
import queue
import re
import shutil
import threading
import time
import uuid

from .config import ANALYSIS_DIR, AUDIO_DIR
from .db import add_route, connect, get_or_create_set, rename_in_routes
from .fetch_songs import record_choice, video_id_from
from .ingest import _register, _ydl_opts

SET_NAME = "sheet"
KEEP_JOBS = 30          # 画面に返す、終わった仕事の数


# ---------- 動画の情報から曲名を推測する ----------
_BRACKETS = r"[【\[［(（<〈][^】\]］)）>〉]*[】\]］)）>〉]"
_NOISE = re.compile(r"(official|music\s*video|lyric|lyrics|audio|video|\bmv\b|\bm/v\b|公式|歌詞|full|ver\.?|"
                    r"version|hd|4k|live|teaser|short)", re.I)


def _clean_artist(s):
    s = re.sub(r"\s*-\s*Topic$", "", s or "")
    s = re.sub(r"(?i)\s*(official\s*(youtube)?\s*(channel)?|オフィシャル\s*(チャンネル)?|公式\s*(チャンネル)?|"
               r"channel|vevo)\s*$", "", s)
    return s.strip(" -_・")


def _norm(s):
    return re.sub(r"[\s・.\-_'’]", "", s or "").lower()


def guess_title(info):
    """yt-dlp の情報 → (曲名, アーティスト)。外れることもあるので画面で直せるようにしておく"""
    channel = info.get("channel") or info.get("uploader") or ""
    artist = _clean_artist(info.get("artist") or info.get("creator") or channel)
    if info.get("track"):                     # 音楽として登録された動画（Topic など）は曲名が付いている
        return info["track"].strip(), artist.split(",")[0].strip()
    t = info.get("title") or ""
    m = re.search(r"[「『](.+?)[」』]", t)      # 米津玄師 MV「LOSER」 / YOASOBI「アイドル」Official Music Video
    if m:
        before = re.sub(r"(?i)(【[^】]*】|\bmv\b|\bm/v\b|music\s*video)", "", t[:m.start()]).strip(" -–—/／|｜")
        return m.group(1).strip(), before or artist
    # 【MV】 [Official Music Video] (Lyric Video) などを外す
    t = re.sub(_BRACKETS, lambda x: "" if _NOISE.search(x.group(0)) or len(x.group(0)) <= 4 else x.group(0), t)
    t = re.sub(r"(?i)\b(official\s+)?(music\s+video|lyric\s+video|mv|audio)\b", "", t).strip(" -–—/／|｜")
    parts = [p.strip() for p in re.split(r"\s+[-–—/／|｜]\s+|\s*[／｜]\s*", t) if p.strip()]
    if len(parts) >= 2:
        # 「アーティスト - 曲名」でも「曲名 / アーティスト」でも、チャンネル名と一致しない方を曲名にする
        same = [_norm(p) and (_norm(p) in _norm(channel) or _norm(channel) in _norm(p) or _norm(p) == _norm(artist))
                for p in parts]
        if any(same) and not all(same):
            i = same.index(True)
            title = next(p for p, s in zip(parts, same) if not s)
            return title, parts[i] if not artist else artist
        return parts[-1], artist or parts[0]
    return t.strip() or (info.get("title") or "").strip(), artist


# ---------- yt-dlp（テストでは差し替える） ----------
def _info(vid):
    import yt_dlp
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}) as y:
        return y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=False)


def _download(vid, hook):
    import yt_dlp
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg が見つかりません。macOS なら `brew install ffmpeg` を実行してください。")
    opts = _ydl_opts(AUDIO_DIR, False)
    opts["progress_hooks"] = [hook]
    with yt_dlp.YoutubeDL(opts) as y:
        return y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)


def _analyze(tid, con):
    from .library import analyze_track
    return analyze_track(tid, con=con, force=True)


def lookup(url):
    """URL の動画を調べて、曲名・アーティストの候補と、すでに取り込み済みかを返す（ダウンロードはしない）"""
    vid = video_id_from(url)
    try:
        info = _info(vid)
    except Exception as e:
        raise RuntimeError(f"動画の情報を取れませんでした: {_first_line(e)}") from e
    title, artist = guess_title(info)
    row = connect().execute("SELECT id, title FROM tracks WHERE source_id=?", (f"yt:{vid}",)).fetchone()
    return {"video_id": vid, "video_title": info.get("title") or "", "channel": info.get("channel") or info.get("uploader") or "",
            "duration": info.get("duration"), "thumbnail": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "title": title, "artist": artist,
            "existing": {"id": row["id"], "title": row["title"]} if row else None}


# ---------- 仕事（1曲分）の管理 ----------
_jobs = {}
_lock = threading.Lock()
_queue = queue.Queue()
_worker = None


def _first_line(e):
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:200]


def _public(j):
    return {k: j[k] for k in ("id", "video_id", "title", "artist", "route", "stage", "progress", "message",
                              "track_id", "error", "created", "finished")}


def jobs():
    with _lock:
        out = sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)
        return [_public(j) for j in out]


def _set(job, **kw):
    with _lock:
        job.update(kw)


def submit(url, title, artist=None, route=None, run_now=False):
    """仕事を受け付ける。run_now=True ならその場で最後まで実行する（コマンドやテスト用）"""
    vid = video_id_from(url)
    title = (title or "").strip()
    if not title:
        raise ValueError("曲名を入れてください")
    if route is not None:
        route = [str(x or "").strip() for x in route]
        if len(route) != 3 or not all(route) or len(set(route)) != 3 or title not in route:
            raise ValueError("ルートは、この曲を含めて3曲とも別々に選んでください")
    with _lock:
        for j in _jobs.values():             # 同じ動画を処理中なら、それを返す
            if j["video_id"] == vid and j["stage"] not in ("done", "error"):
                return _public(j)
        job = {"id": uuid.uuid4().hex[:8], "video_id": vid, "title": title, "artist": (artist or "").strip() or None,
               "route": route, "stage": "queued", "progress": 0.0, "message": "順番待ち",
               "track_id": None, "error": None, "created": time.time(), "finished": None}
        _jobs[job["id"]] = job
        done = sorted((j for j in _jobs.values() if j["stage"] in ("done", "error")), key=lambda j: j["created"])
        for j in done[:-KEEP_JOBS]:
            _jobs.pop(j["id"], None)
    if run_now:
        run(job)
        return _public(job)
    _queue.put(job["id"])
    _ensure_worker()
    return _public(job)


def _ensure_worker():
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_loop, name="autodj-add", daemon=True)
        _worker.start()


def _loop():
    while True:
        jid = _queue.get()
        job = _jobs.get(jid)
        if job:
            run(job)
        _queue.task_done()


def _replace_same_title(con, title, source_id):
    """同じ曲名で別の動画が入っていたら消す（曲名は1曲につき1つ。動画を選び直したとき用）"""
    olds = con.execute("SELECT id, source_id FROM tracks WHERE title=? AND COALESCE(source_id,'')<>?",
                       (title, source_id)).fetchall()
    for o in olds:
        con.execute("DELETE FROM tracks WHERE id=?", (o["id"],))
        for p in (ANALYSIS_DIR / f"{o['id']}.json", ANALYSIS_DIR / f"{o['id']}.wave.json"):
            if p.exists():
                p.unlink()
    con.commit()
    return len(olds)


def run(job):
    """ダウンロード → 登録 → 解析 → ルート。失敗したら stage=error と理由を残す"""
    con = connect()
    vid, title = job["video_id"], job["title"]
    sid = f"yt:{vid}"
    try:
        path = AUDIO_DIR / f"{vid}.mp3"
        row = con.execute("SELECT * FROM tracks WHERE source_id=?", (sid,)).fetchone()
        info = {}
        if row and path.exists():
            _set(job, stage="download", progress=1.0, message="取り込み済みの音源を使います")
        else:
            _set(job, stage="download", progress=0.0, message="ダウンロード中")

            def hook(d):
                if d.get("status") == "downloading":
                    total = d.get("total_bytes") or d.get("total_bytes_estimate")
                    if total:
                        _set(job, progress=min(0.99, d.get("downloaded_bytes", 0) / total))
                elif d.get("status") == "finished":
                    _set(job, progress=1.0, message="mp3 に変換中")
            info = _download(vid, hook) or {}
            if not path.exists():
                raise RuntimeError("mp3 ができませんでした（ffmpeg を確認してください）")

        # ---- DB に登録（同じ曲名の古い動画は置き換え、同じ動画が別の曲名なら名前を付け替える）
        _set(job, stage="register", message="DB に登録中")
        replaced = _replace_same_title(con, title, sid)
        set_id = get_or_create_set(con, SET_NAME)
        old_title = row["title"] if row else None
        tid, _ = _register(con, title=title, artist=job["artist"], url=f"https://www.youtube.com/watch?v={vid}",
                           source_id=sid, path=path, genres=["J-pop"], set_id=set_id,
                           duration=info.get("duration") or (row["duration"] if row else None))
        con.execute("UPDATE tracks SET title=?, artist=COALESCE(?, artist), file_path=? WHERE id=?",
                    (title, job["artist"], str(path), tid))
        con.commit()
        rename_in_routes(con, old_title, title)
        try:
            record_choice(title, vid, title=info.get("title") or "(画面で追加)", duration=info.get("duration") or "")
        except OSError:
            pass                               # 記録できなくても曲は使える
        _set(job, track_id=tid)

        # ---- 解析（1曲20秒くらい）
        _set(job, stage="analyze", progress=0.0, message="解析中（BPM・キー・構成）")
        t0 = time.time()
        res = _analyze(tid, con)

        # ---- ルート
        if job["route"]:
            add_route(con, job["route"])
        msg = f"追加しました（{res['tempo']['bpm']} BPM / {res['key']['name']}・解析 {time.time() - t0:.0f}秒）"
        if replaced:
            msg += "。前の動画と置き換えました"
        _set(job, stage="done", progress=1.0, message=msg, finished=time.time())
    except Exception as e:                      # 画面に理由を出す
        _set(job, stage="error", error=_first_line(e), message="失敗しました", finished=time.time())
    finally:
        con.close()
    return job
