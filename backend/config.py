"""AutoDJ 共通設定。"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
AUDIO_DIR = DATA_DIR / "audio"
ANALYSIS_DIR = DATA_DIR / "analysis"
DB_PATH = DATA_DIR / "autodj.sqlite3"

# 確定ジャンル（v0.3）
GENRES = ["J-pop", "Rock", "Funk", "Fusion", "Jazz", "Classic", "Electro"]

# 解析パラメータ
SR = 22050            # 解析用サンプルレート
ONSET_HOP = 256       # ビート検出用（約11.6ms）
FEAT_HOP = 512        # 特徴量用（約23ms）
ANALYZER_VERSION = "0.3.0"
SCHEMA_VERSION = 1

for d in (AUDIO_DIR, ANALYSIS_DIR):
    d.mkdir(parents=True, exist_ok=True)
