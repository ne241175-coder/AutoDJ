"""シートとの自動同期（backend/autosync.py）で、どの曲を取り込みキューに入れるか。YouTube には繋がない"""
import pytest

from backend import addsong, autosync, db, teamsheet


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite3")
    songs = [{"title": "済み", "url": "https://youtu.be/AAAAAAAAAAA", "genres": []},
             {"title": "解析待ち", "url": "https://youtu.be/BBBBBBBBBBB", "genres": []},
             {"title": "動画変更", "url": "https://youtu.be/CCCCCCCCCCC", "genres": []},
             {"title": "新曲", "url": "https://youtu.be/DDDDDDDDDDD", "genres": []}]
    monkeypatch.setattr(teamsheet, "team_songs", lambda: songs)
    submitted = []
    monkeypatch.setattr(addsong, "submit", lambda url, title: submitted.append(title))
    monkeypatch.setattr(addsong, "jobs", lambda: [])
    monkeypatch.setattr(autosync, "_failed", {})
    con = db.connect()
    for title, sid, done in (("済み", "AAAAAAAAAAA", "now"), ("解析待ち", "BBBBBBBBBBB", None),
                             ("動画変更", "ZZZZZZZZZZZ", "now")):
        con.execute("INSERT INTO tracks (title, source_id, analyzed_at, file_path, added_at) VALUES (?, ?, ?, 'x', 'now')",
                    (title, f"yt:{sid}", done))
    con.commit()
    return con, submitted


def test_submit_new_retries_unanalyzed_and_changed(env):
    con, submitted = env
    queued, no_url = autosync._submit_new(con, log=lambda m: None)
    assert queued == submitted == ["解析待ち", "動画変更", "新曲"]
    assert no_url == []
