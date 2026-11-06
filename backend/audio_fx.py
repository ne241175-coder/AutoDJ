"""テンポを変える（音程はそのまま）・移調する（テンポはそのまま）加工をした音声を作る。

ffmpeg を使う。rubberband フィルタが使えればそれ（高音質）、なければ asetrate＋atempo で代用。
作った音声は data/cache/fx/ に保存して使い回す（元の mp3 は変えない）。
"""
import functools
import shutil
import subprocess
from pathlib import Path

from .config import DATA_DIR

FX_DIR = DATA_DIR / "cache" / "fx"


@functools.lru_cache(maxsize=1)
def has_rubberband():
    if not shutil.which("ffmpeg"):
        return False
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return " rubberband " in out


def _atempo_chain(f):
    """atempo は 0.5〜2.0 の範囲で使う（外れるときは分けてつなぐ）"""
    parts = []
    while f > 2.0:
        parts.append(2.0)
        f /= 2.0
    while f < 0.5:
        parts.append(0.5)
        f /= 0.5
    parts.append(f)
    return ",".join(f"atempo={p:.6f}" for p in parts)


def fx_filter(rate, semitones, sr=44100):
    pitch = 2 ** (semitones / 12)
    if has_rubberband():
        return f"rubberband=tempo={rate:.6f}:pitch={pitch:.6f}:transients=crisp:formant=preserved"
    # 代用: サンプルレートを変えて音程を上げ下げし、atempo でテンポを戻す
    return f"asetrate={sr}*{pitch:.6f},aresample={sr}," + _atempo_chain(rate / pitch)


def processed(src, track_id, rate=1.0, semitones=0):
    """加工済み音声のパス。rate=1, semitones=0 なら元のファイル"""
    if abs(rate - 1) < 1e-4 and semitones == 0:
        return Path(src)
    FX_DIR.mkdir(parents=True, exist_ok=True)
    out = FX_DIR / f"{track_id}_r{rate:.5f}_s{semitones:+d}.mp3"
    if out.exists() and out.stat().st_mtime >= Path(src).stat().st_mtime:
        return out
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg が見つかりません（brew install ffmpeg）")
    tmp = out.with_suffix(".tmp.mp3")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-af", fx_filter(rate, semitones),
                    "-ar", "44100", "-b:a", "256k", str(tmp)], check=True)
    tmp.replace(out)
    return out
