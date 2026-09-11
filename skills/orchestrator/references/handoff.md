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
      "sources": {"model": "explicit", "reasoning_effort": "shared"}
    },
    "coder": {
      "harness": "codex",
      "model": "gpt-5", "reasoning_effort": "high",
      "sources": {"model": "project", "reasoning_effort": "explicit"}
    },
    "reviewer": {
      "harness": "agy",
      "model": null, "reasoning_effort": "high",
      "sources": {"model": "shared", "reasoning_effort": "shared"}
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
roles が役割の唯一の正本である。各 roles.<role> は harness、resolver が解決した model・reasoning_effort、
各値の source だけを持つ。route 選択、子の起動引数、active_child の実績記録は同じ roles.<role> を読む。
プロジェクト YAML と resolver は harness を扱わず、harness は既定または親への明示指定から決める。
active_child にはハーネス、transport（native / external）、選択根拠、セッション・ジョブID、担当、作業ディレクトリ、起動時刻、実際に渡した model・reasoning_effort を記録する。
子のIDは transport とハーネスと組にして扱う。方式を変えても task_id・失敗履歴・成果物の参照を維持する。
review には判定だけでなく、計画版、対象コミットと未コミット差分の識別情報、レビュー結果パスを保存する。
レビュー後に差分が変われば承認を無効にする。公開前の自動整形やコミット hook による変更も含む。
role_settings_files には resolver が返した共有・プロジェクト設定のパスを保存し、プロジェクト設定がない場合は project を null とする。failures には原因ID、初回エラー、修正ごとの担当・アプローチ・結果、試行回数、解除履歴を保存する。

## 既存 state との互換性

再開対象が旧形式なら、全 role について roles.<role> の string harness と
role_settings.<role> の model、reasoning_effort、sources が揃い、型も正しいことを確認する。
確認できた場合だけ in-memory で新しい roles.<role> の統合レコードへ移し、次の state 保存で
旧 role_settings を削除する。いずれかが欠ける、または型が異なる場合は推測で補完せず blocked にする。

.orchestration/ 配下の外部 CLI job state は role map を持たない別スキーマなので、この移行の対象外である。

## 子へ渡すもの

- あなたは子セッションであり、役割は planner / coder / reviewer のいずれか。再委任は禁止。
- 使う同名 skill、task_id、Issue、承認済み計画と版。
- 担当範囲、作業ディレクトリ・ブランチ、許可する操作、変更してよいファイル。
- 同じ roles.<role> レコードから得た harness、解決済みの model・reasoning effort、各 field の source。null の field は起動引数を省略してハーネス既定値を使うこと。
- 今回の目的。reviewer には plan / implementation の区分も渡す。
- 過去の失敗原因・試行回数、今回確認すべき指摘。
- 結果の返却先と、完了・blocked を親へ返す方法。

## 共通の返却形式

Markdown 等で次を報告する。親は自己申告だけでなく成果物・ログも確認する。

- task_id、role、status（completed / blocked。reviewer は approved / changes_requested / blocked）
- 対象の Issue・計画版・コミット／差分
- 成果物のパスと要約
- 検証コマンド、終了結果、ログへの参照。未実行は理由を明記
- 未解決事項、ユーザー判断が必要な点
- 発見した失敗原因と、実際に試した修正・結果
