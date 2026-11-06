"""BPM とキーを合わせる（ビートマッチ・ハーモニックミックス）。

DJ と同じく「入ってくる曲を、いま鳴っている曲に合わせる」:
- BPM: 入る曲のテンポを、出る曲の（合わせた後の）テンポに揃える。半分・倍テンポも同じとみなす。
  伸び縮みは ±MAX_STRETCH まで（DJ のピッチフェーダーの範囲）。それ以上ずれる組は合わせない。
  テンポを変えても音程は変えない（タイムストレッチ）。
- キー: 入る曲を ±MAX_SEMITONES 半音まで移調して、出る曲の（合わせた後の）キーと
  Camelot で同じか隣（相性が良い）になるようにする。すでに相性が良ければ動かさない。
- 合わせた曲は、その曲の最後までその BPM・キーのまま流す（次の曲はそれに合わせる）。

再生用の音声は audio_fx.processed() が作る（元の mp3 は変えない）。
計画の秒数は元の曲の秒数のまま持ち、再生用には pb_*（加工後の音声での秒数）を足す。
"""
import math

from .analysis.key import camelot, camelot_distance, key_name

MAX_STRETCH = 0.08      # テンポを伸び縮みさせる上限（±8%）
MAX_SEMITONES = 2       # 移調の上限（±2半音。歌が不自然にならない範囲）
MIN_STRETCH = 0.002     # これより小さいテンポ差は合わせなくても拍がずれない（加工しない）


def tempo_ratio(bpm_target, bpm_song):
    """bpm_song を bpm_target に揃える倍率（半分・倍テンポも同一視）。揃えられなければ None"""
    best = min((bpm_target * f / bpm_song for f in (0.5, 1.0, 2.0)), key=lambda r: abs(math.log(r)))
    return best if abs(best - 1) <= MAX_STRETCH else None


def shift_key(tonic, mode, semitones):
    t = (tonic + semitones) % 12
    return t, mode, camelot(t, mode), key_name(t, mode)


def best_semitones(cam_target, tonic, mode):
    """cam_target と相性が良くなる一番小さい移調（半音）。同じキー > 隣 の順、同点なら動かす量が小さい方"""
    best = None
    for s in sorted(range(-MAX_SEMITONES, MAX_SEMITONES + 1), key=abs):
        d = camelot_distance(cam_target, camelot((tonic + s) % 12, mode))
        if best is None or (d, abs(s)) < (best[1], abs(best[0])):
            best = (s, d)
    return best


def apply_matching(plan, songs, match_bpm=True, match_key=True):
    """plan（mixplan.plan_chain の結果）に、各曲の rate（テンポ倍率）・semitones（移調）と
    再生用の秒数 pb_* を書き足す。songs は plan の曲順の mixplan.Song（tonic, mode を持つ）"""
    items, trans = plan["items"], plan["transitions"]
    prev = None   # 直前の曲の（合わせた後の）BPM とキー
    for i, (it, s) in enumerate(zip(items, songs)):
        rate, semi = 1.0, 0
        note_bpm = note_key = None
        if prev is not None:
            if match_bpm:
                r = tempo_ratio(prev["bpm"], s.bpm)
                if r is None:
                    note_bpm = f"テンポ差が大きいので合わせない（{s.bpm:.0f}→{prev['bpm']:.0f} BPM）"
                elif abs(r - 1) >= MIN_STRETCH:
                    rate = r
            if match_key and s.tonic is not None:
                d0 = camelot_distance(prev["camelot"], s.camelot)
                if d0 > 1:
                    semi, d = best_semitones(prev["camelot"], s.tonic, s.mode)
                    if d > 1:   # 相性の良いキー（同じか隣）にならないなら、無理に移調しない
                        semi = 0
                        note_key = f"±{MAX_SEMITONES}半音では相性の良いキーにならないので移調しない"
        tonic, mode, cam, name = shift_key(s.tonic, s.mode, semi) if s.tonic is not None else (None, None, s.camelot, None)
        eff_bpm = s.bpm * rate
        it.update({"rate": round(rate, 5), "semitones": semi, "orig_bpm": s.bpm, "orig_camelot": s.camelot,
                   "bpm": round(eff_bpm, 2), "camelot": cam, "key_name": name,
                   "pb_start": round(it["start"] / rate, 3), "pb_end": round(it["end"] / rate, 3),
                   "match_note": [x for x in (note_bpm, note_key) if x]})
        prev = {"bpm": eff_bpm, "camelot": cam}
    for t in trans:
        a, b = items[t["from"]], items[t["to"]]
        t["pb_out"] = round(t["out"] / a["rate"], 3)
        t["pb_in"] = round(t["in"] / b["rate"], 3)
        gap = abs(a["bpm"] / b["bpm"] - 1)
        gap = min(gap, abs(a["bpm"] / (2 * b["bpm"]) - 1), abs(2 * a["bpm"] / b["bpm"] - 1))
        t["matched"] = {"bpm": b["rate"] != 1.0, "key": b["semitones"] != 0,
                        "rate": b["rate"], "semitones": b["semitones"],
                        "tempo_gap_after": round(gap, 4),
                        "key_distance_after": camelot_distance(a["camelot"], b["camelot"])}
        # テンポが揃えば、拍を合わせて長めに重ねられる（おまかせの重ね方もここで決め直す）
        t["tempo_gap"] = round(gap, 3)
        t["overlap_beats"] = 4 if gap <= 0.06 else 1
    plan["matching"] = {"bpm": match_bpm, "key": match_key}
    return plan
