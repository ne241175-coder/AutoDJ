"""つなぎ計画とミックス書き出しのテスト（合成曲）。  実行: python -m pytest -q tests"""
import numpy as np
import pytest
import soundfile as sf

from backend import sheet
from backend.analysis.pipeline import analyze_file
from backend.mixplan import Song, AUTO_ROLE, ROLE_JA, plan_chain, describe
from tests.synth import make_track, SR


def _song(tmp_path, idx, bpm, tonic, mode, sheet_sections=None):
    y, truth = make_track(bpm=bpm, tonic=tonic, mode=mode)
    p = tmp_path / f"s{idx}.wav"
    sf.write(p, y, SR)
    a = analyze_file(p)
    if sheet_sections is None:
        secs = [{"role": AUTO_ROLE.get(s["role"], "other"), "label": ROLE_JA.get(AUTO_ROLE.get(s["role"], "other")),
                 "start": s["start"], "end": s["end"]} for s in a["structure"]["sections"]]
        src = "auto"
    else:
        secs, src = sheet_sections(truth, a), "sheet"
    return Song(idx, f"song{idx}", str(p), a["duration"], a["tempo"]["bpm"], a["key"]["camelot"],
                a["bars"], secs, src), truth


@pytest.fixture(scope="module")
def songs(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("mix")
    return [_song(tmp, i, bpm, tonic, mode) for i, (bpm, tonic, mode) in
            enumerate([(124.0, 2, "minor"), (126.0, 9, "minor"), (124.0, 4, "minor")])]


def test_min_seconds_respected(songs):
    ss = [s for s, _ in songs]
    plan = plan_chain(ss, min_sec=60, chorus_grace=0)   # サビ終わりの猶予なしで厳密に
    print(describe(plan))
    for it in plan["items"][1:-1]:
        assert it["end"] - it["start"] >= 60 - 1e-6
    # つなぎ位置は小節頭（解析した bars のどれか）
    for t, (a, b) in zip(plan["transitions"], zip(ss, ss[1:])):
        assert np.min(np.abs(np.array(a.bars) - t["out"])) < 1e-3
        assert np.min(np.abs(np.array(b.bars) - t["in"])) < 1e-3


def test_longer_min_sec_moves_exit_later(songs):
    ss = [s for s, _ in songs]
    short = plan_chain(ss, min_sec=20)
    long = plan_chain(ss, min_sec=60)
    assert long["items"][1]["end"] - long["items"][1]["start"] >= short["items"][1]["end"] - short["items"][1]["start"]


def test_manual_link_wins(songs):
    ss = [s for s, _ in songs]
    a, b = ss[0], ss[1]
    out_t, in_t = a.bars[20], b.bars[8]
    plan = plan_chain(ss, min_sec=30, manual_links={(0, 1): {"out": out_t + 0.4, "in": in_t - 0.3}})
    t = plan["transitions"][0]
    assert t["manual"] and abs(t["out"] - out_t) < 1e-3 and abs(t["in"] - in_t) < 1e-3


def test_render_reads_offsets_without_splitting(songs, tmp_path):
    from backend.render import render
    ss = [s for s, _ in songs]
    plan = plan_chain(ss, min_sec=40)
    out, cue = render(plan, tmp_path / "mix.wav")
    y, sr = sf.read(out)
    expected = sum(it["end"] - it["start"] for it in plan["items"])
    assert abs(len(y) / sr - expected) < 0.5, (len(y) / sr, expected)
    assert cue.read_text(encoding="utf-8").count("song") == 3


def test_sheet_parsing():
    s = sheet.parse_structure("tests/fixtures/structure.csv")
    assert set(s) == {"前前前世", "リライト"}            # 終了のない 革命道中 は除外
    z = s["前前前世"]["sections"]
    assert z[4]["label"] == "サビ" and z[4]["start"] == 69 and z[4]["end"] == 95
    l = sheet.parse_song_list("tests/fixtures/songs.csv")
    r = {x["leaf"]: x for x in l["routes"]}
    assert r["リライト"]["link_mid_leaf"] == {"out": 111, "in": 75}
    assert r["全力少年"]["link_mid_leaf"] is None
    assert l["songs"]["前前前世"] == "前前前世 RADWIMPS"


def test_chorus_grace_allows_earlier_exit_only_at_chorus(songs):
    ss = [s for s, _ in songs]
    strict = plan_chain(ss, min_sec=70, chorus_grace=0)
    loose = plan_chain(ss, min_sec=70, chorus_grace=20)
    mid_s = strict["items"][1]["end"] - strict["items"][1]["start"]
    mid_l = loose["items"][1]["end"] - loose["items"][1]["start"]
    assert mid_s >= 70 - 1e-6
    assert mid_l <= mid_s
    # 猶予を使って早めに出る場合は、サビ終わりからだけ
    for t, it in zip(loose["transitions"], loose["items"]):
        if it["end"] - it["start"] < 70 - 1e-6:
            assert t["kind"].startswith("サビ終わり"), t


def test_matching_bpm_and_key():
    from types import SimpleNamespace
    from backend.matching import apply_matching, tempo_ratio, best_semitones
    assert abs(tempo_ratio(128, 124) - 128 / 124) < 1e-9
    assert abs(tempo_ratio(140, 71) - 140 / 142) < 1e-9        # 倍テンポも同一視
    assert tempo_ratio(128, 100) is None                          # ±8% を超えたら合わせない
    assert best_semitones("8B", 7, "major")[1] <= 1               # G major(9B) → 隣か同じに
    songs = [SimpleNamespace(bpm=120.0, camelot="8B", tonic=0, mode="major"),     # C major
             SimpleNamespace(bpm=124.0, camelot="10B", tonic=2, mode="major")]    # D major
    plan = {"items": [{"start": 0.0, "end": 100.0}, {"start": 20.0, "end": 120.0}],
            "transitions": [{"from": 0, "to": 1, "out": 100.0, "in": 20.0}]}
    apply_matching(plan, songs)
    b = plan["items"][1]
    assert abs(b["rate"] - 120 / 124) < 1e-4 and abs(b["bpm"] - 120) < 0.01
    assert b["semitones"] == -2 and b["camelot"] == "8B"          # D → C で同じキー
    assert abs(plan["transitions"][0]["pb_in"] - 20.0 / b["rate"]) < 1e-3
    apply_matching(plan, songs, match_bpm=False, match_key=False)
    assert plan["items"][1]["rate"] == 1.0 and plan["items"][1]["semitones"] == 0


def test_audio_fx_keeps_length(tmp_path):
    import numpy as np
    import soundfile as sf
    from backend import audio_fx
    sr = 44100
    t = np.arange(sr * 4) / sr
    src = tmp_path / "a.wav"
    sf.write(src, 0.3 * np.sin(2 * np.pi * 440 * t), sr)
    audio_fx.FX_DIR = tmp_path / "fx"
    out = audio_fx.processed(src, 1, rate=1.25, semitones=2)
    y, osr = sf.read(out)
    assert abs(len(y) / osr - 4 / 1.25) < 0.15                   # テンポだけ変わる
    spec = np.abs(np.fft.rfft(y[int(osr * 0.5):int(osr * 2.5)] if y.ndim == 1 else y[int(osr * 0.5):int(osr * 2.5), 0]))
    f = np.argmax(spec) * osr / (2 * osr)
    assert abs(f - 440 * 2 ** (2 / 12)) < 8                      # 音程は +2半音


def test_link_score_is_features_times_weights(songs):
    """つなぎ候補の点数 = 手がかり × 重み。重みを変えると選ばれる候補も変わる"""
    from backend import linkmodel as lm
    from backend.mixplan import candidate_links
    a, b = songs[0][0], songs[1][0]
    cands = candidate_links(a, b, weights=lm.DEFAULT_WEIGHTS)
    assert cands and all(set(c.features) == set(lm.FEATURES) for c in cands)
    for c in cands:
        assert c.score == pytest.approx(lm.score(c.features, lm.DEFAULT_WEIGHTS))
    plan = plan_chain([s for s, _ in songs], min_sec=30)
    t = plan["transitions"][0]
    assert t["features"] and all("features" in x for x in t["alternatives"]) and plan["model"]["source"] in ("default", "learned")
    # 「曲の頭から入る」を強く好む重みにすると、入る位置の点数の順位が変わる
    w = dict(lm.DEFAULT_WEIGHTS, in_start=1.0)
    best = max(candidate_links(a, b, weights=w), key=lambda c: c.score)
    assert best.features["in_start"] == 1.0
