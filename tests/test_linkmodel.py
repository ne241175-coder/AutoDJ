"""評価から、つなぎ目の点数の重みを学習する仕組みの確認（backend/linkmodel.py）。"""
import pytest

from backend import linkmodel as lm


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(lm, "FEEDBACK_PATH", tmp_path / "feedback.jsonl")
    monkeypatch.setattr(lm, "MODEL_PATH", tmp_path / "link_model.json")
    return tmp_path


def f(**kw):
    base = {k: 0.0 for k in lm.FEATURES}
    base.update(kw)
    return base


# 「サビ頭から入る」候補と「曲の頭から入る」候補（ほかの手がかりは同じ）
CHORUS = f(tempo=1, key=1, energy=0.6, conf=0.7, out_chorus=1, in_chorus=1)
START = f(tempo=1, key=1, energy=0.6, conf=0.7, out_chorus=1, in_start=1)


def test_default_weights_without_model(paths):
    assert lm.current_weights() == lm.DEFAULT_WEIGHTS
    assert lm.model_info()["source"] == "default"
    # 元の点数の式と同じ（テンポ0.30＋キー0.30＋勢い0.25×…＋確かさ0.15×…＋好み）
    assert lm.score(CHORUS) == pytest.approx(0.30 + 0.30 + 0.25 * 0.6 + 0.15 * 0.7 + 0.08 + 0.06)


def test_pairs_from_feedback():
    rec = {"links": [
        {"chosen": START, "rating": "bad", "better": CHORUS, "others": [CHORUS]},   # はっきり選んだ
        {"chosen": CHORUS, "rating": "good", "others": [START, START]},              # 良い → ほかより上
        {"chosen": START, "rating": "bad", "others": [CHORUS, CHORUS, CHORUS]},      # 微妙 → 上位2つの方がまし
    ]}
    pairs = lm.pairs_from_feedback([rec])
    assert len(pairs) == 1 + 2 + 2
    assert pairs[0][:3] == (CHORUS, START, lm.W_PICK)
    assert all(p[2] == lm.W_GOOD for p in pairs[1:3]) and all(p[2] == lm.W_BAD for p in pairs[3:])


def test_learning_follows_preference_but_stays_near_prior(paths):
    # 「曲の頭より、サビ頭から入る方が良い」という評価が続く
    recs = [{"links": [{"chosen": START, "rating": "bad", "better": CHORUS}]} for _ in range(3)]
    for r in recs:
        lm.record(r)
    res = lm.learn()
    assert res["saved"] and lm.model_info()["source"] == "learned"
    w = lm.current_weights()
    assert w["in_chorus"] - w["in_start"] > lm.DEFAULT_WEIGHTS["in_chorus"] - lm.DEFAULT_WEIGHTS["in_start"]
    assert lm.score(CHORUS, w) > lm.score(START, w)
    # 評価が3件だけなら、関係ない重み（テンポ・キー）はほとんど動かない
    assert abs(w["tempo"] - 0.30) < 0.02 and abs(w["key"] - 0.30) < 0.02
    # 件数が増えるほど、より強く評価に合わせる
    few = w["in_chorus"] - w["in_start"]
    many = lm.fit(lm.pairs_from_feedback(recs * 10))
    assert many["in_chorus"] - many["in_start"] > few


def test_too_few_feedback_keeps_default(paths):
    lm.record({"links": [{"chosen": START, "rating": "bad", "better": CHORUS}]})
    res = lm.learn()
    assert not res["saved"] and lm.current_weights() == lm.DEFAULT_WEIGHTS


def test_cross_validate_reports_both(paths):
    recs = [{"links": [{"chosen": START, "rating": "bad", "better": CHORUS}]} for _ in range(4)]
    cv = lm.cross_validate(lm.pairs_from_feedback(recs))
    assert set(cv) == {"learned", "default", "groups"} and cv["groups"] == 4
    assert cv["learned"] >= cv["default"]


def test_feedback_api(paths):
    from backend.app import app
    c = app.test_client()
    assert c.post("/api/feedback", json={"links": []}).status_code == 400
    body = {"songs": [1, 2], "links": [{"k": 0, "rating": "bad", "chosen": START, "better": CHORUS,
                                         "others": [CHORUS], "unknown_field": 1}]}
    for i in range(3):
        r = c.post("/api/feedback", json=body)
        assert r.status_code == 200
    j = r.get_json()
    assert j["learned"] and j["model"]["source"] == "learned" and j["model"]["n_feedback"] == 3
    saved = lm.load_feedback()
    assert len(saved) == 3 and set(saved[0]["links"][0]["chosen"]) <= set(lm.FEATURES)
    g = c.get("/api/feedback").get_json()
    assert g["model"]["source"] == "learned" and set(g["weights"]) == set(lm.FEATURES)


def test_rival_pairs_from_explored_link():
    """お試しで流した候補（2位）の評価は、本来の1位との比較になる"""
    good = {"links": [{"chosen": CHORUS, "rating": "good", "rival": START}]}
    bad = {"links": [{"chosen": CHORUS, "rating": "bad", "rival": START}]}
    assert lm.pairs_from_feedback([good])[0][:3] == (CHORUS, START, lm.W_RIVAL)
    assert lm.pairs_from_feedback([bad])[0][:3] == (START, CHORUS, lm.W_RIVAL)


def test_explore_rate_decreases_and_pick_is_stable(paths):
    assert lm.explore_rate(0) == pytest.approx(lm.EXPLORE_MAX)
    assert lm.explore_rate(20) < lm.explore_rate(0) and lm.explore_rate(10_000) == pytest.approx(lm.EXPLORE_MIN)
    plan = {"transitions": [
        {"kind": "サビ終わり→サビ頭", "out": 80.0, "in": 40.0, "score": 0.90, "features": CHORUS, "alternatives": [
            {"kind": "サビ終わり→曲の頭", "out": 80.0, "in": 0.0, "score": 0.88, "features": START},
            {"kind": "Aメロ終わり→曲の頭", "out": 60.0, "in": 0.0, "score": 0.50, "features": START}]},
        {"kind": "サビ終わり→サビ頭", "out": 90.0, "in": 30.0, "score": 0.90, "features": CHORUS, "manual": True,
         "alternatives": [{"kind": "x", "out": 1.0, "in": 1.0, "score": 0.9, "features": START}]}]}
    ex = lm.pick_explore(plan, [1, 2, 3], rate=1.0)
    assert set(ex) == {0}                                   # 手で選んだつなぎ目は試さない
    alt, rival = ex[0]
    assert alt["score"] >= 0.90 - lm.EXPLORE_MARGIN         # 点数が離れた候補（0.50）は選ばない
    assert rival["kind"] == "サビ終わり→サビ頭" and rival["features"] == CHORUS
    assert lm.pick_explore(plan, [1, 2, 3], rate=1.0) == ex           # 同じ曲なら同じ結果
    assert lm.pick_explore(plan, [1, 2, 3], rate=0.0) == {}
