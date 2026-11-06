"""合成曲（正解既知）で解析精度を検証する。  実行: python -m pytest -q tests"""
import numpy as np
import pytest
import soundfile as sf

from backend.analysis import key as keymod
from backend.analysis.pipeline import analyze_file
from tests.synth import make_track, SR


@pytest.mark.parametrize("bpm,tonic,mode", [(92.0, 7, "major"), (124.0, 2, "minor"), (174.0, 10, "minor")])
def test_full_pipeline(tmp_path, bpm, tonic, mode):
    y, truth = make_track(bpm=bpm, tonic=tonic, mode=mode, lead_in=0.37)
    p = tmp_path / "t.wav"
    sf.write(p, y, SR)
    r = analyze_file(p)

    # テンポ: ±0.5%
    assert abs(r["tempo"]["bpm"] - bpm) / bpm < 0.005
    # 小節頭: 最初の小節頭が ±20ms
    assert abs(r["bars"][0] - truth["first_downbeat"]) < 0.02
    assert r["meter"] == 4
    # キー
    assert (r["key"]["tonic"], r["key"]["mode"]) == (tonic, mode)
    # 構造: 正解境界がすべて ±1小節以内で検出される
    found = [s["start_bar"] for s in r["structure"]["sections"]]
    for b in truth["boundaries"][:-1]:
        assert min(abs(b - f) for f in found) <= 1, (b, found)
    roles = [s["role"] for s in r["structure"]["sections"]]
    assert roles[0] == "intro" and "break" in roles


def _harmonic_only(tonic, mode, bars=8, bar_sec=2.0):
    from tests.synth import MAJOR_PROG, MINOR_PROG, chord_pcs, _tone
    prog = MINOR_PROG if mode == "minor" else MAJOR_PROG
    q = {0: mode == "minor", 5: mode == "minor", 7: False}
    out = []
    for b in range(bars):
        deg = prog[b % 4]
        root = (tonic + deg) % 12
        x = sum(_tone(220 * 2 ** ((pc - 9) / 12), bar_sec, SR) for pc in chord_pcs(root, q[deg]))
        x += 0.8 * _tone(55 * 2 ** (((root - 9) % 12) / 12), bar_sec, SR, harmonics=3)
        out.append(x)
    return np.concatenate(out).astype(np.float32)


def test_key_all_24():
    wrong = []
    for mode in ("major", "minor"):
        for tonic in range(12):
            y = _harmonic_only(tonic, mode)
            chroma, _ = keymod.harmonic_chroma(y, SR, 512)
            k = keymod.estimate_key(chroma, SR, 512)
            if (k["tonic"], k["mode"]) != (tonic, mode):
                wrong.append((keymod.key_name(tonic, mode), k["name"]))
    assert not wrong, wrong


def test_camelot():
    assert keymod.camelot(0, "major") == "8B"
    assert keymod.camelot(9, "minor") == "8A"
    assert keymod.camelot(7, "major") == "9B"
    assert keymod.camelot(2, "minor") == "7A"
    assert keymod.camelot_distance("8A", "8B") == 1
    assert keymod.camelot_distance("12B", "1B") == 1


def _beat_hit_rate(detected, truth, tol=0.05):
    detected = np.asarray(detected)
    errs = np.array([np.min(np.abs(detected - t)) for t in truth])
    return float((errs < tol).mean()), float(np.median(errs))


@pytest.mark.parametrize("drift", [0.08, 0.15])
def test_variable_tempo_follows_drift(tmp_path, drift):
    """クリックなしの生演奏を模したテンポ揺れでも、拍が±50ms以内で追従する"""
    y, truth = make_track(bpm=110.0, tonic=4, mode="major", drift=drift)
    p = tmp_path / "v.wav"
    sf.write(p, y, SR)
    r = analyze_file(p)
    assert not r["tempo"]["stable"]
    assert abs(r["tempo"]["bpm"] - 110.0) < 1.5
    # ドラムのある区間（main: 拍32-95, 128-191 / outro: 192-223）で追従していること。
    # ドラムなしの intro/break は手がかりがなく補間になる（uncertain_beat_ranges に出る）
    tb = truth["beats"]
    drummed = np.concatenate([tb[32:96], tb[128:224]])
    hit, med = _beat_hit_rate(r["beats"], drummed)
    assert hit > 0.95, (hit, med, r["tempo"])
    assert r["tempo"]["uncertain_beat_ranges"], "ドラムなし区間が不確実として報告されていない"
    first_unc = r["tempo"]["uncertain_beat_ranges"][0]
    assert first_unc[0] < tb[8]


def test_limit_sections():
    from backend.analysis.structure import limit_sections
    bounds = list(range(0, 80, 4))           # 19 区間
    strength = {b: (b % 8 == 0) * 1.0 + b / 1000 for b in bounds}
    out, removed = limit_sections(bounds, strength, max_sections=14)
    assert len(out) - 1 == 14 and removed == 5 and out[0] == 0 and out[-1] == 76
    assert all(b % 8 != 0 for b in set(bounds) - set(out))  # 外れるのは弱い（8の倍数でない）境界だけ
