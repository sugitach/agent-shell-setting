# Git Conventions

## 1. ブランチ命名規則 (Git Flow)

- 機能開発: `feature/{issue#}/{short-description}`
  - 例: `feature/101/add-login-api`
- バグ修正: `bugfix/{issue#}/{short-description}` または `feature/...`
  - 例: `feature/102/fix-session-timeout`
- 緊急修正: `hotfix/{issue#}/{short-description}`

## 2. コミットメッセージ規約 (Conventional Commits)

形式: `<type>(<scope>): <description>`

### 主要なプレフィックス
- `feat`: 新機能の追加
- `fix`: バグ修正
- `test`: テストコードの追加・修正
- `refactor`: リファクタリング（機能変更なし）
- `docs`: ドキュメントの変更
- `chore`: ビルドツールやライブラリの更新など雑務

### 例
- `test: add unit tests for user authentication (#12)`
- `feat: implement user authentication flow (#12)`
