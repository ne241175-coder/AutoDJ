"""正解データから「区切り確率」モデルを学習する（教師あり学習）。

正解の出どころ
- スプレッドシートの「構成入力」（sheet-sync で取り込んだもの）
- truth.json（evaluate と同じ形式: [{"track_id": 1, "sections": [開始秒, ...]}]）

やること
1. 解析済みの各曲について、小節ごとの特徴量（analysis の bar_features）と正解ラベル
   （その小節が正解の区間頭に一番近い小節なら 1）を作る
2. 1曲ずつ外して学習→外した曲で採点（leave-one-song-out）し、今のルールより良いかを数字で出す
3. 全曲で学習したモデルを data/model/boundary.json に保存（ルールより悪ければ enabled=false で保存）
正解を足すたびに `python -m backend.cli learn` を実行し直せば、そのぶん賢くなる。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .analysis.boundary_model import FEATURE_NAMES, MODEL_PATH, pick_boundaries
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
    if len(F) < 8:
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


def _fit(X, y):
    from sklearn.linear_model import LogisticRegression
    mean, std = X.mean(0), X.std(0) + 1e-9
    clf = LogisticRegression(class_weight="balanced", C=1.0, max_iter=2000)
    clf.fit((X - mean) / std, y)
    return {"coef": clf.coef_[0].tolist(), "intercept": float(clf.intercept_[0]),
            "mean": mean.tolist(), "std": std.tolist()}


def _prob(m, X):
    z = ((X - np.array(m["mean"])) / np.array(m["std"])) @ np.array(m["coef"]) + m["intercept"]
    return 1 / (1 + np.exp(-z))


def _f(bars, idx, truth):
    est = [float(bars[i]) for i in idx[1:-1] if i < len(bars)]
    return boundary_prf(est, truth, tol=TOL)[2]


def cross_validate(data, threshold=0.75):
    """1曲ずつ外して学習→その曲で採点。戻り値: dict(model_f, rule_f, per_song, best_threshold)"""
    per, probs = [], []
    for k, ex in enumerate(data):
        train = [d for j, d in enumerate(data) if j != k]
        m = _fit(np.vstack([d["X"] for d in train]), np.concatenate([d["y"] for d in train]))
        p = _prob(m, ex["X"])
        probs.append(p)
        per.append({"track_id": ex["track_id"],
                    "model_f": _f(ex["bars"], pick_boundaries(p, threshold), ex["truth"]),
                    "rule_f": _f(ex["bars"], ex["rule_bounds"], ex["truth"])})
    # しきい値を変えたときの平均F（参考）
    grid = np.round(np.arange(0.5, 0.96, 0.05), 2)
    by_thr = {float(t): float(np.mean([_f(ex["bars"], pick_boundaries(p, t), ex["truth"])
                                       for ex, p in zip(data, probs)])) for t in grid}
    return {"model_f": float(np.mean([r["model_f"] for r in per])),
            "rule_f": float(np.mean([r["rule_f"] for r in per])),
            "per_song": per, "f_by_threshold": by_thr,
            "best_threshold": max(by_thr, key=by_thr.get)}


def train_and_save(data, threshold=0.75, path=MODEL_PATH, cv=None):
    m = _fit(np.vstack([d["X"] for d in data]), np.concatenate([d["y"] for d in data]))
    cv = cv or cross_validate(data, threshold)
    m.update({
        "feature_names": FEATURE_NAMES, "threshold": threshold,
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_songs": len(data), "n_boundaries": int(sum(d["y"].sum() for d in data)),
        "cv_model_f": round(cv["model_f"], 3), "cv_rule_f": round(cv["rule_f"], 3),
        # ルールより良いときだけ解析で使う
        "enabled": cv["model_f"] >= cv["rule_f"],
    })
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return m, cv


def learn(truth_json=None, threshold=0.75, con=None, log=print):
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
        raise RuntimeError(f"学習に使える曲が {len(data)} 曲しかありません（正解があり、v0.2 以降で解析済みの曲が3曲以上必要）。"
                           "analyze --set sheet -f を実行してください")
    cv = cross_validate(data, threshold)
    m, _ = train_and_save(data, threshold, cv=cv)
    titles = {r["id"]: r["title"] for r in con.execute("SELECT id, title FROM tracks")}
    for r in cv["per_song"]:
        mark = "↑" if r["model_f"] > r["rule_f"] + 0.01 else ("↓" if r["model_f"] < r["rule_f"] - 0.01 else "＝")
        log(f"  {titles.get(r['track_id'], r['track_id'])}: ルール F={r['rule_f']:.2f} → 学習 F={r['model_f']:.2f} {mark}")
    log(f"--- {len(data)}曲・境界{m['n_boundaries']}個で学習（1曲ずつ外して採点、許容±{TOL:.0f}秒）")
    log(f"境界F値: ルール {cv['rule_f']:.2f} → 学習モデル {cv['model_f']:.2f}（しきい値 {threshold:.2f}）")
    log(f"参考: しきい値を変えた場合の最良 {cv['best_threshold']:.2f}（F={cv['f_by_threshold'][cv['best_threshold']]:.2f}）")
    if m["enabled"]:
        log(f"→ 学習モデルを使います（{MODEL_PATH}）。analyze --set sheet -f で全曲に反映してください")
    else:
        log("→ まだルールの方が良いので、学習モデルは使いません（正解を増やして再実行してください）")
    return m, cv
