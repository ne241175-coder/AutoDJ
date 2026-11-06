"""区切り確率モデルの学習（合成曲で仕組みが動くこと）。"""
import json
import numpy as np
import soundfile as sf

from backend.analysis.boundary_model import pick_boundaries, FEATURE_NAMES, CONTEXT
from backend.analysis.pipeline import analyze_file
from backend.learn import build_dataset, cross_validate, train_and_save
from tests.synth import make_track, SR


def test_pick_boundaries_threshold_and_gap():
    p = np.zeros(40); p[[8, 9, 16, 30]] = [0.9, 0.95, 0.8, 0.6]
    assert pick_boundaries(p, 0.75, min_gap=4) == [0, 9, 16, 40]   # 0.6 は採らない、8 は 9 と近すぎ


def test_learn_on_synthetic(tmp_path):
    pairs = []
    for i, (bpm, tonic, mode) in enumerate([(120.0, 0, "major"), (100.0, 5, "minor"), (132.0, 9, "minor"), (110.0, 2, "major")]):
        y, truth = make_track(bpm=bpm, tonic=tonic, mode=mode)
        p = tmp_path / f"{i}.wav"
        sf.write(p, y, SR)
        a = analyze_file(p)
        bar = 240.0 / bpm
        times = [truth["first_downbeat"] + b * bar for b in truth["boundaries"][1:-1]]
        pairs.append((i, a, times))
    data = build_dataset(pairs)
    assert len(data) == 4 and all(d["X"].shape[1] == len(FEATURE_NAMES) for d in data)
    cv = cross_validate(data)                      # しきい値は自動で選ぶ
    assert cv["model_f"] >= 0.7, cv
    assert cv["threshold"] in cv["f_by_threshold"]
    m, _ = train_and_save(data, path=tmp_path / "model.json", cv=cv)
    saved = json.loads((tmp_path / "model.json").read_text(encoding="utf-8"))
    assert saved["feature_names"] == FEATURE_NAMES
    assert len(saved["coef"]) == len(FEATURE_NAMES) * (2 * CONTEXT + 1)


def test_estimate_offset(tmp_path):
    from backend.align import estimate_offset, aligned_sections
    y, truth = make_track(bpm=118.0, tonic=7, mode="major")
    p = tmp_path / "a.wav"
    sf.write(p, y, SR)
    a = analyze_file(p)
    bar = 240.0 / 118.0
    audio_times = [truth["first_downbeat"] + b * bar for b in truth["boundaries"][:-1]]
    for shift in (7.3, -4.0, 0.0):
        # シートは「音源より shift 秒だけ前（後）」で計った、という状況
        sheet = [{"start": t - shift, "end": None, "role": "verse", "label": "A"} for t in audio_times]
        est = estimate_offset(sheet, a)
        assert abs(est["offset"] - shift) <= 0.6, (shift, est)
    secs, off = aligned_sections({"sections": [{"start": t - 7.3, "end": t, "role": "verse", "label": "A"}
                                               for t in audio_times], "end": None}, a)
    assert abs(off - 7.3) <= 0.6 and abs(secs[1]["start"] - audio_times[1]) <= 0.6
