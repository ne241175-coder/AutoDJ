"""区切りモデル用の追加特徴量（小節ごと）。

実データ（シートの構成 38 曲）で、ルールの境界 F=0.54 → 学習モデル F≈0.66 に上がった組み合わせ。
特に効いていたもの: 音色（MFCC）の変化、直前の小節の最後の1拍のフィル（打楽器の立ち上がり）、
「繰り返しの終わり」、音量の段差、小節頭のアクセント、歌の帯域の段差。
"""
import librosa
import numpy as np

from .structure import foote_novelty, step_change

HOP = 512
EXTRA_NAMES = [
    "f_all16", "f_ch2", "f_ch4", "f_ch8", "f_mf2", "f_mf4", "f_mf8", "head_rms", "rep_end", "rep_start",
    "rms_lvl", "st_bass", "st_cent", "st_hihat", "st_kick", "st_rms2", "st_vocal", "st_vocal4",
    "tail_flux_prev", "tail_rms_prev", "up_rms", "vocal_lvl",
]


def _agg(x, t, a, b):
    m = (t >= a) & (t < b)
    return x[..., m].mean(-1) if m.any() else np.zeros(x.shape[:-1])


def extra_bar_features(y, sr, bars, duration):
    """戻り値: (n_bars x len(EXTRA_NAMES))。曲ごとに中央値と幅で正規化済み"""
    starts = np.asarray(bars, float)
    n = len(starts)
    if n < 8:
        return np.zeros((n, len(EXTRA_NAMES)))
    S = np.abs(librosa.stft(y, n_fft=2048, hop_length=HOP))
    H, P = librosa.decompose.hpss(S, kernel_size=(31, 31))
    t = librosa.frames_to_time(np.arange(S.shape[1]), sr=sr, hop_length=HOP)
    mel = librosa.feature.melspectrogram(S=S ** 2, sr=sr, n_mels=64)
    mfcc = librosa.feature.mfcc(S=librosa.power_to_db(mel), n_mfcc=20)
    chroma = librosa.feature.chroma_stft(S=H ** 2, sr=sr)
    freqs = librosa.fft_frequencies(sr=sr, n_fft=2048)

    def band(M, lo, hi):
        return 10 * np.log10((M[(freqs >= lo) & (freqs < hi)] ** 2).sum(0) + 1e-10)

    vocal = band(H, 300, 3400)      # 歌の帯域（和音成分）
    bass = band(S, 30, 150)
    hihat = band(P, 6000, 11000)
    kick = band(P, 30, 120)
    rms = 10 * np.log10((S ** 2).mean(0) + 1e-10)
    flux = np.r_[0, np.maximum(0, np.diff(np.log1p(P), axis=1)).sum(0)]   # 打楽器の立ち上がり
    cent = librosa.feature.spectral_centroid(S=S, sr=sr)[0]

    bl = np.median(np.diff(starts))
    ends = np.append(starts[1:], min(duration, starts[-1] + bl))
    R = {k: [] for k in ("mf", "ch", "vocal", "bass", "hihat", "kick", "rms", "cent", "tail_flux", "tail_rms", "head_rms")}
    for a, b in zip(starts, ends):
        R["mf"].append(_agg(mfcc, t, a, b))
        R["ch"].append(_agg(chroma, t, a, b))
        for k, v in (("vocal", vocal), ("bass", bass), ("hihat", hihat), ("kick", kick), ("rms", rms), ("cent", cent)):
            R[k].append(_agg(v, t, a, b))
        q = (b - a) / 4      # 小節の最後の1拍（フィル・ブレイク）と最初の1拍（アクセント）
        R["tail_flux"].append(_agg(flux, t, b - q, b) - _agg(flux, t, a, b))
        R["tail_rms"].append(_agg(rms, t, b - q, b) - _agg(rms, t, a, b))
        R["head_rms"].append(_agg(rms, t, a, a + q) - _agg(rms, t, a, b))
    R = {k: np.array(v) for k, v in R.items()}
    mf = (R["mf"] - R["mf"].mean(0)) / (R["mf"].std(0) + 1e-9)
    mfn = mf / (np.linalg.norm(mf, axis=1, keepdims=True) + 1e-9)
    ch = R["ch"] / (np.linalg.norm(R["ch"], axis=1, keepdims=True) + 1e-9)
    Smf, Sch = mfn @ mfn.T, ch @ ch.T
    Sall = (Smf + Sch) / 2

    # 繰り返しの始まり/終わり: i から4小節が別の場所 j と似ていて、その1小節前は似ていない
    L = 4
    D = np.zeros((n, n))
    for k in range(L):
        D[:n - k, :n - k] += Sall[k:, k:]
    D /= L
    idx = np.arange(n)
    far = np.abs(idx[:, None] - idx[None, :]) >= 8
    dstep = np.full((n, n), -np.inf)
    dstep[1:, 1:] = D[1:, 1:] - D[:-1, :-1]
    dstep[~far] = -np.inf
    dstep[:, 0] = -np.inf
    dstep[:, n - L:] = -np.inf
    rep_start = np.zeros(n)
    rep_start[1:n - L] = np.max(dstep[1:n - L], axis=1)
    rep_end = np.zeros(n)
    for i in range(L, n - L):
        # i-L から始まる繰り返しが i で途切れる
        js = np.where(far[i])[0]
        js = js[(js >= L) & (js < n - L)]
        if len(js):
            rep_end[i] = np.max(D[i - L, js - L] - D[i - L + 1, js - L + 1])
    rep_start[~np.isfinite(rep_start)] = 0

    F = {
        "f_mf2": foote_novelty(Smf, 2), "f_mf4": foote_novelty(Smf, 4), "f_mf8": foote_novelty(Smf, 8),
        "f_ch2": foote_novelty(Sch, 2), "f_ch4": foote_novelty(Sch, 4), "f_ch8": foote_novelty(Sch, 8),
        "f_all16": foote_novelty(Sall, 16),
        "st_vocal": step_change(R["vocal"], 2), "st_vocal4": step_change(R["vocal"], 4),
        "st_bass": step_change(R["bass"], 2), "st_hihat": step_change(R["hihat"], 4),
        "st_kick": step_change(R["kick"], 4), "st_rms2": step_change(R["rms"], 2), "st_cent": step_change(R["cent"], 4),
        "tail_flux_prev": np.r_[0, R["tail_flux"][:-1]], "tail_rms_prev": np.r_[0, R["tail_rms"][:-1]],
        "head_rms": R["head_rms"], "rep_start": rep_start, "rep_end": rep_end,
        "vocal_lvl": R["vocal"] - np.median(R["vocal"]), "rms_lvl": R["rms"] - np.median(R["rms"]),
        "up_rms": np.r_[0, [R["rms"][i:i + 4].mean() - R["rms"][max(0, i - 4):i].mean() for i in range(1, n)]],
    }
    X = np.stack([np.nan_to_num(np.asarray(F[k], float)) for k in EXTRA_NAMES], 1)
    # 曲ごとの音量・密度の違いを消す
    return (X - np.median(X, 0)) / (np.percentile(X, 90, 0) - np.percentile(X, 10, 0) + 1e-9)
