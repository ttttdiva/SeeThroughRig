# AoiTalkからの制作連携（protocol1）

## 責務と保存場所

制作、素材の修復・差分、レシピ、メッシュ、物理、編集、書き出しは本リポジトリが担当します。AoiTalkは所有者の確認、既存agent harnessによる制作依頼、ジョブ表示、完成物の検証・保管・使用版の管理を担当します。再生コードの正本と固定配布物の更新手順は [runtime.md](runtime.md) を参照してください。

既定の制作保存先は `<本リポジトリ>/projects/<UUID>/` です。`input`、`recipe`、`material`、`history`、`project.json` は制作を続けるための永続データです。`work`、`logs`、`preview` は再生成可能な中間物・検証物であり、永続データと一緒に消しません。AoiTalkのgenerated-media領域へ制作フォルダを丸ごとコピーしません。通常の制作ジョブは共通エンジンを変更しません。

## 機械可読CLI

正規環境のPythonで次を実行します。Windowsの既存環境は `.venv\Scripts\python.exe` です。標準出力は常に1個のJSON応答、通常の実行ログは標準エラーへ分離します。

```text
python -m seethrough_rig.integration doctor --json
python -m seethrough_rig.integration project-create --source <画像> --project-id <UUID> --owner <所有者> --character <UUID> --json
python -m seethrough_rig.integration project-run --project <UUID> --json
python -m seethrough_rig.integration project-status --project <UUID> --json
python -m seethrough_rig.integration project-cancel --project <UUID> --json
python -m seethrough_rig.integration project-resume --project <UUID> --json
python -m seethrough_rig.integration project-export --project <UUID> --json
```

応答は `identity="seethrough-rig"`、`protocol_version=1`、`ok`、`result`、失敗時の `error.code/message` を含みます。人間向けメッセージを正規表現で判定する必要はありません。CLIの `install` / `update` 自体は固定revisionが指定できない経路として実行を拒否します。AoiTalkが明示操作と固定release契約を検証してコード導入を行います。

同じプロジェクトUUIDでの再作成は、所有者・キャラクター・入力SHAが一致するときだけ再利用します。プロジェクトごとのOSファイルロック、制作領域共通のGPUロック、キャンセル要求を使い、プロセス異常終了後は再開時にハッシュと状態を照合します。入力・クロップ・workflow・使用コードが異なる中間物を無条件に再利用しません。

## 段階と必要素材

新規制作は既存の分解・ビルド処理を呼び出します。分解後の素材が不足している場合やレシピ未作成の場合は `needs_input` と具体的な不足内容を返します。`agent-instructions.md` が制作対象ごとの手順です。制作agentの選択・権限・ログ・外部送信許可はAoiTalkの既存基盤で扱い、本CLIから別のagentや特定モデルを起動しません。

`completed` は書き出し工程の完了であり、見た目の品質承認ではありません。構造検証、動画等による見た目の確認、ユーザーの使用開始は別々です。

既にユーザーが確認したモデルを使う場合は `project-adopt --project <UUID> --model-dir <場所> --source-commit <40桁SHA> --approved` を明示実行できます。採用時はプロジェクトへデータをコピーし、素材再利用と新規推論未実施を記録します。原画をそのままプレビューとして完成扱いにはしません。

## 環境診断と明示的な準備

`doctor` は必要なファイル・依存の存在確認だけを行います。設定画面を開くたびにtorchや巨大な推論モデルをロードしません。`ready` は軽量診断の結果であり、実推論の完了とは異なります。

推論の場所は `SEETHROUGH_ROOT`、`SEETHROUGH_PYTHON`、`SEETHROUGH_MODELS`、保存した `.local/production-environment.json` を優先します。このPCで既に存在するFドライブの導入先は初期検出候補に限られ、他PCへFドライブを要求しません。

`prepare` は明示操作でのみ実行します。既存の制作専用venvを再利用し、不足するnumpy/Pillow/OpenCV/scipyだけを、用意済みの `.local/production-wheels` または固定した公式PyPIからそのvenvへ導入します。一時wheel/cacheは制作リポジトリの `.local` 配下です。既存venvを削除・再作成したり、AoiTalkのPythonへ推論依存を入れたりしません。

推論本体や巨大な重みを無条件にダウンロードする処理はありません。不足する場合は具体的な場所・ファイルを診断結果へ返し、既存環境の登録・準備を求めます。軽量依存の導入成功を推論可能と同一視しません。

## データだけの完成パッケージ

```text
python -m seethrough_rig.integration export --model-dir <場所> --output <未使用ZIP名> --project-id <ID> --revision <版> --source-sha256 <64桁SHA> --source-commit <40桁SHA> --json
python -m seethrough_rig.integration validate --package <ZIP> --json
```

ZIPのルートは `manifest.json`、`model.json`、参照されたatlas、実際に描画した `preview.png`、存在する場合のみ `settings.json` です。設定は省略せず保持します。manifestにはschema/model/runtimeABI、生成元commit、原画ハッシュ、適用profile、実効機能、全データファイルのSHA-256と容量を記録します。新しい書き出しはモデルに結び付く完全な `playbackSettings`（`version: 1`）も記録します。存在する出力ZIPは上書きしません。同じ内容のプロジェクト書き出しは同じrevisionを再利用します。

任意のJS/HTML/実行ファイル、外部URL、絶対パス、秘密情報は含めません。ZIP展開前の容量・件数・重複名・圧縮率・symlink等、画像寸法、typed-arrayのdtype/長さ/数値/インデックス、モデル参照を検証します。正しいハッシュだけで安全性や互換性を保証しません。AoiTalkは受信側の検証も独立して実行します。

会話用の `conversation-v1` は `--wind`（そよ風 0..1）と `--wind-blow`（髪・リボンの紐・胸の紐・スカートが風になびく 0..1、現行エンジンのみ）を指定できます。比較用の `comparison-v1` は風なしで固定です。

`project-run` / `project-resume` / `project-export` の通常書き出しは明示的に `conversation-v1` を使用します。低水準の `export` の既定値は従来どおり `comparison-v1` です。再生設定の解決順は共通既定値 → 旧profile → `model.json.playbackSettings` → `settings.json.playbackSettings` → `manifest.playbackSettings` です。明示した `--wind` / `--wind-blow` は最後のmanifest値へ反映します。比較用と過去の固定エンジンは有効な設定データを保持しても、固定再生の動作へは適用しません。新しいフィールドがない旧パッケージも引き続き受け入れます。

過去の比較モデルと現在のエンジンは別のものです。比較時のモデルSHAを検証できる場合だけ、そのモデルに適合する固定historical coreを指定します。他のキャラクターを同じコードrevisionで制作したことだけで、比較時の葵モデルと混同しません。比較profileはWind=0です。

## 認証済み編集と再書き出し

`project-editor-info` は所有者・入力・モデルを確認し、単一プロジェクトのデータ一覧を返します。`project-editor-save --settings-file <そのproject/requests/*.json>` は内容を検証し、`material/settings.json` へ保存します。採用済みモデルや以前のhistory ZIPは変更しません。再exportで新しい候補になります。

エディタの `core_ref` は書き出しと共通のモデル・atlas・原画ハッシュ検証で決定します。採用時の source commit が現在の版でも、検証済みの過去モデルは固定historical coreで表示し、書き出し先と同じ再生設定の対応範囲を使います。

リグ調整と再生設定を混同せず、再生設定は `settings.json` 内の `playbackSettings` に保存します。風、瞬き、呼吸、待機モーション、視線、物理の型・範囲・既定値は `web/js/runtime/playback-settings.schema.json` が正本です。Python側も同じJSONを読み、未知のversion/キー、不正な数値や間隔を拒否します。モデルのrevisionには解決済み再生設定とprofileも含めます。設定だけの編集では既存の検証済みビルドを再利用し、旧キャッシュも照合して移行できるため、再推論や素材・メッシュの再生成は不要です。

AoiTalkの編集画面は本リポジトリの固定したエディター用ファイルだけと、その利用者が所有するプロジェクトの検証済みデータだけを、既存認証を通して表示します。制作repo全体を公開せず、通常再生のための常駐サーバーも追加しません。Nativeから外部ブラウザを開く場合は、既存のWeb公開URLを使用し、トークンをURLへ含めず、必要なら通常のWebログインを行います。

単体の `strig serve` / `python -m seethrough_rig.cli serve` も利用できます。保存済み設定がないモデルでは、対応サーバーが明示的に空の設定JSONを返します。設定不足による不要な404や初回フレームの負の時間差で再生が停止しないようにしています。

## 検証

```text
python -m pytest tests/test_integration_package.py tests/test_integration_projects.py tests/test_runtime_integration_regressions.py tests/test_playback_settings.py tests/test_editor_core_identity.py
node --test tests/js/runtime.test.mjs tests/js/playback-panel.test.mjs tests/js/editor.test.mjs
```

実モデルの回帰検証はGit上の過去のユーザーモデルをローカルで参照し、素材そのものを新たにテストへコミットしません。過去revisionがない環境では未実施としてskipします。mock GL、実ブラウザ、実Native、実推論は別々に記録してください。
