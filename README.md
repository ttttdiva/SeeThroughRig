# See-through Rig

See-through Rig は、一枚のイラストから操作可能な 2D キャラクターリグを
作るための制作・再生ツールです。公開版の採用モデルは琴葉葵 (Aoi)。剛体
として動く頭、表情・瞬き・ウィンク、視線、5 母音、呼吸、体の動き、髪・
リボン・服の束ごとの物理を、依存なしの WebGL2 ランタイムで再生します。

[![Aoi showcase](docs/media/aoi-showcase.jpg?v=editor20bust)](https://ttttdiva.github.io/SeeThroughRig/showcase.html)

[Aoi showcase ページ](https://ttttdiva.github.io/SeeThroughRig/showcase.html) では、
葵の顔と上半身を大きく映し、左右にある実際の WebGL エディタのスライダーを操作しながら、
表情、視線、口パク、呼吸、体の動き、物理を確認できる 20 秒の動画を見られます。
風は開始フレームから有効です。GitHub Pages が利用できない場合は
[MP4 を直接ダウンロード](docs/media/aoi-showcase.mp4?v=editor20bust)できます。

## 同梱モデルを試す

ホストされたデモは WebGL2 ブラウザだけで動き、推論環境・Python・See-through
の重みを必要としません。ローカル checkout では任意の静的 HTTP サーバーを
使います。次は Python を使う例です。

```powershell
git clone https://github.com/ttttdiva/SeeThroughRig.git
cd SeeThroughRig
python -m http.server 8000 --directory .
```

[動画ページ](http://127.0.0.1:8000/showcase.html)、[対話的な WebGL エディタ](http://127.0.0.1:8000/web/editor.html)、
[モーション再生](http://127.0.0.1:8000/web/showcase.html)、または
`http://127.0.0.1:8000/web/player.html?model=models/aoi&motion=motions/showcase.json`
を開いてください。ブラウザは `file:///` からの module・texture 読み込みを
制限するため、静的サーバー経由で開きます。

データだけの配布物は [`artifacts/models/kotonoha-aoi.zip`](artifacts/models/kotonoha-aoi.zip)
です。`model.json`、atlas、描画プレビュー、manifest を含み、Python、JavaScript、
推論コード、認証情報は含みません。検証は source checkout の protocol で行えます。

```powershell
python -m seethrough_rig.integration validate `
  --package artifacts/models/kotonoha-aoi.zip
```

## 動くもの

- 頭は首元を支点にした `AngleZ` と小さな `AngleY` だけで動く一枚の板です。
  一枚絵から信頼できる横向き形状は作れないため `AngleX` は対応しません。
- 目、眉、表情、瞬き、ウィンクは採用済み画像キーを切り替えます。瞳は白目の
  範囲内で動きます。
- `MouthOpenY` は従来の連続口パクを保ち、`MouthVowel` の 1–5 は あ / い / う /
  え / お、0 は従来の口を選びます。
- 体の傾き・左右の重心・呼吸・視線・待機・発話に伴う動きを通常のパラメータで
  制御します。
- 髪、リボン、胸の紐、スカートはばねと風で束ごとに追従します。`WindBlow` は
  showcase の強い風の動きを表します。

変形、物理、表情切り替え、描画は一つの正本ランタイムで処理されます。エディタの
操作画面は値を調整するだけで、リグや物理を別実装しません。

## ディレクトリ

```text
web/                  WebGL2 プレイヤー、エディタ、ランタイム、採用済み Aoi model
web/motions/          showcase と検証用の決定的なタイムライン
seethrough_rig/       正本の Python 制作・検証・連携コード
recipes/              採用 Aoi のレシピ
scripts/              録画、判定、runtime packaging、公開同期
tests/                Python と Node のテスト
docs/                 ランタイム、連携、公開手順、技術資料
artifacts/models/     検証済みデータ専用 model package
artifacts/runtime/    生成済みデータなし runtime package
```

公開投影には原画、生成した顔・母音の素材、分解 work、推論重み、private な
制作 project、破棄した keyform/reference art、候補 model directory を含めません。
再生用の baked model、atlas、検証済み ZIP が公開アーティファクトです。再ビルドに
必要な入力素材は canonical な制作 checkout に残します。

## 開発時の検証

公開デモは WebGL2 ブラウザと静的サーバーだけで動きます。source のテストには
Node と Python が必要です。

```powershell
python -m pytest -q tests
node --test tests/js/rig.test.mjs tests/js/metrics.test.mjs tests/js/runtime*.mjs
```

実ブラウザでの WebGL2 確認、mock GL、実 Native GL、再推論は別の検証段階です。
一つの結果を他の段階の品質確認として扱いません。

## source 素材からの再ビルド

canonical source checkout には制作 pipeline と採用レシピが残っています。再ビルド
には互換する分解結果と原画素材が必要で、これらは公開投影へ意図的に含めません。
入力を用意した後の通常の build は次の通りです。

```powershell
python -m seethrough_rig.cli build recipes/aoi_vowels.json --out web/models/aoi
```

runtime provenance、model ZIP 検証、SHA-256 inventory、managed destination の規則は
[`docs/publication.md`](docs/publication.md) にまとめています。公開同期ツールは
[`scripts/sync_public.py`](scripts/sync_public.py) です。元 README の制作・形式・
Live2D 対応の詳しい説明は [`docs/technical-guide.md`](docs/technical-guide.md) に
保存しています。

## runtime package

公開投影の `artifacts/runtime/` は、TypeScript declaration を含むデータなしの ESM
package です。`runtime-provenance.json` に正確な source commit と baseline の hash
を記録します。public clone はこの同梱 package を直接使い、履歴依存の generator は
canonical source release checkout で実行します。

## 権利と表示

この repository は first-party の open-source license を宣言していません。runtime
package も upstream の first-party license を unspecified、package metadata を
`UNLICENSED` と記録しています。MIT などの license を推測せず、コードの再利用・
再配布は許諾を得てください。

表示キャラクターは琴葉葵の非公式二次創作です。琴葉葵は AI Inc. © です。現在の
キャラクターおよび二次創作ガイドラインは [公式ページ](https://aivoice.jp/character/kotonoha/)
を確認してください。本プロジェクトは AI Inc. との提携を主張しません。
