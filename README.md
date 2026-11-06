# AutoDJ

人間DJを補助するWebアプリ（ジャンル横断のブリッジ探しが最終目標）。仕様は [SPEC.md](SPEC.md)。
現在の実装範囲: **Phase 0（取り込み）と Phase 1（解析）**。

## セットアップ（macOS）
```bash
brew install ffmpeg            # yt-dlp の mp3 変換に必要
cd AutoDJ
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q tests      # 合成曲で精度テスト（約1分）
```

## 使い方
```bash
# 1. 取り込み（約10曲で1セット）
python -m backend.cli ingest "https://www.youtube.com/watch?v=XXXX" -g J-pop -s set01
python -m backend.cli ingest "https://www.youtube.com/playlist?list=YYYY" -g J-pop -s set01 --playlist
python -m backend.cli add ~/Music/foo.mp3 -g "J-pop,Rock" -s set01     # 手元のファイル

# 2. 解析
python -m backend.cli analyze --set set01

# 3. 確認
python -m backend.cli list
python -m backend.cli show 1
```
ジャンル: J-pop, Rock, Funk, Fusion, Jazz, Classic, Electro（複数可、`tag` で変更）

## 解析の中身（backend/analysis）
| ファイル | 内容 |
|---|---|
| rhythm.py | BPM推定（自己相関＋8分音符補強＋低域による倍テンポ判定）、拍グリッド（オンセットへ直接フィット）、可変テンポ曲の位相追従、小節頭推定 |
| key.py | チューニング補正CQTクロマ、2種のキープロファイル平均、窓ごとの投票、Camelot、転調タイムライン |
| structure.py | 小節単位の自己類似＋音量段差＋低域段差による境界検出、4/8/16小節グリッド吸着、セクションのグループ化と役割推定 |

出力は `data/analysis/<id>.json`（拍・小節頭・キー候補・セクション・小節ごとのエネルギー）。

## スプレッドシート連携とミックス（つなぎ計画）
音声ファイルは**切らない**。1曲1ファイルのまま、「何秒から何秒まで流すか」「どの秒で次の曲に入るか」だけを計画として持つ。
再生は「このファイルを ○秒から」と指定するだけ（書き出しも同じ方式）。

```bash
# 1. シートを取り込む（構成入力＝区間と開始秒、曲リスト＝つなぎメモ）
python -m backend.cli sheet-sync

# 2. シートの曲を一括ダウンロード（シートの「終了」時刻と長さが ±4秒で合う動画を自動で選ぶ）
python -m backend.cli sheet-fetch --dry-run     # まず選ばれる動画を確認
python -m backend.cli sheet-fetch               # ダウンロード
#    違う動画が選ばれたら data/sheet/downloads.csv の video_id を書き換えて再実行

# 3. 解析（BPM・拍・キー・構造）
python -m backend.cli analyze --set sheet

# 4. 自動の構造解析を、シートの構成を正解として採点
python -m backend.cli sheet-eval

# 5. 曲順を渡すと、つなぎ位置と再生区間を決めてミックスを書き出す
python -m backend.cli plan 青と夏 テオ リライト --min-sec 90 --render mix.mp3
```

つなぎの決め方（backend/mixplan.py）
- 候補は「出る曲のサビ終わり」×「入る曲のサビ頭／曲頭／Aメロ頭」。すべて小節頭に合わせる
- シートに構成があればそれを使い、なければ自動解析の構造を使う
- シートのつなぎメモ（例「1:51のサビ終わり→1:15のサビ始まり」）があれば最優先
- 1曲を流す時間は最低 `--min-sec` 秒（既定90秒）。満たせない組合せは選ばない
- 1曲目は、出る位置から最低秒数以上前の区間頭（Aメロ優先）から流し始める
- 重なりは、テンポが近ければ1小節（入る曲を伸縮して拍を揃え、低音を入れ替え）、遠ければ1拍で切り替え

## 精度の測り方
`truth.json` に、聴いて確かめた正解を書いて比較する（分かる項目だけでよい）:
```json
[{"track_id": 1, "bpm": 128, "key": "F# minor", "sections": [0.5, 15.5, 45.2, 75.0]}]
```
```bash
python -m backend.cli evaluate truth.json
```
BPM一致率、Keyスコア（MIREX方式）、境界F値（±1小節）を出力。チューニングはこの数値を見て行う。

## 既知の限界
- 生演奏（クリックなし）の曲で、ドラムのない区間は拍を補間で埋める → `uncertain_beat_ranges` に出る
- 同じバンド編成のまま A メロ→サビに移る曲では、境界を見落とすことがある（歌メロの特徴を使っていないため）
- 拍子は 4/4 と 3/4 のみ判定
- YouTube からの取り込みは個人利用の範囲で（著作権・規約に注意）
