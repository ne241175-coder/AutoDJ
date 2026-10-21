"""キー推定。

既存実装で精度が悪かった点への対策:
- チューニングずれ（A=440Hzからのずれ）を推定してCQTを補正
- 和声成分（HPSS）のみを使い、ドラムのノイズを除外
- 帯域を C2〜C7 に限定し、低い方の音域（ベース・和音の根音）を重み付け
- 1種類のキープロファイルに頼らず Krumhansl-Kessler と Temperley を平均
- 曲全体だけでなく約12秒窓ごとの推定も行い、「票」と全体推定を合算
- 上位候補・確信度・転調タイムラインを出力（判断の材料を残す）
"""
import numpy as np
import librosa

MAJOR_NAMES = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
MINOR_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "G#", "A", "Bb", "B"]

PROFILES = {
    "krumhansl": (
        [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88],
        [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17],
    ),
    "temperley": (
        [5.0, 2.0, 3.5, 2.0, 4.5, 4.0, 2.0, 4.5, 2.0, 3.5, 1.5, 4.0],
        [5.0, 2.0, 3.5, 4.5, 2.0, 4.0, 2.0, 4.5, 3.5, 2.0, 1.5, 4.0],
    ),
}

CQT_FMIN_NOTE = "C2"
CQT_OCTAVES = 5
BPO = 36
OCTAVE_WEIGHTS = np.array([1.4, 1.2, 1.0, 0.8, 0.5])  # C2,C3,C4,C5,C6


def key_name(tonic, mode):
    return f"{MAJOR_NAMES[tonic]} major" if mode == "major" else f"{MINOR_NAMES[tonic]} minor"


def camelot(tonic, mode):
    if mode == "minor":
        tonic = (tonic + 3) % 12  # 相対長調と同じ番号
        letter = "A"
    else:
        letter = "B"
    num = (8 + (tonic * 7) % 12 - 1) % 12 + 1
    return f"{num}{letter}"


def camelot_distance(a, b):
    """Camelot間の距離: 0=同一, 1=±1 または相対調, 2=… (推薦で使う)"""
    na, la = int(a[:-1]), a[-1]
    nb, lb = int(b[:-1]), b[-1]
    d = min((na - nb) % 12, (nb - na) % 12)
    return d + (0 if la == lb else 1)


def harmonic_chroma(y_harm, sr, hop):
    """チューニング補正・オクターブ重み付きのクロマ (12 x frames)。CQT振幅も返す。"""
    tuning = float(librosa.estimate_tuning(y=y_harm, sr=sr, bins_per_octave=BPO))
    fmin = librosa.note_to_hz(CQT_FMIN_NOTE)
    C = np.abs(librosa.cqt(y_harm, sr=sr, hop_length=hop, fmin=fmin,
                           n_bins=BPO * CQT_OCTAVES, bins_per_octave=BPO, tuning=tuning))
    w = np.repeat(OCTAVE_WEIGHTS, BPO)[:, None]
    fold = librosa.filters.cq_to_chroma(n_input=C.shape[0], bins_per_octave=BPO,
                                        n_chroma=12, fmin=fmin)
    chroma = fold @ (np.log1p(10.0 * C / (C.max() + 1e-12)) * w)
    return chroma, tuning


def _profile_matrix():
    """(24, 12) 各キーのテンプレート（z正規化）。index = mode*12 + tonic"""
    mats = []
    for maj, mnr in PROFILES.values():
        rows = []
        for prof in (maj, mnr):
            p = np.asarray(prof, float)
            for t in range(12):
                r = np.roll(p, t)
                rows.append((r - r.mean()) / r.std())
        mats.append(np.array(rows))
    return mats


_PROFILES = _profile_matrix()


def key_scores(chroma_vec):
    """12次元クロマ → 24キーの相関スコア（プロファイル平均）。"""
    v = np.asarray(chroma_vec, float)
    v = (v - v.mean()) / (v.std() + 1e-12)
    return np.mean([m @ v / 12.0 for m in _PROFILES], axis=0)


def _idx_to_key(i):
    return i % 12, ("major" if i < 12 else "minor")


def estimate_key(chroma, sr, hop, window_sec=12.0, step_sec=4.0):
    # 静かなフレームは重みを下げる
    energy = chroma.sum(0)
    w = np.clip(energy / (np.percentile(energy, 90) + 1e-12), 0, 1) ** 0.5
    cn = chroma / (chroma.sum(0, keepdims=True) + 1e-12)
    global_vec = (cn * w).sum(1) / (w.sum() + 1e-12)
    g_scores = key_scores(global_vec)

    fps = sr / hop
    win, step = int(window_sec * fps), int(step_sec * fps)
    votes = np.zeros(24)
    timeline = []
    for s in range(0, max(1, cn.shape[1] - win + 1), step):
        ww = w[s:s + win]
        if ww.sum() < 0.2 * win:
            continue
        sc = key_scores((cn[:, s:s + win] * ww).sum(1) / ww.sum())
        i = int(np.argmax(sc))
        votes[i] += ww.mean()
        timeline.append((s / fps, (s + win) / fps, i, float(sc[i])))
    votes = votes / (votes.sum() + 1e-12)

    total = g_scores + 0.5 * votes
    order = np.argsort(total)[::-1]
    best = int(order[0])
    tonic, mode = _idx_to_key(best)

    top = []
    for i in order[:3]:
        t, m = _idx_to_key(int(i))
        top.append({"key": key_name(t, m), "camelot": camelot(t, m),
                    "score": round(float(total[i]), 4)})

    # 確信度: 1位と2位の差（相対調は区別しづらいので別に判定）
    rel = (tonic + 9) % 12 + 12 if mode == "major" else (tonic + 3) % 12
    second_nonrel = next(int(i) for i in order[1:] if int(i) != rel)
    confidence = float(total[best] - total[second_nonrel])
    relative_ambiguous = bool(total[best] - total[rel] < 0.05)

    # タイムライン（同一キーが続く窓を結合）
    merged = []
    for a, b, i, _ in timeline:
        if merged and merged[-1]["_i"] == i and a <= merged[-1]["end"]:
            merged[-1]["end"] = b
        else:
            t, m = _idx_to_key(i)
            merged.append({"start": a, "end": b, "_i": i, "key": key_name(t, m), "camelot": camelot(t, m)})
    for m in merged:
        m.pop("_i")
        m["start"], m["end"] = round(m["start"], 2), round(m["end"], 2)
    # 短い揺れ（隣接キーへの一時的な寄り道）は除き、30秒以上続く区間だけ残す
    cam = camelot(tonic, mode)
    long_ = [m for m in merged if m["end"] - m["start"] >= 30.0]
    # 転調 = 30秒以上、主キーから Camelot で2以上離れたキーが続く
    modulates = any(camelot_distance(m["camelot"], cam) >= 2 for m in long_)
    merged = long_ or merged

    return {
        "tonic": tonic,
        "mode": mode,
        "name": key_name(tonic, mode),
        "camelot": camelot(tonic, mode),
        "confidence": round(confidence, 4),
        "relative_ambiguous": relative_ambiguous,
        "candidates": top,
        "timeline": merged,
        "modulates": modulates,
    }
