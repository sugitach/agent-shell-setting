# MCP起動ブリッジ（Issue #16）

承認済み計画 revision 7 に対応する claude/codex 用の stdio MCP サーバー。
`launch` は終了を待たずジョブIDを返し、別ツール呼び出しの `status` / `cancel`
で同じジョブを参照する。agy 対応は #17、呼出側の統合は #18 の対象。

## インストールと接続

リポジトリのルートで実行する。

```sh
python3 -m venv .orchestration/mcp-launcher-bridge/venv
.orchestration/mcp-launcher-bridge/venv/bin/python -m pip install -r .orchestration/mcp-launcher-bridge/requirements.txt
```

SDK は `mcp==2.2.0`、import は `mcp.server.mcpserver.MCPServer`。
Claude はプロジェクトの `.mcp.json` の `mcp-launcher-bridge` を使う。
既存の診断用 `agy-bridge` は別サーバーとして維持する。
同じSDKを使用するが、診断サーバーにはジョブ管理の責務を追加しない。
登録するエントリは次のとおり（配置先を変えた場合は絶対パスを変更する）。

```json
{
  "command": "/Users/sugita/work/agent-shell-setting/.orchestration/mcp-launcher-bridge/venv/bin/python",
  "args": ["/Users/sugita/work/agent-shell-setting/.orchestration/mcp-launcher-bridge/server.py"]
}
```

Codex ではあなたが `~/.codex/config.toml` に次を追加する。
本実装はその個人設定ファイルを書き換えない。

```toml
[mcp_servers.mcp-launcher-bridge]
command = "/Users/sugita/work/agent-shell-setting/.orchestration/mcp-launcher-bridge/venv/bin/python"
args = ["/Users/sugita/work/agent-shell-setting/.orchestration/mcp-launcher-bridge/server.py"]
```

リポジトリのルートを作業ディレクトリにしてCLIセッションを開始する。
サーバーが起動時の作業ディレクトリを workspace の境界として使用するため、
各ツールの `workspace` はその配下の絶対パスにする。
Claude はマージ後の対話セッションで `/mcp` を開き、接続と3ツールを目視確認する。
Codex は使用中のCLIのMCP接続一覧で登録・接続状態を確認する。
実CLIからの自動起動・寿命は手動確認対象であり、結合テストは設定と同じ
command/args をSDKの `stdio_client` で起動する代理検証である。

## 起動主体・寿命・PTY

呼び出し元CLIがstdioサブプロセスとしてサーバーを起動し、セッション中は
複数ジョブにまたがって常駐させる。ジョブごとにサーバーは起動しない。
子CLIは `Popen(start_new_session=True)` と通常ファイルの標準入出力で起動し、
PTYを割り当てない。claude/codexの非対話モードなのでPTY制約を回避できる。
サーバーを起動元のサンドボックスの外へ移す仕組みではなく、その制約は継承する。
codex の起動引数は既存runnerと同じく内部サンドボックスを無効化する。
codex の `access=read-only` の強制は親の実行環境が担う。

SIGTERM/SIGINTのハンドラは終了イベントを設定するだけで、専用スレッドが
`shutdown_and_wait_all()` を実行してからサーバーを終了する。
stdio EOF でも `finally` から同じ終了処理を実行する。
新規起動と終了処理はロックで直列化し、終了イベント設定後はlaunchを拒否する。

## ツールと応答契約

- `launch(task_id, harness, role, workspace, prompt, access="read-only", model=null,
  reasoning_effort=null, timeout=600, cancel_grace=5, packet=null, executable_override=null)`
- `status(task_id, workspace, job_id)`
- `cancel(task_id, workspace, job_id)`

現在の `harness` は `claude|codex`、`role` は `planner|coder|reviewer`。
`executable_override` はテスト等でCLI実行ファイルを差し替える用途。
cancelは要求受付時点の状態を返すので、停止完了はstatusで確認する。

共通応答（#17でharnessにagyを追加予定）:

```json
{
  "job_id": "string|null",
  "task_id": "string",
  "harness": "claude|codex|agy",
  "status": "starting|running|stopping|completed|failed|timed_out|cancelled|unknown|blocked",
  "exit_code": "int|null",
  "error": "string|null",
  "reason": "string|null",
  "response_path": "string|null",
  "started_at": "float|null",
  "finished_at": "float|null"
}
```

停止処理の応答には `stop_confirmed`（boolean）も付く。
`reason` はcancelledで `user_requested|shutdown`、timed_outで `timeout`、
それ以外はnull。判定の優先順位は shutdown → cancel → timeout → 自然終了。
終端状態 `completed|failed|timed_out|cancelled` は不変。
自然終了時は終了コードと非空の最終回答を確認し、Claude JSONのエラーや権限拒否も失敗にする。
CodexはNDJSONをstdout.logに保存し、`--output-last-message` の回答ファイルを使用する。

不明なjob_id、必須引数欠落・型不正、workspace境界外、終了中のlaunch、
非オーナーjobのcancelはMCPツールエラー。同じtaskの重複launchは既存応答を返す。
終端jobへのcancelは変更なしで同じ応答を返す。

## ジョブ識別・所有権・再起動

ジョブIDはlaunch時に発行するUUID。状態は
`.orchestration/<task_id>/jobs/<job_id>/state.json` に保存する。
サーバーのregistryに存在するjobだけが書込み対象で、オーナーはメモリ内状態を正本とする。
別サーバー同士のlaunchもファイルロックで直列化する。

| 状況 | 応答 | 永続状態への書込み |
| --- | --- | --- |
| 非オーナーが非終端jobのstatusを読む | unknown | なし |
| 非オーナーが終端jobのstatusを読む | 保存した応答そのまま | なし |
| 非終端の既存ファイルが新規launchを妨げる | blocked、既存job_id、理由 | なし |
| オーナーがSIGKILL後もグループ消滅を確認できない | unknown、stop_confirmed=false | あり |

再起動・別セッションへの監視やcancel権限の引継ぎは非対応。
古い非終端ファイルは読み取れるが、unknownとして返し、同じtaskの再起動をblockedにする。
停止未確認を成功・キャンセル済みと推定してファイルを書き換えない。

## 停止確認

ジョブ単位のロックで冪等に SIGTERM → cancel_grace → SIGKILL → 消滅確認を行う。
確認間隔は最大0.05秒、SIGKILL後の確認期限は独立した `KILL_CONFIRM_TIMEOUT=2` 秒。
毎回 `poll()` で代表プロセスを回収し、`killpg(pid, 0)` の `ProcessLookupError`
によるグループ全体の消滅確認が成立して初めて終了コードと終端状態を確定する。
代表プロセスの回収やシグナル送信だけでは終端化しない。

SIGTERM直後の生存確認で一時的にEPERMが返る場合も消滅とは扱わず再確認する。
待機時間は残り猶予を超えないようにし、期限でも再確認してからSIGKILLへ進む。
期限内に消滅を確認できなければunknownにし、exit_codeは未確定のままにする。
停止未確認のjobは後の停止処理で再確認できるが、強制救済は本Issueの対象外。
`stop_confirmed` は切り離されたプロセスや外部サービス内部の全処理停止を保証しない。

## 検証と後続Issue

pytestを導入した既存venvで実行する（テストは新規venvのサーバーを実起動する）。

```sh
.orchestration/mcp-agy-bridge/venv/bin/python -m pytest tests/test_mcp_launcher_bridge_unit.py -v
.orchestration/mcp-agy-bridge/venv/bin/python -m pytest tests/test_mcp_launcher_bridge_toolflow.py -v
.orchestration/mcp-agy-bridge/venv/bin/python -m pytest tests/test_mcp_launcher_bridge_integration.py -v
.orchestration/mcp-agy-bridge/venv/bin/python -m pytest tests/test_external_runner.py -v
```

単体テストは排他・所有権・終了優先順位・グループ消滅を検証する。
toolflowはPopenモックと同一managerを使う別ツール呼び出し間の状態引継ぎを検証する。
integrationは実stdioサーバーと `tests/fixtures/dummy_cli.py` で3ツールの列挙、
成功・失敗・timeout・cancel、EOF/SIGTERM時の子の消滅とサーバー自身の期限内終了を検証する。
既存 `external_runner.py` は変更しない。
#17/#18は#16統合後、この応答契約を前提に単独でテスト・検証できる。
