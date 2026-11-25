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


def test_feel_bpm_doubles_only_driving_half_time():
    from backend.analysis.feel import feel_bpm
    tempo = {"bpm": 92.5, "candidates": [[92.3, 1.0], [184.55, 0.78], [61.5, 0.5]]}
    # 低音が裏でも鳴っている（半分で拾った速いロック）→ 倍
    assert feel_bpm(92.5, tempo, {"low_off": 1.0}) == (185.0, True)
    # キックが表だけ（バラード）→ そのまま
    assert feel_bpm(92.5, tempo, {"low_off": 0.1}) == (92.5, False)
    # 倍の候補が弱い／もともと速い／特徴が無い → そのまま
    weak = {"bpm": 92.5, "candidates": [[92.3, 1.0], [184.55, 0.3]]}
    assert feel_bpm(92.5, weak, {"low_off": 1.0})[1] is False
    assert feel_bpm(120, {"bpm": 120, "candidates": [[240, 0.9]]}, {"low_off": 1.0}) == (120, False)
    assert feel_bpm(92.5, tempo, None) == (92.5, False)


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
    assert c["tempo"] == {"slow": 2, "mid": 1, "fast": 3, "any": 6} and c["mood"]["dark"] == 3
    # ほかの質問の答えにも合ったままの数（テンポ=速い のとき、雰囲気の選択肢ごと）
    c = rc.choice_counts(LIB, {"tempo": "fast", "mood": "dark"})
    assert c["mood"] == {"bright": 2, "dark": 1, "any": 3}
    assert c["tempo"]["fast"] == 1 and c["tempo"]["any"] == 3


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


def test_slow_band_becomes_relative_when_library_has_no_slow_songs():
    # 100 未満が 3曲以上あれば、いつもどおり 100 で区切る
    assert rc.tempo_bands([80, 90, 95, 120, 150])["slow"] == (0.0, 100.0)
    # 速い曲ばかり（J-pop）だと「ゆったり」が 0曲になるので、遅い方から 2割にする
    b = rc.tempo_bands([116, 118, 120, 124, 128, 135, 150, 160, 170, 180])
    assert b["relative"] and 116 < b["slow"][1] <= 130 and b["mid"][0] == b["slow"][1]
    assert rc._tempo_class(116, b) == "slow" and rc._tempo_class(150, b) == "fast"


def test_feel_bpm_uses_wide_range_double_strength():
    """解析の候補（〜200）に倍が無い 102 BPM の曲（KICK BACK）も、音源から測った倍の強さで倍にできる"""
    from backend.analysis.feel import feel_bpm
    tempo = {"bpm": 101.85, "candidates": [[101.85, 1.0], [136.0, 0.55], [68.0, 0.54]]}
    assert feel_bpm(101.85, tempo, {"low_off": 0.93, "double": 0.63}) == (203.7, True)
    assert feel_bpm(101.85, tempo, {"low_off": 0.93}) == (101.85, False)          # 倍の手がかりが無ければそのまま
    assert feel_bpm(101.85, tempo, {"low_off": 0.2, "double": 0.9}) == (101.85, False)  # 低音が表だけならそのまま


def test_ties_prefer_clear_matches_then_seed():
    lib = [prof(i, 145 + i * 8) for i in range(6)]           # 全部「速い」（145〜185）
    r = rc.pick_roots({"tempo": "fast"}, lib)
    assert [x["id"] for x in r][:2] == [5, 4]                # 速い曲ほど上
    flat = [prof(i, 120) for i in range(8)]                  # 完全に同点
    a, b = rc.pick_roots({}, flat, seed="x"), rc.pick_roots({}, flat, seed="x")
    assert a == b                                            # 同じ seed なら同じ
    assert any(rc.pick_roots({}, flat, seed=str(k)) != a for k in range(5))   # seed が変われば変わる


def test_bpm_reason_shows_compared_tempo():
    assert rc._bpm_why(157, 146) == "BPM 157→146"
    assert rc._bpm_why(157, 85) == "BPM 157→170（85×2）"      # 半分のテンポの曲は倍にして比べている
    assert rc._bpm_why(90, 175) == "BPM 90→88（175÷2）"


def test_route_successors_from_sheet_and_app(monkeypatch):
    """人が聞いて決めたルートの「次の曲」（シートの曲リスト）。おすすめでは一番上に出す"""
    from backend import sheet
    monkeypatch.setattr(sheet, "load_cached", lambda: ({}, {"routes": [
        {"root": "LOSER", "middle": "STAY TUNE", "leaf": "君のまま", "memo": "2:01~と0:16~"},
        {"root": "LOSER", "middle": "踊り子", "leaf": "祝祭", "memo": ""}]}))
    monkeypatch.setattr("backend.db.list_routes", lambda con: [])
    assert rc.route_successors("LOSER", con=object()) == {"STAY TUNE": "", "踊り子": ""}
    assert rc.route_successors("STAY TUNE", con=object()) == {"君のまま": "2:01~と0:16~"}


def test_preference_from_survey_answers():
    """2・3曲目も、アンケートの希望（テンポ・雰囲気など）にどれだけ合うかを見る"""
    fast_bright, slow_dark = prof(1, 175, "major", "loud"), prof(2, 90, "minor", "soft")
    ans = {"tempo": "fast", "mood": "bright", "power": "any"}
    assert rc.preference(ans, fast_bright) == 1.0
    assert rc.preference(ans, slow_dark) == 0.0
    assert rc.preference({}, fast_bright) is None and rc.preference({"tempo": "any"}, fast_bright) is None
