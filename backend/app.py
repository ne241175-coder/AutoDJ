"""AutoDJ の localhost Web アプリ（API + 仮UI）。

起動:  python -m backend.app   →  http://localhost:5050

API（UI を差し替えてもこのまま使える）
  GET  /api/tracks              曲一覧（BPM・キー・解析済みか・シートの構成があるか）
  GET  /api/tracks/<id>         1曲の詳細（区間・小節頭・キー候補）。?auto=1 で自動検知の区間だけ
  GET  /api/audio/<id>          mp3 そのもの（Range対応）。?rate=1.02&semi=-1 でテンポ・キーを合わせた音声
  GET  /api/routes              シートの「ルート曲→中間曲→曲名」とつなぎメモ
  GET  /api/tracks/<id>/wave    波形（ピーク値の列。初回だけ計算して保存）
  GET  /api/jacket/<id>         ジャケット画像（data/jackets/曲名.jpg があればそれ、なければ動画のサムネイル）
  POST /api/plan                {"songs": [id or 曲名...], "min_sec": 90, "chorus_grace": 15,
                                 "styles": ["fade", "cut", ...], "use_sheet": false（＝すべて自動検知）,
                                 "links": {"0": {"out": 秒, "in": 秒}}（候補の選び直し）,
                                 "match_bpm": true, "match_key": true（入る曲を出る曲に合わせる）} → 再生計画
                                 transitions[].explain（なぜここか）/ alternatives（ほかの候補）/ human（シートのメモ）
  POST /api/tracks/<id>/analyze 解析を（再）実行
  POST /api/feedback            ミックスを聞いた人の評価 {"songs": [...], "links": [{"chosen": 特徴, "rating": "good"|"bad",
                                 "better": 特徴|null, "others": [特徴...]}]} → data/feedback.jsonl に保存し、重みを学び直す
  GET  /api/feedback            評価の件数と、いま使っている重み（手で決めた値 or 学習した値）
  POST /api/render              /api/plan と同じ指定で、できたミックスを mp3 に書き出してダウンロード
                                 （同じ指定なら data/cache/mix/ に作ったものを使い回す）
"""
import json
import re
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_file

from .config import ANALYSIS_DIR, DATA_DIR

from .db import connect
from .library import load_analysis, analyze_track
from .mixplan import build_plan, AUTO_ROLE, ROLE_JA
from . import linkmodel

STATIC = Path(__file__).resolve().parent / "static"
JACKET_DIR = DATA_DIR / "jackets"   # ここに「曲名.jpg」を置くとジャケットとして使う
IMG_EXT = (".jpg", ".jpeg", ".png", ".webp")


def _video_id(row):
    sid = row["source_id"] or ""
    return sid[3:] if sid.startswith("yt:") else None


def _jacket_file(title):
    if not title:
        return None
    safe = re.sub(r'[\\/:*?"<>|]', "_", title)
    for name in {title, safe}:
        for ext in IMG_EXT:
            p = JACKET_DIR / f"{name}{ext}"
            if p.exists():
                return p
    return None
app = Flask(__name__, static_folder=str(STATIC), static_url_path="/static")


def _sheet():
    try:
        from .sheet import load_cached
        return load_cached()
    except FileNotFoundError:
        return {}, {"songs": {}, "routes": []}


def _sections(row, analysis, structure):
    sheet = structure.get(row["title"])
    if sheet:
        secs = sheet["sections"]
        if analysis:
            # シートの秒数は計測した動画の時刻なので、取り込んだ音源の時刻に合わせる
            from .align import aligned_sections
            secs, _ = aligned_sections(sheet, analysis)
        return "sheet", [{"role": s["role"], "label": s["label"], "start": s["start"], "end": s["end"]}
                         for s in secs]
    if analysis:
        return "auto", [{"role": AUTO_ROLE.get(s["role"], "other"),
                         "label": ROLE_JA.get(AUTO_ROLE.get(s["role"], "other")),
                         "start": s["start"], "end": s["end"],
                         "confidence": s.get("boundary_confidence")} for s in analysis["structure"]["sections"]]
    return None, []


@app.get("/")
def index():
    return send_file(STATIC / "index.html")


@app.get("/classic")
def classic():
    """以前の仮UI（区間の確認・デバッグ用）"""
    return send_file(STATIC / "classic.html")


@app.get("/api/tracks")
def tracks():
    con = connect()
    structure, _ = _sheet()
    out = []
    for r in con.execute("""SELECT t.*, s.name AS set_name FROM tracks t
                            LEFT JOIN sets s ON t.set_id = s.id ORDER BY t.id"""):
        out.append({"id": r["id"], "title": r["title"], "genre": r["genre"], "set": r["set_name"],
                    "bpm": r["bpm"], "key": r["key_name"], "camelot": r["camelot"],
                    "duration": r["duration"], "analyzed": r["analyzed_at"] is not None,
                    "has_sheet_structure": r["title"] in structure,
                    "artist": r["artist"], "video_id": _video_id(r),
                    "jacket": f"/api/jacket/{r['id']}" if (_jacket_file(r["title"]) or _video_id(r)) else None,
                    # video = 動画のサムネイル（16:9 の上下に黒帯があるので画面側で拡大して切り取る）
                    "jacket_kind": "file" if _jacket_file(r["title"]) else ("video" if _video_id(r) else None),
                    "file_exists": Path(r["file_path"]).exists()})
    return jsonify(out)


@app.get("/api/tracks/<int:tid>")
def track(tid):
    con = connect()
    r = con.execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
    if r is None:
        abort(404)
    a = load_analysis(tid)
    structure, _ = _sheet()
    if request.args.get("auto"):          # 自動検知の構造だけを見る（シートの手入力を使わない）
        structure = {}
    src, sections = _sections(r, a, structure)
    return jsonify({
        "id": tid, "title": r["title"], "duration": a["duration"] if a else r["duration"],
        "bpm": a["tempo"]["bpm"] if a else None, "tempo_stable": a["tempo"]["stable"] if a else None,
        "key": a["key"] if a else None, "bars": a["bars"] if a else [],
        "structure_source": src, "sections": sections,
        "bar_energy": a["structure"].get("bar_energy", []) if a else [],
    })


@app.get("/api/audio/<int:tid>")
def audio(tid):
    r = connect().execute("SELECT file_path FROM tracks WHERE id=?", (tid,)).fetchone()
    if r is None or not Path(r["file_path"]).exists():
        abort(404)
    rate = float(request.args.get("rate", 1.0))
    semi = int(request.args.get("semi", 0))
    path = r["file_path"]
    if abs(rate - 1) > 1e-4 or semi:
        # BPM・キーを合わせた音声（初回だけ数秒かけて作り、以後は使い回す）
        from .audio_fx import processed
        if not (0.5 <= rate <= 2.0 and -6 <= semi <= 6):
            abort(400)
        path = processed(path, tid, rate, semi)
    # conditional=True で Range リクエスト（途中から読む）に対応
    return send_file(path, conditional=True, mimetype="audio/mpeg")


@app.get("/api/jacket/<int:tid>")
def jacket(tid):
    r = connect().execute("SELECT * FROM tracks WHERE id=?", (tid,)).fetchone()
    if r is None:
        abort(404)
    p = _jacket_file(r["title"])
    if p:
        return send_file(p, max_age=3600)
    vid = _video_id(r)
    if vid:
        return redirect(f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg")
    abort(404)


def wave_peaks(path, n=600, sr=4000):
    """mp3 全体を n 個の区間に分け、各区間の最大振幅（0〜1）を返す"""
    import librosa
    import numpy as np
    y, _ = librosa.load(str(path), sr=sr, mono=True)
    if len(y) == 0:
        return []
    edges = np.linspace(0, len(y), n + 1).astype(int)
    peaks = np.array([np.abs(y[a:b]).max() if b > a else 0.0 for a, b in zip(edges[:-1], edges[1:])])
    peaks = peaks / (peaks.max() + 1e-9)
    return [round(float(x), 3) for x in peaks]


@app.get("/api/tracks/<int:tid>/wave")
def wave(tid):
    r = connect().execute("SELECT file_path FROM tracks WHERE id=?", (tid,)).fetchone()
    if r is None or not Path(r["file_path"]).exists():
        abort(404)
    f = Path(r["file_path"])
    cache = ANALYSIS_DIR / f"{tid}.wave.json"
    if cache.exists():
        c = json.loads(cache.read_text())
        if c.get("mtime") == f.stat().st_mtime:
            return jsonify(c)
    c = {"mtime": f.stat().st_mtime, "peaks": wave_peaks(f)}
    cache.write_text(json.dumps(c))
    return jsonify(c)


@app.get("/api/routes")
def routes():
    con = connect()
    titles = {r["title"]: r["id"] for r in con.execute("SELECT id, title FROM tracks")}
    _, lst = _sheet()
    out = []
    for rt in lst.get("routes", []):
        chain = [x for x in (rt["root"], rt["middle"], rt["leaf"]) if x]
        out.append({**rt, "chain": chain, "available": all(c in titles for c in chain),
                    "missing": [c for c in chain if c not in titles]})
    return jsonify(out)


def _plan_from(body):
    songs = body.get("songs") or []
    if len(songs) < 2:
        raise ValueError("2曲以上選んでください")
    return build_plan(songs, min_sec=float(body.get("min_sec", 90)), use_sheet=body.get("use_sheet", True),
                      chorus_grace=float(body.get("chorus_grace", 15)), styles=body.get("styles"),
                      links=body.get("links"), match_bpm=body.get("match_bpm", True),
                      match_key=body.get("match_key", True))


@app.post("/api/plan")
def plan():
    body = request.get_json(force=True)
    try:
        p = _plan_from(body)
    except (KeyError, RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    for it in p["items"]:
        q = f"?rate={it['rate']:.5f}&semi={it['semitones']}" if (it.get("rate", 1.0) != 1.0 or it.get("semitones")) else ""
        it["audio_url"] = f"/api/audio/{it['track_id']}{q}"
        it.pop("file", None)
    return jsonify(p)


MIX_DIR = DATA_DIR / "cache" / "mix"


@app.post("/api/render")
def render_mix():
    """画面で聞いたのと同じミックスを mp3 にする（つなぎ位置・繋ぎ方・BPM/キー合わせもそのまま）"""
    import hashlib
    from .render import render
    body = request.get_json(force=True) or {}
    try:
        p = _plan_from(body)
    except (KeyError, RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    # 同じ計画なら作り直さない（計画の中身と、使う音源の更新時刻から名前を決める）
    sig = json.dumps({"items": [[it["file"], it["start"], it["end"], it.get("rate"), it.get("semitones")] for it in p["items"]],
                      "trans": [[t["out"], t["in"], t.get("overlap_beats"), t.get("style")] for t in p["transitions"]],
                      "mtime": [Path(it["file"]).stat().st_mtime if Path(it["file"]).exists() else 0 for it in p["items"]]},
                     sort_keys=True)
    MIX_DIR.mkdir(parents=True, exist_ok=True)
    out = MIX_DIR / f"{hashlib.sha1(sig.encode()).hexdigest()[:16]}.mp3"
    if not out.exists():
        try:
            made, cue = render(p, out)
        except (OSError, RuntimeError, ValueError) as e:
            return jsonify({"error": f"書き出せませんでした: {e}"}), 500
        if made != out:      # ffmpeg が無いときは wav になる
            out = made
    name = "AutoDJ_" + "_".join(re.sub(r'[\\/:*?"<>|\s]+', "", it["title"])[:20] for it in p["items"]) + out.suffix
    return send_file(out, as_attachment=True, download_name=name,
                     mimetype="audio/mpeg" if out.suffix == ".mp3" else "audio/wav")


@app.post("/api/feedback")
def feedback():
    body = request.get_json(force=True) or {}
    links = [l for l in (body.get("links") or []) if isinstance(l, dict) and l.get("chosen")]
    if not links:
        return jsonify({"error": "評価がありません"}), 400
    allowed = set(linkmodel.FEATURES)
    clean = lambda f: {k: float(v) for k, v in (f or {}).items() if k in allowed} if f else None   # noqa: E731
    entry = {"songs": body.get("songs") or [], "auto": bool(body.get("auto", True)), "source": body.get("source", "ui"),
             "links": [{"k": l.get("k"), "rating": l.get("rating") if l.get("rating") in ("good", "bad") else None,
                        "chosen": clean(l["chosen"]), "better": clean(l.get("better")),
                        "others": [clean(o) for o in (l.get("others") or [])[:6] if o],
                        "kind": l.get("kind"), "better_kind": l.get("better_kind")} for l in links]}
    linkmodel.record(entry)
    res = linkmodel.learn() if app.config.get("AUTO_LEARN", True) else {"saved": False}
    return jsonify({"ok": True, "learned": res.get("saved", False), "model": linkmodel.model_info(),
                    "message": res.get("message")})


@app.get("/api/feedback")
def feedback_summary():
    return jsonify({"model": linkmodel.model_info(), "weights": linkmodel.current_weights(),
                    "default": linkmodel.DEFAULT_WEIGHTS, "labels": linkmodel.FEATURE_JA})


@app.post("/api/tracks/<int:tid>/analyze")
def analyze(tid):
    try:
        r = analyze_track(tid, force=True)
    except (KeyError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True, "bpm": r["tempo"]["bpm"], "key": r["key"]["name"]})


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=5050)
    a = p.parse_args()
    print(f"AutoDJ: http://localhost:{a.port}  （終了は Ctrl+C）")
    app.run(host="127.0.0.1", port=a.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
