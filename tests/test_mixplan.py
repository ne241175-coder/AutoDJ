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


def test_video_tab_and_strict_title():
    from backend.fetch_songs import choose, video_id_from
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "videos.csv").write_text("曲名,動画URL\nリライト,https://youtu.be/omkKMzR_vZY?si=x\n空欄,\n", encoding="utf-8")
    assert sheet.parse_videos(d / "videos.csv") == {"リライト": "omkKMzR_vZY"}
    # 長さが一致しても、Topic 以外で曲名がタイトルにない動画は選ばない
    E = [{"id": "k", "title": "Kagerou", "duration": 248, "channel": "ONE OK ROCK"}]
    assert choose(E, 248, "ONE OK ROCK", "完全感覚Dreamer") is None
    assert choose([{"id": "m", "title": "【432Hz】ポリリズム", "duration": 252, "channel": "x"}], 252, "Perfume", "ポリリズム") is None


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


def test_replace_old_video_and_partial_csv(tmp_path, monkeypatch):
    import csv as _csv
    from backend import fetch_songs
    from backend.db import connect, get_or_create_set
    con = connect(tmp_path / "t.sqlite3")
    sid = get_or_create_set(con, "sheet")
    for src in ("yt:old", "yt:new"):
        con.execute("INSERT INTO tracks(title, source_id, file_path, genre, set_id, added_at) VALUES (?,?,?,?,?,?)",
                    ("曲X", src, f"/x/{src}.mp3", "J-pop", sid, "now"))
    con.execute("DELETE FROM tracks WHERE source_id='yt:new'")
    monkeypatch.setattr(fetch_songs, "ANALYSIS_DIR", tmp_path)
    fetch_songs._replace_old(con, "曲X", "yt:new", sid, log=lambda *_: None)
    assert con.execute("SELECT COUNT(*) FROM tracks WHERE title='曲X'").fetchone()[0] == 0
    # 途中までの結果を書いても、まだの曲の行は残る
    monkeypatch.setattr(fetch_songs, "SHEET_DIR", tmp_path)
    keep = {"B": {"song": "B", "video_id": "bbb", "title": "", "duration": "", "sheet_end": "", "diff": "", "status": "manual"}}
    fetch_songs._write_rows([{"song": "A", "video_id": "aaa", "title": "", "duration": "", "sheet_end": "",
                              "diff": "", "status": "error"}], keep=keep)
    rows = list(_csv.DictReader(open(tmp_path / "downloads.csv", encoding="utf-8")))
    assert [r["song"] for r in rows] == ["A", "B"] and rows[1]["status"] == "manual"
