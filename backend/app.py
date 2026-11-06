"""AutoDJ の localhost Web アプリ（API + 仮UI）。

起動:  python -m backend.app   →  http://localhost:5050

API（UI を差し替えてもこのまま使える）
  GET  /api/tracks              曲一覧（BPM・キー・解析済みか・シートの構成があるか）
  GET  /api/tracks/<id>         1曲の詳細（区間・小節頭・キー候補）
  GET  /api/audio/<id>          mp3 そのもの（Range対応。ブラウザが必要な部分だけ取りに来る）
  GET  /api/routes              シートの「ルート曲→中間曲→曲名」とつなぎメモ
  POST /api/plan                {"songs": [id or 曲名...], "min_sec": 90} → 再生計画
  POST /api/tracks/<id>/analyze 解析を（再）実行
"""
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

from .db import connect
from .library import load_analysis, analyze_track
from .mixplan import build_plan, AUTO_ROLE, ROLE_JA

STATIC = Path(__file__).resolve().parent / "static"
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
        return "sheet", [{"role": s["role"], "label": s["label"], "start": s["start"], "end": s["end"]}
                         for s in sheet["sections"]]
    if analysis:
        return "auto", [{"role": AUTO_ROLE.get(s["role"], "other"),
                         "label": ROLE_JA.get(AUTO_ROLE.get(s["role"], "other")),
                         "start": s["start"], "end": s["end"]} for s in analysis["structure"]["sections"]]
    return None, []


@app.get("/")
def index():
    return send_file(STATIC / "index.html")


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
    # conditional=True で Range リクエスト（途中から読む）に対応
    return send_file(r["file_path"], conditional=True)


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


@app.post("/api/plan")
def plan():
    body = request.get_json(force=True)
    songs = body.get("songs") or []
    if len(songs) < 2:
        return jsonify({"error": "2曲以上選んでください"}), 400
    try:
        p = build_plan(songs, min_sec=float(body.get("min_sec", 90)), use_sheet=body.get("use_sheet", True))
    except (KeyError, RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    for it in p["items"]:
        it["audio_url"] = f"/api/audio/{it['track_id']}"
        it.pop("file", None)
    return jsonify(p)


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
