"""シート（構成入力）の秒数と、取り込んだ音源の時刻のズレを自動で推定する。

シートの秒数は「計測に使った動画」の時刻。取り込んだ音源が別の版（MV の前置きが長い、等）だと、
全体が数秒〜数十秒ずれる。音源側の「変化の大きい小節頭」とシートの区間頭が最もよく重なるように
ずらし量を探す。   音源の時刻 = シートの時刻 + offset
"""
import numpy as np

SEARCH = 60.0   # 探す範囲（±秒）
STEP = 0.25
SIGMA = 1.5     # 区間頭と小節頭の一致の甘さ（秒）


def _score(sheet_times, bar_t, bar_w, off, duration):
    s = 0.0
    for t in sheet_times:
        u = t + off
        if u < 0 or u > duration:   # 音源の外にはみ出すずらし方は不自然 → 減点
            s -= 0.5
            continue
        d = bar_t - u
        s += float(np.max(bar_w * np.exp(-0.5 * (d / SIGMA) ** 2)))
    return s * (1 - 0.002 * abs(off))   # 同程度なら、ずらし量が小さい方


def estimate_offset(sheet_sections, analysis, sheet_end=None):
    """戻り値: dict(offset, confidence, score0, score)。confidence はズレ0の場合と比べた良さ（1.0=同じ）"""
    st = analysis["structure"]
    bar_t = np.asarray(analysis["bars"], float)
    nov = np.asarray(st.get("bar_novelty") or [], float)
    if len(nov) != len(bar_t) or len(bar_t) < 8:
        return {"offset": 0.0, "confidence": 1.0, "score0": 0.0, "score": 0.0}
    w = nov / (nov.max() + 1e-9) + 0.05
    times = [s["start"] for s in sheet_sections if s["start"] > 1.0]
    if not times:
        return {"offset": 0.0, "confidence": 1.0, "score0": 0.0, "score": 0.0}
    offs = np.arange(-SEARCH, SEARCH + STEP / 2, STEP)
    scores = np.array([_score(times, bar_t, w, o, analysis["duration"]) for o in offs])
    # 長さの差（音源が長い分は前か後ろに付いている）を弱い事前知識として加点
    if sheet_end:
        extra = analysis["duration"] - sheet_end
        prior = np.exp(-0.5 * (np.minimum(np.abs(offs), np.abs(offs - extra)) / 6.0) ** 2)
        scores = scores * (0.85 + 0.15 * prior)
    i = int(np.argmax(scores))
    s0 = float(scores[np.argmin(np.abs(offs))])
    return {"offset": float(offs[i]), "confidence": float(scores[i] / max(s0, 1e-3)),
            "score0": s0, "score": float(scores[i])}


def aligned_sections(sheet_song, analysis, min_gain=1.25, min_shift=1.0):
    """シートの区間を音源の時刻に合わせて返す。戻り値: (sections, offset)。
    ずらした方が明らかに合う（score が min_gain 倍以上）ときだけずらす"""
    est = estimate_offset(sheet_song["sections"], analysis, sheet_song.get("end"))
    off = est["offset"] if (abs(est["offset"]) >= min_shift and est["confidence"] >= min_gain) else 0.0
    secs = []
    for s in sheet_song["sections"]:
        x = dict(s)
        x["start"] = max(0.0, s["start"] + off)
        x["end"] = (s["end"] + off) if s.get("end") is not None else None
        secs.append(x)
    return secs, off
