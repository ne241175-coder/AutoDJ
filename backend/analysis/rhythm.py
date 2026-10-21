"""テンポ・拍・小節頭（ダウンビート）の推定。

方針
- 打楽器成分（HPSS）から多帯域オンセット包絡を作る
- 自己相関＋倍音（2倍・4倍周期）補強＋テンポ事前分布で BPM を推定
- librosa の DP ビートトラッキングで拍を取り、テンポが安定していれば
  直線フィット＋位相微調整で「等間隔グリッド」に置き換える（DJ用途で重要）
- 小節頭は「低域アタック・和声変化・全帯域アタック」が4拍周期で最も強い位相を選ぶ
"""
import numpy as np
import librosa


def _bands_below(sr, n_mels, fmin, fmax, cutoff_hz):
    freqs = librosa.mel_frequencies(n_mels=n_mels + 2, fmin=fmin, fmax=fmax)[1:-1]
    return int(np.searchsorted(freqs, cutoff_hz)) or 1


def onset_envelopes(y_perc, sr, hop):
    n_mels, fmin, fmax = 64, 30.0, min(11000.0, sr / 2)
    S = librosa.feature.melspectrogram(y=y_perc, sr=sr, n_fft=1024, hop_length=hop,
                                       n_mels=n_mels, fmin=fmin, fmax=fmax)
    S_db = librosa.power_to_db(S, ref=np.max, top_db=80.0)
    # n_fft を渡さないと librosa は 2048 を仮定して中心補正し、拍が約23ms遅れる
    full = librosa.onset.onset_strength(S=S_db, sr=sr, hop_length=hop, n_fft=1024)
    k = _bands_below(sr, n_mels, fmin, fmax, 200.0)
    low = librosa.onset.onset_strength(S=S_db[:max(k, 2)], sr=sr, hop_length=hop, n_fft=1024)
    return full, low


def _ac_norm(env):
    x = env - env.mean()
    ac = librosa.autocorrelate(x)
    return ac / (ac[0] + 1e-12)


def resolve_octave(bpm, oenv_low, sr, hop, hi=195.0):
    """倍テンポの判定。低域（キック）が倍テンポの周期でも強く脈打つなら倍にする。
    低域だけを見るのは、ハイハットの8分刻みで全帯域の周期性が曖昧になるため。
    半分にする方向には使わない（ロックはキックが1・3拍目だけのことが多い）。"""
    if bpm * 2 > hi:
        return bpm, None
    ac = _ac_norm(oenv_low)
    idx = np.arange(len(ac))
    L = 60.0 * sr / (hop * bpm)
    beat, half = np.interp(L, idx, ac), np.interp(L / 2, idx, ac)
    ratio = float(half / (beat + 1e-9))
    return (bpm * 2 if half > 0.5 and ratio > 0.7 else bpm), round(ratio, 3)


def estimate_bpm(oenv, sr, hop, lo=60.0, hi=200.0, center=120.0, sigma_oct=0.9):
    """自己相関ベースの大域BPM推定。戻り値: (bpm, 候補リスト[(bpm, salience)])"""
    x = oenv - oenv.mean()
    max_lag = int(np.ceil(60.0 * sr / (hop * lo))) * 4 + 2
    ac = librosa.autocorrelate(x, max_size=min(max_lag, len(x) - 1))
    ac = ac / (ac[0] + 1e-12)
    bpms = np.arange(lo, hi, 0.05)
    lag = 60.0 * sr / (hop * bpms)
    idx = np.arange(len(ac))

    def acv(l):
        return np.interp(l, idx, ac, right=0.0)

    # 拍周期の整数倍（2拍・4拍）と、半分（8分音符）にも周期性があるはず → 補強。
    # 半分の項がないと、付点拍（真のテンポの2/3）を誤って選ぶことがある（例: 174→116）
    sal = acv(lag) + 0.5 * acv(2 * lag) + 0.25 * acv(4 * lag) + 0.35 * acv(lag / 2)
    prior = np.exp(-0.5 * (np.log2(bpms / center) / sigma_oct) ** 2)
    score = np.clip(sal, 0, None) * prior
    best = float(bpms[int(np.argmax(score))])
    # 候補（局所最大の上位）
    from scipy.signal import find_peaks
    pk, _ = find_peaks(score, distance=int(3 / 0.05))
    order = pk[np.argsort(score[pk])[::-1]][:4]
    cands = [(round(float(bpms[i]), 2), round(float(score[i] / (score.max() + 1e-12)), 3)) for i in order]
    return best, cands


def fit_grid(oenv, sr, hop, bpm_hint, span=0.03, step=0.002, win_beats=16):
    """オンセット包絡に等間隔グリッドを直接当てはめる。
    周期P候補ごとに  Σ o(t)·exp(-i2πt/P)  の大きさ（その周期での揃い具合）を計算し最大のPを選ぶ。
    位相（グリッドの開始位置）は複素数の角度から求める。
    DPビートを直線フィットする方法は、途中で半拍ずれが起きると破綻するので使わない。
    戻り値: (period, t0, consistency)  consistency = 位相が全体と一致する窓の割合"""
    times = librosa.frames_to_time(np.arange(len(oenv)), sr=sr, hop_length=hop)
    o = np.clip(oenv - np.convolve(oenv, np.ones(64) / 64, mode="same"), 0, None)
    bpms = np.arange(bpm_hint * (1 - span), bpm_hint * (1 + span), step)
    mags = np.empty(len(bpms))
    for i0 in range(0, len(bpms), 64):
        w = 2 * np.pi * bpms[i0:i0 + 64, None] / 60.0
        mags[i0:i0 + 64] = np.abs((o[None, :] * np.exp(-1j * w * times[None, :])).sum(1))
    bpm = float(bpms[int(np.argmax(mags))])
    P = 60.0 / bpm
    w = 2 * np.pi / P
    z = o * np.exp(-1j * w * times)
    t0 = (-np.angle(z.sum()) / w) % P

    # 窓ごとの位相一致率（テンポが本当に一定かの判定）
    win = int(win_beats * P * sr / hop)
    energies, agree = [], []
    for a in range(0, len(o) - win, win // 2):
        zz = z[a:a + win].sum()
        if np.abs(zz) < 1e-9:
            continue
        dt = ((-np.angle(zz) / w) - t0 + P / 2) % P - P / 2
        energies.append(o[a:a + win].sum())
        agree.append(abs(dt) < 0.08 * P)
    energies, agree = np.array(energies), np.array(agree)
    strong = energies > 0.3 * np.median(energies) if len(energies) else energies
    consistency = float(agree[strong].mean()) if len(energies) and strong.any() else 0.0
    return P, t0, consistency


def _refine_phase(t0, P, oenv, sr, hop, duration):
    times = librosa.frames_to_time(np.arange(len(oenv)), sr=sr, hop_length=hop)
    best, best_s = t0, -1.0
    for d in np.linspace(-0.12 * P, 0.12 * P, 49):
        g = t0 + d + P * np.arange(-int(t0 / P) - 1, int((duration - t0) / P) + 2)
        g = g[(g >= 0) & (g < duration)]
        s = np.interp(g, times, oenv).mean()
        if s > best_s:
            best, best_s = t0 + d, s
    return best


def track_local(oenv, sr, hop, P, t0, duration, win_beats=8, smooth_beats=4, oenv_low=None):
    """テンポが揺れる曲（クリックなしの生演奏など）用。
    全体の周期Pを基準に、各拍の周辺（win_beats拍）で位相ずれを測り、
    複素数のまま強さで重み付けして平滑化 → 位相を連続化(unwrap)して拍位置を補正する。
    DPビートトラッキングと違い、途中で半拍ずれる（位相が飛ぶ）ことがない。"""
    times = librosa.frames_to_time(np.arange(len(oenv)), sr=sr, hop_length=hop)

    def peaks(e):
        x = np.clip(e - np.convolve(e, np.ones(64) / 64, mode="same"), 0, None)
        return x / (np.median(x[x > 0]) + 1e-9) if (x > 0).any() else x

    # 低域（キック・ベース）を重視: 裏打ちハイハットだけの区間で裏拍に吸われるのを防ぐ
    o = peaks(oenv) + (1.5 * peaks(oenv_low) if oenv_low is not None else 0)
    w = 2 * np.pi / P
    z = o * np.exp(-1j * w * times)
    k = np.arange(-int(np.floor(t0 / P)), int(np.floor((duration - t0) / P)) + 1)
    grid = t0 + P * k
    half = win_beats * P / 2
    cs = np.concatenate([[0], np.cumsum(z)])
    zz = np.empty(len(grid), complex)
    for i, g in enumerate(grid):
        a = np.searchsorted(times, g - half)
        b = np.searchsorted(times, g + half)
        zz[i] = cs[b] - cs[a]
    # 複素数のままガウス平滑化（弱い窓は自然に影響が小さくなる）
    gk = np.exp(-0.5 * (np.arange(-3 * smooth_beats, 3 * smooth_beats + 1) / smooth_beats) ** 2)
    zs = np.convolve(zz, gk, mode="same")
    # 位相の確かさ: 表拍と裏拍に同じくらい音がある区間（キックなしの裏打ちハイハット等）は
    # 拍周期成分が打ち消し合って弱くなる → そこでは位相を測らず、前後から補間する
    strength = np.abs(zs)
    ok = strength >= 0.35 * np.median(strength)
    if ok.sum() < 8:
        ok[:] = True
    idx = np.flatnonzero(ok)
    O = -np.unwrap(np.angle(zs[idx])) / w             # 確かな拍での位相（連続化）
    ref = idx[int(np.argmax(np.abs(zs[idx])))]
    r = int(np.searchsorted(idx, ref))
    d_ref = ((O[r] - t0) + P / 2) % P - P / 2         # 基準拍でのグリッドからのずれ
    delta_ok = d_ref + (O - O[r])                      # 連続なずれ（累積ドリフトも追従）
    delta = np.interp(np.arange(len(grid)), idx, delta_ok)  # 不確かな区間は補間（端は一定）
    beats = grid + delta
    keep = (beats >= 0) & (beats < duration)
    return beats[keep], ok[keep]


def _ranges(times, mask, min_beats=4):
    """mask が連続して True の区間を [start, end] のリストにする（min_beats 拍以上のみ）"""
    out, i, n = [], 0, len(mask)
    while i < n:
        if mask[i]:
            j = i
            while j + 1 < n and mask[j + 1]:
                j += 1
            if j - i + 1 >= min_beats:
                out.append([round(float(times[i]), 3), round(float(times[j]), 3)])
            i = j + 1
        else:
            i += 1
    return out


def track_beats(oenv, sr, hop, duration, bpm_hint, oenv_low=None):
    """戻り値: (beats[s], bpm, stable, consistency, uncertain_ranges)。
    固定テンポと判定できれば等間隔グリッド、できなければDPビートを返す。
    オンセット強度は1フレーム差分(lag=1)なので、ピークは打撃より約1hop遅れる → 1hop分補正する。"""
    latency = hop / sr
    _, beats = librosa.beat.beat_track(onset_envelope=oenv, sr=sr, hop_length=hop,
                                       bpm=bpm_hint, tightness=120, trim=False, units="time")
    beats = np.asarray(beats, dtype=float).ravel()
    if len(beats) < 16:
        raise RuntimeError("拍がほとんど検出できませんでした（無音や拍のない曲の可能性）")
    dp_bpm = 60.0 / float(np.median(np.diff(beats)))
    P, t0, consistency = fit_grid(oenv, sr, hop, dp_bpm)
    if consistency >= 0.75:
        t0 = _refine_phase(t0, P, oenv, sr, hop, duration)
        kmin = -int(np.floor(t0 / P))
        kmax = int(np.floor((duration - t0) / P))
        grid = t0 + P * np.arange(kmin, kmax + 1) - latency
        grid = grid[(grid >= 0) & (grid < duration)]
        return grid, 60.0 / P, True, consistency, []
    beats, ok = track_local(oenv, sr, hop, P, t0, duration, oenv_low=oenv_low)
    beats = beats - latency
    ok, beats = ok[beats >= 0], beats[beats >= 0]
    mean_bpm = 60.0 * (len(beats) - 1) / (beats[-1] - beats[0])
    return beats, mean_bpm, False, consistency, _ranges(beats, ~ok)


def _local_max(env, frames, w=2):
    out = np.zeros(len(frames))
    n = len(env)
    for i, f in enumerate(frames):
        a, b = max(0, f - w), min(n, f + w + 1)
        out[i] = env[a:b].max() if b > a else 0.0
    return out


def _z(x):
    s = x.std()
    return (x - x.mean()) / s if s > 1e-9 else x * 0


def find_downbeats(beats, oenv_full, oenv_low, onset_hop, chroma, feat_hop, sr):
    """戻り値: dict(meter, phase, downbeats, confidence)"""
    fo = librosa.time_to_frames(beats, sr=sr, hop_length=onset_hop)
    low = _local_max(oenv_low, fo)
    full = _local_max(oenv_full, fo)
    # 拍ごとの平均クロマ → 前2拍 vs 後2拍 の和声変化
    fc = librosa.time_to_frames(beats, sr=sr, hop_length=feat_hop)
    fc = np.clip(fc, 0, chroma.shape[1] - 1)
    bc = np.stack([chroma[:, fc[i]:max(fc[i] + 1, fc[i + 1] if i + 1 < len(fc) else fc[i] + 1)].mean(1)
                   for i in range(len(fc))], axis=1)
    bc = bc / (np.linalg.norm(bc, axis=0, keepdims=True) + 1e-9)
    nov = np.zeros(len(beats))
    for i in range(2, len(beats) - 2):
        a = bc[:, i - 2:i].mean(1)
        b = bc[:, i:i + 2].mean(1)
        nov[i] = 1 - a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9)
    f = _z(low) + _z(nov) + 0.5 * _z(full)

    def phase_strength(m):
        s = np.array([f[p::m].mean() for p in range(m)])
        p = int(np.argmax(s))
        others = np.delete(s, p)
        return p, float((s[p] - others.mean()) / (f.std() + 1e-9))

    p4, d4 = phase_strength(4)
    p3, d3 = phase_strength(3)
    meter, phase, conf = (3, p3, d3) if (d3 > 1.5 * d4 and d3 > 0.3) else (4, p4, d4)
    return {
        "meter": meter,
        "phase": phase,
        "downbeats": beats[phase::meter],
        "confidence": round(conf, 3),
    }
