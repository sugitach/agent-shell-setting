# 呼び出し方式の選択

役割とハーネスを先に確定し、その後に transport を決める。既定は auto。

| 条件 | transport |
| --- | --- |
| 同一ハーネスで必要な標準サブエージェント機能を確認済み | native |
| 別ハーネス、または native が必要な機能を満たさない | external |
| 独立した設定・権限など、外部セッションが必要 | external |
| 要求を満たす呼び出し手段がない | blocked |

native / external を明示指定された場合は、その方式を使う。利用不能なら黙って変更しない。
auto の外部フォールバックは同じ対象ハーネスに限る。ハーネスの変更は別の判断として扱う。

## 利用可否の確認

native-ready は、現在の親で次を確認できた場合だけ指定する。

- 対象と親が同じハーネスであり、標準サブエージェントの起動ツールが実際に利用可能。
- 担当 skill・入力・作業範囲を渡し、独立した子の結果・エラーを取得できる。
- 子の状態確認と停止が可能で、必要な権限・作業ディレクトリを維持できる。

CLI の help、インストール済みバージョン、同じモデル名だけでは native-ready としない。
ACP アダプタごとの公開ツールを確認する。既存の起動済みセッションの証拠があれば再試験は不要。
external-ready もバイナリの存在だけで設定せず、対応アダプタの入力・返却・エラー・停止・権限を確認する。
起動可否の検証にユーザーの作業を重複実行させない。

## 選択ツール

skill ディレクトリ基準で次を実行する。これは方式を決定するだけで、起動や能力の自動検出は行わない。

```sh
python3 scripts/select_route.py --parent codex --target codex --native-ready
python3 scripts/select_route.py --parent claude --target codex --external-ready
```

入力には実際に確認したフラグだけを付ける。成功時は JSON の status=ready、transport=native / external と終了コード0、利用不能時は blocked と終了コード2を返す。
独立した外部設定が必要なら --require-external、明示指定なら --mode native / external を加える。

native は親自身が標準ツールで起動し、external は接続済みの外部委任ツールで起動する。
native / external の起動操作自体には起動元の sandbox 制限を適用しない。子ハーネスの sandbox / permission-mode は独立した権限制御であり、既存の child access 指定を変更しない。詳細は [起動元 sandbox と子 sandbox](external-runner.md#起動元-sandbox-と子-sandbox) を参照する。
external の具体的な手順・権限・停止制約は [external-runner.md](external-runner.md) を読み、同梱の scripts/external_runner.py を使う。
CLI の認証が未完了、必要な権限が不明、停止要件を満たせない場合は external-ready としない。
外部ツールが親の標準サブエージェント機能を呼び出せるとは仮定しない。
親から引き継いだ会話に親の指示があっても、子には担当役割と再委任禁止を明示する。
同じハーネスでも reviewer は作者と別の子にし、自己レビューに置き換えない。

## 起動後の変更

選択結果・根拠と子IDを state.json に記録する。結果形式・失敗履歴・レビュー条件は両方式で共通。
タイムアウトや切断で起動結果が不明なら、稼働状態を調べてから停止または再接続する。
旧子の停止を確認するまで別方式で再起動しない。停止済みでも既存差分・成果物を確認して重複処理を防ぐ。
呼び出し方式を変更しても試行回数と blocked 状態は維持する。
