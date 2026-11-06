"""1曲を解析して JSON 化できる dict を返す。"""
import time
import numpy as np
import librosa

from ..config import SR, ONSET_HOP, FEAT_HOP, ANALYZER_VERSION, SCHEMA_VERSION
from . import rhythm, key as keymod, structure


def _music_bounds(y, sr):
    _, idx = librosa.effects.trim(y, top_db=45)
    return idx[0] / sr, idx[1] / sr


def analyze_file(path, sr=SR, verbose=False):
    t_start = time.time()
    log = (lambda *a: print("  ", *a)) if verbose else (lambda *a: None)

    y, sr = librosa.load(str(path), sr=sr, mono=True)
    duration = len(y) / sr
    music_start, music_end = _music_bounds(y, sr)
    y_harm, y_perc = librosa.effects.hpss(y, margin=(1.0, 2.0))
    log(f"読み込み {duration:.1f}s / HPSS 完了")

    # --- リズム ---
    oenv, oenv_low = rhythm.onset_envelopes(y_perc, sr, ONSET_HOP)
    bpm_est, bpm_cands = rhythm.estimate_bpm(oenv, sr, ONSET_HOP)
    bpm_est, _ = rhythm.resolve_octave(bpm_est, oenv_low, sr, ONSET_HOP)
    beats, bpm, stable, consistency, uncertain = rhythm.track_beats(oenv, sr, ONSET_HOP, duration, bpm_est, oenv_low=oenv_low)
    log(f"BPM {bpm:.2f}（{'固定グリッド' if stable else '可変'}）")

    # --- 和声 ---
    chroma, tuning = keymod.harmonic_chroma(y_harm, sr, FEAT_HOP)
    key = keymod.estimate_key(chroma, sr, FEAT_HOP)
    log(f"Key {key['name']} ({key['camelot']}) conf={key['confidence']}")

    db = rhythm.find_downbeats(beats, oenv, oenv_low, ONSET_HOP, chroma, FEAT_HOP, sr)
    downbeats = np.asarray(db["downbeats"])
    beat_period = 60.0 / bpm
    # 小節番号は「音が鳴り始めた位置以降の最初の小節頭」を 0 とする
    first = int(np.searchsorted(downbeats, music_start - 0.25 * beat_period))
    bars_t = downbeats[first:]
    bars_t = bars_t[bars_t < music_end]

    # --- 構造用の特徴 ---
    mel = librosa.feature.melspectrogram(y=y, sr=sr, n_fft=2048, hop_length=FEAT_HOP, n_mels=64, fmin=30)
    mel_db = librosa.power_to_db(mel, ref=np.max)
    mfcc = librosa.feature.mfcc(S=mel_db, n_mfcc=13)
    freqs = librosa.mel_frequencies(n_mels=66, fmin=30, fmax=sr / 2)[1:-1]
    low_db = librosa.power_to_db(mel[freqs < 150].sum(0) + 1e-10, ref=mel.sum(0).max())
    rms_db = librosa.amplitude_to_db(librosa.feature.rms(y=y, hop_length=FEAT_HOP)[0], ref=1.0)
    rp = librosa.feature.rms(y=y_perc, hop_length=FEAT_HOP)[0]
    rh = librosa.feature.rms(y=y_harm, hop_length=FEAT_HOP)[0]
    nf = min(mfcc.shape[1], chroma.shape[1], len(rms_db), len(rp), len(low_db))
    frame_times = librosa.frames_to_time(np.arange(nf), sr=sr, hop_length=FEAT_HOP)
    feats = {
        "chroma": chroma[:, :nf], "mfcc": mfcc[:, :nf], "rms_db": rms_db[:nf],
        "low_db": low_db[:nf], "perc_ratio": (rp / (rp + rh + 1e-9))[:nf],
    }
    from .extra_features import extra_bar_features
    extra = extra_bar_features(y, sr, bars_t, duration)
    st = structure.analyze_structure(bars_t, duration, frame_times, feats, extra=extra)
    log(f"セクション {len(st['sections'])} 個")

    return {
        "schema_version": SCHEMA_VERSION,
        "analyzer_version": ANALYZER_VERSION,
        "duration": round(duration, 3),
        "music_start": round(music_start, 3),
        "music_end": round(music_end, 3),
        "tempo": {
            "bpm": round(float(bpm), 2),
            "stable": bool(stable),
            "grid_consistency": round(consistency, 3),
            # 可変テンポ曲で、拍の手がかりが弱く補間で埋めた区間（ビートマッチ非推奨）
            "uncertain_beat_ranges": uncertain,
            "candidates": bpm_cands,
            "half_double": [round(bpm / 2, 2), round(bpm * 2, 2)],
        },
        "beats": [round(float(t), 4) for t in beats],
        "meter": db["meter"],
        "downbeat_confidence": db["confidence"],
        "bars": [round(float(t), 4) for t in bars_t],
        "key": {**key, "tuning_cents": round(float(tuning) * 100, 1)},
        "structure": st,
        "loudness_db": round(float(np.mean(rms_db[rms_db > rms_db.max() - 40])), 2),
        "elapsed_sec": round(time.time() - t_start, 1),
    }
