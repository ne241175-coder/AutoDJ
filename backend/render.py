"""再生計画どおりにミックス音源を書き出す（動作確認用）。

ファイルは切らない: librosa.load(offset=開始秒, duration=長さ) で、mp3 の必要な部分だけを読み込む。
UI（ブラウザ）でも同じく AudioBufferSourceNode.start(when, offset) で「この秒から再生」する想定。

つなぎ方
- 出る曲の out 秒と入る曲の in 秒は、どちらも小節頭。
- 重ねる長さ（overlap_beats 拍）だけ手前から、入る曲をフェードイン・出る曲をフェードアウトする。
- テンポが近い場合は、重なり部分だけ入る曲を出る曲のテンポに合わせて伸縮し、拍を揃える。
- 低音の衝突を避けるため、重なりの前半は入る曲の低音をカットし、後半で出る曲の低音をカットする。
"""
import shutil
import subprocess
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf
from scipy.signal import butter, sosfiltfilt

SR = 44100


def _load(path, offset, duration):
    """mp3 の offset 秒から duration 秒だけ読む（ファイルは切らない）。0 秒より前は無音で埋める"""
    pad = max(0.0, -offset)
    y, _ = librosa.load(str(path), sr=SR, mono=False, offset=max(0.0, offset),
                        duration=max(0.01, duration - pad))
    if y.ndim == 1:
        y = np.stack([y, y])
    if pad:
        y = np.concatenate([np.zeros((2, int(pad * SR)), dtype=y.dtype), y], axis=1)
    return y


def _fit(y, n):
    if y.shape[1] >= n:
        return y[:, :n]
    return np.pad(y, ((0, 0), (0, n - y.shape[1])))


def _highpass(y, hz=180):
    sos = butter(4, hz, btype="highpass", fs=SR, output="sos")
    return sosfiltfilt(sos, y, axis=1)


def _stretch(y, rate):
    if abs(rate - 1) < 1e-3:
        return y
    return np.stack([librosa.effects.time_stretch(ch, rate=rate) for ch in y])


def _playback_plan(plan):
    """BPM・キーを合わせた計画なら、加工済みの音声とその秒数に置き換える"""
    if not any(it.get("rate", 1.0) != 1.0 or it.get("semitones", 0) for it in plan["items"]):
        return plan["items"], plan["transitions"]
    from .audio_fx import processed
    items = []
    for it in plan["items"]:
        x = dict(it)
        x["file"] = str(processed(it["file"], it["track_id"], it.get("rate", 1.0), it.get("semitones", 0)))
        x["start"], x["end"] = it.get("pb_start", it["start"]), it.get("pb_end", it["end"])
        items.append(x)
    trans = []
    for t in plan["transitions"]:
        x = dict(t)
        x["out"], x["in"] = t.get("pb_out", t["out"]), t.get("pb_in", t["in"])
        trans.append(x)
    return items, trans


def render(plan, out_path, gain_db=-1.0):
    items, trans = _playback_plan(plan)
    pieces = []  # 時系列に並ぶ音声
    cue = []
    t_mix = 0.0
    cut_in_next = False
    for i, it in enumerate(items):
        start, end = it["start"], it["end"]
        nxt = trans[i] if i < len(trans) else None
        # この曲が単独で鳴る区間: start 〜 次の重なりの手前（入りの重なりは前の曲の処理で生成済み）
        L_out = (nxt["overlap_beats"] * 60.0 / it["bpm"]) if nxt else 0.0
        body = _load(it["file"], start, max(0.01, end - start - L_out))
        if cut_in_next:
            k = min(int(0.015 * SR), body.shape[1])
            body = body.copy()
            body[:, :k] *= np.linspace(0, 1, k)
            cut_in_next = False
        pieces.append(body)
        cue.append((t_mix, it["title"], start))
        t_mix += body.shape[1] / SR

        if nxt and nxt["overlap_beats"] <= 0:
            # カットイン: 小節頭でそのまま切り替える（プチッと鳴らないよう 15ms だけ音量を絞る）
            k = min(int(0.015 * SR), body.shape[1])
            if k:
                pieces[-1] = body.copy()
                pieces[-1][:, -k:] *= np.linspace(1, 0, k)
            cut_in_next = True
            continue
        if nxt:
            b = items[i + 1]
            n = int(round(L_out * SR))
            a_tail = _fit(_load(it["file"], end - L_out, L_out), n)
            # 入る曲: in の手前 overlap_beats 拍（入る曲のテンポで）を読み、出る曲のテンポに伸縮
            beats = nxt["overlap_beats"]
            L_b = beats * 60.0 / b["bpm"]
            b_head = _load(b["file"], max(0.0, nxt["in"] - L_b), L_b)
            if nxt["tempo_gap"] <= 0.06:
                b_head = _stretch(b_head, rate=(b_head.shape[1] / SR) / L_out)
            b_head = _fit(b_head, n)
            ramp = np.linspace(0, 1, n)
            fade_in, fade_out = np.sin(ramp * np.pi / 2), np.cos(ramp * np.pi / 2)
            half = (ramp >= 0.5).astype(float)
            b_mix = _highpass(b_head) * (1 - half) + b_head * half      # 前半は入る曲の低音を切る
            a_mix = a_tail * (1 - half) + _highpass(a_tail) * half      # 後半は出る曲の低音を切る
            pieces.append(a_mix * fade_out + b_mix * fade_in)
            t_mix += n / SR
            # 次の曲の body は in 秒から始まる（items[i+1]["start"] == in）
    y = np.concatenate(pieces, axis=1)
    peak = np.abs(y).max() + 1e-9
    y = y / peak * (10 ** (gain_db / 20))

    out_path = Path(out_path)
    wav = out_path.with_suffix(".wav")
    sf.write(wav, y.T, SR)
    if out_path.suffix.lower() == ".mp3" and shutil.which("ffmpeg"):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav), "-b:a", "256k", str(out_path)], check=True)
        wav.unlink()
    else:
        out_path = wav
    cue_txt = out_path.with_suffix(".txt")
    cue_txt.write_text("\n".join(f"{int(t // 60)}:{t % 60:04.1f}  {title}（{int(s // 60)}:{s % 60:04.1f} から）"
                                 for t, title, s in cue), encoding="utf-8")
    return out_path, cue_txt
