"""「ジャンル横断」シートとのつなぎ（backend/teamsheet.py・下書きの扱い）"""
from backend import teamsheet as ts
from backend.sheet import parse_structure


def test_team_songs_reads_by_header(tmp_path):
    p = tmp_path / "team.csv"
    p.write_text("曲名,ジャンル1（メイン）,ジャンル2,ジャンル3,入っているジャンル（自動）,橋渡し（自動）,YouTube,横断ルート数（自動）,メモ,構成（自動）\n"
                 "曲A,J-pop,アイドル,,x,,https://youtu.be/abcdefghijk,0,,未入力\n曲B,Rock,,,x,,,0,,未入力\n,,,,,,,,,\n",
                 encoding="utf-8")
    s = ts.team_songs(p)
    assert [x["title"] for x in s] == ["曲A", "曲B"]
    assert s[0]["url"].endswith("abcdefghijk") and s[0]["genres"] == ["J-pop", "アイドル"] and s[1]["url"] == ""


def test_drafts_are_not_used_as_truth(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("曲名,ラベル,開始(m:ss),開始(秒)（自動）,終了(秒)（自動）,チェック（自動）,メモ\n"
                 "曲A,サビ,0:00,0,30,下書き: 聞いて直したらメモを消す,下書き（自動解析）\n曲A,終了,0:30,30,,下書き,下書き\n"
                 "曲B,サビ,0:00,0,30,OK,\n曲B,終了,0:30,30,,OK,\n", encoding="utf-8")
    assert list(parse_structure(p)) == ["曲B"]


def test_mmss():
    assert ts._mmss(0) == "0:00" and ts._mmss(88.4) == "1:28" and ts._mmss(59.6) == "1:00" and ts._mmss(-1) == "0:00"


def test_draft_rows_from_real_analysis():
    """解析済みの曲があれば、下書きは 時刻が増える順・最後が「終了」・ラベルはシートのプルダウンの値"""
    from backend.db import connect
    r = connect().execute("SELECT id FROM tracks WHERE analyzed_at IS NOT NULL LIMIT 1").fetchone()
    if r is None:
        return
    rows = ts.draft_rows(r["id"], "曲X")
    secs = [int(m.split(":")[0]) * 60 + int(m.split(":")[1]) for _, _, m, _ in rows]
    assert secs == sorted(secs) and len(set(secs)) == len(secs)
    assert rows[-1][1] == "終了" and all(x[3].startswith("下書き") for x in rows)
    assert {x[1] for x in rows} <= set(ts.LABEL.values()) | {"終了"}


def test_autosync_queues_new_and_changed_songs(monkeypatch):
    """曲リストに新しい曲・URL が変わった曲があればキューへ。同じ動画・URL 無しは入れない"""
    from backend import autosync, addsong, teamsheet

    class Con:
        def execute(self, *a):
            return [{"title": "あり", "source_id": "yt:AAAAAAAAAAA"}, {"title": "変わった", "source_id": "yt:BBBBBBBBBBB"}]
    songs = [{"title": "あり", "url": "https://youtu.be/AAAAAAAAAAA", "genres": []},
             {"title": "変わった", "url": "https://youtu.be/CCCCCCCCCCC", "genres": []},
             {"title": "新曲", "url": "https://www.youtube.com/watch?v=DDDDDDDDDDD", "genres": []},
             {"title": "URLなし", "url": "", "genres": []}]
    monkeypatch.setattr(teamsheet, "team_songs", lambda *a: songs)
    monkeypatch.setattr(addsong, "jobs", lambda: [])
    sent = []
    monkeypatch.setattr(addsong, "submit", lambda url, title, **k: sent.append(title))
    autosync._failed.clear()
    queued, no_url = autosync._submit_new(Con(), log=lambda *a: None)
    assert queued == sent == ["変わった", "新曲"] and no_url == ["URLなし"]


def test_sheet_load_reports_change_only_when_content_changes(tmp_path):
    from backend.sheet import load
    (tmp_path / "structure.csv").write_text("曲名,ラベル,開始(m:ss)\n曲A,サビ,0:00\n曲A,終了,0:30\n", encoding="utf-8")
    (tmp_path / "songs.csv").write_text("ルート曲,情報,中間曲,中間情報,曲名,備考,URL\n", encoding="utf-8")
    assert load(tmp_path, report_change=True)[2] is True
    assert load(tmp_path, report_change=True)[2] is False
    (tmp_path / "structure.csv").write_text("曲名,ラベル,開始(m:ss)\n曲A,サビ,0:00\n,終了,0:40\n", encoding="utf-8")
    s, _, changed = load(tmp_path, report_change=True)
    assert changed and s["曲A"]["end"] == 40           # 曲名が空欄の行は上の曲として読む
