"""アンケートで最初の曲を決め、そのあとの曲は全曲から自動でおすすめする。

流れ
  1. アンケート（テンポ・雰囲気・迫力・始まり方）に答える
  2. 取り込み済みの全曲を、答えにどれだけ合うかで点数化 → 上位 3〜5 曲を「最初の曲」の候補にする
  3. 2曲目・3曲目は、直前の曲から「つながりやすい順」に全曲を並べる
     （mixplan.candidate_links の点数。人の評価から学習した重みがあればそれを使う）

どの曲でも最初の曲になり得る（シートのルートは使わない）。
"""
import json
import random
import time

import numpy as np
from pathlib import Path

from .analysis.feel import FEEL_VERSION, feel_bpm, offbeat_features
from .config import DATA_DIR, SR
from .db import connect
from .library import analysis_path
from .linkmodel import fit_percent
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


FEEL_CACHE = DATA_DIR / "cache" / "feel"


def feel_features(track_id, file_path, analysis):
    """体感テンポ用の特徴。新しい解析結果には入っている。古い解析結果は音源から計算して data/cache/feel/ に残す"""
    f = analysis["tempo"].get("feel")
    if f and f.get("version") == FEEL_VERSION:
        return f
    p = FEEL_CACHE / f"{track_id}.json"
    if p.exists():
        c = json.loads(p.read_text(encoding="utf-8"))
        if c.get("version") == FEEL_VERSION and c.get("beats") == len(analysis["beats"]):
            return c
    import librosa
    y, _ = librosa.load(str(file_path), sr=SR, mono=True)
    f = offbeat_features(y, SR, analysis["beats"])
    if f:
        FEEL_CACHE.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({**f, "beats": len(analysis["beats"])}), encoding="utf-8")
    return f


def song_bpm(bpm, analysis, feats):
    """アンケートで使う体感 BPM。(bpm, 倍にしたか)"""
    b = norm_bpm(bpm)
    return (None, False) if b is None else feel_bpm(b, analysis["tempo"], feats)


SLOW_BPM, FAST_BPM = 100.0, 140.0
MIN_SLOW_SONGS = 3      # 100 BPM 未満がこれより少なければ「ゆったり」は「この中では遅め」にする
SLOW_SHARE = 0.2        # そのときは遅い方から 2割


def tempo_bands(bpms):
    """「ゆったり／ほどよく／速く」の BPM の範囲。
    取り込んだ曲に 100 BPM 未満がほとんど無い（J-pop は速い曲が多い）と「ゆったり」が 0曲になるので、
    そのときは迫力と同じく「この中では遅め」（遅い方から 2割）にする"""
    bs = sorted(b for b in bpms if b)
    slow = SLOW_BPM
    if bs and sum(1 for b in bs if b < SLOW_BPM) < MIN_SLOW_SONGS:
        q = bs[min(len(bs) - 1, max(0, int(len(bs) * SLOW_SHARE) - 1))]
        slow = float(min(FAST_BPM - 10, np.floor(q) + 1))
    return {"slow": (0.0, slow), "mid": (slow, FAST_BPM), "fast": (FAST_BPM, 999.0), "relative": slow != SLOW_BPM}


def _tempo_class(b, bands=None):
    bands = bands or tempo_bands([])
    return None if b is None else "slow" if b < bands["slow"][1] else "mid" if b < FAST_BPM else "fast"


def _bands_of(p):
    """その曲のプロフィールに入っている「ゆったり」の上限から、テンポの範囲を作る（無ければ 100）"""
    slow = p.get("slow_hi", SLOW_BPM)
    return {"slow": (0.0, slow), "mid": (slow, FAST_BPM), "fast": (FAST_BPM, 999.0), "relative": slow != SLOW_BPM}


def questions(profiles=None):
    """画面に出す質問。テンポの説明は、そのときの範囲（tempo_bands）に合わせる"""
    if profiles is None:
        _, profiles = library()
    bands = _bands_of(profiles[0]) if profiles else tempo_bands([])
    slow = bands["slow"][1]
    out = json.loads(json.dumps(QUESTIONS))
    for c in out[0]["choices"]:
        if c["id"] == "slow":
            c["desc"] = f"〜{slow:.0f} BPM" + ("（この中では遅め）" if bands["relative"] else "")
        elif c["id"] == "mid":
            c["desc"] = f"{slow:.0f}〜{FAST_BPM:.0f} BPM"
    return out


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
    songs, profs = {}, []
    for r in rows:
        try:
            s = load_song(con, r["id"], structure)
        except (KeyError, RuntimeError):
            continue
        a = json.loads(analysis_path(r["id"]).read_text(encoding="utf-8"))
        songs[r["id"]] = s
        try:
            feats = feel_features(r["id"], r["file_path"], a)
        except Exception:      # 音源が読めないときは解析の BPM のまま
            feats = None
        b, doubled = song_bpm(s.bpm, a, feats)
        profs.append({"id": r["id"], "title": r["title"], "bpm": b, "tempo": _tempo_class(b), "bpm_doubled": doubled,
                      "mode": s.mode or ("minor" if (s.camelot or "").endswith("A") else "major"),
                      "mode_sure": not a["key"].get("relative_ambiguous", False),
                      "loudness": a.get("loudness_db"), "start": _start_kind(s), "camelot": s.camelot,
                      "structure": s.structure_source})
    bands = tempo_bands([p["bpm"] for p in profs])
    for p in profs:
        p["tempo"] = _tempo_class(p["bpm"], bands)
        p["slow_hi"] = bands["slow"][1]
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
        lo, hi = _bands_of(p)[choice]
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
    t = [(f"体感 {p['bpm']:.0f} BPM" if p.get("bpm_doubled") else f"{p['bpm']:.0f} BPM") if p["bpm"] else None,
         "明るい" if p["mode"] == "major" else "切ない・かっこいい",
         {"soft": "やさしめ", "mid": "迫力ふつう", "loud": "ガツンと"}[p["power"]],
         "いきなりサビ" if p["start"] == "chorus" else "じわじわ"]
    return [x for x in t if x]


def choice_counts(profiles=None, answers=None):
    """選択肢ごとに、はっきり合う曲が何曲あるか（画面に出す）。
    answers を渡すと「ほかの質問の答えにも合ったまま」の曲数（その質問の答えは選び直す前提で無視）。
    "any" は ほかの答えだけで残る曲数"""
    if profiles is None:
        _, profiles = library()
    answers = answers or {}
    out = {}
    for q in QUESTIONS:
        others = {k: v for k, v in answers.items() if k != q["id"]}
        rest = [p for p in profiles if all(match(k, v, p) >= 0.99 for k, v in others.items())]
        out[q["id"]] = {c["id"]: sum(1 for p in rest if match(q["id"], c["id"], p) >= 0.99) for c in q["choices"]}
        out[q["id"]][ANY] = len(rest)
    return out


def n_exact(answers, profiles=None):
    """答えたすべての質問にはっきり合う曲の数"""
    if profiles is None:
        _, profiles = library()
    asked = [q["id"] for q in QUESTIONS if answers.get(q["id"]) not in (None, "", ANY)]
    return sum(1 for p in profiles if all(match(q, answers[q], p) >= 0.99 for q in asked))


def typicality(q, choice, p):
    """答えに「どれだけはっきり」合うか（0〜1）。match が同点の曲どうしの並べ替えに使う。
    例: 「速く」なら速い曲ほど、「ガツンと」なら音が大きい曲ほど上"""
    if q == "tempo" and p.get("bpm"):
        b = p["bpm"]
        lo, hi = _bands_of(p)[choice]
        if choice == "fast":
            return float(np.clip((b - lo) / 50, 0, 1))
        if choice == "slow":
            return float(np.clip((hi - b) / 25, 0, 1))
        return float(1 - min(1, abs(b - (lo + hi) / 2) / ((hi - lo) / 2 + 1e-9)))
    if q == "power" and p.get("power_rank") is not None:
        r = p["power_rank"]
        return {"loud": r, "soft": 1 - r}.get(choice, 1 - abs(r - 0.5) * 2)
    if q == "mood":
        return 1.0 if p.get("mode_sure") else 0.5
    return 0.5


def pick_roots(answers, profiles=None, seed=None):
    """答え {質問id: 選択肢id} → 最初の曲の候補 3〜5曲（合う順）。
    同点は「答えにどれだけはっきり合うか」→ seed で決まる順（画面を開くたびに変わり、同じ画面の中では同じ）。
    seed が無ければ曲名順"""
    if profiles is None:
        _, profiles = library()
    asked = [q["id"] for q in QUESTIONS if answers.get(q["id"]) not in (None, "", ANY)]
    scored = []
    for p in profiles:
        parts = {q: match(q, answers.get(q), p) for q in asked}
        total = sum(parts.values())
        hit = [q for q, v in parts.items() if v >= 0.99]
        typ = sum(typicality(q, answers[q], p) for q in asked)
        tie = random.Random(f"{seed}|{p['id']}").random() if seed else p["title"]
        scored.append((total, p, hit, typ, tie))
    scored.sort(key=lambda x: (-round(x[0], 6), -round(x[3], 6), x[4]))
    if not scored:
        return []
    best = scored[0][0]
    n = sum(1 for s in scored if s[0] >= best - ROOT_MARGIN)
    n = max(MIN_ROOTS, min(MAX_ROOTS, n))
    out = []
    for total, p, hit, _, _ in scored[:n]:
        out.append({"id": p["id"], "title": p["title"], "score": round(total / max(1, len(asked)), 3),
                    "hits": hit, "n_asked": len(asked), "tags": tags(p)})
    return out


# ---------- 2曲目・3曲目のおすすめ ----------
def _bpm_why(a_bpm, b_bpm):
    """「BPM 157→146」。つなぎでは半分・倍を同じテンポとして比べるので、そうしたときは
    「BPM 157→170（85×2）」のように比べた値を出す（85 だけ見せると、遅い曲が上位に来たように見える）"""
    a, b = norm_bpm(a_bpm), norm_bpm(b_bpm)
    if not a or not b:
        return "BPM ?"
    k = min((0.5, 1.0, 2.0), key=lambda m: abs(b * m / a - 1))
    if k == 1.0:
        return f"BPM {a:.0f}→{b:.0f}"
    return f"BPM {a:.0f}→{b * k:.0f}（{b:.0f}{'×2' if k == 2 else '÷2'}）"



def route_successors(title, con=None):
    """人が聞いて決めたルート（シートの曲リスト＋画面で足したルート）で、title の次に来る曲 → つなぎメモ（無ければ ""）"""
    nxt = {}
    try:
        from .sheet import load_cached
        _, lst = load_cached()
    except FileNotFoundError:
        lst = {"routes": []}
    for r in lst.get("routes", []):
        if r.get("root") == title and r.get("middle"):
            nxt.setdefault(r["middle"], "")
        if r.get("middle") == title and r.get("leaf"):
            nxt[r["leaf"]] = r.get("memo") or nxt.get(r["leaf"], "")
    try:
        from .db import list_routes
        for r in list_routes(con or connect()):
            chain = [x for x in (r["root"], r["middle"], r["leaf"]) if x]
            for x, y in zip(chain, chain[1:]):
                if x == title:
                    nxt.setdefault(y, "")
    except Exception:
        pass
    return nxt


PREF_WEIGHT = 0.8    # アンケートの答えに合わない曲の減点（全部はずれで -0.8。つなぎの点数は 0.3〜1.1 くらい）
PREF_Q_WEIGHT = {"tempo": 2.0}   # テンポの希望は一番はっきり聞こえるので2倍で数える


def preference(answers, p):
    """アンケートの答えにどれだけ合うか（0〜1）。答えていない・「どれでもいい」の質問は数えない。答えが無ければ None"""
    asked = [q["id"] for q in QUESTIONS if (answers or {}).get(q["id"]) not in (None, "", ANY)]
    if not asked or p is None:
        return None
    wsum = sum(PREF_Q_WEIGHT.get(q, 1.0) for q in asked)
    return sum(PREF_Q_WEIGHT.get(q, 1.0) * match(q, answers[q], p) for q in asked) / wsum


def recommend_next(after, exclude=(), limit=NEXT_LIMIT, weights=None, answers=None):
    """直前の曲 after(id) から、つながりやすい順に全曲を並べる。
    点数 = その2曲の間で一番良いつなぎ目の点数（テンポ・キー・勢い・区切りの確かさ。学習した重みを使う）
    answers（アンケートの答え）があれば、答えに合わない曲を少し下げる（2・3曲目も最初の希望に沿うように）"""
    songs, profs = library()
    a = songs.get(int(after))
    if a is None:
        raise KeyError(f"曲が見つからないか未解析です: {after}")
    skip = {int(x) for x in exclude} | {a.track_id}
    byid = {p["id"]: p for p in profs}
    routes = route_successors(a.title)       # 人が決めたルートの次の曲は、いつも一番上に出す
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
        why = [_bpm_why(a.bpm, b.bpm) + ("（近い）" if f["tempo"] >= 0.99 else ""),
               f"Key {a.camelot}→{b.camelot}" + ("（相性◎）" if f["key"] >= 0.99 else "（相性○）" if f["key"] >= 0.8 else "")]
        pref = preference(answers, byid.get(bid))
        if pref is not None:
            why.append("希望に合う" if pref >= 0.99 else "希望に少し合う" if pref >= 0.5 else "希望とちがう")
        rank = best.score - (PREF_WEIGHT * (1 - pref) if pref is not None else 0.0)
        out.append({"id": bid, "title": b.title, "score": round(best.score, 4), "fit": round(fit_percent(best.score, weights), 3),
                    "pref": None if pref is None else round(pref, 3), "rank": round(rank, 4),
                    "kind": best.kind, "out": best.out, "in": best.inn, "why": why,
                    "tags": tags(byid[bid]) if bid in byid else [],
                    "route": b.title in routes, "memo": routes.get(b.title) or None})
    out.sort(key=lambda x: (not x["route"], -x["rank"], x["title"]))
    return {"items": out[:limit], "total": len(out), "elapsed": round(time.time() - t0, 3)}
