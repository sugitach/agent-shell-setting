# Antigravity (agy) / Codex / Claude Code グローバル設定 & 開発ワークフロー 構築サマリー

本ドキュメントは、Antigravity (agy)、Codex、Claude Code のグローバル設定（ルール、サーキットブレーカー、標準スキル）の構築経緯および構成内容をまとめたものです。
新規セッション開始時やディレクトリ移動後も、本ファイルを参照することで前回の設計思想と設定状況を即座に把握・再開できます。

設定の原本はこのリポジトリで Git 管理し、ホーム以下にはコピーを配布します。
ホーム側を直接編集せず、原本の編集後に `python3 scripts/deploy.py --apply` を実行してください。
`python3 scripts/deploy.py` は差分確認だけを行い、差分があれば終了コード 1 を返します。
変更前のファイル・リンク・管理対象スキルディレクトリは、`~/.agent-shell-setting-backups/deploy-*/` に退避します。
復旧時は表示されたバックアップ内の対応する項目を元の場所へ戻してください。旧リンク先も同じ配布で変更した場合は、その原本も一緒に戻します。

管理対象は `rules/AGENTS.md`、`skills/`、`claude-code/settings.json`、`claude-code/hooks/circuit-breaker.py` です。
認証情報、会話ログ、キャッシュ、サーキットブレーカーの実行状態、今回扱っていない個別設定は取り込みません。
Claude の settings は確認済みの既存設定全体（theme・enabledPlugins を含む）を原本とし、配布時に全体をコピーします。
現在の hook コマンドには `/Users/sugita` の絶対パスが含まれるため、別ユーザー環境では原本のパスを調整してください。

---

## 1. 構築の背景と最重要方針

エージェント開発において最も懸念される **「同一エラーの無限試行による泥沼ループ」および「トークンの大量浪費」を防止すること** を最優先課題とし、グローバルルールと作業手順（スキル）を設計しました。

### コア設計思想
- **指示の軽量化**: ユーザーは「要件と完了条件 (DoD)」を2〜3行で指示するだけで、定型ルール（ブランチ命名、TDD、PR作成等）はエージェントが自動準拠する。
- **サーキットブレーカー**: 試行上限を厳格に「2回」と定め、迷走する前に自律停止して人間に指示を仰ぐ。

---

## 2. グローバル設定ファイル一覧

| ファイル / ディレクトリ | 役割・内容 |
| :--- | :--- |
| [`~/.gemini/ANTIGRAVITY.md`](file:///Users/sugita/.gemini/ANTIGRAVITY.md) | **全プロジェクト共通の行動規範・安全弁**<br>・サーキットブレーカー（同一エラー修正上限2回）<br>・ディレクトリ内外の実行権限・確認ルール<br>・日本語応答、謝罪抑制、呼称（私/あなた） |
| [`~/.gemini/old-GEMINI.md`](file:///Users/sugita/.gemini/old-GEMINI.md) | 旧 `GEMINI.md` のバックアップ（参照無効化済み） |
| [`~/.gemini/antigravity-cli/skills/github-tdd-workflow/`](file:///Users/sugita/.gemini/antigravity-cli/skills/github-tdd-workflow/) | **標準開発ワークフロースキル**<br>Issue駆動、Git Flow、TDD、PR作成・CI修正の統合手順書 |
| [`~/.gemini/config/skills/github-tdd-workflow/`](file:///Users/sugita/.gemini/config/skills/github-tdd-workflow/) | 上記スキルの同期配置 |
| [`~/.codex/AGENTS.md`](file:///Users/sugita/.codex/AGENTS.md) | Codex のグローバル指示。`rules/AGENTS.md` のコピー。 |
| [`~/.agents/skills/github-tdd-workflow/`](file:///Users/sugita/.agents/skills/github-tdd-workflow/) | Codex のグローバルスキル。`skills/github-tdd-workflow/` のコピー。 |
| [`~/.claude/CLAUDE.md`](file:///Users/sugita/.claude/CLAUDE.md) | Claude Code の個人共通指示。`rules/AGENTS.md` のコピー。 |
| [`~/.claude/skills/github-tdd-workflow/`](file:///Users/sugita/.claude/skills/github-tdd-workflow/) | Claude Code の個人共通スキル。`skills/github-tdd-workflow/` のコピー。 |
| [`~/.claude/settings.json`](file:///Users/sugita/.claude/settings.json) | Claude Code のグローバル hooks 設定。サーキットブレーカーを実行時に強制する。 |
| [`~/.claude/hooks/circuit-breaker.py`](file:///Users/sugita/.claude/hooks/circuit-breaker.py) | Bash・Edit・Write の同一エラーをセッション単位で数え、2回で以降の変更・実行を停止する hook 本体。 |

---

## 3. 定義されたルールの詳細 (`~/.gemini/ANTIGRAVITY.md`)

### ① サーキットブレーカー（無限ループ・トークン浪費防止）
* **同一エラーの修正試行は最大2回まで**。
* 2回連続で失敗した場合、**3回目の修正を勝手に試みることを厳禁とし、即座に作業を停止**。
* **停止時の報告フォーマット**:
  1. 発生している具体的なエラー / 現象（ログ・スタックトレース要約）
  2. これまでに試した2回のアプローチとそれぞれの結果
  3. 考えられる根本原因の仮説と、取りうる今後の選択肢
* **ロールバックファースト**: 失敗した修正はクリーンに戻し、パッチの上にパッチを重ねない。
* **トークン節約**: コマンド出力ログは `grep` / `head` / `tail` で行数を絞る。

### ② 実行権限・確認ルール
* **カレントディレクトリ以下**: 非破壊的操作（作成、編集、閲覧、テスト実行等）は **事前確認不要**。破壊的操作（ハードリセット、重要ディレクトリ削除等）は要確認。
* **カレントディレクトリ外**: ファイル操作・コマンド実行は **すべて事前確認が必要**。
* **サンドボックス環境**: ネットワーク通信を除き、**すべて事前確認不要**。

### ③ コミュニケーション方針
* **言語**: 例外なく日本語で回答（英語指示に対しても日本語応答）。
* **呼称・トーン**: 一人称「私」、二人称「あなた」。親しい同僚のように適度にフランクかつプロフェッショナル。
* **謝罪抑制**: 謝罪は簡潔に1回のみ。繰り返さず問題解決を最優先する。
* **不確実性の早期確認**: 前提やエビデンスが曖昧な場合は当てずっぽうに進めず、ユーザーに確認する。

---

## 4. グローバルスキル (`github-tdd-workflow`) の詳細

タスク指示を受けた際にエージェントが自律的に適用する4段階のワークフローです。

```mermaid
flowchart TD
    A[タスク指示] --> B{Issue が存在するか?}
    B -- No --> C[要件・完了条件を整理して Issue 作成]
    B -- Yes --> D[Issue 内容の検証]
    C --> D
    D --> E{要件 & DoD は十分か?}
    E -- 不足あり --> F[不足情報の補完候補をサジェストし確認]
    F --> D
    E -- 十分 --> G[ブランチ作成: feature/issue#/name]
    G --> H[Step 1: テストコード作成 Red]
    H --> I[Step 2: 最小限の実装コード作成 Green]
    I --> J{テスト全通過 & DoD 達成?}
    J -- No --> K{エラー修正 2回以内?}
    K -- 2回失敗 --> L[サーキットブレーカー発動: 停止して報告]
    K -- Yes --> I
    J -- Yes --> M[コミット & プッシュ]
    M --> N[PR作成: gh pr create]
    N --> O[GitHub Actions / CI チェック確認]
    O -- CI成功 --> P[タスク完了報告]
    O -- CI失敗 --> Q[CIエラー修正 最大2回]
    Q --> O
```

1. **Step 1: チケット駆動（Issue）**
   - 既存Issueを確認。ない場合は要件・完了条件（DoD）を整理して作成。
   - 要件やDoDが不足している場合は、勝手に進めず **補完候補をサジェスト** してユーザーの合意を得る。
2. **Step 2: Git Flow ブランチ管理**
   - スタートポイント: **原則として `develop`**（依存する先行未マージブランチがある場合は検索して特定）。
   - ブランチ命名規則: `feature/{issue#}/{short-description}`
3. **Step 3: テスト駆動開発 (TDD)**
   - テスト先行作成（Red確認）→ 最小限の実装（Green確認）。
   - **テストコード保護**: テストが通らないからといってテスト期待値を勝手に緩和・修正することは厳禁。
4. **Step 4: PR作成 & CI修正**
   - Conventional Commits 規約準拠でコミットし、`gh pr create`（`Closes #{issue#}` を明記）。
   - PR作成後の GitHub Actions（CI）エラーもタスク範囲として自動修正（上限2回）。

---

## 5. 次回セッション以降の検討・拡張候補

個別プロジェクトの進行に合わせて、必要に応じて以下を順次整備していく方針です：
1. **プロジェクト固有の `AGENTS.md` テンプレート作成**
2. **コードベース探索・リポジトリ調査スキル**
3. **MCPサーバー連携やツール固有設定**

---

## 6. Codex / Claude Code との共有方法

以前のホーム間シンボリックリンクを通常のコピーに変更し、全ハーネスの原本をこのリポジトリに集約します。
配布スクリプトは次の対応でコピーします。無関係なスキルや設定には触れません。

```text
rules/AGENTS.md
  => ~/.gemini/ANTIGRAVITY.md
  => ~/.codex/AGENTS.md
  => ~/.claude/CLAUDE.md

skills/<skill-name>/
  => ~/.gemini/antigravity-cli/skills/<skill-name>/
  => ~/.gemini/config/skills/<skill-name>/
  => ~/.agents/skills/<skill-name>/
  => ~/.claude/skills/<skill-name>/

claude-code/settings.json => ~/.claude/settings.json
claude-code/hooks/circuit-breaker.py => ~/.claude/hooks/circuit-breaker.py
```

共通ルール・スキルを変更する際はリポジトリの原本を編集し、差分をレビューしてから配布します。
配布後は差分確認を再実行し、`0 target(s) differ` を確認してください。
スキルディレクトリは全体を退避・コピーするため、配布先だけに加えた編集は次の配布で置き換わります。
原本からスキルを削除しても、ホーム側のスキルは自動削除しません。

---

## 7. Claude Code のサーキットブレーカー強制

`~/.claude/settings.json` のグローバル hooks で、`CLAUDE.md` の「同一エラーの修正試行は最大2回」のルールを強制する。

1. `PostToolUseFailure` が Bash・Edit・Write の失敗を記録し、エラー内容を正規化して同一性を判定する。
2. 同一エラーが2回発生すると、同一セッション内の `Bash`、`Edit`、`Write` を `PreToolUse` が拒否する。
3. `Read` は拒否しないため、根本原因の調査と停止報告は継続できる。
4. ユーザーが「サーキットブレーカー解除」または `circuit breaker reset` と明示すると、`UserPromptSubmit` hook がそのセッションの停止状態を解除する。

hook のソースは [`claude-code/hooks/circuit-breaker.py`](claude-code/hooks/circuit-breaker.py) に保管し、実行用のコピーを `~/.claude/hooks/` に配置する。スクリプトを更新した場合は実行用コピーも同期する。
