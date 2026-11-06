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
| extra_features.py | 区切りモデル用の追加特徴（音色・和声の変化、ドラムのフィル、繰り返しの始まり/終わり、歌・低音の段差など） |
| boundary_model.py | 小節ごとの「区切り確率」（前後2小節も見るロジスティック回帰）と、採った区切りの信頼度 |

出力は `data/analysis/<id>.json`（拍・小節頭・キー候補・セクション・小節ごとのエネルギー）。

## ブラウザで使う（localhost）
```bash
source .venv/bin/activate
python -m backend.app          # → http://localhost:5050 をブラウザで開く
```
画面の流れ（Figma「DJプロジェクト」の UI ページが元）
1. **曲を選ぶ**: 左の円盤をスクロール（↑↓）で回し、クリック／Enter で選ぶ（選んだ順に色枠と番号）。
   右の大きいジャケットを押す（スペース）とサビから試聴。波形の色は区間（サビ=オレンジ / Aメロ=青 …）。
   最後に選んだ曲から、シートのルートでつながる曲には「シートでつながる曲」と出る
2. **繋ぎ方を選ぶ**: 曲の間の ▶ を押して フェードイン（ピンク）／カットイン（青）／おまかせ（黄）。
   カーソルを合わせるとそのつなぎ方で試聴できる。下で「1曲を最低○秒流す」を変えられる
3. **再生**: 中央のジャケットが回る（押すと一時停止）。周りの輪が音に合わせて動く。▶▶▶（→キー）で次の曲へ、
   下の波形をクリックでその位置へ

- ジャケット: `data/jackets/曲名.jpg`（png/webp も可）を置くとそれを使う。無ければ取り込んだ YouTube 動画のサムネイル
- 再生はブラウザの Web Audio API で「この mp3 を○秒から」と指定して鳴らし、つなぎ目は音量フェードと低音カットで行う（ファイルは切らない）
- 以前の確認用の画面は http://localhost:5050/classic
- つなぎ方はコマンドでも指定できる: `python -m backend.cli plan 曲A 曲B 曲C --styles fade,cut --render mix.mp3`

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

## 区切りの学習（シートの構成を正解にする）
```bash
python -m backend.cli learn                       # シートの構成で学習 → data/model/boundary.json
python -m backend.cli learn --min-confidence 0.75 # 信頼度75%未満の区切りは採らない
python -m backend.cli analyze --set sheet -f      # 学習したモデルで解析し直す
```
- 1曲ずつ外して学習→外した曲で採点し、ルールより良いときだけ使う。しきい値も採点で一番良い値を自動で選ぶ
- 採った区切りには「信頼度」（その区切りが正解の±3秒以内にある見込み）が付く（`sections[].boundary_confidence`）
- 自分で learn していなくても、同梱のモデル（シート44曲で学習、`backend/analysis/default_boundary_model.json`）を使う
- 2026-11 時点の成績（シート44曲、1曲ずつ外して採点、±3秒）: ルール F=0.55 → 学習モデル F=0.68

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
