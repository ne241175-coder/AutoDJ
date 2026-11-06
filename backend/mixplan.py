"""つなぎ位置の分析と再生計画。

考え方
- 音声ファイルは切らない。各曲について「何秒から何秒まで流すか」と「どの秒で次の曲に入るか」だけを決める。
- 曲の構造は、シート（構成入力）に手付けがあればそれを優先し、なければ自動解析の結果を使う。
- つなぎの候補は「出る曲のサビ終わり」×「入る曲のサビ頭／曲頭（イントロ）／Aメロ頭」。
  すべて小節頭に吸着させる（拍がずれないように）。
- シートの「つなぎメモ」（例 1:28→1:07）があれば、その候補を最優先にする。
- 1曲を流す時間は最低 min_sec 秒。入った位置から min_sec 秒以上経ったサビ終わりでしか出られない。
  1曲目は、出る位置から min_sec 秒以上前の区間頭（できればAメロ）から流し始める。
- 曲順が決まっていれば、全体のスコアが最大になる組合せを動的計画法で選ぶ。
"""
import json
import math
from dataclasses import dataclass, field, asdict
from pathlib import Path

import numpy as np

from .analysis.key import camelot_distance
from .db import connect
from .library import load_analysis

DEFAULT_MIN_SEC = 90.0

# 自動解析の役割名 → シートと同じ大分類へ
AUTO_ROLE = {"intro": "intro", "verse": "verse", "chorus": "chorus", "build": "prechorus",
             "break": "interlude", "outro": "outro"}
ROLE_JA = {"intro": "イントロ", "verse": "Aメロ", "prechorus": "Bメロ", "bridge": "Cメロ",
           "chorus": "サビ", "chorus_drop": "落ちサビ", "interlude": "間奏", "outro": "アウトロ",
           "other": "その他", "start": "曲頭"}


@dataclass
class Song:
    track_id: int
    title: str
    file: str
    duration: float
    bpm: float
    camelot: str
    bars: list            # 小節頭の時刻（秒）
    sections: list        # [{role, label, start, end}]
    structure_source: str  # "sheet" or "auto"

    def snap(self, t, tol=1.6):
        """t を最寄りの小節頭へ。tol 秒以内に小節頭がなければそのまま"""
        if not self.bars:
            return float(t)
        b = np.asarray(self.bars)
        i = int(np.argmin(np.abs(b - t)))
        return float(b[i]) if abs(b[i] - t) <= tol else float(t)


def load_song(con, key, sheet_structure=None):
    """key: track_id(int) または 曲名(str)"""
    if isinstance(key, int) or str(key).isdigit():
        row = con.execute("SELECT * FROM tracks WHERE id=?", (int(key),)).fetchone()
    else:
        row = con.execute("SELECT * FROM tracks WHERE title=? ORDER BY id DESC", (key,)).fetchone()
    if row is None:
        raise KeyError(f"曲が見つかりません: {key}")
    a = load_analysis(row["id"])
    if a is None:
        raise RuntimeError(f"未解析です: [{row['id']}] {row['title']}（analyze を実行してください）")
    sheet = (sheet_structure or {}).get(row["title"])
    if sheet:
        sections = [{"role": s["role"], "label": s["label"], "start": s["start"], "end": s["end"]}
                    for s in sheet["sections"]]
        src = "sheet"
    else:
        sections = [{"role": AUTO_ROLE.get(s["role"], "other"), "label": ROLE_JA.get(AUTO_ROLE.get(s["role"], "other")),
                     "start": s["start"], "end": s["end"]} for s in a["structure"]["sections"]]
        src = "auto"
    return Song(row["id"], row["title"], row["file_path"], a["duration"], a["tempo"]["bpm"],
                a["key"]["camelot"], a["bars"], sections, src)


# ---------- 相性 ----------
def tempo_gap(bpm_a, bpm_b):
    """半分・倍テンポも同一視した BPM の差（割合）"""
    return min(abs(bpm_b * f / bpm_a - 1) for f in (0.5, 1.0, 2.0))


def tempo_score(bpm_a, bpm_b):
    g = tempo_gap(bpm_a, bpm_b)
    return 1.0 if g <= 0.03 else max(0.0, 1 - (g - 0.03) / 0.12)


def key_score(cam_a, cam_b):
    d = camelot_distance(cam_a, cam_b)
    return {0: 1.0, 1: 0.8, 2: 0.4}.get(d, 0.1)


# ---------- 候補 ----------
def exit_points(song):
    """出る位置の候補: サビの終わり（小節頭に吸着）"""
    out = [(song.snap(s["end"]), "chorus") for s in song.sections if s["role"] == "chorus" and s["end"]]
    if not out:  # サビが無い/検出できない → 音量の大きい区間の終わり
        out = [(song.snap(s["end"]), s["role"]) for s in song.sections if s["end"]]
    return out


def entry_points(song):
    """入る位置の候補: サビ頭・曲頭・Aメロ頭"""
    pts = []
    for s in song.sections:
        if s["role"] == "chorus":
            pts.append((song.snap(s["start"]), "chorus"))
        elif s["role"] == "verse":
            pts.append((song.snap(s["start"]), "verse"))
    first = song.sections[0] if song.sections else None
    pts.append((song.snap(first["start"]) if first else (song.bars[0] if song.bars else 0.0), "start"))
    return sorted(set(pts))


TYPE_BONUS = {("chorus", "chorus"): 0.30, ("chorus", "start"): 0.20, ("chorus", "verse"): 0.10}


@dataclass
class Link:
    out: float
    inn: float
    score: float
    kind: str
    reasons: list = field(default_factory=list)
    manual: bool = False


def candidate_links(a: Song, b: Song, manual=None):
    ts, ks = tempo_score(a.bpm, b.bpm), key_score(a.camelot, b.camelot)
    base = 0.35 * ts + 0.35 * ks
    why = [f"BPM {a.bpm:.0f}→{b.bpm:.0f}", f"Key {a.camelot}→{b.camelot}"]
    links = []
    for xo, ko in exit_points(a):
        for xi, ki in entry_points(b):
            bonus = TYPE_BONUS.get((ko, ki), 0.0)
            links.append(Link(xo, xi, base + bonus, f"{ROLE_JA.get(ko, ko)}終わり→{ROLE_JA.get(ki, ki)}頭", why))
    if manual:
        links.append(Link(a.snap(manual["out"]), b.snap(manual["in"]), base + 2.0, "シートのつなぎメモ",
                          why + ["手動指定"], manual=True))
    return links


# ---------- 計画 ----------
def plan_chain(songs, min_sec=DEFAULT_MIN_SEC, manual_links=None):
    """songs: [Song...]（曲順固定）。manual_links: {(i, i+1): {"out":秒,"in":秒}}"""
    manual_links = manual_links or {}
    n = len(songs)
    if n < 2:
        raise ValueError("2曲以上必要です")
    cands = [candidate_links(songs[i], songs[i + 1], manual_links.get((i, i + 1))) for i in range(n - 1)]
    warnings = []

    def length_penalty(play):
        # 必要以上に長く流しすぎない（同点なら短い方）
        return 0.002 * max(0.0, play - min_sec)

    # DP: best[i][j] = 最初から i 番目のつなぎに候補 j を使ったときの最大スコア
    best = [np.full(len(c), -np.inf) for c in cands]
    back = [np.full(len(c), -1, dtype=int) for c in cands]
    for j, l in enumerate(cands[0]):
        play = l.out - 0.0
        if play >= min_sec or l.manual:
            best[0][j] = l.score - length_penalty(play)
    for i in range(1, n - 1):
        for j, l in enumerate(cands[i]):
            for k, p in enumerate(cands[i - 1]):
                if not np.isfinite(best[i - 1][k]):
                    continue
                play = l.out - p.inn
                if play <= 0 or (play < min_sec and not l.manual):
                    continue
                s = best[i - 1][k] + l.score - length_penalty(play)
                if s > best[i][j]:
                    best[i][j], back[i][j] = s, k
    if not np.isfinite(best[-1]).any():
        warnings.append(f"最低 {min_sec:.0f} 秒の条件を満たす組合せがありません。条件を外して計画しました")
        return plan_chain(songs, min_sec=0.0, manual_links=manual_links) if min_sec > 0 else None

    # 復元
    j = int(np.argmax(best[-1]))
    chosen = [None] * (n - 1)
    for i in range(n - 2, -1, -1):
        chosen[i] = cands[i][j]
        j = back[i][j]

    items = []
    for i, s in enumerate(songs):
        if i == 0:
            start = first_start(s, chosen[0].out, min_sec)
        else:
            start = chosen[i - 1].inn
        if i < n - 1:
            end = chosen[i].out
        else:
            end = last_end(s, start, min_sec)
        if end - start < min_sec - 1e-6:
            warnings.append(f"{s.title}: 再生 {end - start:.0f} 秒（最低 {min_sec:.0f} 秒に届かず）")
        items.append({"track_id": s.track_id, "title": s.title, "file": s.file, "start": round(start, 3),
                      "end": round(end, 3), "bpm": s.bpm, "camelot": s.camelot,
                      "structure_source": s.structure_source,
                      "sections_played": [f"{x['label']}" for x in s.sections
                                          if x["end"] and x["end"] > start + 0.5 and x["start"] < end - 0.5]})
    transitions = []
    for i, l in enumerate(chosen):
        a, b = songs[i], songs[i + 1]
        gap = tempo_gap(a.bpm, b.bpm)
        transitions.append({"from": i, "to": i + 1, "out": round(l.out, 3), "in": round(l.inn, 3),
                            "kind": l.kind, "score": round(l.score, 3), "reasons": l.reasons,
                            "manual": l.manual, "tempo_gap": round(gap, 3),
                            # テンポが近ければ1小節かけて混ぜる、遠ければ1拍で切り替える
                            "overlap_beats": 4 if gap <= 0.06 else 1})
    return {"min_sec": min_sec, "items": items, "transitions": transitions, "warnings": warnings}


def first_start(song, exit_t, min_sec):
    """1曲目: exit から min_sec 以上前の区間頭のうち最も遅いもの（Aメロ優先）"""
    starts = [(song.snap(s["start"]), s["role"]) for s in song.sections]
    ok = [(t, r) for t, r in starts if exit_t - t >= min_sec]
    if not ok:
        return 0.0
    verse = [t for t, r in ok if r == "verse"]
    return max(verse) if verse else max(t for t, _ in ok)


def last_end(song, start, min_sec):
    """最後の曲: start から min_sec 以上たったサビ終わり、なければ曲の終わり"""
    for t, _ in sorted(exit_points(song)):
        if t - start >= min_sec:
            return t
    return float(song.sections[-1]["end"] or song.duration) if song.sections else song.duration


def fmt(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def describe(plan):
    lines = []
    clock = 0.0
    for i, it in enumerate(plan["items"]):
        lines.append(f"[{fmt(clock)}] {it['title']}  {fmt(it['start'])} 〜 {fmt(it['end'])}"
                     f"（{it['end'] - it['start']:.0f}秒, {it['bpm']:.0f}BPM {it['camelot']}, 構造:{it['structure_source']}）")
        lines.append(f"          流れ: {' → '.join(it['sections_played'])}")
        clock += it["end"] - it["start"]
        if i < len(plan["transitions"]):
            t = plan["transitions"][i]
            lines.append(f"   ↓ {t['kind']}  {fmt(t['out'])} → {fmt(t['in'])}  "
                         f"score {t['score']}  ({', '.join(t['reasons'])})")
    for w in plan["warnings"]:
        lines.append(f"※ {w}")
    return "\n".join(lines)


def build_plan(keys, min_sec=DEFAULT_MIN_SEC, use_sheet=True, con=None):
    """keys: 曲名 or track_id のリスト（曲順）。シートのつなぎメモがあれば自動で使う"""
    con = con or connect()
    structure, lst = ({}, {"routes": []})
    if use_sheet:
        try:
            from .sheet import load_cached
            structure, lst = load_cached()
        except FileNotFoundError:
            pass
    songs = [load_song(con, k, structure) for k in keys]
    manual = {}
    for i in range(len(songs) - 1):
        a, b = songs[i].title, songs[i + 1].title
        for r in lst.get("routes", []):
            if r["middle"] == a and r["leaf"] == b and r.get("link_mid_leaf"):
                manual[(i, i + 1)] = r["link_mid_leaf"]
            if r["root"] == a and r["middle"] == b and r.get("link_root_mid"):
                manual[(i, i + 1)] = r["link_root_mid"]
    return plan_chain(songs, min_sec=min_sec, manual_links=manual)


def save_plan(plan, path):
    Path(path).write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
