<!-- markdownlint-disable MD033 MD041 -->
<table><tr>
<td><img src="assets/icon.png" width="60" alt="claude-meter icon"></td>
<td><h1>claude-meter</h1></td>
</tr></table>
<!-- markdownlint-enable MD033 MD041 -->

ClaudeCodeの利用ログおよびAWS Bedrockの推定コストを解析・表示する、ローカル完結型のツールです。

## クイックスタート

### `uv` を使用する場合
以下のコマンドを実行すると、初回実行時は初期化が行われ、その後バックグラウンドでのログ監視とともにUIが起動します。

```bash
uv run claude-meter start   # または: uv run cm start
```

### ワンラインインストール（ローカルクローン不要）

<!-- markdownlint-disable MD013 -->

> **重要**: Bitbucket App passwords は Atlassian により非推奨となりました。2026年7月28日に完全に削除されます。詳細は以下の通りです:
>
> - 2025年6月9日: 非推奨が発表され、12ヶ月の移行期間が開始
> - 2025年9月9日: 新規 App password の作成が無効化（既存のものは動作継続）
> - 2026年6月9日: 既存 App password の段階的な停止が開始
> - 2026年7月28日: App password が完全に削除される
>
> 本ドキュメントは、この非推奨化とは無関係な Bitbucket の Repository Access Token
> （リポジトリ単位のトークン）を使用した認証方法で構成されています。

プライベートリポジトリからパッケージを取得するため、対象リポジトリの Settings → Security →
Access tokens で作成した Repository Access Token（`Repository Read` スコープ）が必要です。
個人の Atlassian アカウントとは無関係なトークンのため、email やユーザー名を別途用意する必要は
ありません。REST API・raw取得は `Authorization: Bearer` ヘッダーで、Git操作はユーザー名
`x-token-auth` とトークンで認証します。

認証情報がコマンド履歴や画面に残らないよう、次のように環境変数で認証情報を渡したうえで実行してください。`claude-meter` コマンドのインストール、`claude-meter init` の実行、デスクトップランチャーの作成までを自動で行います（ローカルへのクローンは不要です）。

```bash
export BITBUCKET_API_TOKEN=<your-repository-access-token>
(tmp="$(mktemp)" && printf 'header = "Authorization: Bearer %s"\n' "$BITBUCKET_API_TOKEN" \
  | curl -fsSL -K - -o "$tmp" \
  "https://api.bitbucket.org/2.0/repositories/<BITBUCKET_WORKSPACE_NAME>/<BITBUCKET_REPOSITORY_NAME>/src/master/install.sh" \
  && sh "$tmp"; status=$?; rm -f "$tmp"; exit $status)
```

上記のコマンドはリモートのスクリプトを一時ファイルにダウンロードしてから実行します。`curl` が完全に成功した場合のみ `sh` が実行されるため、ダウンロード中に回線が切断されて不完全なスクリプトが実行されてしまうリスクを防げます。一時ファイルは実行後（失敗時も含め）に削除されます。安全のため、実行前にスクリプトの内容を必ず確認することを推奨します。

`curl -H "Authorization: Bearer $BITBUCKET_API_TOKEN"` は展開後のトークンがそのまま `curl` の
コマンドライン引数として渡され、実行中は同じホスト上の他ユーザーが `ps aux` や
`/proc/{pid}/cmdline` から読み取れてしまいます。上記のコマンドは代わりにヘッダー指定を標準入力経由の
設定ファイルとして `curl -K -` に渡すため、プロセス引数には一切現れません。

なお、この `curl` コマンド自体は `master` ブランチから取得するため、取得元スクリプトそのものは
改版不変（immutable）な参照には固定されていません。インストールされる `claude-meter`
パッケージ自体は、インストーラーがBitbucket APIから解決した最新タグに固定され、解決に失敗した
場合は未レビューのデフォルトブランチへフォールバックせず処理を中断します。
<!-- markdownlint-enable MD013 -->

### `uvx` を使用する場合（Bitbucketリポジトリから直接実行）

<!-- markdownlint-disable MD013 -->
```bash
# Gitリポジトリから直接実行する場合
# ※プライベートリポジトリの場合は、SSH接続（git+ssh://）を使用するか、下記のHTTPS接続の例（GIT_ASKPASS使用）を参照してください。

## SSH接続を使用する場合（推奨・要SSHキー設定）
uvx --from git+ssh://git@bitbucket.org/<BITBUCKET_WORKSPACE_NAME>/claude-meter.git claude-meter start

## HTTPS接続を使用する場合
uvx --from git+https://bitbucket.org/<BITBUCKET_WORKSPACE_NAME>/claude-meter.git claude-meter start
# プライベートリポジトリでHTTPS接続を使用する場合は、認証情報をURLに直接埋め込まないでください
# （シェル履歴・プロセス引数・Gitのリモート設定に平文で残ってしまいます）。
# 代わりに GIT_ASKPASS 経由で Repository Access Token を渡してください
# （ユーザー名は Bitbucket の規約で固定文字列 x-token-auth を使用します）:
# export BITBUCKET_API_TOKEN=<your-repository-access-token>
# askpass="$(mktemp)" && printf '#!/bin/sh\necho "$BITBUCKET_API_TOKEN"\n' > "$askpass" && chmod +x "$askpass"
# GIT_ASKPASS="$askpass" uvx --from "git+https://x-token-auth@bitbucket.org/<BITBUCKET_WORKSPACE_NAME>/claude-meter.git" claude-meter start
# rm -f "$askpass"
```
<!-- markdownlint-enable MD013 -->

### `pip` を使用する場合

```bash
pip install -e .
claude-meter init
claude-meter collect
claude-meter ui
```

すべてのデータは `~/.claude-meter/` に保存されます。AWS Bedrockの最新料金データを更新するときのみ外部ネットワークにアクセスしますが、それ以外の処理はすべてローカルPC上で完結します。

## プライバシーについて

デフォルトでは `store_prompts: true` に設定されており、使用メトリクスやメタデータとともにプロンプトおよびレスポンスのテキストが記録されます。プロンプトやレスポンスの本文を保存したくない場合は、`store_prompts: false` に設定してください。この設定をオフにしても、トークン数やコストなどの利用メトリクス、リクエストのメタデータ（プロジェクト、セッション、モデル、タイムスタンプなど）は収集・保持されます。また、応答時間 (`response_time_ms`) は設定に関わらずタイムスタンプから常に算出されます。

プロジェクト名は、各JSONLレコードの `cwd` （`.git/config` またはディレクトリ名）から自動で判別されます。デフォルトのClaudeデータディレクトリにある `history.jsonl` （macOS/Linuxでは `~/.claude/history.jsonl`、Windowsでは `%LOCALAPPDATA%\Claude\history.jsonl`）は、プロジェクトの表示名を補正するためのヒントとして任意で参照されます。この履歴データが存在しない場合でも、収集処理が妨げられることはありません。

`uuid` が存在しないレコードには、決定論的な合成ID（synthetic ID）が割り当てられるため、`(session_id, request_id)` のユニーク制約が維持されます。

## 動作確認済みバージョン

本ツールは **Claude Code v2.1.159** での動作を確認しています。Claude Codeは利用履歴を
`~/.claude/projects/` 以下（Windowsでは対応するディレクトリ）にJSONLファイルとして保存しますが、
そのレコードのスキーマ（フィールド名、階層構造、`message.content` のブロック種別など）は
Claude Code内部の実装詳細であり、バージョンアップによって変更されないことは保証されていません。

Claude CodeがJSONLの構造を変更するバージョンにアップグレードされた場合、`claude-meter` が
レコードを正しく解析できなくなったり、一部フィールドが欠落したり、データ収集が完全に停止したり
する可能性があります。Claude Codeをアップグレードした後に収集が突然動作しなくなった場合は、
JSONL形式が変更されていないか確認し、必要に応じて開発者に報告してください。

## 主な機能

- 設定されたプロジェクトディレクトリ（macOS/Linuxではデフォルトで `~/.claude/projects/*/*.jsonl`、Windowsでは `%LOCALAPPDATA%\Claude\projects\...`）から、ClaudeCodeのJSONLログを差分解析します。
- JSONLファイルからプロンプト・レスポンスの本文および応答時間を直接抽出します。`response_text` は各 `assistant` レコードの `message.content` 内の `type == "text"` ブロックを結合したものです。`prompt_text` は `parentUuid` チェーンを遡って見つかった最も近い `user` の発言です。`response_time_ms` は入力レコードとアシスタントレコードのタイムスタンプの差分から計算されます。
- キャッシュされたモデル・リージョンごとの料金情報を利用して、AWS Bedrockの推定コストを計算します。
- すべてのデータをローカルの `~/.claude-meter/data.db` に保存します（`requests`、`pricing`、`sync_state`、`daily_summary` テーブル）。
- `http://127.0.0.1:8501` でアクセス可能なStreamlitダッシュボードを提供します。ダッシュボードには、概要（Overview）、プロジェクト別内訳（Project Breakdown）、モデル別内訳（Model Breakdown）、セッションエクスプローラー（Session Explorer）、料金設定（Pricing Settings）、構成設定（Config）のページがあります。
- Windows、macOS、Ubuntuに対応しており、`pathlib` を使用することでOSに依存しないパス処理を実現しています。
- ClaudeCodeの内部モデル名とBedrockのARN形式のIDを正規化レイヤーによってマッピングし、リージョンプレフィックスが異なるバリアント間でも料金を一致させることができます。

## CLIコマンド

| コマンド | 説明 |
| --- | --- |
| `claude-meter init` | 設定ファイルとSQLiteデータベースを新規作成します。 |
| `claude-meter collect` | JSONLログを1回解析し、コストを計算してDBに反映します。 |
| `claude-meter collect --reparse` | すべてのJSONLファイルを最初から再解析してインポートします。 |
| `claude-meter watch` | 設定されたデータディレクトリを監視します（`watchdog` またはポーリングを使用）。 |
| `claude-meter ui` | Streamlit UIを起動します。 |
| `claude-meter ui --watch [--poll N]` | UIを起動し、同時にログファイルの監視を行います。 |
| `claude-meter start` | 初回起動時の初期化を行い、ログ監視付きでUIを起動します。 |
| `claude-meter pricing update [--force]` | Bedrockの料金キャッシュを更新します。 |
| `claude-meter config` | 設定ファイルのパスを表示します。 |

## 設定

`~/.claude-meter/config.yaml`:

```yaml
claude:
  projects_dir: null      # デフォルト: OS依存 (下記参照)
  transcripts_dir: null     # デフォルト: OS依存 (下記参照)
  region: "us-east-1"     # コスト計算に使用するリージョン

storage:
  db_path: "~/.claude-meter/data.db"

pricing:
  primary_source: "models_dev"
  fallback_source: "aws_bedrock_json"
  cache_ttl_hours: 24

privacy:
  store_prompts: true
  max_prompt_length: 10000
  max_response_length: 10000
  show_prompts_in_ui: true

ui:
  port: 8501
  host: "127.0.0.1"
```

### OSごとのデフォルトのClaudeデータディレクトリ

| OS | デフォルトのパス |
| --- | --- |
| macOS / Linux | `~/.claude` |
| Windows | `%LOCALAPPDATA%\Claude` |

アーキテクチャ、データソース、SQLiteスキーマ、モデル正規化、UIページの設計、および対象外とする要件の詳細については、[SPEC.md](SPEC.md)（英語）を参照してください。

## コスト計算方法

コストは以下の計算式に基づいてコンポーネントごとに算出されます。

```text
input_cost  = input_tokens × input_price_per_1k / 1000
output_cost = output_tokens × output_price_per_1k / 1000
cache_cost  = cache_creation_input_tokens × cache_creation_price_per_1k / 1000
            + cache_read_input_tokens × cache_read_price_per_1k / 1000
total_cost  = input_cost + output_cost + cache_cost
```

トークン数が存在するにもかかわらず対応する単価が不明な場合、過小評価を防ぐために `cost_usd` は0ではなく `NULL` に設定されます。また、未定義のモデルについても `cost_usd = NULL` となり、UI上では「Unknown model」と表示されます。

### 推定精度について

コストは各 `assistant` レコードの `usage` ブロック（Claude Code が Bedrock から受け取るリクエスト単位の使用量レポート）から推定されます。output およびキャッシュのトークンは AWS 実額とよく一致しますが、**global クロスリージョン推論**と大量のプロンプトキャッシュが重なる場合、推定値が AWS 実額を数パーセント**下回る**ことがあります。

原因は AWS 課金側での分類の違いです。AWS は、トランスクリプトが `cache_read` として記録しているトークンの一部を、fresh な `input`（クロスリージョンのキャッシュミス、または5分のキャッシュTTL失効によるもの）として課金することがあります。総トークン量自体は一致しますが、`input` は `cache_read` の約10倍の単価であるため、その分だけ AWS 側のコストが高くなります。実際にあるSonnet 4.6のデータでは乖離は約5.6%で、`cache_read` の約2%を `input` に振り替えると完全に解消しました。

これはトランスクリプトのみから推定する方式に固有の制約です。この再分類はレスポンス記録後にサーバ側で発生し、JSONL には痕跡が残らない（`inference_geo` も空）ため、AWS Cost Explorer のデータ（本ツールの対象外機能）なしには補正できません。推定値は実際の Bedrock コストに近い下限値として捉えてください。

## 料金情報の参照元

1. **`models.dev` API** (`https://models.dev/api.json`) — プライマリソース。ARN形式のBedrockモデルIDを含む料金データを取得します。
2. **AWS Bedrock料金JSON** — セカンダリソース。ARN形式のキーを生成できる場合にのみデータを受け入れます。
3. **内蔵フォールバックJSON** — 外部ソースがすべて利用できない場合に使用されます。

ソースの優先順位は設定で変更でき、値は `models_dev` または `aws_bedrock_json` のいずれかになります。

`~/.claude-meter/` 内のキャッシュファイル：

| ファイル名 | 用途 |
| --- | --- |
| `pricing.json` | キャッシュされた料金レコード |
| `pricing-meta.yaml` | キャッシュの有効期限（TTL）管理用のタイムスタンプ |
| `pricing-overrides.json` | ユーザーがUIから手動で上書きした料金設定 |

キャッシュの書き込みは、並行処理時の安全性を担保するため一時ファイルを作成した後にアトミックに置換 (`os.replace`) されます。外部ソースとの通信がすべて失敗した場合でも、ARN形式の古いキャッシュが存在すれば内蔵のフォールバックよりも優先して使用されます。TTLは設定可能（デフォルトは24時間）です。

## 開発

```bash
# POSIX / macOS / Linux
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check src tests
.venv/bin/python -m mypy --strict src

# Windows
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check src tests
.venv\Scripts\python -m mypy --strict src
```

## 使用技術

Python 3.10+、SQLite、Streamlit、watchdog、requests、pydantic / pydantic-settings、Altair、pandas、Click。

## 対象外の機能（Non-goals）

- AWS CloudTrail や Bedrockのログ自体の解析
- IAM権限が必要な AWS Cost Explorer との連携
- クラウドベースでのデータ共有や集約
