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
- **mp3 で保存**: 右上のメニュー（☰）の「⬇ MP3で保存」（評価の画面にもあり）で、聞いたのと同じミックス（つなぎ位置・繋ぎ方・BPM/キー合わせ）を
  1つの mp3（256kbps）としてダウンロードできる。ファイル名は `AutoDJ_曲1_曲2_曲3.mp3`。
  一度作ったものは `data/cache/mix/` に残り、同じ指定なら書き出し直さない。ffmpeg が無いときは wav になる

### 画面から曲を追加する（YouTube）
右上のメニュー（☰）→「＋ 曲を追加」で、YouTube の URL を貼る。動画の題名から曲名・アーティストを推測して入れる（直せる）。
「追加する」を押すと、裏で **ダウンロード → DB に登録 → 解析** まで進み（1曲 30秒〜1分）、終わると曲選びに出る。
進み具合は ☰ のバッジとメニューの「追加した曲」に出る。画面はそのまま使っていてよい。

- **曲選びのルートに入れる**: 「1曲目 → 2曲目 → 3曲目」のどこに入れるかを選ぶ（曲選びはルートでつながる曲だけが出るため）。
  ルートだけ足したいときは「＋ ルートを追加」。画面で足したルートはそこで ✕ で消せる
- **曲選びで「音源なし」の曲**: その曲を選んだまま ☰ →「この曲の動画を追加」で、その曲名のまま音源を入れられる
- 同じ曲名がすでにあれば、新しい動画に入れ替える。同じ動画を別の曲名で入れると、曲名を付け替える
- DB（`data/autodj.sqlite3`）に入るもの: 曲（`tracks`、シートの曲と同じ `sheet` セット）と、画面で足したルート（`routes`）。
  「この曲はこの動画」は `data/sheet/downloads.csv` に手動指定（manual）として残すので、あとで `sheet-fetch` しても選び直さない。
  スプレッドシート自体は書き換えない（曲選びには シートのルート ＋ 画面で足したルート の両方が出る）
- コマンドでも同じことができる:
  `python -m backend.cli add-song 'https://youtu.be/...' --route 青と夏 LOSER アイドル`（`--title` で曲名を指定）、
  画面で足したルートの一覧・削除は `python -m backend.cli routes [--delete ID]`

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
- 候補は「出る曲の**どの区間の終わり**」×「入る曲の**どの区間の頭**（アウトロ以外）／曲頭」。Aメロ終わり→別の曲のサビ頭 などもOK。すべて小節頭に合わせる
- 点数 =「つながりそうか」: テンポの近さ 0.30 ＋ キーの相性（Camelot）0.30 ＋ 音の勢いのつながり（出る直前2小節と入った直後2小節の音の大きさの近さ）0.25 ＋ 区切りの確かさ 0.15
  ＋ 区間の種類の小さな好み（サビ終わり・サビ頭・曲頭を少しだけ優先、2回目以降の区間に入るのは少し不利）
- シートに構成があればそれを使い、なければ自動解析の構造を使う（画面では「自動検知」で、シートを使わずに決められる）
- シートのつなぎメモ（例「1:51のサビ終わり→1:15のサビ始まり」）があれば最優先（自動検知モードでは比較用に表示だけ）
- 1曲を流す時間は最低 `--min-sec` 秒（既定90秒）。サビ終わりで出るときは 15 秒早くてもよい。満たせない組合せは選ばない
- 1曲目は、出る位置から最低秒数以上前の区間頭（Aメロ優先）から流し始める
- 重なりは、テンポが近ければ1小節（入る曲を伸縮して拍を揃え、低音を入れ替え）、遠ければ1拍で切り替え。`--styles fade,cut` でつなぎ方を指定

BPM とキーを合わせる（backend/matching.py・audio_fx.py。既定でオン）
- 入る曲を、いま鳴っている曲に合わせる。合わせた曲はその曲の最後までそのまま流し、次の曲はそれに合わせる
- BPM: 入る曲のテンポを出る曲に揃える（半分・倍テンポも同一視、±8% まで。音程は変えない）
- キー: 入る曲を ±2半音まで移調して、Camelot で同じか隣のキーにする（すでに相性が良ければ動かさない）
- 加工は ffmpeg（rubberband フィルタがあれば高音質、なければ asetrate＋atempo）。`data/cache/fx/` に保存して使い回す。元の mp3 は変えない
- 画面の「BPMを合わせる」「キーを合わせる」、コマンドの `plan --no-match-bpm --no-match-key` で切り替え

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

## 評価からつなぎ方を学習する（使うほど賢くなる）

再生が終わると「つなぎ目はどうだった？」が出ます（再生中も右上の「★ 評価する」で開ける）。
つなぎ目ごとに **良い／微妙** を押し、微妙なら「こっちの方が良さそう」をほかの候補から選べます（任意）。
「繋ぎ方を選んでね」の画面で候補を選び直してからスタートした場合も、「自動の1位より、こっちが良い」という評価として送られます。

- 評価は `data/feedback.jsonl` に貯まり、送るたびにつなぎ目の点数の重みを学び直します（`data/link_model.json`）。
- 点数 = 手がかり（テンポの近さ・キーの相性・音の勢いのつながり・区切りの確かさ・どの区間か）× 重み。
  最初は手で決めた重み（0.30 / 0.30 / 0.25 / 0.15 ＋ 小さな好み）で、評価を「どちらのつなぎ目が良いか」の比較にして
  学習します（Bradley–Terry ／ ロジスティック回帰）。評価が少ないうちは手で決めた重みからあまり離れません。
- いまどちらの重みを使っているかは、右上のメニュー（☰）に出ます。
- **お試しモード**（メニューで ON/OFF、既定は ON）: ときどき1位ではなく、点数が1位に近い2位・3位の候補でつなぎます
  （画面に「お試し」と表示）。その評価は「本来の1位と比べてどうだったか」として学習に使います（強化学習のバンディットの考え方）。
  試す確率は評価が無いうちは50%、評価が20回たまると約25%、最低10%まで下がります。同じ曲の組み合わせなら、評価が増えるまでは同じ結果です。

```bash
python -m backend.cli feedback                 # 集まった評価と、いまの重み・答え合わせの結果
python -m backend.cli learn-links              # 評価から学び直す（画面から送ったときは自動で学習済み）
python -m backend.cli learn-links --memos      # シートのつなぎメモも「人が選んだ位置」としてお手本に加える
python -m backend.cli learn-links --reset      # 学習した重みを消して、手で決めた値に戻す
```

答え合わせは「評価1回分（ミックス1回）ずつ外して学習 → 外した評価を当てられたか」で、手で決めた重みの一致率と並べて出します。

## 起動が遅いとき（iCloud）

デスクトップや書類フォルダは iCloud と同期されていることが多く、「Mac のストレージを最適化」がオンだと、
しばらく使っていないファイル（`.venv` のライブラリや mp3）が Mac から消され、使うたびにダウンロードし直しになります。
起動や読み込みが極端に遅いときは、プロジェクトを iCloud の外に移してください。

```bash
mkdir -p ~/dev
mv ~/Desktop/project/AutoDJ ~/dev/AutoDJ
cd ~/dev/AutoDJ
rm -rf .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
python -m backend.app
```

移動したあと、DB に入っている古い場所は、最初の起動時に `data/audio`・`data/analysis` へ自動で付け替えます。
移さない場合は、Finder で AutoDJ フォルダを右クリック →「ダウンロードしたままにする」でも改善します。

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
