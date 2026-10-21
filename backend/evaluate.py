"""手で付けた正解と解析結果を比べて精度を出す（仕様書 11章「評価方法」）。

正解ファイル（JSON）の例:
[
  {"track_id": 1, "bpm": 96, "key": "Ab major",
   "sections": [2.5, 12.5, 32.5, 52.5, 72.5]},      ← セクション開始時刻(秒)。曲を聴いてメモする
  {"track_id": 2, "bpm": 128, "key": "F# minor"}      ← 分かる項目だけでよい
]
"""
import json
import numpy as np

from .analysis.key import MAJOR_NAMES, MINOR_NAMES
from .library import load_analysis

_ALIASES = {"C#": "Db", "D#": "Eb", "Gb": "F#", "G#": "Ab", "A#": "Bb", "Db": "C#", "Ab": "G#"}


def parse_key(s):
    name, mode = s.strip().split()
    mode = mode.lower()
    names = MAJOR_NAMES if mode == "major" else MINOR_NAMES
    if name not in names:
        name = _ALIASES.get(name, name)
    return names.index(name), mode


def key_score(est, ref):
    """MIREX 方式: 一致1.0 / 5度0.5 / 相対調0.3 / 同主調0.2 / その他0"""
    (et, em), (rt, rm) = est, ref
    if (et, em) == (rt, rm):
        return 1.0, "一致"
    if em == rm and (et - rt) % 12 in (5, 7):
        return 0.5, "5度違い"
    if em != rm:
        rel = (rt + 9) % 12 if rm == "major" else (rt + 3) % 12
        if et == rel:
            return 0.3, "相対調"
        if et == rt:
            return 0.2, "同主調"
    return 0.0, "不一致"


def bpm_score(est, ref):
    if abs(est - ref) / ref <= 0.04:
        return "一致"
    for f in (2, 0.5, 3, 1 / 3, 1.5, 2 / 3):
        if abs(est - ref * f) / (ref * f) <= 0.04:
            return f"倍率違い(x{f:.2g})"
    return "不一致"


def boundary_prf(est, ref, tol):
    est, ref = np.asarray(est), np.asarray(ref)
    if len(est) == 0 or len(ref) == 0:
        return 0.0, 0.0, 0.0
    used, hits = set(), 0
    for r in ref:
        d = np.abs(est - r)
        j = int(np.argmin(d))
        if d[j] <= tol and j not in used:
            used.add(j)
            hits += 1
    p, r = hits / len(est), hits / len(ref)
    return p, r, (2 * p * r / (p + r) if p + r else 0.0)


def evaluate(truth_path):
    truth = json.load(open(truth_path, encoding="utf-8"))
    rows, ks, bs, fs = [], [], [], []
    for t in truth:
        a = load_analysis(t["track_id"])
        if a is None:
            rows.append(f"track {t['track_id']}: 未解析")
            continue
        line = [f"track {t['track_id']}:"]
        if "bpm" in t:
            r = bpm_score(a["tempo"]["bpm"], t["bpm"])
            bs.append(r == "一致")
            line.append(f"BPM {a['tempo']['bpm']} vs {t['bpm']} → {r}")
        if "key" in t:
            sc, why = key_score((a["key"]["tonic"], a["key"]["mode"]), parse_key(t["key"]))
            ks.append(sc)
            line.append(f"Key {a['key']['name']} vs {t['key']} → {why}")
        if "sections" in t:
            bar = 240.0 / a["tempo"]["bpm"]
            est = [s["start"] for s in a["structure"]["sections"]]
            p, r, f = boundary_prf(est, t["sections"], tol=bar)
            fs.append(f)
            line.append(f"境界 P={p:.2f} R={r:.2f} F={f:.2f}（許容±1小節）")
        rows.append("  ".join(line))
    summary = []
    if bs:
        summary.append(f"BPM一致率 {np.mean(bs):.0%} ({len(bs)}曲)")
    if ks:
        summary.append(f"Keyスコア(MIREX) {np.mean(ks):.2f} / 完全一致 {np.mean([k == 1 for k in ks]):.0%}")
    if fs:
        summary.append(f"境界F値 平均 {np.mean(fs):.2f}")
    return "\n".join(rows + ["---"] + summary)
