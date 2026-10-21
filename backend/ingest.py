"""Phase 0: 取り込み。YouTube URL またはローカルファイル → data/audio に保存 → DB 登録。

注意: YouTube からのダウンロードは個人利用の範囲で、著作権と YouTube の規約に従って行うこと。
"""
import shutil
from pathlib import Path

from .config import AUDIO_DIR
from .db import connect, get_or_create_set, normalize_genres, now


def _ydl_opts(outdir: Path, playlist: bool):
    return {
        "format": "bestaudio/best",
        "outtmpl": str(outdir / "%(id)s.%(ext)s"),
        "noplaylist": not playlist,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                            "preferredquality": "320"}],
    }


def _register(con, *, title, artist, url, source_id, path, genres, set_id, duration):
    row = con.execute("SELECT id FROM tracks WHERE source_id=?", (source_id,)).fetchone()
    if row:
        con.execute("UPDATE tracks SET genre=?, set_id=COALESCE(?, set_id) WHERE id=?",
                    (",".join(genres), set_id, row["id"]))
        con.commit()
        return row["id"], False
    cur = con.execute(
        """INSERT INTO tracks(title, artist, source_url, source_id, file_path, genre, set_id, duration, added_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (title, artist, url, source_id, str(path), ",".join(genres), set_id, duration, now()))
    con.commit()
    return cur.lastrowid, True


def ingest_urls(urls, genres, set_name=None, playlist=False, con=None):
    """YouTube（yt-dlp 対応サイト）から取り込む。戻り値: [(track_id, title, 新規か)]"""
    import yt_dlp
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg が見つかりません。macOS なら `brew install ffmpeg` を実行してください。")
    con = con or connect()
    genres = normalize_genres(genres)
    set_id = get_or_create_set(con, set_name) if set_name else None
    results = []
    with yt_dlp.YoutubeDL(_ydl_opts(AUDIO_DIR, playlist)) as ydl:
        for url in urls:
            info = ydl.extract_info(url, download=True)
            entries = info.get("entries") or [info]
            for e in entries:
                if not e:
                    continue
                path = AUDIO_DIR / f"{e['id']}.mp3"
                tid, new = _register(
                    con, title=e.get("title"), artist=e.get("artist") or e.get("uploader"),
                    url=e.get("webpage_url") or url, source_id=f"yt:{e['id']}", path=path,
                    genres=genres, set_id=set_id, duration=e.get("duration"))
                results.append((tid, e.get("title"), new))
    return results


def ingest_files(paths, genres, set_name=None, copy=True, con=None):
    """手元の音源ファイルを取り込む（copy=True なら data/audio に複製）。"""
    con = con or connect()
    genres = normalize_genres(genres)
    set_id = get_or_create_set(con, set_name) if set_name else None
    results = []
    for p in map(Path, paths):
        if not p.exists():
            raise FileNotFoundError(p)
        dest = AUDIO_DIR / p.name if copy else p.resolve()
        if copy and not dest.exists():
            shutil.copy2(p, dest)
        tid, new = _register(con, title=p.stem, artist=None, url=None,
                             source_id=f"file:{p.name}", path=dest, genres=genres,
                             set_id=set_id, duration=None)
        results.append((tid, p.stem, new))
    return results
