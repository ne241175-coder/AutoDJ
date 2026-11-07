"""Web アプリ（API と画面）の簡単な確認。"""
from backend.app import app


def test_index_and_tracks():
    c = app.test_client()
    r = c.get("/")
    assert r.status_code == 200 and b'id="root"' in r.data
    r = c.get("/api/tracks")
    assert r.status_code == 200 and isinstance(r.get_json(), list)
    assert c.get("/static/vendor/react.production.min.js").status_code == 200


def test_plan_needs_two_songs():
    c = app.test_client()
    r = c.post("/api/plan", json={"songs": [1]})
    assert r.status_code == 400 and "2曲以上" in r.get_json()["error"]


def test_audio_404_for_unknown():
    assert app.test_client().get("/api/audio/999999").status_code == 404


def test_styles_change_overlap():
    from backend.mixplan import apply_styles
    plan = {"items": [{"start": 10, "end": 120, "bpm": 120}, {"start": 30, "end": 150, "bpm": 120},
                      {"start": 20, "end": 140, "bpm": 90}],
            "transitions": [{"in": 30, "tempo_gap": 0.0, "overlap_beats": 4},
                            {"in": 20, "tempo_gap": 0.3, "overlap_beats": 1}]}
    apply_styles(plan, ["fade", "cut"])
    t0, t1 = plan["transitions"]
    assert t0["style"] == "fade" and t0["overlap_beats"] == 16
    assert t1["style"] == "cut" and t1["overlap_beats"] == 0
    apply_styles(plan, [])
    assert all(t["style"] == "auto" for t in plan["transitions"])


def test_render_cut_in(tmp_path):
    import numpy as np
    import soundfile as sf
    from backend.render import render
    sr = 22050
    files = []
    for k, f in enumerate((220.0, 330.0)):
        t = np.arange(sr * 6) / sr
        p = tmp_path / f"s{k}.wav"
        sf.write(p, 0.3 * np.sin(2 * np.pi * f * t), sr)
        files.append(str(p))
    plan = {"items": [{"title": "a", "file": files[0], "start": 0.0, "end": 3.0, "bpm": 120},
                      {"title": "b", "file": files[1], "start": 2.0, "end": 5.0, "bpm": 120}],
            "transitions": [{"in": 2.0, "overlap_beats": 0, "tempo_gap": 0.0}]}
    out, _ = render(plan, tmp_path / "mix.wav")
    y, osr = sf.read(out)
    assert abs(len(y) / osr - 6.0) < 0.05   # 重ならずにそのままつながる（3秒＋3秒）


def test_wave_and_jacket_404():
    c = app.test_client()
    assert c.get("/api/tracks/999999/wave").status_code == 404
    assert c.get("/api/jacket/999999").status_code == 404
    assert c.get("/classic").status_code == 200


def test_render_overlap_from_song_start(tmp_path):
    """入る位置が曲の頭（in=0）でも、重なりの分は無音で埋め、入る曲の頭を二重に鳴らさない"""
    import numpy as np
    import soundfile as sf
    from backend.render import render
    sr = 22050
    files = []
    for k in range(2):
        y = np.zeros(sr * 6)
        if k == 1:
            y[: sr // 10] = 0.5          # 入る曲の頭 0.1 秒だけ音（ここが1回だけ鳴るはず）
        else:
            y[:] = 0.2 * np.sin(2 * np.pi * 220 * np.arange(sr * 6) / sr)
        p = tmp_path / f"s{k}.wav"
        sf.write(p, y, sr)
        files.append(str(p))
    plan = {"items": [{"title": "a", "file": files[0], "start": 0.0, "end": 4.0, "bpm": 120},
                      {"title": "b", "file": files[1], "start": 0.0, "end": 3.0, "bpm": 120}],
            "transitions": [{"in": 0.0, "overlap_beats": 4, "tempo_gap": 0.0}]}   # 4拍 = 2秒重ねる
    out, _ = render(plan, tmp_path / "mix.wav")
    y, osr = sf.read(out)
    assert abs(len(y) / osr - 7.0) < 0.05          # 4秒 + 3秒（重なりは出る曲の最後の2秒に入る曲の「頭の前の無音」）
    loud = np.abs(y).max(axis=1) if y.ndim == 2 else np.abs(y)
    # 入る曲の頭（0.5 の音）は mix の 4.0 秒付近に1回だけ。重なり区間（2〜4秒）には出てこない
    seg = loud[int(2.05 * osr):int(3.95 * osr)]
    assert seg.max() < loud[int(4.0 * osr):int(4.1 * osr)].max()


def test_render_api_needs_two_songs():
    r = app.test_client().post("/api/render", json={"songs": [1]})
    assert r.status_code == 400 and "2曲以上" in r.get_json()["error"]
