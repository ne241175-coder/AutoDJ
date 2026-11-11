"""つなぎ目の点数の重みを、人の評価から学習する。

つなぎ候補の点数は「手がかり（特徴）× 重み」の合計（mixplan.candidate_links）。
最初の重みは手で決めた値（DEFAULT_WEIGHTS）。ミックスを聞いた人の評価を data/feedback.jsonl に貯め、
「どちらのつなぎ目の方が良いか」の比較（ペア）にして重みを学び直す。

- 学習: P(良い方が勝つ) = sigmoid(SCALE × 重み・(良い方の特徴 − 悪い方の特徴)) を最大にする（Bradley–Terry）。
  データが少ないうちは手で決めた重みから離れすぎないよう、元の重みへの引き戻し（L2）を入れる。
  → 評価が少なければほぼ元のまま、増えるほど評価に合わせて動く。
- 採点: 評価ごと（1回のミックス分）に外して学習 → 外した評価のペアを当てられるか（一致率）。
  元の重みでの一致率と並べて、学習で良くなったかを確かめる。
"""
import json
import time
from pathlib import Path

import numpy as np

from .config import DATA_DIR

FEATURES = ["tempo", "key", "energy", "conf",
            "out_chorus", "out_outro", "in_chorus", "in_start", "in_verse", "later_entry"]
FEATURE_JA = {"tempo": "テンポの近さ", "key": "キーの相性", "energy": "音の勢いのつながり",
              "conf": "区切りの確かさ", "out_chorus": "サビ終わりで出る", "out_outro": "アウトロで出る",
              "in_chorus": "サビ頭から入る", "in_start": "曲の頭から入る", "in_verse": "Aメロ頭から入る",
              "later_entry": "2回目以降の区間から入る"}
# 手で決めた重み（mixplan の元の値と同じ）
DEFAULT_WEIGHTS = {"tempo": 0.30, "key": 0.30, "energy": 0.25, "conf": 0.15,
                   "out_chorus": 0.08, "out_outro": 0.02, "in_chorus": 0.06, "in_start": 0.06,
                   "in_verse": 0.03, "later_entry": -0.05}

FEEDBACK_PATH = DATA_DIR / "feedback.jsonl"
MODEL_PATH = DATA_DIR / "link_model.json"

SCALE = 20.0     # 点数の差（0.1 くらい）を確率に直すときの倍率
PRIOR = 5.0      # 元の重みへの引き戻しの強さ（評価が少ないうちはほぼ元のまま）
# 評価の種類ごとのペアの重み（はっきりした選択ほど重く）
W_PICK = 1.0     # 候補を選び直した／「こっちが良い」と選んだ
W_GOOD = 0.5     # 「良い」→ 選ばれなかった候補より良い
W_BAD = 0.3      # 「微妙」→ 次点の候補の方がましだったかもしれない


def vec(features):
    return np.array([float((features or {}).get(k, 0.0)) for k in FEATURES])


def score(features, weights=None):
    w = weights or current_weights()
    return float(sum(w.get(k, 0.0) * float((features or {}).get(k, 0.0)) for k in FEATURES))


# ---------------------------------------------------------------- 重みの読み込み
_cache = {"mtime": None, "model": None}


def load_model(path=None):
    p = Path(path or MODEL_PATH)
    if not p.exists():
        return None
    m = p.stat().st_mtime
    if _cache["mtime"] != m or _cache.get("path") != str(p):
        try:
            _cache.update(mtime=m, path=str(p), model=json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None
    return _cache["model"]


def current_weights():
    m = load_model()
    if m and m.get("use", True) and m.get("weights"):
        return {k: float(m["weights"].get(k, DEFAULT_WEIGHTS[k])) for k in FEATURES}
    return dict(DEFAULT_WEIGHTS)


def model_info():
    m = load_model()
    if not (m and m.get("use", True) and m.get("weights")):
        return {"source": "default", "n_feedback": count_feedback()}
    return {"source": "learned", "n_pairs": m.get("n_pairs"), "n_feedback": m.get("n_feedback"),
            "trained_at": m.get("trained_at"), "accuracy": m.get("accuracy")}


# ---------------------------------------------------------------- 評価の保存
def record(entry, path=None):
    """1回分の評価（1つのミックス）を追記する"""
    p = Path(path or FEEDBACK_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def load_feedback(path=None):
    p = Path(path or FEEDBACK_PATH)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


def count_feedback(path=None):
    return len(load_feedback(path))


def pairs_from_feedback(records):
    """評価 → (良い方の特徴, 悪い方の特徴, 重み, まとまりの番号) のリスト

    1件の評価 = {"links": [{"chosen": {...features}, "rating": "good"|"bad"|None,
                            "better": {...features}|None, "others": [{...features}, ...]}, ...]}
    - better があれば: better > chosen（はっきりした選択）
    - good: chosen > others（選ばれなかった候補より良かった）
    - bad で better なし: others の上位2つ > chosen（弱め）
    """
    pairs = []
    for g, rec in enumerate(records):
        for l in rec.get("links", []):
            ch = l.get("chosen")
            if not ch:
                continue
            others = [o for o in (l.get("others") or []) if o]
            if l.get("better"):
                pairs.append((l["better"], ch, W_PICK, g))
            if l.get("rating") == "good":
                pairs += [(ch, o, W_GOOD, g) for o in others]
            elif l.get("rating") == "bad" and not l.get("better"):
                pairs += [(o, ch, W_BAD, g) for o in others[:2]]
    return pairs


# ---------------------------------------------------------------- 学習
def fit(pairs, prior=None, lam=PRIOR, steps=400, lr=0.05):
    """ペアから重みを学ぶ。prior（元の重み）から離れるほど罰を与える"""
    w0 = vec(prior or DEFAULT_WEIGHTS)
    if not pairs:
        return dict(zip(FEATURES, w0.tolist()))
    D = np.array([vec(a) - vec(b) for a, b, _, _ in pairs])
    c = np.array([p[2] for p in pairs], dtype=float)
    w = w0.copy()
    for _ in range(steps):
        z = SCALE * D @ w
        p = 1.0 / (1.0 + np.exp(np.clip(z, -30, 30)))      # = 1 - sigmoid(z)
        grad = -SCALE * (D * (c * p)[:, None]).sum(0) / c.sum() + 2 * lam * (w - w0) / max(1.0, c.sum())
        w -= lr * grad
    return {k: round(float(v), 4) for k, v in zip(FEATURES, w)}


def agreement(pairs, weights):
    """重み付きのペア一致率（良い方の点数が高いか）"""
    if not pairs:
        return None
    w = vec(weights)
    ok = sum(c for a, b, c, _ in pairs if (vec(a) - vec(b)) @ w > 0)
    tie = sum(c * 0.5 for a, b, c, _ in pairs if (vec(a) - vec(b)) @ w == 0)
    return (ok + tie) / sum(p[2] for p in pairs)


def cross_validate(pairs):
    """評価1件（ミックス1回）ずつ外して学習 → 外した分を当てられたか。元の重みと比べる"""
    groups = sorted({p[3] for p in pairs})
    if len(groups) < 2:
        return None
    hit_l = hit_d = tot = 0.0
    for g in groups:
        train = [p for p in pairs if p[3] != g]
        test = [p for p in pairs if p[3] == g]
        w = fit(train)
        tw = sum(p[2] for p in test)
        hit_l += agreement(test, w) * tw
        hit_d += agreement(test, DEFAULT_WEIGHTS) * tw
        tot += tw
    return {"learned": round(hit_l / tot, 3), "default": round(hit_d / tot, 3), "groups": len(groups)}


def learn(records=None, min_pairs=3, path=None, extra_pairs=None):
    """評価を読み込んで学習し、data/link_model.json に保存する。結果の要約を返す"""
    records = load_feedback() if records is None else records
    pairs = pairs_from_feedback(records) + list(extra_pairs or [])
    if len(pairs) < min_pairs:
        return {"saved": False, "n_pairs": len(pairs), "n_feedback": len(records),
                "message": f"評価がまだ少ないので学習しません（ペア {len(pairs)} / {min_pairs}）"}
    w = fit(pairs)
    cv = cross_validate(pairs)
    model = {"weights": w, "default": DEFAULT_WEIGHTS, "n_pairs": len(pairs), "n_feedback": len(records),
             "trained_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "accuracy": cv,
             "train_agreement": {"learned": round(agreement(pairs, w), 3),
                                 "default": round(agreement(pairs, DEFAULT_WEIGHTS), 3)},
             "use": True}
    p = Path(path or MODEL_PATH)
    p.write_text(json.dumps(model, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"saved": True, **model}


def describe_weights(w):
    lines = []
    for k in FEATURES:
        d = w[k] - DEFAULT_WEIGHTS[k]
        mark = "" if abs(d) < 0.005 else (" ↑" if d > 0 else " ↓")
        lines.append(f"  {FEATURE_JA[k]:<14} {DEFAULT_WEIGHTS[k]:+.3f} → {w[k]:+.3f}{mark}")
    return "\n".join(lines)


# ---------------------------------------------------------------- シートのつなぎメモもお手本にする
W_MEMO = 0.5     # シートのつなぎメモ（人が決めた位置）> 自動の上位候補


def memo_pairs(con=None, n_others=5, tol=3.0):
    """シートの「つなぎメモ」を、評価と同じ形のペアにする（人の位置 > 自動の上位候補）。
    構造は自動検知のもの（発表のときと同じ条件）で候補を作る。評価が少ないうちの足し算用"""
    from .align import aligned_sections
    from .db import connect
    from .library import load_analysis
    from .mixplan import candidate_links, features_at, load_song
    from .sheet import load_cached
    con = con or connect()
    structure, lst = load_cached()
    w0 = dict(DEFAULT_WEIGHTS)
    pairs, seen, songs = [], set(), {}

    def song(title):
        if title not in songs:
            try:
                s = load_song(con, title, {})
                sh = structure.get(title)
                a = load_analysis(s.track_id) if sh else None
                songs[title] = (s, aligned_sections(sh, a)[1] if (sh and a) else 0.0)
            except (KeyError, RuntimeError):
                songs[title] = None
        return songs[title]

    for r in lst.get("routes", []):
        for ka, kb, link in ((r["root"], r["middle"], r.get("link_root_mid")),
                             (r["middle"], r["leaf"], r.get("link_mid_leaf"))):
            if not link or (ka, kb) in seen:
                continue
            seen.add((ka, kb))
            sa, sb = song(ka), song(kb)
            if not sa or not sb:
                continue
            (a, oa), (b, ob) = sa, sb
            ro, ri = link["out"] + oa, link["in"] + ob
            human = features_at(a, b, ro, ri)
            cands = sorted(candidate_links(a, b, weights=w0), key=lambda x: -x.score)
            others = [c for c in cands if abs(c.out - ro) > tol or abs(c.inn - ri) > tol][:n_others]
            g = 100000 + len(seen)
            pairs += [(human, c.features, W_MEMO, g) for c in others]
    return pairs
