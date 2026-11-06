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
