# 葵の目・5母音キー採用（2026-10-03）

白目境界の補修では `restore.boundary_tags` に左右の白目だけを指定する。
生成分離素材に付いた灰色の閉じた囲み線を、元絵の同じ座標のRGBで
アルファの縁まで復元する。通常の自分の縁から2pxを除外する処理は他の
パーツで保持し、上に描く虹彩・まつ毛・髪の遮蔽と `no_restore` も保持する。
白目のアルファ、メッシュ、目キー、虹彩クリップ、再生コードは変更しない。
顔素材の薄い旧目の残像も分離レイヤーで確認する。元絵の影や頬の線を
色だけで一括除去しない。同倍率の元絵・修正前・修正後と実WebGLの結果を
`87_see-though_rig-eye-fix` の報告に記録し、Native実表示とは区別する。

既存の開眼・半目・閉眼・左右ウィンクを保持し、生成済み1254×1254の5母音原画を採用した。原画の追加生成や再推論は実施していない。`recipes/aoi_vowels.json` は既存リグの全設定と素材参照を保持する独立レシピ。候補 `web/models/aoi` と通常起動の `web/models/aoi` は同じレビュー済みデータである。旧モデルと旧レシピは `5c6dc87913f7f773a2228066575ca7c00f42108b` から復元できる。

通常の `python -m seethrough_rig.cli serve` → `/editor.html` で左パラメータの目の開閉、口開閉、母音を操作する。母音スライダーは整数刻み。0は従来の口、1..5は あ・い・う・え・お。`MouthForm` と連続した `MouthOpenY` 入力は保持する。母音選択中だけ口画像の開口判定を0.18で切り替え、旧レシピの狭い補間区間で閉口と開口が両方半透明になることを防ぐ。従来の0モードの補間と固定historical engineは変更していない。

`/face-keys-review.html` は既定モデル、`?model=models/aoi` は候補を表示する。自動瞬き、母音、表情、首傾き、境界、反復、seekの確認に利用できる。初回dtは下限0。

既存の実Edge WebGL2の22ケース・PNG15枚を親agentが目視評価した。目・口の位置、輪郭、穴、不要な重なりに問題は見られず、5母音が区別できる。追加の修正境界と通常UIはローカル実WebGLで確認する。静止画像で時間方向のちらつき全体を保証するものではない。実機Webと実Nativeは別々に記録する。

正式モデルZIPは `seethrough_rig.integration export` と `validate`、コード配布は `scripts/package_runtime.py --source-ref <実commit>` を使用する。データZIPに実行コードは含めない。manifestの実Git commit、データ・原画・previewのSHA-256とruntime provenanceを照合する。暗号署名機構はこのリポジトリに存在せず、署名済みとは扱わない。

最終のcommit・配布先・追加検証実測・スクショ・ハッシュは、ユーザー指定の `<private source handoff path omitted>` に記録する。AoiTalk同梱runtimeを直接編集しない。

今回の対象は2D原画のキー切り替えで、3D素材の調達・シーン・リグ制作は含まない。恒久規約を読み、BlenderKit、Fab、Sketchfab、Mixamo、Cargoはいずれも用途なしとして検索・取得・導入・シーン適用を実施していない。Blender MCPは使用していない。
