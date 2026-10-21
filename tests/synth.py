"""正解が分かっている合成曲を作る（解析精度の自動テスト用）。

構成（小節）: intro 8（パッド+ハット） / main 16（全部） / break 8（パッドのみ）
             / main 16 / outro 8（キック+ハット+ベース）
1小節1コード。キーは引数で指定。
"""
import numpy as np

SR = 22050
MAJOR_PROG = [0, 5, 7, 0]        # I IV V I
MINOR_PROG = [0, 5, 7, 0]        # i iv V i（Vは長三和音＝導音あり）
SECTIONS = [("intro", 8), ("main", 16), ("break", 8), ("main", 16), ("outro", 8)]


def _tone(freq, dur, sr, harmonics=6, decay=0.0):
    t = np.arange(int(dur * sr)) / sr
    x = sum(np.sin(2 * np.pi * freq * h * t) / h for h in range(1, harmonics + 1))
    env = np.exp(-decay * t) if decay else np.minimum(1, t * 50) * np.minimum(1, (dur - t) * 50)
    return x * env


def _kick(sr):
    t = np.arange(int(0.25 * sr)) / sr
    f = 50 + 100 * np.exp(-t * 40)
    return np.sin(2 * np.pi * np.cumsum(f) / sr) * np.exp(-t * 18)


def _noise(dur, sr, decay, seed, hp=False):
    rng = np.random.default_rng(seed)
    n = rng.standard_normal(int(dur * sr))
    if hp:
        n = np.diff(n, prepend=0)
    return n * np.exp(-np.arange(len(n)) / sr * decay)


def chord_pcs(root, minor_triad):
    return [root, root + (3 if minor_triad else 4), root + 7]


def make_track(bpm=124.0, tonic=2, mode="minor", lead_in=0.37, sr=SR, drift=0.0):
    """drift>0 で人間の演奏のようなテンポ揺れ（拍位置を ±drift 秒、64拍周期で揺らす）"""
    beat = 60.0 / bpm
    bar = 4 * beat
    total_bars = sum(n for _, n in SECTIONS)

    def tmap(bp):  # 拍位置 → 時刻
        return lead_in + bp * beat + drift * np.sin(2 * np.pi * bp / 64)

    y = np.zeros(int((tmap(total_bars * 4) + 1.0 + drift) * sr))
    prog = MINOR_PROG if mode == "minor" else MAJOR_PROG
    # 各度数の三和音の長短（ダイアトニック）
    minor_quality = {0: True, 5: True, 7: False} if mode == "minor" else {0: False, 5: False, 7: False}

    kick, snare = _kick(sr), _noise(0.18, sr, 25, 1)
    hat = _noise(0.05, sr, 80, 2, hp=True)

    def add(sig, t, gain):
        i = int(t * sr)
        j = min(len(y), i + len(sig))
        y[i:j] += gain * sig[:j - i]

    bar_idx = 0
    boundaries = []
    for name, nbars in SECTIONS:
        boundaries.append(bar_idx)
        for b in range(nbars):
            t0 = tmap((bar_idx + b) * 4)
            bar_dur = tmap((bar_idx + b) * 4 + 4) - t0
            deg = prog[b % 4]
            root = (tonic + deg) % 12
            pcs = chord_pcs(root, minor_quality[deg])
            pad_gain = 0.10 if name != "outro" else 0.0
            for pc in pcs:
                add(_tone(220 * 2 ** ((pc - 9) / 12), bar_dur, sr), t0, pad_gain)
            if name in ("main", "outro"):
                add(_tone(55 * 2 ** (((root - 9) % 12) / 12), bar_dur, sr, harmonics=3), t0, 0.25)
            for k in range(4):
                bp = (bar_idx + b) * 4 + k
                tb = tmap(bp)
                half = (tmap(bp + 1) - tb) / 2
                if name in ("main", "outro"):
                    add(kick, tb, 0.9)
                if name == "main" and k in (1, 3):
                    add(snare, tb, 0.35)
                if name in ("intro", "main", "outro"):
                    add(hat, tb + half, 0.12)
                    add(hat, tb, 0.08)
            if name == "main" and b == 0:
                add(_noise(1.5, sr, 3, 3, hp=True), t0, 0.15)  # クラッシュ
        bar_idx += nbars
    y /= np.abs(y).max() + 1e-9
    truth = {"bpm": bpm, "tonic": tonic, "mode": mode, "first_downbeat": lead_in,
             "beats": np.array([tmap(i) for i in range(total_bars * 4)]),
             "boundaries": boundaries + [total_bars], "total_bars": total_bars}
    return y.astype(np.float32), truth
