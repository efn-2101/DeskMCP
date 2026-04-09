# DeskToDo MCP Server

AI秘書用タスク管理MCP（Model Context Protocol）サーバー

## 概要

DeskToDoは、完全ローカルで動作する汎用的なAI秘書向けタスク管理MCPサーバーです。Roo Code、Cline、Cursor、その他あらゆるMCP互換クライアントから独立して呼び出し可能です。

### 主な機能

- **タスク管理**: タスクの追加、更新、削除、検索
- **ステータス管理**: pending（未完了）、completed（完了）、archived（アーカイブ）、canceled（キャンセル）
- **優先度設定**: high（高）、medium（中）、low（低）
- **カテゴリ管理**: タスクのカテゴリ分け
- **期日管理**: 期日の設定・変更、期限切れタスクの取得
- **全文検索**: FTS5による高速な全文検索
- **変更履歴**: タスクの変更履歴の記録・参照
- **一括操作**: 複数タスクの一括登録・更新・削除
- **埋め込み検索**: セマンティック検索（Ollama/LMStudio/OpenAI互換API対応）

## インストール

### 前提条件

- Python 3.10以上
- SQLite3（Python標準ライブラリに含まれる）

### 依存パッケージのインストール

```bash
pip install -r requirements.txt
```

### 手動インストール

```bash
pip install mcp>=1.0.0 pyyaml>=6.0 ExtractMsg>=0.45.0 requests>=2.28.0
```

## 使用方法

### MCPサーバーとして起動

```bash
python desktodo_mcp.py
```

### 設定ファイル

`desktodo_config.yaml.example` を `desktodo_config.yaml` にコピーして編集してください。

```bash
cp desktodo_config.yaml.example desktodo_config.yaml
```

設定ファイルの検索順序:
1. 環境変数 `DESKTODO_CONFIG_FILE` で指定されたパス
2. `~/.desktodo/config.yaml`（ユーザーホームディレクトリ）
3. `./desktodo_config.yaml`（カレントディレクトリ）

## MCPクライアントでの設定

> **注意:** 設定例の `<DESKTODO_INSTALL_PATH>` は、DeskToDoをインストールしたディレクトリのパスに置き換えてください。
>
> 例:
> - Windows: `C:\\Users\\<ユーザー名>\\DeskToDo` または `C:\\tools\\DeskToDo`
> - macOS/Linux: `/home/<ユーザー名>/DeskToDo` または `~/DeskToDo`

### 設定パターン1: cwd使用（推奨）

`cwd`（カレントワーキングディレクトリ）を設定することで、相対パスが使用可能になります。

```json
{
  "mcpServers": {
    "DeskToDo": {
      "command": "python",
      "args": ["desktodo_mcp.py"],
      "cwd": "<DESKTODO_INSTALL_PATH>",
      "env": {
        "DESKTODO_DATA_DIR": "./mydata"
      }
    }
  }
}
```

**メリット:**
- プロジェクトディレクトリを移動してもパス変更が不要
- 設定ファイルの共有が容易
- `` `args` ``がシンプルになる

### 設定パターン2: 絶対パスのみ（cwdなし）

```json
{
  "mcpServers": {
    "DeskToDo": {
      "command": "python",
      "args": ["<DESKTODO_INSTALL_PATH>/desktodo_mcp.py"],
      "env": {
        "DESKTODO_DATA_DIR": "<DESKTODO_INSTALL_PATH>/mydata"
      }
    }
  }
}
```

### 設定パターン3: uv使用（高速起動）

```json
{
  "mcpServers": {
    "DeskToDo": {
      "command": "uv",
      "args": ["run", "desktodo_mcp.py"],
      "cwd": "<DESKTODO_INSTALL_PATH>",
      "env": {
        "DESKTODO_DATA_DIR": "./mydata"
      }
    }
  }
}
```

### Roo Code / Cline / Cursor 共通設定

同様にMCP設定に追加してください。

## 提供ツール一覧

### 基本操作

| ツール名 | 説明 |
|---------|------|
| `add_task` | 新しいタスクを追加 |
| `list_pending_tasks` | 未完了タスクの一覧を取得 |
| `list_all_tasks` | 全タスクの一覧を取得 |
| `complete_task` | タスクを完了状態に変更 |
| `archive_task` | タスクをアーカイブ |
| `restore_task` | アーカイブ済みタスクを復元 |
| `delete_task` | タスクを削除 |

### 更新操作

| ツール名 | 説明 |
|---------|------|
| `update_task_date` | タスクの期日を変更 |
| `update_task_title` | タスクのタイトルを変更 |
| `update_task_description` | タスクの説明を変更 |
| `update_task_priority` | タスクの優先度を変更 |
| `update_task_category` | タスクのカテゴリを変更 |
| `update_task_status` | タスクのステータスを変更 |

### 検索・参照

| ツール名 | 説明 |
|---------|------|
| `search_tasks` | キーワードでタスクを検索 |
| `search_tasks_advanced` | 詳細条件でタスクを検索 |
| `fuzzy_search_tasks` | あいまい検索（FTS5） |
| `semantic_search_tasks` | セマンティック検索（埋め込み使用） |
| `search_tasks_by_content_fragments` | 断片的なキーワードからタスクを検索 |
| `get_all_unique_words` | タスクから抽出した全ユニーク単語を取得 |
| `get_task_history` | タスクの変更履歴を取得 |

### 一括操作

| ツール名 | 説明 |
|---------|------|
| `add_tasks_bulk` | 複数タスクを一括登録 |
| `delete_tasks_bulk` | 複数タスクを一括削除 |
| `update_tasks_status_bulk` | 複数タスクのステータスを一括変更 |
| `update_tasks_due_date_bulk` | 複数タスクの期日を一括変更 |

### 統計・レポート

| ツール名 | 説明 |
|---------|------|
| `get_task_statistics` | タスクの統計情報を取得 |
| `get_overdue_tasks` | 期限切れタスクの一覧を取得 |
| `get_recent_tasks` | 最近追加されたタスクを取得 |
| `get_completed_tasks` | 最近完了したタスクを取得 |
| `get_recently_modified_tasks` | 最近更新されたタスクを取得 |
| `get_tasks_by_date_range` | 日付範囲でタスクを取得 |

### ファイル操作

| ツール名 | 説明 |
|---------|------|
| `read_document_file` | ドキュメントファイル（.eml, .msg, .txt, .md等）を読み込み |

### システム管理

| ツール名 | 説明 |
|---------|------|
| `backup_database` | データベースのバックアップを作成 |
| `get_server_info` | サーバー情報を取得 |

### 埋め込み検索

| ツール名 | 説明 |
|---------|------|
| `rebuild_embeddings` | 全タスクの埋め込みを再構築 |

## AI向け使用ガイド

このセクションは、AIアシスタントがこのMCPサーバーを効果的に使用するためのガイドです。

### タスク操作の基本フロー

1. **タスクの登録**: ユーザーが「タスクを追加」「やることを登録」等の指示をした場合
   - ツール: `add_task`
   - 必須パラメータ: `title`
   - オプション: `description`, `due_date`（未指定時は3営業日後に自動設定）

2. **タスクの確認**: ユーザーが「タスク一覧」「やることリスト」等の指示をした場合
   - 未完了タスクのみ: `list_pending_tasks`
   - 全タスク（完了・キャンセル済みを含む）: `list_all_tasks`

3. **タスクの完了**: ユーザーが「タスクを完了」「やった」等の指示をした場合
   - ツール: `complete_task`
   - 必須パラメータ: `task_id`

4. **タスクの検索**: ユーザーが「〜を探して」「〜というタスク」等の指示をした場合
   - 単純なキーワード検索: `search_tasks`
   - あいまい検索（関連度順）: `fuzzy_search_tasks`
   - 意味検索（意味的類似性）: `semantic_search_tasks`
   - 高度な検索（複数条件）: `search_tasks_advanced`

### 類似ツールの使い分け

| 目的 | 使用するツール | 説明 |
|------|---------------|------|
| 未完了タスクの一覧 | `list_pending_tasks` | 日常的なタスク確認用 |
| 全タスクの一覧 | `list_all_tasks` | 履歴確認用（完了・キャンセル済みを含む） |
| キーワード検索 | `search_tasks` | 単純な部分一致検索 |
| あいまい検索 | `fuzzy_search_tasks` | FTS5全文検索、関連度順 |
| 意味検索 | `semantic_search_tasks` | エンベディング使用、意味的類似性 |
| 高度な検索 | `search_tasks_advanced` | 複数条件、フィルタリング |
| 物理削除 | `delete_task` | 完全に削除、復元不可 |
| アーカイブ | `archive_task` | 論理削除、復元可能 |

### パラメータの省略について

多くのパラメータはオプションであり、省略するとデフォルト値が使用されます：
- `due_date`: 省略時は3営業日後に自動設定
- `priority`: 省略時は'medium'
- `status`: 省略時は'pending'

ユーザーが明示的に指定しない限り、オプションパラメータは省略してください。

### エラー発生時の対応

エラーメッセージには修正方法が含まれています。エラーが発生した場合は、メッセージに従ってパラメータを修正し、再試行してください。

## 環境変数一覧

| 環境変数 | 説明 | デフォルト値 |
|---------|------|-------------|
| `DESKTODO_CONFIG_FILE` | 設定ファイルのパス | 自動検索 |
| `DESKTODO_DATA_DIR` | データベースディレクトリ | スクリプトと同一ディレクトリ |
| `DESKTODO_BACKUP_DIR` | バックアップディレクトリ | データベースと同一ディレクトリ |
| `DESKTODO_LOG_FILE` | ログファイルのパス | 標準エラー出力のみ |
| `DESKTODO_LOG_LEVEL` | ログレベル | `INFO` |
| `DESKTODO_LANG` | 言語設定 | `ja` |
| `TZ` | タイムゾーン | `Asia/Tokyo` |
| `DESKTODO_EMBEDDING_ENABLED` | 埋め込み検索の有効化 | `true` |
| `DESKTODO_EMBEDDING_PROVIDER` | 埋め込みプロバイダ | `ollama` |
| `DESKTODO_EMBEDDING_API_URL` | 埋め込みAPI URL | `http://localhost:11434` |
| `DESKTODO_EMBEDDING_MODEL` | 埋め込みモデル名 | `qwen3-embedding:0.6b` |
| `DESKTODO_ALLOWED_DIRS` | ファイル読み込み許可ディレクトリ | - |
| `DESKTODO_MAX_FILE_SIZE_MB` | ファイル読み込み最大サイズ（MB） | - |

## データベース構造

### テーブル

- **tasks**: タスク情報
- **task_history**: タスク変更履歴
- **task_embeddings**: タスク埋め込みベクトル
- **tasks_fts**: 全文検索用仮想テーブル（FTS5）

### インデックス

- `idx_tasks_status`: ステータス別検索用
- `idx_tasks_due_date`: 期日別検索用
- `idx_tasks_category`: カテゴリ別検索用
- `idx_tasks_created_at`: 作成日時別検索用
- `idx_task_history_task_id`: 履歴検索用
- `idx_task_history_changed_at`: 履歴日時検索用
- `idx_task_embeddings_task_id`: 埋め込み検索用

## ライセンス

MIT License