"""画面から曲を足す仕組みの確認（backend/addsong.py・/api/add・/api/routes）。YouTube には繋がない"""
import pytest

from backend import addsong, db, fetch_songs


@pytest.fixture
def env(tmp_path, monkeypatch):
    audio, ana, sheet = tmp_path / "audio", tmp_path / "analysis", tmp_path / "sheet"
    for d in (audio, ana, sheet):
        d.mkdir()
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite3")
    monkeypatch.setattr(addsong, "AUDIO_DIR", audio)
    monkeypatch.setattr(addsong, "ANALYSIS_DIR", ana)
    monkeypatch.setattr(fetch_songs, "SHEET_DIR", sheet)
    calls = {"download": 0, "analyze": []}

    def fake_info(vid):
        return {"id": vid, "title": "YOASOBI「アイドル」Official Music Video", "channel": "Ayase / YOASOBI", "duration": 213}

    def fake_download(vid, hook):
        calls["download"] += 1
        hook({"status": "downloading", "downloaded_bytes": 5, "total_bytes": 10})
        (audio / f"{vid}.mp3").write_bytes(b"x")
        hook({"status": "finished"})
        return {"id": vid, "title": "YOASOBI「アイドル」Official Music Video", "duration": 213}

    def fake_analyze(tid, con):
        calls["analyze"].append(tid)
        con.execute("UPDATE tracks SET analyzed_at='now', bpm=166 WHERE id=?", (tid,))
        con.commit()
        return {"tempo": {"bpm": 166}, "key": {"name": "A# minor"}}

    monkeypatch.setattr(addsong, "_info", fake_info)
    monkeypatch.setattr(addsong, "_download", fake_download)
    monkeypatch.setattr(addsong, "_analyze", fake_analyze)
    return {"tmp": tmp_path, "audio": audio, "sheet": sheet, "calls": calls}


@pytest.mark.parametrize("info, want", [
    ({"title": "YOASOBI「アイドル」Official Music Video", "channel": "Ayase / YOASOBI"}, "アイドル"),
    ({"title": "米津玄師 MV「LOSER」", "channel": "米津玄師"}, "LOSER"),
    ({"title": "Mrs. GREEN APPLE - 青と夏", "channel": "Mrs. GREEN APPLE"}, "青と夏"),
    ({"title": "Official髭男dism - ノーダウト［Official Video］", "channel": "Official髭男dism"}, "ノーダウト"),
    ({"title": "夜の踊り子 / サカナクション", "channel": "サカナクション"}, "夜の踊り子"),
    ({"title": "【MV】踊り子 / Vaundy", "channel": "Vaundy"}, "踊り子"),
    ({"title": "Odoriko", "track": "踊り子", "artist": "Vaundy", "channel": "Vaundy - Topic"}, "踊り子"),
])
def test_guess_title(info, want):
    assert addsong.guess_title(info)[0] == want


def test_add_song_registers_analyzes_and_adds_route(env):
    j = addsong.submit("https://youtu.be/ZRtdQ81jPUQ", "アイドル", "YOASOBI", ["青と夏", "LOSER", "アイドル"], run_now=True)
    assert j["stage"] == "done", j
    con = db.connect()
    t = con.execute("SELECT * FROM tracks WHERE id=?", (j["track_id"],)).fetchone()
    assert t["title"] == "アイドル" and t["source_id"] == "yt:ZRtdQ81jPUQ" and t["artist"] == "YOASOBI"
    assert t["analyzed_at"] and con.execute("SELECT name FROM sets WHERE id=?", (t["set_id"],)).fetchone()["name"] == "sheet"
    assert [(r["root"], r["middle"], r["leaf"]) for r in db.list_routes(con)] == [("青と夏", "LOSER", "アイドル")]
    # downloads.csv に手動指定として残る（あとで sheet-fetch しても選び直さない）
    assert fetch_songs._manual_overrides()["アイドル"]["status"] == "manual"


def test_same_title_new_video_replaces_old(env):
    addsong.submit("https://youtu.be/AAAAAAAAAAA", "アイドル", run_now=True)
    j = addsong.submit("https://youtu.be/BBBBBBBBBBB", "アイドル", run_now=True)
    rows = db.connect().execute("SELECT source_id FROM tracks WHERE title='アイドル'").fetchall()
    assert [r["source_id"] for r in rows] == ["yt:BBBBBBBBBBB"] and "置き換え" in j["message"]


def test_same_video_is_not_downloaded_twice_and_rename_follows(env):
    addsong.submit("https://youtu.be/AAAAAAAAAAA", "あいどる", None, ["A", "B", "あいどる"], run_now=True)
    addsong.submit("https://www.youtube.com/watch?v=AAAAAAAAAAA", "アイドル", run_now=True)
    con = db.connect()
    assert env["calls"]["download"] == 1
    assert [r["title"] for r in con.execute("SELECT title FROM tracks")] == ["アイドル"]
    assert db.list_routes(con)[0]["leaf"] == "アイドル"


def test_errors_are_reported(env, monkeypatch):
    with pytest.raises(ValueError):
        addsong.submit("https://example.com/x", "x")
    with pytest.raises(ValueError):
        addsong.submit("https://youtu.be/AAAAAAAAAAA", "x", route=["x", "x", "y"])
    monkeypatch.setattr(addsong, "_download", lambda vid, hook: (_ for _ in ()).throw(RuntimeError("Video unavailable\nmore")))
    j = addsong.submit("https://youtu.be/CCCCCCCCCCC", "y", run_now=True)
    assert j["stage"] == "error" and j["error"] == "Video unavailable"


def test_api_add_lookup_routes(env):
    from backend.app import app
    c = app.test_client()
    r = c.post("/api/add/lookup", json={"url": "https://youtu.be/ZRtdQ81jPUQ"}).get_json()
    assert r["title"] == "アイドル" and r["existing"] is None and r["duration"] == 213
    assert c.post("/api/add/lookup", json={"url": "nope"}).status_code == 400
    assert c.post("/api/add", json={"url": "https://youtu.be/ZRtdQ81jPUQ", "title": ""}).status_code == 400
    j = c.post("/api/add", json={"url": "https://youtu.be/ZRtdQ81jPUQ", "title": "アイドル",
                                 "route": ["青と夏", "LOSER", "アイドル"]})
    assert j.status_code == 202
    addsong._queue.join()                     # 裏の処理が終わるまで待つ
    jobs = c.get("/api/add").get_json()["jobs"]
    assert jobs[0]["stage"] == "done" and jobs[0]["title"] == "アイドル"
    routes = c.get("/api/routes").get_json()
    app_routes = [r for r in routes if r["source"] == "app"]
    assert app_routes[-1]["chain"] == ["青と夏", "LOSER", "アイドル"] and app_routes[-1]["missing"] == ["青と夏", "LOSER"]
    # 画面からルートだけ足す・消す
    assert c.post("/api/routes", json={"chain": ["アイドル", "LOSER", "青と夏"]}).get_json()["new"]
    assert c.post("/api/routes", json={"chain": ["アイドル", "", "青と夏"]}).status_code == 400
    rid = [r for r in c.get("/api/routes").get_json() if r["source"] == "app"][-1]["route_id"]
    assert c.delete(f"/api/routes/{rid}").status_code == 200
    assert c.delete(f"/api/routes/{rid}").status_code == 404
