# See-through Rig

一枚絵から、Live2D のように自然に動く 2D キャラクターを作って調整するツールです。
[See-through](https://github.com/shitagaki-lab/see-through)（SIGGRAPH 2026、アニメ絵の単一画像レイヤー分解）で
イラストを部位レイヤーに分けます。そこにメッシュ・変形器・物理演算・表情差分を自動で組み、
ブラウザ上のエディタで動かしながら調整します。

![Aoi showcase](media/aoi-showcase.jpg?v=editor20bust)

- 確認用動画：[Aoi showcase](../showcase.html)（20 秒、1920×1080）。葵の顔と上半身を大きく映し、左右にある実際のエディタスライダーを操作し、開始フレームから風を有効にした録画です。動画ファイルは [media/aoi-showcase.mp4](media/aoi-showcase.mp4?v=editor20bust)
- 既定のモデルは `models/aoi`（レシピ `recipes/aoi_vowels.json`）。目・5母音の採用記録は [face-keys-release.md](face-keys-release.md)、従来の経緯は [CANDIDATES.md](CANDIDATES.md)
- 対応する動き：首をかしげる・うなずく、体の傾き、呼吸、視線、瞬き・ウィンク、口パク、
  表情（通常／にっこり／驚き／困り／照れ）、体全体の前後（近づく・遠のく）と左右の重心移動、
  髪の束ごとの揺れ（前髪5束・横の房2本・後ろ髪15束がそれぞれ別々に揺れる）、リボンの紐・胸の紐（房飾り）の揺れ、スカートの揺れ
- 再生設定「風が吹く」：髪・リボン・胸の紐・スカートが風になびく（エディタの共通スライダー、プレイヤーは `player.html?wind=0.7`、パラメータ `WindBlow`）
- 横を向く動き（AngleX）はありません（1枚の絵からは形を作れないため。理由は [RESEARCH.md](RESEARCH.md)）

## 仕組み

```
original.png ──► [1] decompose (See-through CLI を外部プロセスで実行)
                    full パス 1280 / full パス 1024 / 顔まわりの head パス (≒2.7倍密度)
                          │  layers + depth + info.json
                          ▼
recipe.json ───► [2] build
                    ・パスごとのレイヤー選択、差し替えアート（補修済みリボン）
                    ・原画ピクセルでの復元（見えている部分だけ原画の色に戻す）
                    ・隠れ部分の補修（顔の裏の後ろ髪に出た輪郭線の除去）
                    ・髪の上に描かれたまつ毛を別パーツに分離（目を閉じたとき髪に残らない）
                    ・髪を線画に沿って束ごとに切り分け（束ごとに別のメッシュ）。スカートも同様にパネルに分割
                    ・リボンと胸の紐（房飾り）を色と連結性で切り出し、下の布・服を周りの色で補修
                    ・表情差分画像から 目/眉/口 のパーツを差分抽出
                    ・ランドマーク推定 → 頭の楕円体・首/腰ピボット・まぶたの曲線
                    ・グリッドメッシュ + 頂点属性（奥行き z、頭/体の重み、揺れチェーンの束縛）
                    ・テクスチャアトラス → web/models/<name>/model.json
                          ▼
                 [3] web ランタイム (WebGL2, 依存なし)
                    パラメータ → 変形 → 物理 → 描画、エディタ / プレイヤー / 録画
```

### 変形（Live2D のデフォーマ階層に相当）

各頂点は次の順に処理されます（`web/js/core/deform.js`）。

1. **顔の中**：瞳は白目でクリップして平行移動、眉は上下・回転。目と口の形は変形させず、絵の切り替えで表します（下記）。
2. **頭（1枚の硬い板）**：顔・目・眉・口・鼻・耳・前髪・髪飾り・首は、全部まとめて1枚の板として動きます。
   動きは首元を支点にした傾き（AngleZ）と小さな上下（AngleY）だけで、部位どうしのずれや形の変化は一切ありません。
   一枚絵を曲げると歪んで見えるため、横を向く動き（AngleX）は使いません（レシピ `rig.head.mode: "rigid"` では無視されます）。
   髪は線画（束の境目の暗い線と毛先の隙間）に沿って自動で束ごとに切り分け、束ごとに別のメッシュにしています（Live2D で束ごとにアートメッシュを分けるのと同じ）。
   隣の束の下には数 px だけ絵を延ばしてあるので、束どうしが別々に揺れても隙間は開きません（レシピ `bundles`）。
   それぞれの束とリボンの紐は、束の芯に沿った「回転の連なり」で曲げます（Live2D の回転デフォーマの連鎖と同じ考え方）。
   根元は頭と一緒に回り、その下はすぐに曲がって重力の向き（真下）へ垂れます（Live2D の「角度 Z で垂れる」）。揺れも曲がる角度だけを変えます。
   横の房と後ろ髪はこめかみ〜耳の高さから曲がり始め、決まった長さ（後ろ髪 360px）の間に真下へ向きを変えます（レシピ `gravity`・`bendLen`）。
   そのため頭や体を傾けても、髪の上の方が板のように一緒に傾くことはありません。
   束の切れ目は、根元近くでは髪の外側の輪郭から 32px 以上離して引きます（輪郭線が束のずれでずれないように）。
   各行はその高さの芯の点に乗って回るだけなので、束は曲がっても、せん断されたり伸びたりしません。
   - **歪みの自動判定**：`scripts/warp_check.py` が、全部位・全三角形で「回転以外の変形（伸び・縮み・せん断）」を測り、5% を超えたら不合格にします。
     例外は、手前の不透明な部品に完全に隠れている三角形だけです（全身の均一な拡大率は差し引いて測ります）。
     束の切れ目でも、隣り合う束が重なり幅の 3/4 以上ずれていないか（隙間が開かないか）を全コマで確かめます。対象は静止 13 姿勢と、確認用モーション 600 コマ・首振りモーション 360 コマ（30fps）。
     `scripts/rigid_check.py` は、頭の部位が1枚の板として動いているかを確かめます。調査と方針は [RESEARCH.md](RESEARCH.md)。
3. **体**：体をひねったり曲げたりはせず、全身を1枚の絵のまま動かします。
   傾き（BodyAngleZ）は下端を支点に回し、前後（BodyAngleY）は肩の高さを中心に全身を最大 ±5% 均一に拡大・縮小して「近づく・遠のく」を表し、
   左右（BodyAngleX）は全身を横に移して重心移動を表します。均一な拡大は形を変えないので歪みません（髪の束も一緒に拡大されます）。
   待機中はゆっくり近づいたり遠のいたり、重心を移したりし、話している間は少し前に寄ります。呼吸では胸から上が 2px ほど上下します。
4. **物理演算**：Live2D と同じく「振り子で計算した値で、曲がる角度を動かす」方式です（`physics.js` の `updateStrands`）。
   束ごとに、中ほどと毛先の角度をばね（遅れ・行き過ぎ・戻り）で追従させます。頭や体の動き、根元の加速度、風が入力です。
   束ごとに揺れの速さと風の位相を少しずつ変えているので、同じ方向に一斉に動かず、1本ずつ別々に揺れます。
   「風が吹く」（`WindBlow`）では、右からの一定の押し・強弱の波（右から左へ少し遅れて伝わる）・細かいはためきを、各部の曲がる角度に加えます。
   揺れの上限は「風で押された位置」を中心に測るので、強い風でも頭や体の動きによる遅れは普段と同じ範囲に収まります。
   幅の広いリボンの垂れは揺らしません（大きく曲げると伸びが目立つため）。スカートも体と一体です。

### 表情・瞬き・口（絵の切り替えだけ）

- 瞬き：開き目 → 半目 → 閉じ目の絵を切り替えます（目の絵をつぶす変形はしません）。
- 口パク：従来の閉じ口 → 半開き → 開き口を維持し、5母音の絵も切り替えられます。`MouthVowel=0` は従来、`1..5` は あ・い・う・え・お。母音選択中は `MouthOpenY<0.18` で閉じ口、`0.18` 以上で選択した口を1枚表示します。素材と採用記録は canonical source checkout にあり、採用記録は [顔キー採用記録](face-keys-release.md) にあります。
- 表情：にっこり・驚き・困り・照れの目・眉・口の絵に切り替えます。切り替えは瞬きの間に行い、どの瞬間も絵は必ず1枚だけ表示されます（半透明の二重像を出しません。`scripts/warp_check.py` で全コマ確認）。
- 視線：白目の中で瞳だけが動きます（白目からはみ出さない範囲）。

### 動きの付け方

- 頭の傾き・うなずき・体の傾きに、ばねによる「遅れて追いつき、少し行き過ぎて戻る」動きを付けています。
- 話している間は、音節に合わせて小さくうなずきます。体を傾けたときは、頭がわずかに遅れてついてきます。
- 視線を横に向けると、頭が少しそちらへかしげます。

## 使い方

### 必要なもの

- Python 3.10+（ツール本体）、Node.js（JS テストのみ）、ffmpeg（録画）
- See-through の既存環境（推論を実行する場合のみ）。場所は環境変数で指定します。

| 変数 | 既定値 |
| --- | --- |
| `SEETHROUGH_ROOT` | `${SEETHROUGH_ROOT}` |
| `SEETHROUGH_PYTHON` | `${SEETHROUGH_PYTHON}` |
| `SEETHROUGH_MODELS` | `${SEETHROUGH_MODELS}` |

```bash
uv venv .venv --python 3.12
uv pip install -e ".[dev]" playwright==1.62.0 --python .venv/Scripts/python.exe
```

### 1. 分解（See-through 推論）

```bash
.venv/Scripts/python.exe -m seethrough_rig.cli decompose private original artwork (omitted from public projection) --work work/aoi --extra-full 1024
```

NF4 量子化・group offload なしで、1 パスあたり約 2〜5 分、ピーク VRAM は約 6.7GB（RTX 5090 で計測）です。
出力は `work/`（git 管理外）に入ります。

### （参考）採用しなかった実験

向き違いの絵を Codex で生成して部位の変形を最適化する機能（`strig keyforms`、`seethrough_rig/fitkeys_torch.py`）と、
それに使った生成画像（`discarded keyform art (omitted from public projection)/`）が残っています。
目などがせん断されて歪んだため採用しておらず、現在のレシピとランタイムでは使っていません。経緯は [CANDIDATES.md](CANDIDATES.md)。

### 2. ビルド

```bash
.venv/Scripts/python.exe -m seethrough_rig.cli build recipes/aoi_vowels.json --out web/models/aoi
```

`recipes/aoi.json`（共通の土台）と `recipes/aoi.json`（従来のリグ版）を引き継いだ `recipes/aoi_vowels.json` が現在の採用版です。独立した候補は `--out web/models/aoi` で生成します。
- レイヤーごとの採用パス
- 差し替えアート
- 描画順
- 表情画像と、そのパラメータへの対応
- メッシュ密度
- 揺れチェーンの上書き

### 3. エディタで確認・調整

```bash
.venv/Scripts/python.exe -m seethrough_rig.cli serve
```

`http://127.0.0.1:8765/editor.html` を開きます（`?model=` で別のモデルも指定可能）。

- 左：AoiTalk Web と同じ再生設定パネル（風、瞬きの間隔・閉じる／保持／開く時間、呼吸）、テスト発話／マイク口パク、表情、全パラメータのスライダー
- 中央：左ドラッグで顔を向ける、右ドラッグで移動、ホイールでズーム。メッシュとガイド（頭の中心・首/腰ピボット・揺れチェーン）を重ねて表示できます
- 右：共通の再生設定（待機、視線・追従、物理）、リグ調整（回転量、奥行き、パース、体の各量、呼吸、瞳の可動域）、揺れの各チェーン（剛性・減衰・伝播・慣性・風・最大変位）、レイヤーの表示切替とハイライト
- デスクトップは中央のキャラクターを表示したまま左右の設定をスクロールできます。狭い画面ではプレビューを先頭に配置し、ページ全体を自然にスクロールできます
- 「設定を保存」で `web/models/<name>/settings.json` にリグ調整とバージョン付き `playbackSettings` を分けて書き出し、次回の読み込み時に適用します。既存の他の安全な調整項目は保持します
- 対応しない機能、固定比較プロファイル、過去の固定エンジンの再生設定は変更できない表示になります。制作プロジェクトでは再書き出しで新しいモデル版になり、既存版や素材を上書きしません

### 4. 再生・録画

- プレイヤー：`/player.html?motion=motions/demo.json&view=bust`
- 録画（決まった時間刻みで 1 フレームずつ描画）：

```bash
.venv/Scripts/python.exe scripts/record.py --model models/aoi --motion motions/demo.json --out docs/media/demo.mp4
```

- ポーズ一覧の画像（確認用）：`scripts/pose_sheet.py --set head|body|face|expr`

### テスト・判定

```bash
.venv/Scripts/python.exe -m pytest -q tests
node --test tests/js/rig.test.mjs tests/js/metrics.test.mjs
.venv/Scripts/python.exe scripts/warp_check.py --url http://127.0.0.1:8765        # 歪み・すき間・二重像（全コマ）
.venv/Scripts/python.exe scripts/rigid_check.py --url http://127.0.0.1:8765       # 頭が1枚の板として動くか
```

## データ形式（model.json）

- `drawables[]`：メッシュ（位置・UV・インデックス）
  - 頂点属性：`z`（頭の奥行き）、`head`（頭の重み）、`zb`（体の奥行き）、`twist`、`lean`、`breath`
  - `deformers`：iris / brow
  - `opacity`：パラメータのキー補間、`oneMinusSum` による差し替え（絵の切り替え）
  - `clip`：クリッピングマスク
  - `strandIds` と頂点属性 `stw`（束の重み）・`sts`（束の上の位置）：その頂点がぶら下がる髪の束
- `strands[]`：髪の束の芯（点列）、垂れの量、物理パラメータ
- `rig`：頭（`mode: "rigid"`、首元の支点）、体（`rigid`）、まぶた曲線、眉
- `expressions[]`：表情プリセット（パラメータの組）

バイナリ配列は base64 で格納しています。

## Live2D との対応と違い

| Live2D Cubism | このツール |
| --- | --- |
| アートメッシュ（手動メッシュ） | レイヤーのアルファから自動で作るグリッドメッシュ |
| 回転デフォーマ（顔の傾き） | 頭の全部位を首元の支点で1枚のまま回す |
| 髪の束ごとのアートメッシュ | 線画に沿って自動で束に切り分け（隣の束の下に絵を延長） |
| 髪のワープデフォーマ・揺れの自動生成（根元固定・毛先が曲がる・角度 Z で垂れる） | 束の芯に沿った回転の連なり（せん断なし） |
| 体の前後（パース） | 全身の均一な拡大・縮小（近づく・遠のく） |
| 標準パラメータ（ParamAngleX 等） | 同名に準拠（AngleX, BodyAngleZ, EyeLOpen, MouthOpenY, …） |
| 物理演算（振り子→パラメータ→変形） | ばねで束の曲がる角度を動かす |
| 表情（exp3）・モーション（motion3） | 表情プリセット＋キーフレームのタイムライン（JSON） |
| クリッピング | ステンシルによるクリップ（瞳を白目で切り抜く） |

Live2D で人が手で作る「顔の向きごとの形」は、1枚の絵からは作れないので対応していません。

## 既知の制約

- 横を向く動きはありません。
- 束の間を切ったとき、後ろの束の延長部分は元絵の隣の束の絵なので、大きく揺らすと柄のずれが見える可能性があります。揺れ幅は控えめにしています。
- 体の前後は均一な拡大・縮小なので、本物のパース（近い部分ほど大きくなる）はありません。
- See-through の分解は乱数シードで結果が変わるため、悪い回はシードを変えてやり直します。
- 表情差分は、元絵と位置の合った画像が前提です（今回は元絵の頭部 crop に合わせた生成画像を使用）。
- See-through の分解はパス・解像度によって欠けることがあります（今回は 1280 でリボン・太ももが欠けたため、1024 パスと補修済みリボンを採用）。
  そのため、レシピでレイヤー単位に採用元を選ぶ設計にしています。
- 後ろ髪の内側など、元絵で見えない部分の品質は See-through の補完に依存します。

## ディレクトリ

```
seethrough_rig/   分解・ビルド・サーバー（Python）
web/              ランタイム・エディタ・プレイヤー（WebGL2, ES modules, ビルド不要）
  models/aoi/     ビルド済みモデル（atlas + model.json）
  motions/        デモのタイムライン
recipes/          キャラクターごとのレシピ
private source artwork (omitted from public projection)/       入力素材（元絵、表情差分、補修済みリボン、向きの参考図）
scripts/          録画・歪み判定・ポーズ一覧
tests/            pytest / node:test
docs/             動画・調査メモ
```

## 参考

- See-through: Single-image Layer Decomposition for Anime Characters（Lin et al., SIGGRAPH 2026）
- Live2D Cubism マニュアル：[標準パラメータ](https://docs.live2d.com/en/cubism-editor-manual/standard-parameter-list/)、
  [XY の顔の動き](https://docs.live2d.com/en/cubism-editor-tutorials/xy/)、[物理演算](https://docs.live2d.com/en/cubism-editor-manual/physics-operation/)、
  [ワープデフォーマ](https://docs.live2d.com/en/cubism-editor-manual/making-and-placement-of-warp-deformer/)
- 同様に See-through の出力を動かすコミュニティ作例：StretchyStudio、Anime2.5DRig、PachiPakuGen、PNGAL（See-through README の Community Support より）

## AoiTalkとの制作・再生連携

AoiTalkは完成モデルを取り込んで保管・再生し、制作はこのリポジトリの独立プロジェクトで行います。任意連携の設定、機械可読CLI、環境診断、候補の書き出し、認証済みの再編集については [制作連携](production-integration.md)、正本から生成する共通再生ライブラリと固定版の更新方法は [再生ランタイム](runtime.md) を参照してください。
