"""シートとの自動同期（アプリを起動しているあいだ、数分ごと）。

人がやるのは「曲リストに曲を足す（曲名と YouTube の URL）」と「構成タブに構成を入れる」だけ。あとはここが自動でやる:
  1. シートを取り込む（構成・曲リスト・以前のシートのルート）。中身が変わったときだけ書き直す
  2. 曲リストにあってアプリに無い曲 → ダウンロード → 登録 → 解析（addsong のキューに入れる）
  3. 曲リストの URL が変わった曲 → その動画で取り込み直す（構成を測った動画と音源をそろえる）
  4. 構成が入った・直った曲 → 次に曲を選ぶときから、その構成でつなぎ目を決める（library のキャッシュが作り直される）

AUTODJ_SYNC_MIN（分。既定 5、0 で止める）で間隔を変えられる。
"""
import os
import threading
import time

from .fetch_songs import video_id_from

INTERVAL_MIN = float(os.environ.get("AUTODJ_SYNC_MIN", "5"))

_lock = threading.Lock()
_status = {"last": None, "ok": None, "error": None, "version": 0, "structure": 0, "team_songs": 0,
           "queued": [], "no_url": [], "running": False, "interval_min": INTERVAL_MIN}
_failed = {}       # video_id → 失敗した時刻（同じ動画は 1 時間は試し直さない）
_thread = None


def status():
    with _lock:
        return dict(_status)


def _submit_new(con, log):
    """新しい曲・URL が変わった曲を取り込みキューへ。戻り値: (キューに入れた曲名, URL が無い曲名)"""
    from . import addsong
    from .teamsheet import team_songs
    # 解析まで済んだ曲だけ「取り込み済み」。ダウンロードだけで止まった曲（途中でアプリを閉じた等）はもう一度キューへ
    have = {r["title"]: (r["source_id"] or "") for r in
            con.execute("SELECT title, source_id FROM tracks WHERE analyzed_at IS NOT NULL")}
    known = {r["title"] for r in con.execute("SELECT title FROM tracks")}
    busy = {j["video_id"] for j in addsong.jobs() if j["stage"] not in ("done", "error")}
    queued, no_url = [], []
    for s in team_songs():
        if not s["url"]:
            if s["title"] not in known:
                no_url.append(s["title"])
            continue
        try:
            vid = video_id_from(s["url"])
        except ValueError:
            log(f"URL が読めません: {s['title']} {s['url']}")
            continue
        if s["title"] in have and have[s["title"]].endswith(vid):
            continue                                 # 取り込み済み（同じ動画）
        if vid in busy or time.time() - _failed.get(vid, 0) < 3600:
            continue
        try:
            addsong.submit(s["url"], s["title"])
            queued.append(s["title"])
            why = "（URL が変わったので取り込み直し）" if s["title"] in have else "（解析のやり直し）" if s["title"] in known else ""
            log(f"取り込みます: {s['title']}{why}")
        except ValueError as e:
            _failed[vid] = time.time()
            log(f"取り込めません: {s['title']}: {e}")
    return queued, no_url


def _note_failures():
    from . import addsong
    for j in addsong.jobs():
        if j["stage"] == "error":
            _failed.setdefault(j["video_id"], j.get("finished") or time.time())


def run_once(log=print, fetch=True):
    """1回ぶんの同期。変わったものがあれば version を上げる（画面はそれを見て曲の一覧を読み直す）"""
    from .db import connect
    from .sheet import fetch_csv, load
    with _lock:
        if _status["running"]:
            return dict(_status)
        _status["running"] = True
    changed = False
    try:
        if fetch:
            fetch_csv()
        structure, lst, changed = load(report_change=True)
        _note_failures()
        con = connect()
        queued, no_url = _submit_new(con, log)
        from .teamsheet import team_songs
        with _lock:
            _status.update(last=time.time(), ok=True, error=None, structure=len(structure),
                           team_songs=len(team_songs()), queued=queued, no_url=no_url)
            if changed:
                _status["version"] += 1
    except Exception as e:      # ネットが切れている等。次の回にまた試す
        log(f"シートの同期に失敗: {e}")
        with _lock:
            _status.update(last=time.time(), ok=False, error=str(e))
    finally:
        with _lock:
            _status["running"] = False
    return status()


def bump():
    """曲の取り込みが終わったときなど、画面に読み直してほしいとき"""
    with _lock:
        _status["version"] += 1


def start(log=print):
    """バックグラウンドで定期的に同期する（起動直後に1回）"""
    global _thread
    if INTERVAL_MIN <= 0 or (_thread and _thread.is_alive()):
        return

    def loop():
        while True:
            run_once(log)
            time.sleep(INTERVAL_MIN * 60)
    _thread = threading.Thread(target=loop, name="autosync", daemon=True)
    _thread.start()
