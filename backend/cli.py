"""AutoDJ コマンドライン（Phase 0〜1）。

使い方:
  python -m backend.cli ingest URL [URL...] -g J-pop -s "set01"     YouTubeから取り込み
  python -m backend.cli add FILE [FILE...] -g Rock -s "set01"        手元ファイルを取り込み
  python -m backend.cli analyze --all | --set set01 | ID [ID...]     解析（-f で再解析）
  python -m backend.cli list [--set set01]                           一覧
  python -m backend.cli show ID                                      解析結果の詳細
  python -m backend.cli tag ID -g "J-pop,Rock"                       ジャンル変更
  python -m backend.cli evaluate truth.json                          手付け正解と比較
  python -m backend.cli genres                                       使えるジャンル
"""
import argparse
import sys

from .config import GENRES
from .db import connect, normalize_genres


def _fmt_time(sec):
    return f"{int(sec // 60)}:{sec % 60:04.1f}"


def cmd_ingest(a):
    from .ingest import ingest_urls
    for tid, title, new in ingest_urls(a.urls, a.genre, a.set, playlist=a.playlist):
        print(f"[{tid}] {'追加' if new else '既存'}: {title}")


def cmd_add(a):
    from .ingest import ingest_files
    for tid, title, new in ingest_files(a.files, a.genre, a.set, copy=not a.no_copy):
        print(f"[{tid}] {'追加' if new else '既存'}: {title}")


def cmd_analyze(a):
    from .library import analyze_track
    con = connect()
    if a.all:
        ids = [r["id"] for r in con.execute("SELECT id FROM tracks ORDER BY id")]
    elif a.set:
        ids = [r["id"] for r in con.execute(
            "SELECT t.id FROM tracks t JOIN sets s ON t.set_id=s.id WHERE s.name=? ORDER BY t.id", (a.set,))]
    else:
        ids = a.ids
    if not ids:
        print("対象の曲がありません")
        return
    for i, tid in enumerate(ids, 1):
        title = con.execute("SELECT title FROM tracks WHERE id=?", (tid,)).fetchone()
        print(f"({i}/{len(ids)}) [{tid}] {title['title'] if title else '?'}")
        try:
            r = analyze_track(tid, con=con, force=a.force, verbose=True)
            print(f"   → {r['tempo']['bpm']} BPM / {r['key']['name']} ({r['key']['camelot']}) / "
                  f"{len(r['structure']['sections'])} セクション / {r['elapsed_sec']}s")
        except Exception as e:  # 1曲の失敗で全体を止めない
            print(f"   × 失敗: {e}")


def cmd_list(a):
    con = connect()
    q = """SELECT t.*, s.name AS set_name FROM tracks t LEFT JOIN sets s ON t.set_id=s.id"""
    rows = con.execute(q + (" WHERE s.name=?" if a.set else "") + " ORDER BY t.id",
                       (a.set,) if a.set else ()).fetchall()
    print(f"{'ID':>3}  {'BPM':>6}  {'Key':<10} {'Cam':<4} {'Genre':<14} {'Set':<8} Title")
    for r in rows:
        print(f"{r['id']:>3}  {r['bpm'] or '-':>6}  {r['key_name'] or '-':<10} {r['camelot'] or '-':<4} "
              f"{r['genre']:<14} {r['set_name'] or '-':<8} {r['title']}")


def cmd_show(a):
    from .library import load_analysis
    r = load_analysis(a.id)
    if r is None:
        print("未解析です（analyze を実行してください）")
        return
    t, k, st = r["tempo"], r["key"], r["structure"]
    print(f"長さ {_fmt_time(r['duration'])}  音の開始 {r['music_start']}s")
    print(f"BPM {t['bpm']}  {'固定テンポ（等間隔グリッド）' if t['stable'] else '可変テンポ（揺れに追従）'}"
          f"  一致度 {t['grid_consistency']}  候補 {t['candidates']}")
    if t.get("uncertain_beat_ranges"):
        print(f"  拍が不確かな区間: {t['uncertain_beat_ranges']}")
    print(f"拍子 {r['meter']}/4  小節頭の確信度 {r['downbeat_confidence']}  小節数 {len(r['bars'])}")
    print(f"Key {k['name']} ({k['camelot']})  確信度 {k['confidence']}"
          f"{'  ※相対調と紛らわしい' if k['relative_ambiguous'] else ''}  チューニング {k['tuning_cents']}c")
    print(f"  候補: " + ", ".join(f"{c['key']}({c['camelot']})" for c in k["candidates"]))
    if k["modulates"]:
        print("  転調: " + " → ".join(f"{_fmt_time(m['start'])} {m['camelot']}" for m in k["timeline"]))
    print(f"構造（フレーズ位相 {st.get('phrase_offsets')}）")
    for s in st["sections"]:
        grid = "16" if s["on_grid16"] else "8" if s["on_grid8"] else "4" if s["on_grid4"] else "-"
        bar = "█" * int(round(s["energy"] * 10))
        print(f"  bar {s['start_bar']:>3}-{s['end_bar']:<3} {_fmt_time(s['start']):>7}  "
              f"{s['role']:<7} [{s['group']}] grid:{grid:<2} {bar}")


def cmd_tag(a):
    con = connect()
    g = normalize_genres(a.genre)
    con.execute("UPDATE tracks SET genre=? WHERE id=?", (",".join(g), a.id))
    con.commit()
    print(f"[{a.id}] ジャンル → {', '.join(g)}")


def cmd_evaluate(a):
    from .evaluate import evaluate
    print(evaluate(a.truth))


def main(argv=None):
    p = argparse.ArgumentParser(prog="autodj", description="AutoDJ（取り込み・解析）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="YouTube URL から取り込み")
    s.add_argument("urls", nargs="+")
    s.add_argument("-g", "--genre", required=True, help=f"例: J-pop / 複数は 'J-pop,Rock'（{', '.join(GENRES)}）")
    s.add_argument("-s", "--set", help="セット名（約10曲の単位）")
    s.add_argument("--playlist", action="store_true", help="プレイリストURLを丸ごと取り込む")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("add", help="手元の音源ファイルを取り込み")
    s.add_argument("files", nargs="+")
    s.add_argument("-g", "--genre", required=True)
    s.add_argument("-s", "--set")
    s.add_argument("--no-copy", action="store_true", help="data/audio に複製せず元の場所を参照")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("analyze", help="解析")
    s.add_argument("ids", nargs="*", type=int)
    s.add_argument("--all", action="store_true")
    s.add_argument("--set")
    s.add_argument("-f", "--force", action="store_true", help="解析済みでもやり直す")
    s.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("list", help="曲一覧")
    s.add_argument("--set")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="解析結果の詳細")
    s.add_argument("id", type=int)
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("tag", help="ジャンルを変更")
    s.add_argument("id", type=int)
    s.add_argument("-g", "--genre", required=True)
    s.set_defaults(fn=cmd_tag)

    s = sub.add_parser("evaluate", help="手付け正解と比較して精度を出す")
    s.add_argument("truth")
    s.set_defaults(fn=cmd_evaluate)

    s = sub.add_parser("genres", help="使えるジャンル一覧")
    s.set_defaults(fn=lambda a: print(", ".join(GENRES)))

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except (ValueError, KeyError, FileNotFoundError, RuntimeError) as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
