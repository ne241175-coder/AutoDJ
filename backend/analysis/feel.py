"""体感テンポ（アンケートの「ゆったり／速く」用）。

解析の BPM は、速いロック（180〜200）を半分の 90〜100 で拾うことが多い（拍グリッドとしてはそれで正しく、
つなぎもその BPM で合わせている）。けれど聞いた感じは「速い」ので、アンケートだけはこの体感テンポを使う。

判定に使うもの
  - 倍テンポ候補の強さ（解析の tempo.candidates。自己相関で 2倍の周期がどれだけ目立つか）
  - 拍の「裏」（拍と拍のちょうど中間）で、低音（キック・ベース）がどれだけ鳴っているか。
    半分で拾った速い曲では、本当の拍のキックやベースの刻みが裏に来るので、表とほぼ同じ強さで鳴る（取り込み済み13曲で 0.83〜1.4）。
    本当にゆったりした曲は、キックが表にあり、裏はハイハットくらいなので低音は弱い（合成曲で 0.1 前後）。
    スネア帯（150〜2500Hz）も試したが、ハイハットの8分刻みを拾ってしまい区別がつかなかった。

v2: 解析の BPM 候補は 200 までしか探さないので、204 の KICK BACK（解析では 102）は倍の候補が無く、倍にできなかった。
  → 音源から 240 BPM まで探した倍テンポの強さ（double）も持ち、100〜108 の曲も倍にするか考える。
"""
import numpy as np
import librosa

FEEL_VERSION = 2
SLOW_BELOW = 108.0      # これより遅いと判定された曲だけ倍にするか考える（KICK BACK は解析で 102）
MAX_FEEL = 216.0        # 倍にしてもこれ以下（KICK BACK の 204 などは入る）
MIN_DOUBLE_CAND = 0.6   # 倍テンポ候補の強さ（1位=1.0）
LOW_OFF = 0.75          # 裏の低音 / 表の低音 がこれ以上なら「低音が8分で走っている」


def offbeat_features(y, sr, beats, hop=256):
    """拍の表と裏（中間）での低音（〜150Hz）の強さの比"""
    beats = np.asarray(beats, dtype=float)
    beats = beats[beats < len(y) / sr - 0.1]
    if len(beats) < 16:
        return None
    S = librosa.power_to_db(librosa.feature.melspectrogram(
        y=y, sr=sr, n_fft=1024, hop_length=hop, n_mels=64, fmin=30.0, fmax=min(11000.0, sr / 2)), ref=np.max)
    f = librosa.mel_frequencies(n_mels=66, fmin=30.0, fmax=min(11000.0, sr / 2))[1:-1]
    e = librosa.onset.onset_strength(S=S[f < 150], sr=sr, hop_length=hop, n_fft=1024)

    def at(ts):
        fr = librosa.time_to_frames(ts, sr=sr, hop_length=hop)
        fr = fr[fr < len(e) - 2]
        return np.array([e[max(0, x - 2):x + 3].max() for x in fr])

    on, off = at(beats[:-1]), at((beats[:-1] + beats[1:]) / 2)
    bpm = 60.0 / float(np.median(np.diff(beats)))
    oenv = librosa.onset.onset_strength(S=S, sr=sr, hop_length=hop)
    return {"low_off": round(float(np.median(off) / (np.median(on) + 1e-9)), 3),
            "double": round(_double_salience(oenv, sr, hop, bpm), 3), "version": FEEL_VERSION}


def _double_salience(oenv, sr, hop, bpm, lo=60.0, hi=240.0, center=120.0, sigma_oct=0.9):
    """倍テンポ（2×bpm）の強さ ÷ いまの bpm の強さ。rhythm.estimate_bpm と同じ点数の付け方で、240 BPM まで見る"""
    x = oenv - oenv.mean()
    ac = librosa.autocorrelate(x, max_size=min(int(np.ceil(60.0 * sr / (hop * lo))) * 4 + 2, len(x) - 1))
    ac = ac / (ac[0] + 1e-12)
    idx = np.arange(len(ac))
    acv = lambda l: np.interp(l, idx, ac, right=0.0)      # noqa: E731
    bpms = np.arange(lo, hi, 0.05)
    lag = 60.0 * sr / (hop * bpms)
    sal = acv(lag) + 0.5 * acv(2 * lag) + 0.25 * acv(4 * lag) + 0.35 * acv(lag / 2)
    score = np.clip(sal, 0, None) * np.exp(-0.5 * (np.log2(bpms / center) / sigma_oct) ** 2)

    def near(b):
        m = np.abs(bpms / b - 1) < 0.02
        return float(score[m].max()) if m.any() else 0.0
    return near(2 * bpm) / (near(bpm) + 1e-12)


def double_strength(tempo):
    """解析の tempo.candidates から、2倍の BPM 候補の強さ（無ければ 0）"""
    bpm = tempo.get("bpm") or 0
    for b, s in tempo.get("candidates") or []:
        if bpm and abs(b / bpm - 2) < 0.05:
            return float(s)
    return 0.0


def feel_bpm(bpm, tempo, feats):
    """体感 BPM。速い曲を半分で拾っていそうなら倍にする。(bpm, 倍にしたか)"""
    if not bpm or bpm >= SLOW_BELOW or bpm * 2 > MAX_FEEL or not feats:
        return bpm, False
    dbl = max(double_strength(tempo), float(feats.get("double") or 0.0))   # 解析の候補（〜200）か、音源から（〜240）
    if dbl >= MIN_DOUBLE_CAND and feats.get("low_off", 0) >= LOW_OFF:
        return bpm * 2, True
    return bpm, False
