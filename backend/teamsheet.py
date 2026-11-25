"""「ジャンル横断」シート（チームの曲リスト・構成）とアプリのつなぎ。

新しい曲が入ったときの流れ
  1. だれかが シートの「曲リスト」に曲名と YouTube の URL を足す
  2. `python -m backend.cli sheet-sync`        … シートを取り込む
  3. `python -m backend.cli sheet-import`      … まだ取り込んでいない曲をダウンロード → 解析
  4. `python -m backend.cli structure-draft`   … 構成が「未入力」の曲の下書き（自動解析の区切り）を TSV で出す
     → シートの「構成」タブの一番下に貼る。メモが「下書き」の行は チェックが橙になり、アプリも正解としては使わない
  5. 聞きながら時刻とラベルを直し、メモの「下書き」を消す → 「曲リスト」の 構成（自動）が「入力済み」になる
"""
import csv
import io
from pathlib import Path

from .db import connect
from .library import load_analysis
from .sheet import SHEET_DIR

DRAFT = "下書き"
# 自動解析の役割（mixplan.AUTO_ROLE の行き先）→ シートのラベル
LABEL = {"intro": "イントロ", "verse": "A", "prechorus": "B", "bridge": "Cメロ", "chorus": "サビ",
         "chorus_drop": "落ちサビ", "interlude": "間奏", "outro": "アウトロ", "other": "その他"}


def _rows(path):
    p = Path(path)
    if not p.exists():
        return []
    return list(csv.reader(io.StringIO(p.read_text(encoding="utf-8-sig"))))


def team_songs(path=SHEET_DIR / "team_songs.csv"):
    """曲リスト → [{title, url, genres}]（曲名の無い行は飛ばす）"""
    rows = _rows(path)
    if not rows:
        return []
    head = [h.strip() for h in rows[0]]

    def col(prefix):
        return next((i for i, h in enumerate(head) if h.startswith(prefix)), None)
    ti, ui = col("曲名"), col("YouTube")
    gi = [i for i in (col("ジャンル1"), col("ジャンル2"), col("ジャンル3")) if i is not None]
    out = []
    for r in rows[1:]:
        r = r + [""] * (len(head) - len(r))
        title = r[ti].strip() if ti is not None else ""
        if title:
            out.append({"title": title, "url": r[ui].strip() if ui is not None else "",
                        "genres": [r[i].strip() for i in gi if r[i].strip()]})
    return out


def structure_titles(path=SHEET_DIR / "structure.csv"):
    """「構成」タブに1行でも入っている曲名（下書きや書きかけも含む）"""
    rows = _rows(path)
    return {r[0].strip() for r in rows[1:] if r and r[0].strip()}


def _mmss(t):
    t = int(round(max(0.0, t)))
    return f"{t // 60}:{t % 60:02d}"


def draft_rows(track_id, title=None, con=None):
    """自動解析の区切り → 「構成」タブに貼る行 [曲名, ラベル, 開始(m:ss), メモ]。最後に「終了」"""
    from .mixplan import load_song
    con = con or connect()
    s = load_song(con, int(track_id), {})          # シートを使わず、自動解析の区切り
    a = load_analysis(int(track_id))
    title = title or s.title
    out, last = [], None
    for sec in s.sections:
        if sec["start"] is None:
            continue
        m = _mmss(sec["start"])
        if m == last:                                  # 秒に丸めると同じになる短い区切りは捨てる
            continue
        out.append([title, LABEL.get(sec["role"], "その他"), m, DRAFT])
        last = m
    end = _mmss(a.get("music_end") or a["duration"])
    if out and end != last:
        out.append([title, "終了", end, DRAFT])
    if out:
        out[0][3] = f"{DRAFT}（自動解析。聞いて直したらメモを消す）"
    return out


def missing_drafts(con=None):
    """曲リストにあって 構成が未入力 で、解析済みの曲の下書き。戻り値: (行, 解析待ちの曲名)"""
    con = con or connect()
    have = structure_titles()
    rows, waiting = [], []
    for song in team_songs():
        if song["title"] in have:
            continue
        r = con.execute("SELECT id FROM tracks WHERE title=? AND analyzed_at IS NOT NULL ORDER BY id DESC",
                        (song["title"],)).fetchone()
        if r is None:
            waiting.append(song["title"])
            continue
        rows += draft_rows(r["id"], song["title"], con)
    return rows, waiting


def to_tsv(rows):
    return "\n".join("\t".join(r) for r in rows)


def new_songs(con=None):
    """曲リストにあって、アプリにまだ無い曲（URL があるもの / 無いもの）"""
    con = con or connect()
    titles = {r["title"] for r in con.execute("SELECT title FROM tracks")}
    todo = [s for s in team_songs() if s["title"] not in titles]
    return [s for s in todo if s["url"]], [s["title"] for s in todo if not s["url"]]


def import_new(dry_run=False, log=print):
    """曲リストの新しい曲を ダウンロード → 登録 → 解析（1曲ずつ。失敗しても次へ）"""
    from . import addsong
    todo, no_url = new_songs()
    for t in no_url:
        log(f"  - {t}: YouTube の URL が無いのでとばします")
    done = []
    for i, s in enumerate(todo, 1):
        log(f"[{i}/{len(todo)}] {s['title']}  {s['url']}")
        if dry_run:
            continue
        try:
            j = addsong.submit(s["url"], s["title"], run_now=True)
        except ValueError as e:
            log(f"    → できません: {e}")
            continue
        log(f"    → {j['message'] if j['stage'] != 'error' else 'エラー: ' + str(j['error'])}")
        if j["stage"] != "error":
            done.append(s["title"])
    return todo, done
