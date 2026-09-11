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
  "roles": {"planner": "claude", "coder": "codex", "reviewer": "agy"},
  "role_settings": {
    "planner": {"model": null, "reasoning_effort": null},
    "coder": {"model": "gpt-5", "reasoning_effort": "high"},
    "reviewer": {"model": null, "reasoning_effort": "medium"}
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
active_child にはハーネス、transport（native / external）、選択根拠、セッション・ジョブID、担当、作業ディレクトリ、起動時刻、実際に渡した model・reasoning_effort を記録する。
子のIDは transport とハーネスと組にして扱う。方式を変えても task_id・失敗履歴・成果物の参照を維持する。
review には判定だけでなく、計画版、対象コミットと未コミット差分の識別情報、レビュー結果パスを保存する。
レビュー後に差分が変われば承認を無効にする。公開前の自動整形やコミット hook による変更も含む。
failures には原因ID、初回エラー、修正ごとの担当・アプローチ・結果、試行回数、解除履歴を保存する。

## 子へ渡すもの

- あなたは子セッションであり、役割は planner / coder / reviewer のいずれか。再委任は禁止。
- 使う同名 skill、task_id、Issue、承認済み計画と版。
- 担当範囲、作業ディレクトリ・ブランチ、許可する操作、変更してよいファイル。
- 指定された model・reasoning effort。未指定ならハーネス既定値を使うこと。
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
