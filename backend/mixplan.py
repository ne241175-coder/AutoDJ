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
    sheet_offset: float = 0.0  # シートの秒数 → 音源の秒数 のずらし量
    energy: list = field(default_factory=list)   # 小節ごとの音の大きさ（曲内で 0〜1）
    tonic: int = None     # キーの主音（0=C）と長調/短調。移調でキーを合わせるときに使う
    mode: str = None

    def energy_near(self, t, before, n=2):
        """t の直前（before=True）／直後 n 小節の音の大きさ（0〜1）。分からなければ None"""
        if not self.energy or not self.bars:
            return None
        b = np.asarray(self.bars[:len(self.energy)])
        i = int(np.argmin(np.abs(b - t)))
        seg = self.energy[max(0, i - n):i] if before else self.energy[i:i + n]
        return float(np.mean(seg)) if len(seg) else None

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
    offset = 0.0
    if sheet:
        from .align import aligned_sections
        aligned, offset = aligned_sections(sheet, a)   # 計測した動画と音源のズレを補正
        sections = [{"role": s["role"], "label": s["label"], "start": s["start"], "end": s["end"]}
                    for s in aligned]
        src = "sheet"
    else:
        sections = [{"role": AUTO_ROLE.get(s["role"], "other"), "label": ROLE_JA.get(AUTO_ROLE.get(s["role"], "other")),
                     "start": s["start"], "end": s["end"], "confidence": s.get("boundary_confidence")}
                    for s in a["structure"]["sections"]]
        src = "auto"
    return Song(row["id"], row["title"], row["file_path"], a["duration"], a["tempo"]["bpm"],
                a["key"]["camelot"], a["bars"], sections, src, offset,
                energy=a["structure"].get("bar_energy") or [],
                tonic=a["key"].get("tonic"), mode=a["key"].get("mode"))


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
    """出る位置の候補: すべての区間の終わり（小節頭に吸着）。Aメロの終わりで出てもよい"""
    out = [(song.snap(s["end"]), s["role"]) for s in song.sections if s["end"]]
    return sorted(set(out))


def entry_points(song, with_order=False):
    """入る位置の候補: すべての区間の頭（アウトロ以外）と曲頭。with_order=True なら (秒, 役割, 何回目か)"""
    pts, count = [], {}
    for s in song.sections:
        if s["role"] == "outro":
            continue
        count[s["role"]] = count.get(s["role"], 0) + 1
        pts.append((song.snap(s["start"]), s["role"], count[s["role"]]))
    first = song.sections[0] if song.sections else None
    pts.append((song.snap(first["start"]) if first else (song.bars[0] if song.bars else 0.0), "start", 1))
    # 同じ位置は1つに（曲頭が Aメロ頭と同じ、など → 曲頭・若い番号を残す）
    uniq = {}
    for t, r, k in pts:
        if t not in uniq or r == "start" or (uniq[t][1] != "start" and k < uniq[t][2]):
            uniq[t] = (t, r, k)
    out = sorted(uniq.values())
    return out if with_order else [(t, r) for t, r, _ in out]


# 「つながりそうか」の点数 = 手がかり × 重み の合計（linkmodel.py）。
# 重みは最初は手で決めた値（テンポ0.30・キー0.30・勢い0.25・確かさ0.15 ＋ 区間の種類の小さな好み）で、
# 人の評価が貯まると data/link_model.json の学習した重みに置き換わる。
from .linkmodel import DEFAULT_WEIGHTS, current_weights, model_info
W_TEMPO, W_KEY, W_ENERGY, W_CONF = (DEFAULT_WEIGHTS[k] for k in ("tempo", "key", "energy", "conf"))
LENGTH_PENALTY = 0.002   # 最低秒数より長く流すほど少し減点（同じ条件なら早めに次へ）
DEFAULT_CHORUS_GRACE = 15.0   # サビ終わりで出るときは、最低秒数をこれだけ短くしてよい


def energy_fit(a, b, out, inn):
    """出る直前2小節と入った直後2小節の音の大きさが近いほど 1（盛り上がりがそのまま続く）"""
    ea, eb = a.energy_near(out, before=True), b.energy_near(inn, before=False)
    if ea is None or eb is None:
        return 0.5, ea, eb
    return max(0.0, 1.0 - abs(ea - eb) * 1.5), ea, eb


def section_conf(song, t, edge):
    """その位置の区切りの確かさ（シートの手入力なら 1）"""
    if song.structure_source == "sheet":
        return 1.0
    best, bd = None, 1e9
    for x in song.sections:
        if x["start"] is None:
            continue
        d = abs(song.snap(x["start"]) - t)
        if d < bd:
            best, bd = x, d
    c = best.get("confidence") if best is not None and bd <= 2.0 else None
    return 0.5 if c is None else float(c)


@dataclass
class Link:
    out: float
    inn: float
    score: float
    kind: str
    reasons: list = field(default_factory=list)
    manual: bool = False
    out_role: str = "chorus"
    features: dict = field(default_factory=dict)


def link_features(a: Song, b: Song, out, inn, out_role, in_role, nth=1):
    """つなぎ目1つの手がかり（linkmodel.FEATURES）。点数はこれと重みの掛け算の合計"""
    ef, _, _ = energy_fit(a, b, out, inn)
    ci = 1.0 if in_role == "start" else section_conf(b, inn, "start")
    co = section_conf(a, out, "end")
    return {"tempo": round(tempo_score(a.bpm, b.bpm), 4), "key": key_score(a.camelot, b.camelot),
            "energy": round(ef, 4), "conf": round((co + ci) / 2, 4),
            "out_chorus": float(out_role == "chorus"), "out_outro": float(out_role == "outro"),
            "in_chorus": float(in_role == "chorus"), "in_start": float(in_role == "start"),
            "in_verse": float(in_role == "verse"), "later_entry": float(nth > 1)}


def features_at(a: Song, b: Song, out, inn):
    """任意の位置（シートのメモ・選び直した位置など）の手がかり。区間の種類は近い区切りから推定"""
    out, inn = a.snap(out), b.snap(inn)
    io, ii = _section_at(a, out, "end"), _section_at(b, inn, "start")
    ro = a.sections[io]["role"] if io is not None else "other"
    first = b.sections[0]["start"] if b.sections else 0.0
    if abs(inn - b.snap(first)) <= 2.0:
        ri, nth = "start", 1
    elif ii is not None:
        ri = b.sections[ii]["role"]
        nth = sum(1 for x in b.sections[:ii + 1] if x["role"] == ri)
    else:
        ri, nth = "other", 1
    return link_features(a, b, out, inn, ro, ri, nth)


def candidate_links(a: Song, b: Song, manual=None, weights=None):
    """つなぎ候補: 出る曲のどの区間の終わり × 入る曲のどの区間の頭 でもよい。
    点数 = テンポの近さ・キーの相性・音の大きさのつながり・区切りの確かさ ＋ 区間の種類の小さな好み
    （それぞれの重みは linkmodel。人の評価から学習した重みがあればそれを使う）"""
    w = weights or current_weights()

    def sc(f):
        return sum(w[k] * f[k] for k in f)

    why = [f"BPM {a.bpm:.0f}→{b.bpm:.0f}", f"Key {a.camelot}→{b.camelot}"]
    links = []
    for xo, ko in exit_points(a):
        for xi, ki, nth in entry_points(b, with_order=True):
            f = link_features(a, b, xo, xi, ko, ki, nth)
            kin = "曲の頭" if ki == "start" else ROLE_JA.get(ki, ki) + "頭"
            links.append(Link(xo, xi, sc(f), f"{ROLE_JA.get(ko, ko)}終わり→{kin}", why, out_role=ko, features=f))
    if manual:
        f = features_at(a, b, manual["out"], manual["in"])
        base = w["tempo"] * f["tempo"] + w["key"] * f["key"]
        links.append(Link(a.snap(manual["out"]), b.snap(manual["in"]), base + 2.0, manual.get("kind", "シートのつなぎメモ"),
                          why + [manual.get("why", "手動指定")], manual=True, features=f))
    return links


# ---------- 計画 ----------
def plan_chain(songs, min_sec=DEFAULT_MIN_SEC, manual_links=None, chorus_grace=DEFAULT_CHORUS_GRACE):
    """songs: [Song...]（曲順固定）。manual_links: {(i, i+1): {"out":秒,"in":秒}}
    1曲は最低 min_sec 秒流す。ただしサビ終わりで出る場合は chorus_grace 秒だけ早く出てよい"""
    manual_links = manual_links or {}
    n = len(songs)
    if n < 2:
        raise ValueError("2曲以上必要です")
    cands = [candidate_links(songs[i], songs[i + 1], manual_links.get((i, i + 1))) for i in range(n - 1)]
    warnings = []

    def required(l):
        return max(0.0, min_sec - chorus_grace) if l.out_role == "chorus" else min_sec

    def length_penalty(play):
        # 必要以上に長く流しすぎない（同点なら短い方）
        return LENGTH_PENALTY * max(0.0, play - min_sec)

    # DP: best[i][j] = 最初から i 番目のつなぎに候補 j を使ったときの最大スコア
    best = [np.full(len(c), -np.inf) for c in cands]
    back = [np.full(len(c), -1, dtype=int) for c in cands]
    for j, l in enumerate(cands[0]):
        play = l.out - 0.0
        if play >= required(l) or l.manual:
            best[0][j] = l.score - length_penalty(play)
    for i in range(1, n - 1):
        for j, l in enumerate(cands[i]):
            for k, p in enumerate(cands[i - 1]):
                if not np.isfinite(best[i - 1][k]):
                    continue
                play = l.out - p.inn
                if play <= 0 or (play < required(l) and not l.manual):
                    continue
                s = best[i - 1][k] + l.score - length_penalty(play)
                if s > best[i][j]:
                    best[i][j], back[i][j] = s, k
    if not np.isfinite(best[-1]).any():
        warnings.append(f"最低 {min_sec:.0f} 秒の条件を満たす組合せがありません。条件を外して計画しました")
        return plan_chain(songs, min_sec=0.0, manual_links=manual_links, chorus_grace=0.0) if min_sec > 0 else None

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
        need = min_sec - (chorus_grace if (i < n - 1 and chosen[i].out_role == "chorus") else 0.0)
        if end - start < need - 1e-6:
            warnings.append(f"{s.title}: 再生 {end - start:.0f} 秒（最低 {need:.0f} 秒に届かず）")
        items.append({"track_id": s.track_id, "title": s.title, "file": s.file, "start": round(start, 3),
                      "end": round(end, 3), "bpm": s.bpm, "camelot": s.camelot,
                      "structure_source": s.structure_source,
                      "sections_played": [f"{x['label']}" for x in s.sections
                                          if x["end"] and x["end"] > start + 0.5 and x["start"] < end - 0.5]})
    transitions = []
    for i, l in enumerate(chosen):
        a, b = songs[i], songs[i + 1]
        gap = tempo_gap(a.bpm, b.bpm)
        prev_in = chosen[i - 1].inn if i > 0 else items[0]["start"]
        next_out = chosen[i + 1].out if i + 1 < len(chosen) else None
        transitions.append({"from": i, "to": i + 1, "out": round(l.out, 3), "in": round(l.inn, 3),
                            "kind": l.kind, "score": round(l.score, 3), "reasons": l.reasons,
                            "manual": l.manual, "tempo_gap": round(gap, 3), "features": l.features,
                            # テンポが近ければ1小節かけて混ぜる、遠ければ1拍で切り替える
                            "overlap_beats": 4 if gap <= 0.06 else 1,
                            "explain": explain_link(a, b, l),
                            "alternatives": alternatives(cands[i], l, prev_in, next_out, min_sec, chorus_grace, a, b)})
    return {"min_sec": min_sec, "chorus_grace": chorus_grace, "items": items, "transitions": transitions,
            "warnings": warnings, "model": model_info()}


def _section_at(song, t, edge):
    """t が区間の終わり（edge="end"）／頭（edge="start"）になっている区間"""
    best, bd = None, 1e9
    for idx, x in enumerate(song.sections):
        v = x["end"] if edge == "end" else x["start"]
        if v is None:
            continue
        d = abs(song.snap(v) - t)
        if d < bd:
            best, bd = idx, d
    return best if bd <= 2.0 else None


def _boundary_conf(song, t):
    """t の位置の区切りの信頼度（自動解析で、学習モデルを使ったときだけ）"""
    i = _section_at(song, t, "start")
    if i is None or song.structure_source != "auto":
        return None
    c = song.sections[i].get("confidence")
    return None if c is None else round(float(c), 3)


def explain_link(a, b, l):
    """「なぜここでつなぐか」を画面で説明するための材料"""
    io, ii = _section_at(a, l.out, "end"), _section_at(b, l.inn, "start")
    so = a.sections[io] if io is not None else None
    si = b.sections[ii] if ii is not None else None
    d = camelot_distance(a.camelot, b.camelot)
    return {
        "source": "sheet" if (a.structure_source == "sheet" or b.structure_source == "sheet") else "auto",
        "out_section": {"label": so["label"], "role": so["role"], "start": so["start"], "end": so["end"]} if so else None,
        "in_section": {"label": si["label"], "role": si["role"], "start": si["start"], "end": si["end"]} if si else None,
        "tempo": {"a": round(a.bpm, 1), "b": round(b.bpm, 1), "gap": round(tempo_gap(a.bpm, b.bpm), 3),
                  "score": round(tempo_score(a.bpm, b.bpm), 2)},
        "key": {"a": a.camelot, "b": b.camelot, "distance": d, "score": key_score(a.camelot, b.camelot)},
        "confidence": {"out": _boundary_conf(a, l.out), "in": _boundary_conf(b, l.inn)},
        "energy": dict(zip(("fit", "out", "in"), [None if v is None else round(v, 2) for v in energy_fit(a, b, l.out, l.inn)])),
        "structure": {"a": a.structure_source, "b": b.structure_source},
    }


def alternatives(cands, chosen, prev_in, next_out, min_sec, chorus_grace, a, b, n=4):
    """選ばれなかった候補のうち、前後の曲の条件を満たすもの（スコア順）"""
    out, seen = [], {(round(chosen.out, 1), round(chosen.inn, 1))}
    for l in sorted(cands, key=lambda x: -x.score):
        key = (round(l.out, 1), round(l.inn, 1))
        if key in seen:
            continue
        need = max(0.0, min_sec - chorus_grace) if l.out_role == "chorus" else min_sec
        if l.out - prev_in < need and not l.manual:
            continue
        if next_out is not None and next_out - l.inn <= 0:
            continue
        seen.add(key)
        out.append({"out": round(l.out, 3), "in": round(l.inn, 3), "kind": l.kind, "score": round(l.score, 3),
                    "manual": l.manual, "features": l.features})
        if len(out) >= n:
            break
    return out


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
            lines.append(f"   ↓ {t['kind']}【{t.get('style_label', 'おまかせ')}】  {fmt(t['out'])} → {fmt(t['in'])}  "
                         f"score {t['score']}  ({', '.join(t['reasons'])})")
    for w in plan["warnings"]:
        lines.append(f"※ {w}")
    return "\n".join(lines)


# つなぎ方（UI の「繋ぎ方を選んでね」）
#   auto: おまかせ（テンポが近ければ1小節、遠ければ1拍で混ぜる）
#   fade: フェードイン（長めに重ねてじわっと入れ替える）
#   cut : カットイン（小節頭でスパッと切り替える）
STYLES = {"auto": "おまかせ", "fade": "フェードイン", "cut": "カットイン"}


def apply_styles(plan, styles):
    """styles: つなぎ目ごとの "auto"/"fade"/"cut"（足りない分は auto）。plan を書き換えて返す"""
    styles = list(styles or [])
    items = plan["items"]
    for k, t in enumerate(plan["transitions"]):
        st = styles[k] if k < len(styles) and styles[k] in STYLES else "auto"
        t["style"], t["style_label"] = st, STYLES[st]
        if st == "cut":
            t["overlap_beats"] = 0
        elif st == "fade":
            a, b = items[k], items[k + 1]
            beats = 16 if t["tempo_gap"] <= 0.06 else 8
            # 重ねる長さは、入る曲の in より前・出る曲の再生時間の 1/3 までに収める
            room = min(t["in"] * b["bpm"] / 60.0, (a["end"] - a["start"]) / 3 * a["bpm"] / 60.0)
            t["overlap_beats"] = int(max(1, min(beats, room)))
    return plan


def build_plan(keys, min_sec=DEFAULT_MIN_SEC, use_sheet=True, con=None, chorus_grace=DEFAULT_CHORUS_GRACE,
               styles=None, links=None, match_bpm=True, match_key=True, explore=False):
    """keys: 曲名 or track_id のリスト（曲順）。シートのつなぎメモがあれば自動で使う。
    use_sheet=False なら、構造もつなぎ位置もすべて自動検知（シートの手入力はお手本として比べるだけ）。
    match_bpm / match_key: 入る曲のテンポ・キーを、出る曲に合わせる（matching.py）。
    links: {つなぎ目の番号: {"out": 秒, "in": 秒}} 画面で選び直した候補（最優先）
    explore: お試しモード。ときどき1位ではなく、点数が近い2位・3位の候補でつなぐ（linkmodel.pick_explore）。
      1位以外の評価も集まるので、「1位と2位のどちらが良いか」を学べる（強化学習のバンディットの考え方）"""
    con = con or connect()
    structure, lst = ({}, {"routes": []})
    try:
        from .sheet import load_cached
        structure, lst = load_cached()
    except FileNotFoundError:
        pass
    sheet_lst = lst
    if not use_sheet:
        structure, lst = {}, {"routes": []}
    songs = [load_song(con, k, structure) for k in keys]
    plan = _build(songs, lst, min_sec, chorus_grace, links)
    explored = {}
    if explore:
        from .linkmodel import pick_explore
        explored = pick_explore(plan, keys, fixed={int(k) for k in (links or {})})
        if explored:
            links2 = dict(links or {})
            for k, (alt, _) in explored.items():
                links2[str(k)] = {"out": alt["out"], "in": alt["in"], "kind": alt["kind"]}
            plan = _build(songs, lst, min_sec, chorus_grace, links2)
            for k, (alt, rival) in explored.items():
                t = plan["transitions"][k]
                t.update(manual=False, explored=True, rival={"kind": rival["kind"], "out": rival["out"],
                                                              "in": rival["in"], "features": rival["features"]})
    from .linkmodel import explore_rate
    plan["explore"] = {"on": bool(explore), "n": len(explored), "rate": round(explore_rate(), 3)}
    _attach_reference(plan, songs, sheet_lst, con)
    # BPM とキーを合わせる（入る曲を、いま鳴っている曲に合わせる）
    from .matching import apply_matching
    apply_matching(plan, songs, match_bpm=match_bpm, match_key=match_key)
    return apply_styles(plan, styles)


def _attach_reference(plan, songs, lst, con):
    """シートに人が書いたつなぎメモがあれば、自動検知との差を出せるように添える（音源の時刻に合わせて）"""
    try:
        from .sheet import load_cached
        structure, _ = load_cached()
    except FileNotFoundError:
        structure = {}
    from .align import aligned_sections
    offs = {}
    for s in songs:
        sh = structure.get(s.title)
        a = load_analysis(s.track_id) if sh else None
        offs[s.title] = aligned_sections(sh, a)[1] if (sh and a) else 0.0
    for t in plan["transitions"]:
        a, b = songs[t["from"]], songs[t["to"]]
        ref = None
        for r in lst.get("routes", []):
            if r["middle"] == a.title and r["leaf"] == b.title and r.get("link_mid_leaf"):
                ref = r["link_mid_leaf"]
            if r["root"] == a.title and r["middle"] == b.title and r.get("link_root_mid"):
                ref = r["link_root_mid"]
        if ref:
            ro, ri = ref["out"] + offs[a.title], ref["in"] + offs[b.title]
            h = {"out": round(ro, 3), "in": round(ri, 3),
                 "diff_out": round(t["out"] - ro, 2), "diff_in": round(t["in"] - ri, 2)}
            # 自動検知した候補の中に、人が決めた位置があるか（一番近い候補と、その順位）
            cands = sorted(candidate_links(a, b), key=lambda x: -x.score)
            if cands:
                j = int(np.argmin([abs(c.out - ro) + abs(c.inn - ri) for c in cands]))
                c = cands[j]
                h["nearest"] = {"out": round(c.out, 3), "in": round(c.inn, 3), "kind": c.kind,
                                "diff_out": round(c.out - ro, 2), "diff_in": round(c.inn - ri, 2),
                                "rank": j + 1, "of": len(cands)}
            t["human"] = h


def _build(songs, lst, min_sec, chorus_grace, links):
    manual = {}
    for i in range(len(songs) - 1):
        a, b = songs[i].title, songs[i + 1].title
        for r in lst.get("routes", []):
            link = None
            if r["middle"] == a and r["leaf"] == b and r.get("link_mid_leaf"):
                link = r["link_mid_leaf"]
            if r["root"] == a and r["middle"] == b and r.get("link_root_mid"):
                link = r["link_root_mid"]
            if link:
                # つなぎメモもシートの秒数なので、それぞれの曲のズレを足す
                manual[(i, i + 1)] = {"out": link["out"] + songs[i].sheet_offset,
                                      "in": link["in"] + songs[i + 1].sheet_offset}
    for k, v in (links or {}).items():
        manual[(int(k), int(k) + 1)] = {"out": float(v["out"]), "in": float(v["in"]),
                                        "kind": v.get("kind", "選んだ候補"), "why": "画面で選び直し"}
    return plan_chain(songs, min_sec=min_sec, manual_links=manual, chorus_grace=chorus_grace)


def save_plan(plan, path):
    Path(path).write_text(json.dumps(plan, ensure_ascii=False, indent=1), encoding="utf-8")
