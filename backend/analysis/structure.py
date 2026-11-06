"""構造（セクション境界とラベル）の推定。

既存実装で精度が悪かった点への対策:
- 全処理を「小節単位」で行う（境界が拍の途中に来ない）
- 境界の手がかりを3種類組み合わせる
    1) 自己類似行列(SSM)＋チェッカーボードカーネルによる新規性（音色・和声の変化）
    2) 音量の段差（前4小節と後4小節の差）
    3) 低域・打楽器量の段差（キックの抜き差し＝ブレイク/ドロップ）
- 4小節グリッドの位相を推定し、±1小節以内の境界をグリッドへ吸着
- 4小節未満のセクションは弱い方の境界を削除
- 似たセクションをまとめてグループ(A,B,C…)にし、役割(intro/verse/chorus/break/build/outro)を推定
"""
import numpy as np
from scipy.signal import find_peaks


def bar_aggregate(feat, frame_times, bar_starts, bar_ends):
    out = np.zeros((feat.shape[0], len(bar_starts)))
    for i, (a, b) in enumerate(zip(bar_starts, bar_ends)):
        m = (frame_times >= a) & (frame_times < b)
        out[:, i] = feat[:, m].mean(1) if m.any() else (out[:, i - 1] if i else 0)
    return out


def _zrows(x):
    return (x - x.mean(1, keepdims=True)) / (x.std(1, keepdims=True) + 1e-9)


def _norm01(x):
    hi = np.percentile(x, 95) if len(x) else 1
    return np.clip(x / (hi + 1e-9), 0, 1)


def _checkerboard(k):
    g = np.exp(-0.5 * ((np.arange(-k, k) + 0.5) / (k / 2)) ** 2)
    sign = np.sign(np.arange(-k, k) + 0.5)
    return np.outer(g * sign, g * sign)


def foote_novelty(S, k=4):
    n = S.shape[0]
    K = _checkerboard(k)
    Sp = np.pad(S, k, mode="edge")
    return np.array([max(0.0, (K * Sp[i:i + 2 * k, i:i + 2 * k]).sum()) for i in range(n)])


def step_change(x, k=4):
    """各小節iで「後k小節平均 − 前k小節平均」の絶対値。"""
    n = len(x)
    out = np.zeros(n)
    for i in range(1, n):
        a = x[max(0, i - k):i].mean()
        b = x[i:min(n, i + k)].mean()
        out[i] = abs(b - a)
    return out


def detect_boundaries(nov, n_bars, min_len=4, grid=4):
    # グリッド位相: 新規性が4小節周期で最も強い位相（0を優先）
    sums = np.array([nov[o::grid].sum() for o in range(grid)])
    offset = int(np.argmax(sums)) if sums.max() > 1.3 * sums[0] else 0

    height = nov.mean() + 0.4 * nov.std()
    peaks, props = find_peaks(nov, height=height, distance=2)
    strength = dict(zip(peaks.tolist(), props["peak_heights"].tolist()))

    snapped = {}
    for p in peaks:
        g = offset + grid * round((p - offset) / grid)
        q = g if abs(p - g) <= 1 and 0 < g < n_bars else p
        snapped[q] = max(snapped.get(q, 0), strength[p])

    bounds = sorted(b for b in snapped if 0 < b < n_bars)
    # 短すぎるセクションを解消（弱い境界を削除）
    changed = True
    while changed and bounds:
        changed = False
        full = [0] + bounds + [n_bars]
        for i in range(1, len(full)):
            if full[i] - full[i - 1] < min_len:
                cand = [b for b in (full[i - 1], full[i]) if b in snapped and 0 < b < n_bars]
                if not cand:
                    continue
                weakest = min(cand, key=lambda b: snapped[b])
                bounds.remove(weakest)
                changed = True
                break
    return [0] + bounds + [n_bars], offset, snapped


def split_long_sections(bounds, nov, offset8, strength, max_len=16, min_len=4):
    """フレーズの事前知識: ポップスの区間は多くが 10〜32 秒（シート25曲の実測、中央値19秒）。
    max_len 小節を超える区間は、内部の8小節グリッド（なければ4小節）上で新規性が最大の点で必ず分割する。"""
    out = list(bounds)
    changed = True
    while changed:
        changed = False
        for a, b in zip(out[:-1], out[1:]):
            if b - a <= max_len:
                continue
            cands = [c for c in range(a + min_len, b - min_len + 1) if (c - offset8) % 8 == 0]
            if not cands:
                cands = [c for c in range(a + min_len, b - min_len + 1) if (c - offset8) % 4 == 0]
            if not cands:
                continue
            c = max(cands, key=lambda c: nov[c])
            strength[c] = float(nov[c])
            out = sorted(out + [c])
            changed = True
            break
    return out


def repetition_score(C, min_lag=8, span=4):
    """各小節について「曲中の別の場所（min_lag 小節以上離れた所）に、同じ和声の流れが span 小節続けて
    現れるか」の強さ。サビは同じメロディ・コード進行を何度も繰り返すので高くなる。
    C: 小節ごとの正規化クロマ (12 x n)"""
    n = C.shape[1]
    S = C.T @ C
    rep = np.zeros(n)
    for i in range(n):
        best = 0.0
        for j in range(n):
            if abs(i - j) < min_lag:
                continue
            k = min(span, n - i, n - j)
            if k < 2:
                continue
            v = np.mean([S[i + t, j + t] for t in range(k)])
            best = max(best, v)
        rep[i] = best
    return rep


def _group_sections(seg_feats, thresh=0.3):
    """和声(クロマ)と音色(MFCC+音量)の距離を半々で合成し、貪欲クラスタリング → 'A','B',…
    seg_feats: list of (chroma_mean, timbre_mean)"""
    timbre = np.array([t for _, t in seg_feats])
    scale = np.median(np.linalg.norm(timbre - timbre.mean(0), axis=1)) + 1e-9

    def dist(i, j):
        ci, cj = seg_feats[i][0], seg_feats[j][0]
        dc = 1 - ci @ cj / (np.linalg.norm(ci) * np.linalg.norm(cj) + 1e-9)
        dt = np.linalg.norm(timbre[i] - timbre[j]) / (2 * scale)
        return 0.5 * dc * 4 + 0.5 * min(dt, 2.0)

    labels, protos = [], []
    for i in range(len(seg_feats)):
        if protos:
            d = [dist(i, p) for p in protos]
            j = int(np.argmin(d))
            if d[j] < thresh:
                labels.append(j)
                continue
        protos.append(i)
        labels.append(len(protos) - 1)
    return [chr(ord("A") + l) if l < 26 else f"Z{l}" for l in labels]


def assign_roles(sections):
    n = len(sections)
    e = np.array([s["energy"] for s in sections])
    low = np.array([s["low_energy"] for s in sections])
    rep = np.array([s.get("repetition", 0.0) for s in sections])
    med = np.median(e)

    def z(x):
        return (x - x.mean()) / (x.std() + 1e-9)

    # 区間ごとの「サビらしさ」= 繰り返し度と音量（シート25曲では サビ=最も繰り返され、音量も大きい）
    score = 0.55 * z(rep) + 0.45 * z(e)
    for s, sc in zip(sections, score):
        s["chorus_score"] = round(float(sc), 3)
    groups = {}
    for i, s in enumerate(sections):
        groups.setdefault(s["group"], []).append(i)

    def group_value(idx):
        v = float(np.mean(score[idx]))
        if 2 <= len(idx) <= 4:      # サビは 2〜4 回（シートでは3回が最多）
            v += 0.3
        if idx and min(idx) == 0:   # 曲頭から始まるグループはイントロの可能性が高い
            v -= 0.2
        return v

    chorus_group = max(groups, key=lambda g: group_value(groups[g]))
    gmean = float(np.mean(score[groups[chorus_group]]))

    for i, s in enumerate(sections):
        role = "verse"
        if (s["group"] == chorus_group and s["energy"] >= med) or \
                (score[i] >= gmean and s["energy"] >= med and s["bars"] >= 6):
            role = "chorus"
        if 0 < i < n - 1:
            neighbors = max(e[i - 1], e[i + 1])
            if e[i] < neighbors - 0.12 and low[i] < np.median(low):
                role = "break"
            elif (e[i + 1] > e[i] + 0.08 and s["energy_slope"] > 0.05
                  and sections[i + 1]["group"] == chorus_group and role != "chorus"):
                role = "build"
        if i == 0 and (e[0] < med or low[0] < np.median(low)):
            role = "intro"
        if i == n - 1 and n > 1 and (e[-1] < med or s["energy_slope"] < -0.05):
            role = "outro"
        s["role"] = role
    return sections


def analyze_structure(bars_t, duration, frame_times, feats):
    """
    bars_t: 小節頭の時刻配列
    feats: dict(chroma(12xF), mfcc(13xF), rms_db(F), low_db(F), perc_ratio(F))
    """
    starts = np.asarray(bars_t, float)
    if len(starts) < 8:
        return {"sections": [], "grid_offset": 0, "bar_novelty": []}
    bar_len = np.median(np.diff(starts))
    ends = np.append(starts[1:], min(duration, starts[-1] + bar_len))

    ch = bar_aggregate(feats["chroma"], frame_times, starts, ends)
    ch = ch / (np.linalg.norm(ch, axis=0, keepdims=True) + 1e-9)
    mf = _zrows(bar_aggregate(feats["mfcc"], frame_times, starts, ends))
    rms = bar_aggregate(feats["rms_db"][None], frame_times, starts, ends)[0]
    low = bar_aggregate(feats["low_db"][None], frame_times, starts, ends)[0]
    perc = bar_aggregate(feats["perc_ratio"][None], frame_times, starts, ends)[0]
    n = len(starts)

    # SSM（和声＋音色、2小節の時間遅れ埋め込み）
    X = np.vstack([ch * 2.0, mf / np.sqrt(mf.shape[0])])
    Xe = (X + np.hstack([X[:, 1:], X[:, -1:]])) / 2
    Xe = Xe / (np.linalg.norm(Xe, axis=0, keepdims=True) + 1e-9)
    S = Xe.T @ Xe
    nov = (0.5 * _norm01(foote_novelty(S, k=4))
           + 0.3 * _norm01(step_change(rms))
           + 0.2 * _norm01(step_change(low) + step_change(perc * 20)))

    bounds, offset, strength = detect_boundaries(nov, n)
    # 8/16小節フレーズの位相: 4小節グリッドと矛盾しない位相のうち、境界の強さの合計が大きい方
    # （例: 4小節イントロの曲は 8小節フレーズが bar 4 から始まる）
    def pick(base, period):
        cands = [base + k for k in range(0, period, period // 2)]
        score = [sum(strength.get(b, 0) for b in bounds if (b - c) % period == 0) for c in cands]
        return cands[int(np.argmax(score))] % period
    offset8 = pick(offset, 8)
    offset16 = pick(offset8, 16)
    max_bars = max(8, 4 * int(round(32.0 / bar_len / 4)))   # 約32秒（4小節単位）
    bounds = split_long_sections(bounds, nov, offset8, strength, max_len=max_bars)
    rep = repetition_score(ch)

    # 0〜1に正規化した小節エネルギー（曲内相対）
    def rel(x):
        lo, hi = np.percentile(x, 5), np.percentile(x, 95)
        return np.clip((x - lo) / (hi - lo + 1e-9), 0, 1)

    e_rel, low_rel = rel(rms), rel(low)
    e_rel_z, low_z = (rms - rms.mean()) / (rms.std() + 1e-9), (low - low.mean()) / (low.std() + 1e-9)
    seg_feats, sections = [], []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        seg_feats.append((ch[:, a:b].mean(1), np.concatenate([mf[:, a:b].mean(1), [e_rel_z[a:b].mean() * 2, low_z[a:b].mean() * 2]])))
        half = max(1, (b - a) // 2)
        sections.append({
            "start_bar": int(a), "end_bar": int(b), "bars": int(b - a),
            "start": round(float(starts[a]), 3), "end": round(float(ends[b - 1]), 3),
            "energy": round(float(e_rel[a:b].mean()), 3),
            "low_energy": round(float(low_rel[a:b].mean()), 3),
            "energy_slope": round(float(e_rel[a + half:b].mean() - e_rel[a:a + half].mean()), 3),
            "boundary_strength": round(float(strength.get(a, 1.0 if a == 0 else 0.0)), 3),
            "on_grid4": bool((a - offset) % 4 == 0),
            "on_grid8": bool((a - offset8) % 8 == 0),
            "on_grid16": bool((a - offset16) % 16 == 0),
            "repetition": round(float(rep[a:b].mean()), 3),
        })
    for s, g in zip(sections, _group_sections(seg_feats)):
        s["group"] = g
    assign_roles(sections)

    return {
        "sections": sections,
        "grid_offset": offset,
        "phrase_offsets": {"4": int(offset), "8": int(offset8), "16": int(offset16)},
        "bar_novelty": [round(float(v), 3) for v in nov],
        "bar_energy": [round(float(v), 3) for v in e_rel],
        "bar_low_energy": [round(float(v), 3) for v in low_rel],
    }
