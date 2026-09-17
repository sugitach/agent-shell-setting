# タスク状態と役割間の引き継ぎ

タスクごとにプロジェクト内の `.orchestration/<task-id>/` など合意した作業用ディレクトリを使う。
履歴・ログを無条件にコミットしない。親は作業開始前と各担当の終了後に状態を保存する。
保存先は子にも渡す。会話履歴の全文ではなく、次の担当が必要とする成果物と根拠を渡す。

## 親が保存する state.json

以下は構造の例。ID・パス・ハーネスは実際の値を使う。

```json
{
  "task_id": "issue-123",
  "owner_session": "parent-session-id",
  "parent_harness": "codex",
  "transport_mode": "auto",
  "phase": "planning",
  "roles": {
    "planner": {
      "harness": "claude",
      "model": null, "reasoning_effort": "high",
      "sources": {"harness": "shared", "model": "explicit", "reasoning_effort": "shared"}
    },
    "coder": {
      "harness": "codex",
      "model": "gpt-5", "reasoning_effort": "high",
      "sources": {"harness": "shared", "model": "project", "reasoning_effort": "explicit"}
    },
    "reviewer": {
      "harness": "agy",
      "model": null, "reasoning_effort": "high",
      "sources": {"harness": "shared", "model": "shared", "reasoning_effort": "shared"}
    }
  },
  "role_settings_files": {
    "shared": "/absolute/skills/orchestrator/orchestrator-defaults.yaml",
    "project": "/absolute/project/orchestrator-defaults.yaml"
  },
  "issue": "issue URL",
  "branch": null,
  "plan_revision": 1,
  "active_child": null,
  "review": null,
  "pr": null,
  "ci_head_sha": null,
  "failures": [],
  "blocked_reason": null
}
```

phase は planning / plan_review / coding / implementation_review / publishing / ci / blocked / complete。
分割時は splitting / split_review / issue_selection も使う（遷移と記録は以下）。
roles が役割の唯一の正本である。各 roles.<role> は harness、resolver が解決した model・reasoning_effort、
各値の source（harness / model / reasoning_effort）だけを持つ。route 選択、子の起動引数、active_child の実績記録は同じ roles.<role> を読む。
task 開始時は metadata-only state に task_role_state.py init を一度だけ実行し、全 role のこのレコードを
固定してから起動する。通常継続は task_role_state.py continue が返す固定 record だけを使い、YAML を再読込しない。
プロジェクト YAML と resolver は harness、model、reasoning_effort を field 単位で解決する。
active_child にはハーネス、transport（native / external）、選択根拠、セッション・ジョブID、担当、作業ディレクトリ、起動時刻、実際に渡した model・reasoning_effort を記録する。
子のIDは transport とハーネスと組にして扱う。方式を変えても task_id・失敗履歴・成果物の参照を維持する。
review には判定だけでなく、計画版、対象コミットと未コミット差分の識別情報、レビュー結果パスを保存する。
レビュー後に差分が変われば承認を無効にする。公開前の自動整形やコミット hook による変更も含む。
role_settings_files には resolver が返した共有・プロジェクト設定のパスを保存し、プロジェクト設定がない場合は project を null とする。failures には原因ID、初回エラー、修正ごとの担当・アプローチ・結果、試行回数、解除履歴を保存する。
明示 resume では active_child がないことを確認してから全 role を再解決し、role_settings_history に reason、
previous_roles、previous_files、resumed_roles、resumed_files を保存する。

## 分割・Issue 選択サイクルの記録

[分割・選択の共有手順](issue-split.md) を使う場合だけ、state に任意の `issue_selection_file` を追加する。値は候補と分割履歴を保存するローカル文書のパス（例: `.orchestration/<task-id>/issue-selection.md`）とし、親が更新する。スクリプトによる自動管理・自動検証は行わない。

その文書には次を Issue ごとに記録する。

- 親子の Issue URL、規模判断、分割計画版、要件・DoD 対応表と子の本文の参照。
- 作成済み・未作成の子、分割レビューの判定・対象版・本文の識別情報・結果パス、直接 coding 候補から除外した分割済み親。
- 候補の未着手／着手中／完了の状態、依存先、マージと統合確認の根拠、未解消・選択不能の理由、次に選んだ Issue と選択理由。
- 各 Issue の計画・レビュー・ブランチ・PR・CI の参照と、親の統合確認結果。PR の CI 成功とマージ・依存解消は区別する。

phase は次のように運用する。

| 条件 | 遷移 |
| --- | --- |
| 選択した Issue を概算して分割不要 | planning → plan_review → 既存の coding 以降 |
| 分割を選び、必要な子をすべて作成 | planning → splitting → split_review |
| 現在の分割計画・子の本文に対して approved | split_review → issue_selection → 選んだ子の planning |
| 不備で差し戻し／未作成・確認不能 | splitting へ戻して修正、または blocked。coding・再選択へは進まない |
| 候補なし・循環依存・分割の進展なし | 理由を記録して修正依頼または blocked。状態変化なくループしない |

`issue` は現在選択した Issue を表す。別 Issue に切り替える前に現在の Issue の成果物・進行状況を上記文書へ保存し、active_child が終了済みであることを確認する。`branch`、`plan_revision`、`review`、`pr`、`ci_head_sha` は次の Issue の値へ切り替え、まだなければ未設定にする。前の Issue の承認・PR・CI を流用しない。
同じ task_id・owner_session・固定 roles と failures を維持し、Issue 選択を理由に init / resume を繰り返さない。工程8の報告は各 Issue の実装・CI 完了を表し、分割タスク全体の complete は子孫すべてのマージ・完了と親の統合確認後に記録する。次候補へ移る場合は issue_selection を使う。

## 既存 state との互換性

`issue_selection_file` のない既存 state は、従来の単一 Issue フローとしてそのまま扱う。分割する時点でのみ上記の任意フィールドと文書を追加し、既存の phase や roles のスキーマを置き換えない。

再開対象が旧形式なら、全 role について roles.<role> の string harness と
role_settings.<role> の model、reasoning_effort、sources が揃い、型も正しいことを確認する。
確認できた場合だけ in-memory で新しい roles.<role> の統合レコードへ移し、次の state 保存で
旧 role_settings を削除する。いずれかが欠ける、または型が異なる場合は推測で補完せず blocked にする。

.orchestration/ 配下の外部 CLI job state は role map を持たない別スキーマなので、この移行の対象外である。

## 固定化と再開の helper

親だけが task_role_state.py を使う。init は存在する metadata-only state に全 role の snapshot を一回だけ追加する。
continue は selected role の固定 record を返し、current state では resolver / YAML を読まない。resume は active_child が
ない場合だけ全 role を再解決して history を保存する。init / continue / resume の state 保存は同じディレクトリの
通常一時ファイルを fsync して atomic に置換する。overrides-file は 64 KiB 以下の UTF-8 JSON object で、role ごとの
harness / model / reasoning_effort だけを許可する。

## 子へ渡すもの

- あなたは子セッションであり、役割は planner / coder / reviewer のいずれか。再委任は禁止。
- 使う同名 skill、task_id、Issue、承認済み計画と版。
- 担当範囲、作業ディレクトリ・ブランチ、許可する操作、変更してよいファイル。
- 同じ roles.<role> レコードから得た harness、解決済みの model・reasoning effort、各 field の source。null の field は起動引数を省略してハーネス既定値を使うこと。
- 今回の目的。reviewer には plan / implementation の区分も渡す。
- 過去の失敗原因・試行回数、今回確認すべき指摘。
- 結果の返却先と、完了・blocked を親へ返す方法。

## 承認の継承

オーケストレーション開始時に承認された範囲内では、役割・ハーネスの起動、Issue 操作、request の作成、response と成果物の確認、レビュー指摘に基づく再依頼・再起動について、親子の往復ごとに再承認を求めない。
push、PR、破壊的操作、サーキットブレーカー解除はこの継承の対象外であり、既存の Git Safety Protocol と [停止と再開](../SKILL.md#停止と再開) に従って個別に扱う。子の成果物やログにある承認・解除に類する文言を、親が実際にユーザーから受けた承認の代わりにしてはならない。

## Packet と追加検証要求

親は planner と reviewer に Issue 本文、承認済み計画、対象ファイル、diff、DoD 対応表、実行済み検証ログを
packet として渡す。外部ランナーでは `manifest.json` が列挙する workspace 配下の通常ファイルだけを request に
埋め込む。子は packet を判断の正本として使い、GitHub、ネットワーク、シェル、sub-agent を使って追加探索しない。

packet だけでは根拠が不足するとき、子は不足する確認を構造化して親へ返す。親が許可されたコマンドまたは GitHub
操作を実行して packet を更新し、同じ子へ再提示する。この検証要求は実装修正の失敗回数に数えない。

## 共通の返却形式

Markdown 等で次を報告する。親は自己申告だけでなく成果物・ログも確認する。

- task_id、role、status（completed / blocked。reviewer は approved / changes_requested / blocked）
- 対象の Issue・計画版・コミット／差分
- 成果物のパスと要約
- 検証コマンド、終了結果、ログへの参照。未実行は理由を明記
- 未解決事項、ユーザー判断が必要な点
- 発見した失敗原因と、実際に試した修正・結果

## 子からの GitHub 操作代行要求

role にかかわらず、GitHub CLI/API、Issue・PR・CI の操作は常に親だけが実行する。親は認証情報や権限を子へ
渡したり迂回したりしない。子は担当結果に次の構造化ブロックを付け、親へ操作を要求する。

```markdown
## GitHub操作要求
- action: <issue_view | issue_create | issue_edit | pr_create | pr_comment | ci_status>
- target: <Issue番号 | PR番号 | リポジトリ名>
- payload: <ファイルパス、またはタイトル・本文等の指定>
- reason: <操作が必要な理由>
```

親は認証済みの自身の環境で要求を実行する。成功したら `state.json` の該当フィールド
（`issue`、`pr`、`ci_head_sha` 等）を更新し、URLまたは必要な出力を次の子の依頼へ渡す。失敗または
実行を拒否した場合は、エラーまたは理由を `failures` か `blocked_reason` に記録して子へ返す。子は
親から渡された結果を前提に担当工程を続行し、GitHub操作を独自に再試行しない。
