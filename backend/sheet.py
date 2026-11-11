"""Googleスプレッドシート（曲リスト・構成入力）の取り込み。

- 構成入力: 曲ごとのセクション（イントロ/A/B/サビ…）と開始秒 → 手付けの構造（正解データ兼・再生計画の材料）
- 曲リスト: ルート曲 → 中間曲 → 曲名 の流れと、つなぎメモ（例「1:28→1:07」）→ つなぎ秒数

シートは「リンクを知っている全員が閲覧可」にしておけば CSV で直接取得できる。
取得できない場合は、各タブを「ファイル → ダウンロード → CSV」で data/sheet/ に置けばよい。
"""
import csv
import io
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

from .config import DATA_DIR

SHEET_DIR = DATA_DIR / "sheet"
DEFAULT_SHEET_ID = "1tqNzwou98-HD8sY1n_6fh7o9hl4Geh5TA833hjDkch8"
TABS = {"songs": 633969671, "structure": 1001, "videos": 1003}   # 曲リスト / 構成入力 / 動画URL の gid
OPTIONAL_TABS = {"videos"}   # 無くても動く

# ラベル → 大分類（推薦や計画で使う）
ROLE = {
    "イントロ": "intro", "A": "verse", "B": "prechorus", "Cメロ": "bridge",
    "サビ": "chorus", "落ちサビ": "chorus_drop", "間奏": "interlude",
    "アウトロ": "outro", "その他": "other",
}

# 曲リストの「ルート曲」「中間曲」は検索URLがないので、アーティストをここで補う（要確認・編集可）
EXTRA_ARTISTS = {
    "青と夏": "Mrs. GREEN APPLE", "シルエット": "KANA-BOON", "テオ": "Omoi",
    "君と夏フェス": "SHISHAMO", "イケナイ太陽": "ORANGE RANGE", "夜の踊り子": "サカナクション",
    "U.S.A": "DA PUMP", "ノーダウト": "Official髭男dism", "好きすぎて滅": "M!LK",
    "チャンカパーナ": "NEWS", "女々しくて": "ゴールデンボンバー", "サマータイムシンデレラ": "緑黄色社会",
    "LOSER": "米津玄師", "STAY TUNE": "Suchmos", "踊り子": "Vaundy", "ハレンチ(ちゃんみな)": "ちゃんみな",
    "3原色": "YOASOBI", "最高到達点": "SEKAI NO OWARI", "Mela !": "緑黄色社会", "TOKYO GIRL": "Perfume",
}

_TIME = re.compile(r"(\d{1,2}):(\d{2})")


def parse_time(s):
    """'1:28' → 88.0。数値ならそのまま。解釈できなければ None"""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    m = _TIME.fullmatch(s)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    try:
        return float(s)
    except ValueError:
        return None


def times_in(text):
    """メモ中の m:ss を順に秒で返す（'1:51のサビ終わり→1:15のサビ始まり' → [111, 75]）"""
    return [int(a) * 60 + int(b) for a, b in _TIME.findall(text or "")]


# ---------- 取得 ----------
def fetch_csv(sheet_id=DEFAULT_SHEET_ID, out_dir=SHEET_DIR):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, gid in TABS.items():
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = r.read()
        except Exception:
            if name in OPTIONAL_TABS:
                continue
            raise
        if data.lstrip().startswith(b"<"):
            raise RuntimeError("CSVではなくHTMLが返りました。シートを「リンクを知っている全員が閲覧可」にするか、"
                               f"手動で CSV を {out_dir}/{name}.csv に保存してください。")
        p = out_dir / f"{name}.csv"
        p.write_bytes(data)
        paths[name] = p
    return paths


def _rows(path):
    text = Path(path).read_text(encoding="utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


# ---------- 構成入力 ----------
def parse_structure(path):
    """戻り値: {曲名: {"sections": [{label, role, start, end, memo}], "end": 秒}}"""
    rows = _rows(path)[1:]
    songs = {}
    for r in rows:
        r = (r + [""] * 6)[:6]
        name, label, mmss, sec, _end, memo = (c.strip() for c in r)
        if not name or not label:
            continue
        t = parse_time(sec) if sec else parse_time(mmss)
        if t is None:
            t = parse_time(mmss)
        if t is None:
            continue
        song = songs.setdefault(name, {"sections": [], "end": None})
        if label == "終了":
            song["end"] = float(t)
            continue
        song["sections"].append({"label": label, "role": ROLE.get(label, "other"),
                                 "start": float(t), "memo": memo})
    for name, s in list(songs.items()):
        secs = sorted(s["sections"], key=lambda x: x["start"])
        for a, b in zip(secs, secs[1:]):
            a["end"] = b["start"]
        if secs:
            secs[-1]["end"] = s["end"] if s["end"] else None
        s["sections"] = secs
        if not secs or s["end"] is None:
            songs.pop(name)  # 未完成の曲は除外
    return songs


# ---------- 曲リスト ----------
def _query_from_url(url):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("search_query", [""])[0]
    return q.strip()


def parse_song_list(path):
    """戻り値: dict(songs={曲名: 検索クエリ}, routes=[{root, middle, leaf, link_mid_leaf, link_root_mid, ...}])"""
    rows = _rows(path)
    head, body = rows[0], rows[1:]
    songs, routes = {}, []
    for r in body:
        r = (r + [""] * 7)[:7]
        root, root_info, mid, mid_info, leaf, memo, url = (c.strip() for c in r)
        if not leaf:
            continue
        songs[leaf] = _query_from_url(url) or leaf
        for name in (root, mid):
            if name and name not in songs:
                songs[name] = f"{name} {EXTRA_ARTISTS.get(name, '')}".strip()
        route = {"root": root, "middle": mid, "leaf": leaf, "memo": memo, "middle_info": mid_info,
                 "root_bpm_key": root_info}
        t = times_in(memo)
        route["link_mid_leaf"] = {"out": t[0], "in": t[1]} if len(t) >= 2 else None
        # 中間情報は通常「BPM,Key」。時刻が2つあれば ルート→中間 のつなぎとみなす（要確認）
        t2 = times_in(mid_info)
        route["link_root_mid"] = {"out": t2[0], "in": t2[1], "unverified": True} if len(t2) >= 2 else None
        routes.append(route)
    return {"songs": songs, "routes": routes}


# ---------- 動画URL ----------
def parse_videos(path):
    """「動画URL」タブ: 曲名 → YouTube 動画ID。構成入力の秒数を計った動画を入れておけば、推測せずにそれを使う"""
    from .fetch_songs import video_id_from
    out = {}
    if not Path(path).exists():
        return out
    for r in _rows(path)[1:]:
        r = (r + ["", ""])[:2]
        name, url = r[0].strip(), r[1].strip()
        if name and url:
            try:
                out[name] = video_id_from(url)
            except ValueError:
                pass
    return out


def load(sheet_dir=SHEET_DIR):
    """data/sheet/ の CSV を読み、structure.json / songs.json を作って返す"""
    sheet_dir = Path(sheet_dir)
    structure = parse_structure(sheet_dir / "structure.csv")
    lst = parse_song_list(sheet_dir / "songs.csv")
    lst["videos"] = parse_videos(sheet_dir / "videos.csv")
    (sheet_dir / "structure.json").write_text(json.dumps(structure, ensure_ascii=False, indent=1), encoding="utf-8")
    (sheet_dir / "songs.json").write_text(json.dumps(lst, ensure_ascii=False, indent=1), encoding="utf-8")
    return structure, lst


def load_cached(sheet_dir=SHEET_DIR):
    sheet_dir = Path(sheet_dir)
    s, l = sheet_dir / "structure.json", sheet_dir / "songs.json"
    if not (s.exists() and l.exists()):
        return load(sheet_dir)
    return json.loads(s.read_text(encoding="utf-8")), json.loads(l.read_text(encoding="utf-8"))
