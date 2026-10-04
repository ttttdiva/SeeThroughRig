# 調査メモ：Live2D と関連研究から分かったこと（2026-09-30）

## 読んだもの（15件）

| # | 資料 | 要点 |
| --- | --- | --- |
| 1 | [Live2D 公式チュートリアル「体の動きをつける」](https://docs.live2d.com/en/cubism-editor-tutorials/deformer/) | 顔の傾き（AngleZ）は**あごの位置を支点にした回転デフォーマ**。髪は束ごとにワープデフォーマ。体の傾きは「横にずらすのではなく弧を描く」「足が地面から離れない」 |
| 2 | [Live2D 公式「XY の顔の動き」](https://docs.live2d.com/en/cubism-editor-tutorials/xy/) | 顔の向きはパーツごとのワープデフォーマのキーフォームを**人が手で**作る |
| 3 | [Live2D 公式「揺れの自動生成」](https://docs.live2d.com/en/cubism-editor-manual/auto-generation-of-sway-motion/) | 髪の揺れは「根元固定・毛先が動く、**髪に沿った曲がり**」。柔らかさ＝上ほど動かず下ほど動く。「角度 Z で垂れる」＝頭を傾けると毛先は鉛直に戻ろうとする |
| 4 | [Live2D 公式「物理演算」](https://docs.live2d.com/en/cubism-editor-manual/physics-operation/) | 振り子（多段）で計算した値を**パラメータ**に出し、そのパラメータが上の曲がり変形を動かす |
| 5 | [See-through（SIGGRAPH 2026）](https://github.com/shitagaki-lab/see-through) | 分解は出発点。「リギング（メッシュ・物理・キーフォーム）が最も重要で手間のかかる工程で、本研究は扱わない」 |
| 6 | [Bunraku（arXiv 2607.27348）](https://arxiv.org/html/2607.27348v1) | 1枚絵→Live2D。**8,884 体の Live2D モデルで学習**して頂点ごとのキーポーズ変位を予測。層ごと独立に予測すると「瞳が白目から滑る」「前髪が額から離れる」裂けが出るので**全層を同時に**予測する |
| 7 | [CartoonAlive（arXiv 2507.17327）](https://arxiv.org/pdf/2507.17327) | 顔写真→Live2D。既存の Live2D 構造にパーツを合わせる方式。髪と顔の重なりは難しいと明記 |
| 8 | [Textoon（arXiv 2501.10020）](https://arxiv.org/html/2501.10020v2) | 文章→Live2D。**既製の Live2D テンプレート**のリグをそのまま使い、見た目だけ生成（前髪3・中間3・後ろ髪5 種の部品） |
| 9 | [2.5D Cartoon Models（Rivers, Igarashi, Durand, SIGGRAPH 2010）](https://www-ui.is.s.u-tokyo.ac.jp/~takeo/papers/alec_siggraph2010_cartoon.pdf) | 回転は「部品の位置を 3D で動かし、**形は描かれた複数の視点の絵の間で補間**」。1視点だけでは形が作れない |
| 10 | [As-Rigid-As-Possible Shape Manipulation（Igarashi et al., SIGGRAPH 2005）](https://www-ui.is.s.u-tokyo.ac.jp/~takeo/research/rigid) | 各三角形の歪み（回転以外の成分）を最小化。空間ワープより「剛さ」が保たれる |
| 11 | [Bounded Biharmonic Weights（Jacobson et al., SIGGRAPH 2011）](https://homes.cs.washington.edu/~jovan/papers/jacobson-2011-bbw.pdf) | 線形ブレンドスキニング（2つの変換の位置を重みで混ぜる）は、つぶれ・キャンディ包み・せん断が出る |
| 12 | [Talking Head Anime（Khungurn）](https://dmv.nico/en/articles/talkingheadanime/) | ニューラルで1枚絵から顔の向き・目・口を生成。変形ではなく画像生成 |
| 13 | [Animated Drawings（Smith et al. 2023）](https://arxiv.org/abs/2303.12741) | 子どもの絵：骨格＋**ARAP** でメッシュを動かす |
| 14 | [DrawingSpinUp（SIGGRAPH Asia 2024）](https://arxiv.org/abs/2409.08615) | 1枚絵→3D。輪郭線は視点依存なので一度消して描き戻す必要がある |
| 15 | [2.5D childlike rigs（arXiv 2502.17866）](https://arxiv.org/abs/2502.17866) | 部品ごとの 2.5D リグで見た目を保つ |

## 考えたこと

1. **Live2D も変形はする。問題は「変形の仕方」**
   Live2D のキーフォームも頂点を動かす（Bunraku が学習しているのはまさにその頂点変位）。
   違いは、人が「髪は束に沿って曲げる」「顔の傾きはあごを支点に回す」など、絵の構造に沿った変形だけを作ること。
   これまでの私の実装は、構造に関係なく「上は頭、下は体」と位置を混ぜていた。これは資料 11 の線形ブレンドのせん断そのもので、ご指摘の後ろ髪の斜めのずれの原因。

2. **歪みの定義をはっきりさせる**
   「回転以外の変形」（各三角形の伸び・縮み・せん断）を歪みとする（資料 10 の ARAP の考え方）。
   髪が束に沿って曲がるのは、各部分の回転が少しずつ違うだけなので、歪みはほぼ 0。これなら**全部位・全コマ・例外なしで**測れる。
   前回までの判定は、揺れる髪を「別扱い」にして緩めていたため、せん断を見逃した。

3. **髪は「束に沿った回転の連なり」で動かす**
   根元は頭と一緒に回り、下に行くほど体の向き（重力側）へ戻る（資料 3 の「角度 Z で垂れる」）。
   揺れも、位置をずらすのではなく「曲がる角度」をばねで遅らせて作る（資料 4 の、振り子→パラメータ→曲がり変形）。
   各行は束の芯の位置に置き、その行の角度で回すだけなので、横幅はそのまま保たれる。

4. **顔の向き（横向き）はやらない**
   資料 2・6・9 のとおり、横向きには人が作ったキーフォームか、別視点の絵が必要。
   1枚の絵と自動処理だけでは形を作れないので、ユーザーの判断どおり行わない。

5. **それ以外の歪みも消す**
   - 瞳：寄り目のときの横縮み → 平行移動だけにする
   - スカート：揺れで横にずらしていた → 体と一体にする
   - リボンの紐：束に沿った曲がりにする

## 合格基準（`scripts/warp_check.py`）

- すべての部位のすべての三角形で、回転以外の変形が **5% 以下**
  - 例外は、手前の不透明な部品に完全に隠れている三角形だけ（見えないため）
- 10 の静止姿勢と、確認用モーション全 600 コマ（30fps）
- すき間なし、半透明の二重像なし
- そのうえで、実寸で目視する
