# claude-meter

ClaudeCodeの利用ログおよびAWS Bedrockの推定コストを解析・表示する、ローカル完結型のツールです。

## クイックスタート

### `uv` を使用する場合
以下のコマンドを実行すると、初回実行時は初期化が行われ、その後バックグラウンドでのログ監視とともにUIが起動します。

```bash
uv run claude-meter start   # または: uv run cm start
```

### `uvx` を使用する場合（Bitbucketリポジトリから直接実行）

```bash
# パッケージ（tar.gz）を指定して実行する場合
# (<VERSION> を対象のバージョンに、<BITBUCKET_WORKSPACE_NAME> と <BITBUCKET_REPOSITORY_NAME> をご自身のリポジトリ情報に置き換えてください)
uvx --from https://bitbucket.org/<BITBUCKET_WORKSPACE_NAME>/<BITBUCKET_REPOSITORY_NAME>/raw/master/packages/claude-meter/claude-meter-<VERSION>.tar.gz claude-meter start

# Gitリポジトリから直接実行する場合
# (<BITBUCKET_WORKSPACE_NAME> をご自身のワークスペース名に置き換えてください)
uvx --from git+https://bitbucket.org/<BITBUCKET_WORKSPACE_NAME>/claude-meter.git claude-meter start
```

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
