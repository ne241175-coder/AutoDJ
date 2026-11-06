"""区切り（セクション境界）の確率モデル。

小節ごとに「ここで区間が切り替わる確率」を出し、確率の高い順に境界を採る。
- 確率がしきい値（既定 0.75）未満の小節は採らない
- 境界どうしは最低 min_gap 小節あける
- 区間数は上限（MAX_SECTIONS）まで
モデルはロジスティック回帰（特徴量の重み付き和 → 0〜1 の確率）。正解データから `learn` で学習する。
v0.3: 小節ごとの特徴量に extra_features の22個を足し、さらに前後2小節の特徴量も並べて判断する。
確率は「その小節がちょうど区切り」の確率なので値は小さめ。採った境界には、学習時の答え合わせから
求めた「信頼度」（その境界が正解の±3秒以内に入っている割合）を付ける。
"""
import json
from pathlib import Path

import numpy as np

from ..config import DATA_DIR
from .extra_features import EXTRA_NAMES

MODEL_PATH = DATA_DIR / "model" / "boundary.json"
CONTEXT = 2   # 前後何小節の特徴量も並べるか
BASE_NAMES = [
    "novelty_4bar",      # 音色・和声の変化（前後4小節の比較）
    "novelty_8bar",      # 同（前後8小節）
    "energy_step",       # 音量の段差
    "low_perc_step",     # 低域・打楽器量の段差（キックの抜き差し）
    "chroma_change",     # 和声の変化（直前の小節との差）
    "repetition_step",   # 繰り返し度の段差（サビに入る/出るところで大きい）
    "on_grid4", "on_grid8", "on_grid16",   # 4/8/16小節の区切りに乗っているか
    "position",          # 曲中の位置（0〜1）
]
FEATURE_NAMES = BASE_NAMES + EXTRA_NAMES


def with_context(F, k=CONTEXT):
    """各小節の特徴量に、前後 k 小節の特徴量を並べる（端は0で埋める）"""
    F = np.asarray(F, float)
    cols = [F]
    for d in range(1, k + 1):
        cols.append(np.vstack([np.zeros((d, F.shape[1])), F[:-d]]))
        cols.append(np.vstack([F[d:], np.zeros((d, F.shape[1]))]))
    return np.hstack(cols)


def bar_features(foote4, foote8, energy_step, lowperc_step, chroma, rep, offsets, n):
    """各小節の特徴量 (n x len(FEATURE_NAMES))"""
    ch_change = np.zeros(n)
    for i in range(1, n):
        ch_change[i] = 1 - float(chroma[:, i] @ chroma[:, i - 1])
    rep_step = np.zeros(n)
    for i in range(1, n):
        a = rep[max(0, i - 4):i].mean()
        b = rep[i:min(n, i + 4)].mean()
        rep_step[i] = abs(b - a)
    o4, o8, o16 = offsets
    idx = np.arange(n)

    def norm(x):
        hi = np.percentile(x, 95) if len(x) else 1
        return np.clip(x / (hi + 1e-9), 0, 1.5)

    F = np.stack([
        norm(foote4), norm(foote8), norm(energy_step), norm(lowperc_step), norm(ch_change), norm(rep_step),
        ((idx - o4) % 4 == 0).astype(float), ((idx - o8) % 8 == 0).astype(float),
        ((idx - o16) % 16 == 0).astype(float), idx / max(1, n - 1),
    ], axis=1)
    F[0, :6] = 0.0  # 曲頭は境界候補にしない（0小節目は常に区間の始まり）
    return F


def load_model(path=MODEL_PATH):
    p = Path(path)
    if not p.exists():
        return None
    m = json.loads(p.read_text(encoding="utf-8"))
    if m.get("feature_names") != FEATURE_NAMES or not m.get("enabled", True):
        return None
    return m


def predict(model, F):
    X = with_context(F, model.get("context", 0))
    X = (X - np.array(model["mean"])) / np.array(model["std"])
    z = X @ np.array(model["coef"]) + model["intercept"]
    return 1 / (1 + np.exp(-z))


def confidence(model, p):
    """確率 → 信頼度（学習時の答え合わせで「採った境界が正解だった割合」に直したもの）"""
    cal = model.get("calibration")
    if not cal:
        return float(p)
    return float(np.interp(p, cal["x"], cal["y"]))


def pick_boundaries(prob, threshold=0.75, min_gap=4, max_sections=14):
    """確率の高い順に境界を採る。しきい値未満は採らない。戻り値: [0, ..., n]（小節番号）"""
    n = len(prob)
    chosen = []
    for i in np.argsort(prob)[::-1]:
        i = int(i)
        if prob[i] < threshold or len(chosen) >= max_sections - 1:
            break
        if i < min_gap or n - i < min_gap:
            continue
        if any(abs(i - c) < min_gap for c in chosen):
            continue
        chosen.append(i)
    return [0] + sorted(chosen) + [n]
