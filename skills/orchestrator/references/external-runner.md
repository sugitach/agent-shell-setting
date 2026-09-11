# 外部ハーネスの実行

このランナーは macOS / Linux 向けの Python 3 標準ライブラリ実装。別途 codex / claude / agy CLI と認証が必要。
標準サブエージェントは親のツールで直接起動する。以下は transport=external の場合に使う。

## 起動

親は task_id、Issue・計画版、担当範囲、成果物と返却形式、失敗履歴を含む依頼ファイルを用意する。
役割 skill 本文はランナーが配置先の隣接ディレクトリから読み、依頼の前に挿入する。

```sh
python3 /path/to/skills/orchestrator/scripts/external_runner.py run \
  --workspace /absolute/project \
  --task-id issue-123 \
  --harness codex --role coder --access workspace-write \
  --prompt-file /absolute/project/.orchestration/issue-123/request.md \
  --timeout 600
```

`/path/to/skills` は実際のスキル配置先（Codex は `~/.agents/skills`、Claude は `~/.claude/skills`、agy は `~/.gemini/antigravity-cli/skills` など）に置き換える。
run はフォアグラウンドで実行する。親の長時間コマンド実行機能で起動し、そのセッションを維持して結果を待つ。起動直後に表示される JSON の job パスを記録する。
CLI 実行方式は Codex=`exec --json`、Claude=`--print --output-format json`、agy=`--sandbox --input-format stream-json --output-format stream-json`。
すべて標準入力で依頼本文を渡す。agy は user イベントを input.jsonl に保存して渡し、依頼ファイルの読み取り権限に依存しない。
`--model` は明示指定が必要なときだけ渡す。未指定では CLI の既存設定を使う。
`--reasoning-effort` は `low`、`medium`、`high`、`xhigh`、`max` を受け付ける。
Codex は `-c model_reasoning_effort="…"`、Claude は `--effort` に変換する。agy は `--effort` を使うが、対応値は `low`、`medium`、`high` だけなので、それ以外は起動前に拒否する。
native 呼び出しでは親の公開ツールが model・reasoning effort を個別指定できることを確認してから渡す。指定できない場合は既定値へ黙って落とさず、external を選べるか確認し、どちらも不可なら blocked とする。

## 権限

- 既定は read-only。Codex は read-only sandbox、Claude は Read/Glob/Grep のみに制限する。テスト実行や Issue 操作が必要なら、その範囲が許可されていることを親が確認する。
- workspace-write は Codex の workspace-write sandbox、Claude の acceptEdits を指定する。Claude のシェル実行等は既存の許可設定に依存し、全コマンドを許可するものではない。
- agy は read-only 強制手段が未確認なので既定では起動を拒否する。workspace-write を明示した場合のみ sandbox を有効にして起動する。これは Codex と同じ権限制御を保証するものではない。
- 権限回避フラグ、認証情報のコピー、hook の無効化は行わない。Claude 内からの Claude CLI 起動がネスト制約に阻まれる場合も環境変数で迂回せず、native または利用可能な接続方式を検討する。

## 状態・結果・停止

保存先は `<workspace>/.orchestration/<task-id>/jobs/<job-id>/`。
state.json、request.md、stdout.log、stderr.log、response.md を保存する。状態は原子的に置き換え、ジョブディレクトリを他ユーザーへ公開しない。
agy は input.jsonl と harness-result.json も保存し、state.json に conversation_id、harness_status、stop_confirmed を記録する。
ログ・会話を自動コミットしない。対象プロジェクトでも `.orchestration/` を管理対象外にするか、成果物だけを明示的に選ぶ。

```sh
python3 /path/to/skills/orchestrator/scripts/external_runner.py status --job /absolute/job/path
python3 /path/to/skills/orchestrator/scripts/external_runner.py cancel --job /absolute/job/path
```

cancel は取消要求を記録するだけ。親は status が cancelled / unknown 等になるまで確認する。
Codex / Claude の取消・タイムアウト・SIGINT/SIGTERM は、起動したローカルプロセスグループに SIGTERM、猶予後 SIGKILL を送り回収する。
agy は stopping に遷移して SIGINT を送り、`--cancel-grace`（既定5秒、最大60秒）の間、終了応答を待つ。
init と同じ会話IDの終端 result と CLI 終了が確認でき、強制停止へ移行していなければ stop_confirmed=true とする。result 不在・会話ID不一致・強制停止時は unknown とし、再起動を拒否する。
終了処理中の中断も成功扱いしない。SUCCESS でも denied_actions または空回答があれば failed とする。
stop_confirmed はハーネスの終了応答を意味し、切り離されたプロセスや外部サービス内部の全処理停止を保証しない。
ランナーを SIGKILL すると回収処理を実行できない。ロックが消えても旧 starting/running ジョブがあるタスクの再起動は拒否する。
状態不明時は子の停止を外部から確認し、根拠を記録して旧ジョブの状態を解決する。確認せず状態を削除・書き換えて再起動しない。

| 状態 | 意味 |
| --- | --- |
| starting / running | 起動準備／ローカルCLI実行中 |
| stopping | agy に取消を送り、終了応答を待機中 |
| completed | CLI終了と非空の回答取得が完了。レビュー承認・DoD達成とは別 |
| failed | CLI失敗、構造化エラー、権限拒否、最終回答なし |
| timed_out / cancelled | ローカル停止処理済み。起動済み agy は同一会話の終端結果も確認済み |
| unknown | 残存処理等の停止確認が必要。再起動不可 |

run の終了コードは completed=0、実行失敗・取消等=1、事前条件不備=2。
status / cancel は状態不明・不正入力で2、それ以外は0。cancel の終了コード0だけでは停止完了を意味しない。
親は response.md を読み、役割の status と証拠を検証してから次工程へ進む。

## 排他とサーキットブレーカーの範囲

同一 task_id の外部起動は flock で排他する。親 state.json が blocked の場合も起動しない。
子の内部操作・同一エラー判定・修正回数はこのランナーでは自動判定しない。親が既存のタスク状態に記録する。
native と external をまたぐ排他、親の単一起動、異なる task_id 間の作業ディレクトリ競合は未強制。親が担当を直列に動かす。
切り離されたプロセスや外部サービスの停止をローカルプロセスグループの回収だけで保証しない。

CLI仕様の根拠: [Codex 非対話実行](https://learn.chatgpt.com/docs/non-interactive-mode)、[Claude Code プログラム実行](https://code.claude.com/docs/en/headless)。agy はこの環境の `agy --help` を確認して実装。
