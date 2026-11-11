"""アンケート → 最初の曲の候補、そのあとのおすすめ（backend/recommend.py）"""
from backend import recommend as rc


def prof(i, bpm, mode="major", power="mid", start="build", sure=True):
    return {"id": i, "title": f"曲{i}", "bpm": bpm, "tempo": rc._tempo_class(bpm), "mode": mode, "mode_sure": sure,
            "loudness": -10, "power": power, "start": start, "camelot": "8B", "structure": "auto"}


LIB = [prof(1, 175, "major", "loud", "chorus"), prof(2, 172, "minor", "loud", "build"),
       prof(3, 90, "major", "soft", "build"), prof(4, 120, "minor", "mid", "chorus"),
       prof(5, 145, "major", "mid", "chorus"), prof(6, 98, "minor", "soft", "build", sure=False)]


def test_norm_bpm_halves_and_doubles():
    assert rc.norm_bpm(87.5) == 87.5 and rc.norm_bpm(60) == 120 and rc.norm_bpm(240) == 120 and rc.norm_bpm(0) is None


def test_match_is_soft_near_borders():
    assert rc.match("tempo", "fast", prof(0, 175)) == 1.0
    assert 0 < rc.match("tempo", "fast", prof(0, 130)) < 1 and rc.match("tempo", "fast", prof(0, 90)) == 0
    assert rc.match("mood", "bright", prof(0, 100, "minor", sure=False)) == 0.4
    assert rc.match("power", "loud", prof(0, 100, power="mid")) == 0.4 and rc.match("tempo", "any", prof(0, 1)) == 1


def test_pick_roots_3_to_5_and_best_first():
    r = rc.pick_roots({"tempo": "fast", "mood": "bright", "power": "loud", "start": "chorus"}, LIB)
    assert 3 <= len(r) <= 5 and r[0]["id"] == 1 and set(r[0]["hits"]) == {"tempo", "mood", "power", "start"}
    # 何も選ばなければ全曲同点 → 上限の5曲
    assert len(rc.pick_roots({}, LIB)) == 5
    # 同じ答えなら同じ結果
    assert rc.pick_roots({"mood": "dark"}, LIB) == rc.pick_roots({"mood": "dark"}, LIB)


def test_choice_counts():
    c = rc.choice_counts(LIB)
    assert c["tempo"] == {"slow": 2, "mid": 1, "fast": 3} and c["mood"]["dark"] == 3


def test_survey_api_and_next():
    from backend.app import app
    c = app.test_client()
    j = c.get("/api/survey").get_json()
    assert [q["id"] for q in j["questions"]] == ["tempo", "mood", "power", "start"]
    roots = c.post("/api/survey", json={"answers": {"tempo": "fast"}}).get_json()["roots"]
    assert len(roots) <= rc.MAX_ROOTS and (len(roots) >= rc.MIN_ROOTS or len(roots) == j["n_songs"])
    if len(roots) >= 2:
        n = c.get(f"/api/next?after={roots[0]['id']}&exclude={roots[1]['id']}").get_json()
        ids = [x["id"] for x in n["items"]]
        assert roots[0]["id"] not in ids and roots[1]["id"] not in ids
        assert [x["score"] for x in n["items"]] == sorted((x["score"] for x in n["items"]), reverse=True)
    assert c.get("/api/next?after=999999").status_code == 400
