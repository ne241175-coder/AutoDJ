"""アンケートで最初の曲を決め、そのあとの曲は全曲から自動でおすすめする。

流れ
  1. アンケート（テンポ・雰囲気・迫力・始まり方）に答える
  2. 取り込み済みの全曲を、答えにどれだけ合うかで点数化 → 上位 3〜5 曲を「最初の曲」の候補にする
  3. 2曲目・3曲目は、直前の曲から「つながりやすい順」に全曲を並べる
     （mixplan.candidate_links の点数。人の評価から学習した重みがあればそれを使う）

どの曲でも最初の曲になり得る（シートのルートは使わない）。
"""
import time
from pathlib import Path

from .db import connect
from .library import analysis_path
from .mixplan import candidate_links, load_song

# ---------- アンケートの質問 ----------
# 各選択肢の match(プロフィール) が 0〜1 を返す。「どれでもいい」は全曲 1。
QUESTIONS = [
    {"id": "tempo", "q": "どんなテンポがいい？", "choices": [
        {"id": "slow", "label": "ゆったり", "desc": "〜100 BPM"},
        {"id": "mid", "label": "ほどよく", "desc": "100〜140 BPM"},
        {"id": "fast", "label": "速く・ノリノリ", "desc": "140 BPM〜"}]},
    {"id": "mood", "q": "雰囲気は？", "choices": [
        {"id": "bright", "label": "明るい", "desc": "長調の曲"},
        {"id": "dark", "label": "切ない・かっこいい", "desc": "短調の曲"}]},
    {"id": "power", "q": "音の迫力は？", "choices": [
        {"id": "soft", "label": "やさしめ", "desc": "この中では静かな方"},
        {"id": "mid", "label": "ふつう", "desc": ""},
        {"id": "loud", "label": "ガツンと", "desc": "この中では音が大きい方"}]},
    {"id": "start", "q": "始まり方は？", "choices": [
        {"id": "chorus", "label": "いきなりサビ", "desc": "最初の20秒以内にサビ"},
        {"id": "build", "label": "じわじわ盛り上がる", "desc": "イントロ・Aメロから"}]},
]
ANY = "any"
MIN_ROOTS, MAX_ROOTS = 3, 5
ROOT_MARGIN = 0.75      # 1位との差がこの点数以内なら候補に入れる（3〜5曲の範囲で）
NEXT_LIMIT = 10         # 2曲目・3曲目のおすすめを何曲まで出すか


def norm_bpm(bpm):
    """半分・倍に取り違えやすいので 75〜200 に収める"""
    b = float(bpm or 0)
    if b <= 0:
        return None
    while b < 75:
        b *= 2
    while b > 200:
        b /= 2
    return b


def _tempo_class(b):
    return None if b is None else "slow" if b < 100 else "mid" if b < 140 else "fast"


# ---------- 曲ごとのプロフィール（解析結果から。キャッシュする） ----------
_cache = {"key": None, "songs": {}, "profiles": []}


def _sheet_structure():
    try:
        from .sheet import load_cached, SHEET_DIR
        s, _ = load_cached()
        p = SHEET_DIR / "structure.json"
        return s, (p.stat().st_mtime if p.exists() else 0)
    except FileNotFoundError:
        return {}, 0


def _start_kind(song):
    """最初の20秒以内にサビが始まるか（イントロが短くすぐサビ、も含む）"""
    for s in song.sections:
        if s["start"] is None:
            continue
        if s["start"] > 20:
            break
        if s["role"] in ("chorus", "chorus_drop"):
            return "chorus"
    return "build"


def library(con=None):
    """使える曲（解析済み・音源あり）の Song とプロフィール。解析結果かシートが変わったら作り直す"""
    con = con or connect()
    rows = con.execute("SELECT * FROM tracks WHERE analyzed_at IS NOT NULL ORDER BY id").fetchall()
    rows = [r for r in rows if Path(r["file_path"]).exists() and analysis_path(r["id"]).exists()]
    structure, smt = _sheet_structure()
    key = (smt, tuple((r["id"], r["title"], analysis_path(r["id"]).stat().st_mtime) for r in rows))
    if _cache["key"] == key:
        return _cache["songs"], _cache["profiles"]
    import json
    songs, profs = {}, []
    for r in rows:
        try:
            s = load_song(con, r["id"], structure)
        except (KeyError, RuntimeError):
            continue
        a = json.loads(analysis_path(r["id"]).read_text(encoding="utf-8"))
        songs[r["id"]] = s
        b = norm_bpm(s.bpm)
        profs.append({"id": r["id"], "title": r["title"], "bpm": b, "tempo": _tempo_class(b),
                      "mode": s.mode or ("minor" if (s.camelot or "").endswith("A") else "major"),
                      "mode_sure": not a["key"].get("relative_ambiguous", False),
                      "loudness": a.get("loudness_db"), "start": _start_kind(s), "camelot": s.camelot,
                      "structure": s.structure_source})
    # 迫力は「この曲たちの中で」静か／ふつう／大きい（3等分）
    louds = sorted(p["loudness"] for p in profs if p["loudness"] is not None)
    for p in profs:
        if p["loudness"] is None or len(louds) < 3:
            p["power"] = "mid"
            continue
        rank = sum(1 for x in louds if x < p["loudness"]) / max(1, len(louds) - 1)
        p["power"] = "soft" if rank < 1 / 3 else "loud" if rank > 2 / 3 else "mid"
        p["power_rank"] = rank
    _cache.update(key=key, songs=songs, profiles=profs)
    return songs, profs


# ---------- アンケート → 最初の曲の候補 ----------
def match(q, choice, p):
    """1曲が1つの答えにどれだけ合うか（0〜1）"""
    if choice in (None, "", ANY):
        return 1.0
    if q == "tempo":
        b = p["bpm"]
        if b is None:
            return 0.3
        lo, hi = {"slow": (0, 100), "mid": (100, 140), "fast": (140, 999)}[choice]
        d = 0 if lo <= b < hi else min(abs(b - lo), abs(b - hi))
        return max(0.0, 1 - d / 25)                 # 境目の近くは少しだけ合う
    if q == "mood":
        want = "major" if choice == "bright" else "minor"
        return 1.0 if p["mode"] == want else (0.4 if not p["mode_sure"] else 0.0)
    if q == "power":
        order = {"soft": 0, "mid": 1, "loud": 2}
        return {0: 1.0, 1: 0.4}.get(abs(order[choice] - order[p["power"]]), 0.0)
    if q == "start":
        return 1.0 if p["start"] == choice else 0.0
    return 1.0


def tags(p):
    t = [f"{p['bpm']:.0f} BPM" if p["bpm"] else None,
         "明るい" if p["mode"] == "major" else "切ない・かっこいい",
         {"soft": "やさしめ", "mid": "迫力ふつう", "loud": "ガツンと"}[p["power"]],
         "いきなりサビ" if p["start"] == "chorus" else "じわじわ"]
    return [x for x in t if x]


def choice_counts(profiles=None):
    """選択肢ごとに、はっきり合う曲が何曲あるか（画面に出す）"""
    if profiles is None:
        _, profiles = library()
    return {q["id"]: {c["id"]: sum(1 for p in profiles if match(q["id"], c["id"], p) >= 0.99) for c in q["choices"]}
            for q in QUESTIONS}


def n_exact(answers, profiles=None):
    """答えたすべての質問にはっきり合う曲の数"""
    if profiles is None:
        _, profiles = library()
    asked = [q["id"] for q in QUESTIONS if answers.get(q["id"]) not in (None, "", ANY)]
    return sum(1 for p in profiles if all(match(q, answers[q], p) >= 0.99 for q in asked))


def pick_roots(answers, profiles=None):
    """答え {質問id: 選択肢id} → 最初の曲の候補 3〜5曲（合う順）"""
    if profiles is None:
        _, profiles = library()
    asked = [q["id"] for q in QUESTIONS if answers.get(q["id"]) not in (None, "", ANY)]
    scored = []
    for p in profiles:
        parts = {q: match(q, answers.get(q), p) for q in asked}
        total = sum(parts.values())
        hit = [q for q, v in parts.items() if v >= 0.99]
        scored.append((total, p, hit))
    # 同点は曲名順（毎回同じ結果）
    scored.sort(key=lambda x: (-x[0], x[1]["title"]))
    if not scored:
        return []
    best = scored[0][0]
    n = sum(1 for s in scored if s[0] >= best - ROOT_MARGIN)
    n = max(MIN_ROOTS, min(MAX_ROOTS, n))
    out = []
    for total, p, hit in scored[:n]:
        out.append({"id": p["id"], "title": p["title"], "score": round(total / max(1, len(asked)), 3),
                    "hits": hit, "n_asked": len(asked), "tags": tags(p)})
    return out


# ---------- 2曲目・3曲目のおすすめ ----------
def recommend_next(after, exclude=(), limit=NEXT_LIMIT, weights=None):
    """直前の曲 after(id) から、つながりやすい順に全曲を並べる。
    点数 = その2曲の間で一番良いつなぎ目の点数（テンポ・キー・勢い・区切りの確かさ。学習した重みを使う）"""
    songs, profs = library()
    a = songs.get(int(after))
    if a is None:
        raise KeyError(f"曲が見つからないか未解析です: {after}")
    skip = {int(x) for x in exclude} | {a.track_id}
    byid = {p["id"]: p for p in profs}
    t0 = time.time()
    out = []
    for bid, b in songs.items():
        if bid in skip:
            continue
        links = candidate_links(a, b, weights=weights)
        if not links:
            continue
        best = max(links, key=lambda l: l.score)
        f = best.features
        why = [f"BPM {norm_bpm(a.bpm):.0f}→{norm_bpm(b.bpm):.0f}" + ("（近い）" if f["tempo"] >= 0.99 else ""),
               f"Key {a.camelot}→{b.camelot}" + ("（相性◎）" if f["key"] >= 0.99 else "（相性○）" if f["key"] >= 0.8 else "")]
        out.append({"id": bid, "title": b.title, "score": round(best.score, 4), "fit": round(min(1.0, max(0.0, best.score)), 3),
                    "kind": best.kind, "out": best.out, "in": best.inn, "why": why,
                    "tags": tags(byid[bid]) if bid in byid else []})
    out.sort(key=lambda x: (-x["score"], x["title"]))
    return {"items": out[:limit], "total": len(out), "elapsed": round(time.time() - t0, 3)}
