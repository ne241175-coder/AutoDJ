"""正解データから「区切り確率」モデルを学習する（教師あり学習）。

正解の出どころ
- スプレッドシートの「構成入力」（sheet-sync で取り込んだもの）
- truth.json（evaluate と同じ形式: [{"track_id": 1, "sections": [開始秒, ...]}]）

やること
1. 解析済みの各曲について、小節ごとの特徴量（analysis の bar_features）と正解ラベル
   （その小節が正解の区間頭に一番近い小節なら 1）を作る
2. 1曲ずつ外して学習→外した曲で採点（leave-one-song-out）し、今のルールより良いかを数字で出す
   ・しきい値は、この答え合わせで一番 F 値が良くなるものを自動で選ぶ
   ・採った境界が実際に正解だった割合から「信頼度」の換算表を作る（確率 0.3 → 信頼度 0.5 など）
3. 全曲で学習したモデルを data/model/boundary.json に保存（ルールより悪ければ enabled=false で保存）
正解を足すたびに `python -m backend.cli learn` を実行し直せば、そのぶん賢くなる。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .analysis.boundary_model import CONTEXT, FEATURE_NAMES, MODEL_PATH, pick_boundaries, with_context
from .db import connect
from .evaluate import boundary_prf
from .library import load_analysis

TOL = 3.0  # 境界の一致とみなす許容（秒）


def collect_truth(con=None, truth_json=None):
    """{track_id: [正解の区間頭（秒）...]}  0秒付近の曲頭は除く"""
    con = con or connect()
    out = {}
    try:
        from .sheet import load_cached
        structure, _ = load_cached()
    except FileNotFoundError:
        structure = {}
    from .align import aligned_sections
    for name, s in structure.items():
        row = con.execute("SELECT id FROM tracks WHERE title=?", (name,)).fetchone()
        a = load_analysis(row["id"]) if row else None
        if a is not None:
            secs, _ = aligned_sections(s, a)   # 計測した動画と音源のズレを補正
            out[row["id"]] = [x["start"] for x in secs]
    if truth_json and Path(truth_json).exists():
        for t in json.loads(Path(truth_json).read_text(encoding="utf-8")):
            if t.get("sections"):
                out[int(t["track_id"])] = list(t["sections"])
    return {k: sorted(t for t in v if t > 1.0) for k, v in out.items()}


def song_example(analysis, truth_times):
    st = analysis["structure"]
    F = np.asarray(st.get("bar_features") or [], float)
    bars = np.asarray(analysis["bars"][:len(F)], float)
    if len(F) < 8 or F.shape[1] != len(FEATURE_NAMES):   # 古い解析（特徴量が足りない）は使わない
        return None
    y = np.zeros(len(F))
    for t in truth_times:
        i = int(np.argmin(np.abs(bars - t)))
        if abs(bars[i] - t) <= TOL:
            y[i] = 1
    return {"X": F, "y": y, "bars": bars, "truth": truth_times,
            "rule_bounds": st.get("rule_bounds") or [s["start_bar"] for s in st["sections"]] + [len(F)]}


def build_dataset(pairs):
    """pairs: [(track_id, analysis, truth_times)]"""
    data = []
    for tid, a, truth in pairs:
        ex = song_example(a, truth)
        if ex is not None and ex["y"].sum() > 0:
            ex["track_id"] = tid
            data.append(ex)
    return data


def _train_rows(d):
    """前後の小節も並べた特徴量と正解。正解の隣の小節は「区切りではない」と教えない
    （シートの秒数は ±1 小節ずれることが多いので、隣を不正解にすると学習が混乱する）"""
    X, y = with_context(d["X"], CONTEXT), d["y"]
    keep = np.ones(len(y), bool)
    for i in np.where(y == 1)[0]:
        for dd in (-1, 1):
            if 0 <= i + dd < len(y) and y[i + dd] == 0:
                keep[i + dd] = False
    return X[keep], y[keep]


def _fit(data, C=0.3):
    from sklearn.linear_model import LogisticRegression
    rows = [_train_rows(d) for d in data]
    X, y = np.vstack([r[0] for r in rows]), np.concatenate([r[1] for r in rows])
    mean, std = X.mean(0), X.std(0) + 1e-9
    clf = LogisticRegression(C=C, max_iter=4000)
    clf.fit((X - mean) / std, y)
    return {"coef": clf.coef_[0].tolist(), "intercept": float(clf.intercept_[0]),
            "mean": mean.tolist(), "std": std.tolist(), "context": CONTEXT}


def _prob(m, X):
    Z = (with_context(X, m["context"]) - np.array(m["mean"])) / np.array(m["std"])
    return 1 / (1 + np.exp(-(Z @ np.array(m["coef"]) + m["intercept"])))


def _f(bars, idx, truth):
    est = [float(bars[i]) for i in idx[1:-1] if i < len(bars)]
    return boundary_prf(est, truth, tol=TOL)[2]


THRESHOLDS = (0.05, 0.08, 0.1, 0.12, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.75)


def cross_validate(data, threshold=None):
    """1曲ずつ外して学習→その曲で採点。threshold=None なら一番良いしきい値を選ぶ。
    戻り値: dict(model_f, rule_f, per_song, f_by_threshold, best_threshold, threshold, calibration)"""
    probs = []
    for k, ex in enumerate(data):
        m = _fit([d for j, d in enumerate(data) if j != k])
        probs.append(_prob(m, ex["X"]))
    by_thr = {t: float(np.mean([_f(ex["bars"], pick_boundaries(p, t), ex["truth"]) for ex, p in zip(data, probs)]))
              for t in THRESHOLDS}
    best = max(by_thr, key=by_thr.get)
    thr = best if threshold is None else threshold
    per = [{"track_id": ex["track_id"],
            "model_f": _f(ex["bars"], pick_boundaries(p, thr), ex["truth"]),
            "rule_f": _f(ex["bars"], ex["rule_bounds"], ex["truth"])} for ex, p in zip(data, probs)]
    return {"model_f": float(np.mean([r["model_f"] for r in per])),
            "rule_f": float(np.mean([r["rule_f"] for r in per])),
            "per_song": per, "f_by_threshold": by_thr, "best_threshold": best, "threshold": thr,
            "calibration": _calibrate(data, probs, thr)}


def _calibrate(data, probs, thr):
    """採った境界の確率 → 実際に正解（±TOL秒）だった割合、の換算表（単調増加）"""
    from sklearn.isotonic import IsotonicRegression
    raw, hit = [], []
    for ex, p in zip(data, probs):
        truth = np.asarray(ex["truth"], float)
        for i in pick_boundaries(p, thr)[1:-1]:
            raw.append(float(p[i]))
            hit.append(float(len(truth) > 0 and np.min(np.abs(truth - ex["bars"][i])) <= TOL))
    if len(raw) < 10:
        return None
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0, y_max=1).fit(raw, hit)
    xs = np.linspace(0, 1, 41)
    return {"x": xs.round(3).tolist(), "y": np.round(iso.predict(xs), 3).tolist(),
            "precision": round(float(np.mean(hit)), 3)}


def train_and_save(data, threshold=None, path=MODEL_PATH, cv=None, min_confidence=0.0):
    m = _fit(data)
    cv = cv or cross_validate(data, threshold)
    m.update({
        "feature_names": FEATURE_NAMES, "threshold": cv["threshold"], "min_confidence": min_confidence,
        "calibration": cv["calibration"],
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_songs": len(data), "n_boundaries": int(sum(d["y"].sum() for d in data)),
        "cv_model_f": round(cv["model_f"], 3), "cv_rule_f": round(cv["rule_f"], 3),
        # ルールより良いときだけ解析で使う
        "enabled": cv["model_f"] >= cv["rule_f"],
    })
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return m, cv


def learn(truth_json=None, threshold=None, min_confidence=0.0, con=None, log=print):
    con = con or connect()
    truth = collect_truth(con, truth_json)
    pairs = []
    for tid, times in truth.items():
        a = load_analysis(tid)
        if a is None or not a["structure"].get("bar_features"):
            continue
        pairs.append((tid, a, times))
    data = build_dataset(pairs)
    if len(data) < 3:
        raise RuntimeError(f"学習に使える曲が {len(data)} 曲しかありません（正解があり、v0.3 以降で解析済みの曲が3曲以上必要）。"
                           "analyze --set sheet -f を実行してください")
    cv = cross_validate(data, threshold)
    m, _ = train_and_save(data, threshold, cv=cv, min_confidence=min_confidence)
    titles = {r["id"]: r["title"] for r in con.execute("SELECT id, title FROM tracks")}
    for r in cv["per_song"]:
        mark = "↑" if r["model_f"] > r["rule_f"] + 0.01 else ("↓" if r["model_f"] < r["rule_f"] - 0.01 else "＝")
        log(f"  {titles.get(r['track_id'], r['track_id'])}: ルール F={r['rule_f']:.2f} → 学習 F={r['model_f']:.2f} {mark}")
    log(f"--- {len(data)}曲・境界{m['n_boundaries']}個で学習（1曲ずつ外して採点、許容±{TOL:.0f}秒）")
    log(f"境界F値: ルール {cv['rule_f']:.2f} → 学習モデル {cv['model_f']:.2f}（しきい値 {cv['threshold']:.2f}）")
    log("しきい値ごとの F: " + "  ".join(f"{t:.2f}→{v:.2f}" for t, v in cv["f_by_threshold"].items()))
    cal = cv["calibration"]
    if cal:
        def c(p):
            return float(np.interp(p, cal["x"], cal["y"]))
        log(f"採った境界のうち正解だった割合 {cal['precision']:.0%}。"
            f"信頼度の目安: 確率0.2→{c(0.2):.0%} / 0.4→{c(0.4):.0%} / 0.6→{c(0.6):.0%}")
    if min_confidence > 0:
        log(f"信頼度 {min_confidence:.0%} 未満の境界は採りません（--min-confidence）")
    if m["enabled"]:
        log(f"→ 学習モデルを使います（{MODEL_PATH}）。analyze --set sheet -f で全曲に反映してください")
    else:
        log("→ まだルールの方が良いので、学習モデルは使いません（正解を増やして再実行してください）")
    return m, cv
