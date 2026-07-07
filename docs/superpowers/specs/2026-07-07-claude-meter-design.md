# claude-meter 設計書

## 概要

`claude-meter` は、ClaudeCode（AWS Bedrock 経由）の利用トークン量と API 利用料金を **ローカル環境で完結して** 可視化・分析するツールである。

会社の一般ユーザー IAM には最小限の権限しか与えられておらず、AWS Cost Explorer も翌日反映となるため、利用状況をリアルタイムに把握する手段がないという課題を解決する。

### 対象ユーザー

- AWS Bedrock 経由で ClaudeCode を利用している開発者
- ローカルで完結し、社外への情報漏洩リスクを排除したい組織
- 自分・チームの ClaudeCode 利用コストを把握したいユーザー

## 目的

- ClaudeCode の利用トークン量（入力・出力・キャッシュ）を計測する
- Bedrock の単価をもとに利用料金を推定する
- 時系列・プロジェクト・モデル別に利用状況を分析する
- 入出力プロンプト・応答時間なども紐付けて詳細調査できる
- Windows / macOS / Ubuntu のマルチ OS 環境で動作する

## 絶対条件

- **すべてのデータはローカルに保存する**
- 外部への情報送信は行わない
- 単価取得のみ AWS 公式 pricing JSON または `models.dev` から取得する（取得できない場合は内蔵単価表を使用）

## MUST / BETTER 対応表

| 要件 | 対応方法 | 優先度 |
|---|---|---|
| 入力トークン | `~/.claude/projects/*/*.jsonl` の `assistant.message.usage.input_tokens` | MUST |
| 出力トークン | 同上 `output_tokens` | MUST |
| 可視化 | Streamlit + SQLite によるローカル Web UI | MUST |
| マルチ OS 対応 | Python 3.10+、Windows/macOS/Ubuntu で動作 | MUST |
| 最新 Bedrock 単価取得 | AWS pricing JSON → `models.dev` → 内蔵フォールバック | MUST |
| 入出力プロンプト | `~/.claude/transcripts/*.jsonl` から紐付けて保存 | BETTER |
| プロジェクト名 | `cwd` から `.git/config` またはディレクトリ名を導出 | BETTER |
| 時刻 | `assistant.timestamp`（ISO 8601）を保存 | BETTER |
| 応答時間 | 同一 `requestId` の前後 `user`/`assistant` レコードの差分で推定 | BETTER |

## 収集データソース

### 主要データ源：`~/.claude/projects/<project-name>/<session-id>.jsonl`

ClaudeCode は各プロジェクト・各セッションごとに JSONL ファイルを作成する。`assistant` タイプのレコードに推論結果とトークン使用量が含まれる。

```json
{
  "type": "assistant",
  "timestamp": "2026-05-02T19:12:26.067Z",
  "cwd": "/home/user/project/opencode-cursor-plugin",
  "sessionId": "1d5edb59-e626-4cb0-b7c7-8506fbe48624",
  "requestId": "req_011CaeGoapfoopFq5gARLr7Q",
  "message": {
    "model": "claude-haiku-4-5-20251001",
    "usage": {
      "input_tokens": 10,
      "cache_creation_input_tokens": 36963,
      "cache_read_input_tokens": 0,
      "output_tokens": 621
    }
  }
}
```

### プロンプト本文：`~/.claude/transcripts/<session-id>.jsonl`

同一 `sessionId` の transcripts ファイルから、対応する `user` / `assistant` メッセージ本文を取得する。

### 補助データ源：`~/.claude/history.jsonl`

UI 上の `display` テキストとプロジェクトパス、タイムスタンプを記録。主にプロジェクト紐付けの補助に使用する。

## 収集方式

- Collector が `~/.claude/projects/*/*.jsonl` と `~/.claude/transcripts/*.jsonl` を監視する
- ファイル変更を `watchdog` またはポーリングで検知する
- `sync_state` テーブルで各ファイルの最終パース位置を管理し、**増分パース**を行う
- 新規・変更分のみを SQLite に書き込む

### 応答時間の計算

`assistant` レコードには `durationMs` が含まれないため、同一 `requestId` に対応する直前の `user` レコードとの `timestamp` 差分で推定する。

## 保存先：SQLite

### ファイル配置

- データベース：`~/.claude-meter/data.db`
- 設定ファイル：`~/.claude-meter/config.yaml`
- 単価キャッシュ：`~/.claude-meter/pricing.json`

### テーブル設計

#### `requests`

```sql
CREATE TABLE requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME NOT NULL,
    session_id TEXT NOT NULL,
    request_id TEXT,
    project TEXT,
    git_repository TEXT,
    model TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_creation_input_tokens INTEGER,
    cache_read_input_tokens INTEGER,
    response_time_ms INTEGER,
    cost_usd REAL,
    prompt_text TEXT,
    response_text TEXT,
    source_file TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (session_id, request_id)
);

CREATE INDEX idx_requests_timestamp ON requests(timestamp);
CREATE INDEX idx_requests_project ON requests(project);
CREATE INDEX idx_requests_model ON requests(model);
CREATE INDEX idx_requests_session ON requests(session_id);
```

#### `pricing`

```sql
CREATE TABLE pricing (
    model TEXT NOT NULL,
    region TEXT NOT NULL,
    PRIMARY KEY (model, region),
    input_price_per_1k REAL,
    output_price_per_1k REAL,
    cache_creation_price_per_1k REAL,
    cache_read_price_per_1k REAL,
    source TEXT,
    updated_at DATETIME
);
```

#### `sync_state`

```sql
CREATE TABLE sync_state (
    file_path TEXT PRIMARY KEY,
    last_size INTEGER,
    last_line INTEGER,
    last_modified DATETIME
);
```

#### `daily_summary`（オプション）

日次の集計テーブル。大量データ時の表示高速化用。

```sql
CREATE TABLE daily_summary (
    date TEXT NOT NULL,
    project TEXT NOT NULL,
    model TEXT NOT NULL,
    total_input_tokens INTEGER,
    total_output_tokens INTEGER,
    total_cache_creation_input_tokens INTEGER,
    total_cache_read_input_tokens INTEGER,
    total_cost_usd REAL,
    request_count INTEGER,
    avg_response_time_ms REAL,
    PRIMARY KEY (date, project, model)
);
```

## Bedrock 単価取得

### 取得元の優先順位

1. **AWS 公式 Pricing JSON**
   - URL: `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/`
   - IAM 権限不要
   - 取得間隔：デフォルト 24 時間

2. **`models.dev` API**
   - URL: `https://models.dev/providers/amazon-bedrock/`
   - AWS 公式 JSON が取得できない場合のフォールバック

3. **内蔵フォールバック単価表**
   - ツールに同梱された JSON/YAML
   - 外部通信不可の環境で使用

### 単価キャッシュ

- 取得した単価は `~/.claude-meter/pricing.json` に保存
- TTL 24 時間（設定可能）
- TTL 内はキャッシュを使用、切れれば再取得
- 取得失敗時は前回キャッシュまたは内蔵表を使用

### コスト計算ロジック

```text
input_cost  = input_tokens × input_price_per_1k_tokens / 1000
output_cost = output_tokens × output_price_per_1k_tokens / 1000
cache_cost  = cache_creation_input_tokens × cache_creation_price / 1000
            + cache_read_input_tokens × cache_read_price / 1000
total_cost  = input_cost + output_cost + cache_cost
```

### モデル名の正規化

ClaudeCode 内部名（`claude-haiku-4-5-20251001`）と Bedrock ARN 形式（`anthropic.claude-3-5-sonnet-20241022-v2:0`）の両方に対応するため、名前正規化レイヤーを設ける。正規化できないモデルは `cost_usd = NULL` とし、UI 上で「Unknown model」として表示する。

## 可視化：Streamlit

### ページ構成

| ページ | パス | 用途 |
|---|---|---|
| Overview | `/` | 全体サマリー、日次/週次/月次の推移 |
| Project Breakdown | `/project-breakdown` | プロジェクト別コスト・トークン |
| Model Breakdown | `/model-breakdown` | モデル別利用分布 |
| Session Explorer | `/session-explorer` | セッション単位の詳細、プロンプト・応答閲覧 |
| Pricing Settings | `/pricing-settings` | 単価ソース確認・強制更新・内蔵表編集 |
| Config | `/config` | 設定確認・編集 |

### Overview ページ

- 本日の合計コスト（Stat）
- 本日の入力/出力トークン（Stat）
- 期間指定セレクタ（今日 / 7日 / 30日 / カスタム）
- 日次コスト推移（折れ線グラフ）
- プロジェクト別コスト（棒グラフ）
- モデル別トークン分布（円グラフ）
- 平均応答時間推移
- 高コストプロンプト TOP 10（テーブル）

### Session Explorer ページ

- セッション一覧テーブル
- クリックで詳細表示
- プロンプト・応答の全文表示
- 全文検索

### プライバシー制御

- 設定で「プロンプト本文を表示する / しない」を切り替え可能
- コスト・トークンのみ表示するモードを提供

## CLI 設計

ツール名：`claude-meter`（エイリアス `cm`）

| コマンド | 用途 |
|---|---|
| `claude-meter init` | 初回設定、SQLite DB 作成 |
| `claude-meter collect` | 手動で一度 JSONL をパース |
| `claude-meter watch` | `~/.claude` を監視し、リアルタイム収集 |
| `claude-meter ui` | Streamlit UI を起動（`http://127.0.0.1:8501`） |
| `claude-meter pricing update` | 単価情報を強制更新 |
| `claude-meter config` | 設定ファイルのパスを表示 |

## 設定ファイル

`~/.claude-meter/config.yaml`

```yaml
claude:
  projects_dir: "~/.claude/projects"
  transcripts_dir: "~/.claude/transcripts"

storage:
  db_path: "~/.claude-meter/data.db"

pricing:
  primary_source: "aws_bedrock_json"
  fallback_source: "models_dev"
  cache_ttl_hours: 24

privacy:
  store_prompts: true
  max_prompt_length: 10000
  show_prompts_in_ui: true

ui:
  port: 8501
  host: "127.0.0.1"
```

## マルチ OS 対応

- Python 3.10+ で動作
- 依存パッケージは `pip` / `uv` で解決
- パスは `pathlib` を使用し、OS 依存を排除
- Windows/macOS/Ubuntu すべてで `claude-meter ui` コマンドで Streamlit を起動

### OS 別 Claude データディレクトリの解決

| OS | デフォルトパス | 備考 |
|---|---|---|
| macOS | `~/.claude` | 現状の想定パス |
| Linux | `~/.claude` | 現状の想定パス |
| Windows | `%LOCALAPPDATA%\Claude` | Claude Desktop / Claude Code の実装に準拠。`%APPDATA%\Claude` ではない |

- 設定ファイル `config.yaml` の `claude.projects_dir` / `claude.transcripts_dir` で上書き可能
- 実装時は `pathlib` に加え、OS 別のデフォルト解決ロジックを用意し、存在するディレクトリを自動選択する
## ローカル完結の担保

- すべてのデータは `~/.claude-meter/` 以下に保存
- 外部通信は単価取得のみ
- プロンプト本文を含む全データは SQLite 内に留まる
- 設定で `store_prompts: false` にすれば、トークン数・コストのみ記録

## 技術スタック

- Python 3.10+
- SQLite（標準ライブラリ）
- Streamlit（Web UI）
- watchdog（ファイル監視）
- requests（単価取得）
- pydantic（設定・データ検証）
- Altair / Plotly（グラフ）

## 将来の拡張可能性

- InfluxDB + Grafana へのデータ連携（収集部分を差し替え可能）
- Slack / Teams 通知（ローカル Webhook 経由）
- チーム全体の集計（社内共有 SQLite または S3 経由）
- Bedrock 以外のプロバイダ対応

## 非対応範囲（今回のスコープ外）

- AWS 側の CloudTrail / Bedrock ログの解析
- IAM 権限を必要とする Cost Explorer 連携
- クラウド上でのデータ共有・集計
