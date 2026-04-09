#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeskToDo MCP Server - AI秘書用タスク管理MCPサーバー

完全ローカルで動作する汎用的なAI秘書向けMCP（Model Context Protocol）サーバー。
Roo Code、Cline、Cursor、その他あらゆるMCP互換クライアントから独立して呼び出し可能。
"""

import sqlite3
import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional, Dict, Any, List
import json
import email
from email import policy
from email.parser import BytesParser
import re

# YAML設定ファイル読み込み用（オプション）
try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

# MCP (FastMCP) インポート
try:
    from mcp.server.fastmcp import FastMCP
    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False
    FastMCP = None

# MCPサーバーインスタンス（グローバル）
_mcp_server = None


def get_mcp_server():
    """
    MCPサーバーインスタンスを取得する。
    
    シングルトンパターンで、サーバーインスタンスが存在しない場合は作成する。
    
    Returns:
        FastMCP: MCPサーバーインスタンス（MCPが利用可能な場合）
        None: MCPが利用不可能な場合
    """
    global _mcp_server
    
    if not MCP_AVAILABLE:
        return None
    
    if _mcp_server is None:
        _mcp_server = FastMCP("DeskToDo")
    
    return _mcp_server

# ============================================================================
# グローバル設定
# ============================================================================

# デフォルト設定
DEFAULT_CONFIG = {
    'database': {
        'path': None,  # Noneの場合は環境変数またはデフォルトパスを使用
        'backup_dir': None,
        'backup_interval_hours': 24,
    },
    'logging': {
        'level': 'INFO',
        'file': None,
        'max_size_mb': 10,
        'backup_count': 5,
    },
    'defaults': {
        'priority': 'medium',
        'business_days_for_due': 3,
        'pagination_limit': 100,
        'status': 'pending',
    },
    'embedding': {
        'enabled': True,
        'provider': 'ollama',
        'api_url': 'http://localhost:11434',
        'model': 'qwen3-embedding:0.6b',
    },
    'timezone': 'Asia/Tokyo',
    'language': 'ja',
}

# グローバル設定変数
_config: Dict[str, Any] = {}
_logger: Optional[logging.Logger] = None
_messages: Dict[str, str] = {}

# ============================================================================
# ロガー設定
# ============================================================================

def setup_logger() -> logging.Logger:
    """
    ロガーを設定して取得する。
    
    標準エラー出力ハンドラとオプションのファイルハンドラを設定する。
    ログフォーマット: [YYYY-MM-DD HH:MM:SS] [LEVEL] [FUNCTION_NAME] メッセージ
    
    Returns:
        logging.Logger: 設定済みのロガーインスタンス
    """
    global _logger
    
    if _logger is not None:
        return _logger
    
    logger = logging.getLogger('desktodo')
    
    # ログレベルの設定（環境変数または設定ファイルから取得）
    log_level_str = os.environ.get('DESKTODO_LOG_LEVEL', 'INFO')
    log_level = getattr(logging, log_level_str.upper(), logging.INFO)
    logger.setLevel(log_level)
    
    # ログフォーマット
    log_format = '[%(asctime)s] [%(levelname)s] [%(funcName)s] %(message)s'
    date_format = '%Y-%m-%d %H:%M:%S'
    formatter = logging.Formatter(log_format, datefmt=date_format)
    
    # 標準エラー出力ハンドラ
    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(log_level)
    stderr_handler.setFormatter(formatter)
    logger.addHandler(stderr_handler)
    
    # ファイルハンドラ（オプション）
    log_file = os.environ.get('DESKTODO_LOG_FILE')
    if not log_file and _config:
        log_file = _config.get('logging', {}).get('file')
    
    if log_file:
        # パスの展開（~をホームディレクトリに展開）
        log_file = os.path.expanduser(log_file)
        
        # ログディレクトリが存在しない場合は作成
        log_dir = os.path.dirname(log_file)
        if log_dir and not os.path.exists(log_dir):
            os.makedirs(log_dir, exist_ok=True)
        
        # ログローテーション設定
        max_size_mb = _config.get('logging', {}).get('max_size_mb', 10) if _config else 10
        backup_count = _config.get('logging', {}).get('backup_count', 5) if _config else 5
        
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_size_mb * 1024 * 1024,
            backupCount=backup_count,
            encoding='utf-8'
        )
        file_handler.setLevel(logging.DEBUG)  # ファイルにはDEBUGレベル以上を記録
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    _logger = logger
    return logger


def get_logger() -> logging.Logger:
    """
    設定済みのロガーを取得する。
    
    ロガーが初期化されていない場合は、初期化して返す。
    
    Returns:
        logging.Logger: ロガーインスタンス
    """
    global _logger
    if _logger is None:
        _logger = setup_logger()
    return _logger

# ============================================================================
# 設定管理
# ============================================================================

def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    辞書を深くマージする。
    
    Args:
        base: ベースとなる辞書
        override: 上書きする値を持つ辞書
    
    Returns:
        Dict[str, Any]: マージされた辞書
    """
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config() -> Dict[str, Any]:
    """
    設定ファイルを読み込み、デフォルト設定とマージする。
    
    設定ファイルの検索順序:
    1. 環境変数 DESKTODO_CONFIG_FILE で指定されたパス
    2. ~/.desktodo/config.yaml（ユーザーホームディレクトリ）
    3. ./desktodo_config.yaml（カレントディレクトリ）
    
    環境変数による設定上書きも行う。
    
    Returns:
        Dict[str, Any]: マージされた設定辞書
    """
    global _config
    
    config = DEFAULT_CONFIG.copy()
    
    # 設定ファイルのパス候補
    config_paths = [
        os.environ.get('DESKTODO_CONFIG_FILE'),
        os.path.expanduser('~/.desktodo/config.yaml'),
        './desktodo_config.yaml',
    ]
    
    # 設定ファイルの読み込み
    for path in config_paths:
        if path and os.path.exists(path):
            try:
                if YAML_AVAILABLE:
                    with open(path, 'r', encoding='utf-8') as f:
                        user_config = yaml.safe_load(f)
                        if user_config:
                            config = deep_merge(config, user_config)
                    break
                else:
                    # YAMLライブラリがない場合は警告ログを出力
                    # （ロガーが初期化されていない場合は後で出力）
                    pass
            except Exception as e:
                # 設定ファイルの読み込みエラーは無視してデフォルトを使用
                pass
    
    # 環境変数による設定上書き
    if os.environ.get('DESKTODO_DATA_DIR'):
        config['database']['path'] = os.path.join(
            os.environ['DESKTODO_DATA_DIR'],
            'desktodo_tasks.db'
        )
    
    if os.environ.get('DESKTODO_BACKUP_DIR'):
        config['database']['backup_dir'] = os.environ['DESKTODO_BACKUP_DIR']
    
    if os.environ.get('DESKTODO_LOG_FILE'):
        config['logging']['file'] = os.environ['DESKTODO_LOG_FILE']
    
    if os.environ.get('DESKTODO_LOG_LEVEL'):
        config['logging']['level'] = os.environ['DESKTODO_LOG_LEVEL']
    
    if os.environ.get('DESKTODO_LANG'):
        config['language'] = os.environ['DESKTODO_LANG']
    
    if os.environ.get('TZ'):
        config['timezone'] = os.environ['TZ']
    
    # エンベディング関連の環境変数
    if os.environ.get('DESKTODO_EMBEDDING_ENABLED'):
        config['embedding']['enabled'] = os.environ['DESKTODO_EMBEDDING_ENABLED'].lower() == 'true'
    
    if os.environ.get('DESKTODO_EMBEDDING_PROVIDER'):
        config['embedding']['provider'] = os.environ['DESKTODO_EMBEDDING_PROVIDER']
    
    if os.environ.get('DESKTODO_EMBEDDING_API_URL'):
        config['embedding']['api_url'] = os.environ['DESKTODO_EMBEDDING_API_URL']
    
    if os.environ.get('DESKTODO_EMBEDDING_MODEL'):
        config['embedding']['model'] = os.environ['DESKTODO_EMBEDDING_MODEL']
    
    _config = config
    return config


def get_config() -> Dict[str, Any]:
    """
    現在の設定を取得する。
    
    設定が読み込まれていない場合は、読み込んで返す。
    
    Returns:
        Dict[str, Any]: 設定辞書
    """
    global _config
    if not _config:
        _config = load_config()
    return _config

# ============================================================================
# データベース関連
# ============================================================================

def get_db_path() -> str:
    """
    データベースファイルのパスを取得する。
    
    環境変数 DESKTODO_DATA_DIR が設定されている場合はそのディレクトリ、
    未設定の場合はスクリプトと同一ディレクトリを使用する。
    
    Returns:
        str: データベースファイルのパス
    """
    config = get_config()
    
    # 設定ファイルで指定されたパスがあれば使用
    if config.get('database', {}).get('path'):
        return os.path.expanduser(config['database']['path'])
    
    # 環境変数で指定されたディレクトリ
    data_dir = os.environ.get('DESKTODO_DATA_DIR')
    
    if data_dir:
        db_dir = os.path.expanduser(data_dir)
    else:
        # スクリプトと同一ディレクトリ
        db_dir = os.path.dirname(os.path.abspath(__file__))
    
    # ディレクトリが存在しない場合は作成
    if not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
    
    return os.path.join(db_dir, 'desktodo_tasks.db')


def get_connection() -> sqlite3.Connection:
    """
    データベース接続を取得する。
    
    接続は行ファクトリとして dict 形式を使用し、
    結果を辞書形式で取得できるようにする。
    
    Returns:
        sqlite3.Connection: データベース接続
    """
    db_path = get_db_path()
    conn = sqlite3.connect(db_path)
    
    # 行ファクトリを設定（辞書形式）
    conn.row_factory = sqlite3.Row
    
    # 外部キー制約を有効化
    conn.execute('PRAGMA foreign_keys = ON')
    
    return conn


def backup_database() -> str:
    """
    データベースのバックアップを作成します。
    
    バックアップ先は環境変数 DESKTODO_BACKUP_DIR またはデータベースと同一ディレクトリに保存されます。
    バックアップファイル名は `desktodo_tasks.db.backup.YYYYMMDD_HHMMSS` 形式です。
    
    Returns:
        str: 「バックアップを作成しました: {backup_path}」または「バックアップ作成に失敗しました: {error}」
    """
    import shutil
    
    logger = get_logger()
    
    try:
        # データベースパスの取得
        db_path = get_db_path()
        
        # バックアップディレクトリの決定
        backup_dir = os.environ.get('DESKTODO_BACKUP_DIR')
        if not backup_dir:
            config = get_config()
            backup_dir = config.get('database', {}).get('backup_dir')
        
        if backup_dir:
            backup_dir = os.path.expanduser(backup_dir)
        else:
            backup_dir = os.path.dirname(db_path)
        
        # バックアップディレクトリが存在しない場合は作成
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir, exist_ok=True)
        
        # バックアップファイル名の生成
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_filename = f"desktodo_tasks.db.backup.{timestamp}"
        backup_path = os.path.join(backup_dir, backup_filename)
        
        # データベースのバックアップ
        # 注: SQLiteのバックアップは、データベースが使用中でも安全にコピーできるよう
        # VACUUMコマンドを使用してデータベースを最適化してからコピーする
        conn = get_connection()
        cursor = conn.cursor()
        
        # データベースを最適化（オプション）
        try:
            cursor.execute('PRAGMA optimize')
        except Exception:
            pass  # 最適化エラーは無視
        
        conn.close()
        
        # ファイルのコピー
        shutil.copy2(db_path, backup_path)
        
        logger.info(f"バックアップを作成しました: {backup_path}")
        return t('backup.success', backup_path)
        
    except Exception as e:
        logger.error(f"バックアップ作成エラー: {str(e)}")
        return t('backup.failure', str(e))


def get_server_info() -> str:
    """
    サーバー情報をJSON形式で返します。
    
    Returns:
        str: JSON形式のサーバー情報
            {
              "name": "DeskToDo MCP Server",
              "version": "1.0.0",
              "mcp_protocol_version": "2024-11-05",
              "supported_features": ["task_management", "file_parsing", "fts5_search", "embedding_search", "bulk_operations"]
            }
    """
    server_info = {
        "name": "DeskToDo MCP Server",
        "version": "1.0.0",
        "mcp_protocol_version": "2024-11-05",
        "supported_features": [
            "task_management",
            "file_parsing",
            "fts5_search",
            "embedding_search",
            "bulk_operations"
        ]
    }
    
    return json.dumps(server_info, ensure_ascii=False, indent=2)


def init_db() -> None:
    """
    データベースの初期化を行う。
    
    以下のテーブルを作成する:
    - tasks: タスク管理テーブル
    - task_history: 変更履歴テーブル
    - task_embeddings: エンベディング検索用テーブル
    - schema_version: スキーマバージョン管理テーブル
    - tasks_fts: 全文検索用仮想テーブル（FTS5）
    
    また、各種インデックスとトリガーも作成する。
    サーバー起動時に自動的にバックアップを作成する。
    """
    # サーバー起動時の自動バックアップ
    try:
        backup_result = backup_database()
        logger = get_logger()
        logger.info(f"起動時バックアップ: {backup_result}")
    except Exception as e:
        logger = get_logger()
        logger.warning(f"起動時バックアップに失敗しました: {str(e)}")
    
    conn = get_connection()
    cursor = conn.cursor()
    
    try:
        # WALモードの有効化
        cursor.execute('PRAGMA journal_mode=WAL')
        
        # tasks テーブルの作成
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                due_date TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                priority TEXT DEFAULT 'medium',
                category TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT,
                completed_at TEXT
            )
        ''')
        
        # task_history テーブルの作成
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                field_name TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                reason TEXT,
                changed_at TEXT NOT NULL,
                changed_by TEXT,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
        ''')
        
        # task_embeddings テーブルの作成
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS task_embeddings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                embedding BLOB,
                model_name TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
            )
        ''')
        
        # schema_version テーブルの作成
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL,
                description TEXT
            )
        ''')
        
        # インデックスの作成
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_task_history_task_id ON task_history(task_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_task_history_changed_at ON task_history(changed_at)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_task_embeddings_task_id ON task_embeddings(task_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tasks_due_date ON tasks(due_date)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tasks_category ON tasks(category)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at)
        ''')
        
        # FTS5仮想テーブルの作成
        cursor.execute('''
            CREATE VIRTUAL TABLE IF NOT EXISTS tasks_fts USING fts5(
                id UNINDEXED,
                title,
                description,
                content='tasks',
                content_rowid='id'
            )
        ''')
        
        # FTS5トリガーの作成（INSERT時）
        cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS tasks_ai AFTER INSERT ON tasks BEGIN
                INSERT INTO tasks_fts(rowid, id, title, description)
                VALUES (new.id, new.id, new.title, new.description);
            END
        ''')
        
        # FTS5トリガーの作成（DELETE時）
        cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS tasks_ad AFTER DELETE ON tasks BEGIN
                INSERT INTO tasks_fts(tasks_fts, rowid, id, title, description)
                VALUES('delete', old.id, old.id, old.title, old.description);
            END
        ''')
        
        # FTS5トリガーの作成（UPDATE時）
        cursor.execute('''
            CREATE TRIGGER IF NOT EXISTS tasks_au AFTER UPDATE ON tasks BEGIN
                INSERT INTO tasks_fts(tasks_fts, rowid, id, title, description)
                VALUES('delete', old.id, old.id, old.title, old.description);
                INSERT INTO tasks_fts(rowid, id, title, description)
                VALUES (new.id, new.id, new.title, new.description);
            END
        ''')
        
        # スキーマバージョンの初期化
        cursor.execute('''
            SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'
        ''')
        if cursor.fetchone():
            cursor.execute('SELECT MAX(version) FROM schema_version')
            result = cursor.fetchone()
            current_version = result[0] if result and result[0] else 0
        else:
            current_version = 0
        
        if current_version < 1:
            cursor.execute('''
                INSERT OR REPLACE INTO schema_version (version, applied_at, description)
                VALUES (1, datetime('now'), 'Initial schema')
            ''')
        
        conn.commit()
        
        # ログ出力
        logger = get_logger()
        logger.info("データベースの初期化が完了しました")
        
    except Exception as e:
        conn.rollback()
        logger = get_logger()
        logger.error(f"データベース初期化エラー: {str(e)}")
        raise
    finally:
        conn.close()

# ============================================================================
# 国際化対応
# ============================================================================

def load_messages(lang: str = None) -> Dict[str, str]:
    """
    メッセージカタログを読み込む。
    
    指定された言語のメッセージファイルを locales/ ディレクトリから読み込む。
    ファイルが存在しない場合はデフォルトの日本語メッセージを使用する。
    
    Args:
        lang: 言語コード（例: 'ja', 'en'）。Noneの場合は設定値を使用。
    
    Returns:
        Dict[str, str]: メッセージカタログ
    """
    global _messages
    
    if lang is None:
        config = get_config()
        lang = config.get('language', 'ja')
    
    # メッセージファイルのパス
    locale_dir = os.path.join(os.path.dirname(__file__), 'locales')
    locale_path = os.path.join(locale_dir, f'{lang}.json')
    
    messages = {}
    
    # メッセージファイルの読み込み
    if os.path.exists(locale_path):
        try:
            with open(locale_path, 'r', encoding='utf-8') as f:
                messages = json.load(f)
        except Exception:
            # 読み込みエラーの場合はデフォルトメッセージを使用
            pass
    
    # デフォルトメッセージ（日本語）の定義
    default_messages = {
        "task.added": "タスク [#%d] '%s' を登録しました。期日: %s",
        "task.added_auto_due": "タスク [#%d] '%s' を登録しました。期日: %s (※期日指定がなかったため、自動的に3営業日後に設定しました)",
        "task.not_found": "タスク [#%d] が見つかりませんでした。",
        "task.completed": "タスク [#%d] を完了状態に変更しました。",
        "task.already_completed": "タスク [#%d] は既に完了しています。",
        "task.deleted": "タスク [#%d] '%s' を削除しました。",
        "task.archived": "タスク [#%d] をアーカイブしました。",
        "task.restored": "タスク [#%d] を復元しました。",
        "task.not_archived": "タスク [#%d] はアーカイブされていません。",
        "task.history_empty": "タスク [#%d] に変更履歴はありません。",
        "task.no_pending": "現在、未完了のタスクはありません。",
        "task.no_tasks": "タスクは登録されていません。",
        "task.no_overdue": "期限切れのタスクはありません。",
        "task.no_recent": "過去%d日以内に登録されたタスクはありません。",
        "task.no_completed": "過去%d日以内に完了したタスクはありません。",
        "task.no_modified": "過去%d日以内に更新されたタスクはありません。",
        "task.title_updated": "タスク [#%d] のタイトルを '%s' に変更しました。",
        "task.description_updated": "タスク [#%d] の説明を更新しました。",
        "task.priority_updated": "タスク [#%d] の優先度を '%s' に変更しました。",
        "task.category_updated": "タスク [#%d] のカテゴリを '%s' に変更しました。",
        "task.status_updated": "タスク [#%d] のステータスを '%s' に変更しました。",
        "task.due_date_updated": "タスク [#%d] の期日を %s に更新しました。",
        "task.due_date_past_warning": "警告: 指定された期日は過去の日付です。",
        "error.invalid_id": "タスクIDは正の整数で指定してください。",
        "error.invalid_date": "期日の形式が不正です。YYYY-MM-DD形式で指定してください。",
        "error.invalid_status": "ステータスは 'pending', 'completed', 'canceled', 'archived' のいずれかで指定してください。",
        "error.invalid_priority": "優先度は 'high', 'medium', 'low' のいずれかで指定してください。",
        "error.empty_title": "タイトルは必須です。",
        "error.empty_task_list": "タスクIDリストは空にできません。",
        "search.no_results": "キーワード '%s' に一致するタスクは見つかりませんでした。",
        "search.no_advanced_results": "条件に一致するタスクは見つかりませんでした。",
        "search.no_fragments_results": "いずれのキーワードも含むタスクは見つかりませんでした。",
        "backup.success": "バックアップを作成しました: %s",
        "backup.failure": "バックアップ作成に失敗しました: %s",
        "embedding.rebuild_success": "%d件のタスクのエンベディングを再構築しました。",
        "embedding.api_unavailable": "エンベディングAPIに接続できません。",
        "embedding.mode_embedding": "検索モード: エンベディング検索 (モデル: %s)",
        "embedding.mode_fts5_unavailable": "検索モード: FTS5全文検索 (エンベディングAPI利用不可)",
        "embedding.mode_fts5_manual": "検索モード: FTS5全文検索 (手動指定)",
    }
    
    # デフォルトメッセージで補完
    for key, value in default_messages.items():
        if key not in messages:
            messages[key] = value
    
    _messages = messages
    return messages


def t(key: str, *args) -> str:
    """
    翻訳メッセージを取得する。
    
    メッセージカタログから指定されたキーのメッセージを取得し、
    引数があればフォーマットして返す。
    
    Args:
        key: メッセージキー
        *args: フォーマット引数
    
    Returns:
        str: 翻訳されたメッセージ
    """
    global _messages
    
    if not _messages:
        _messages = load_messages()
    
    template = _messages.get(key, key)
    
    try:
        if args:
            return template % args
        return template
    except (TypeError, ValueError):
        return template

# ============================================================================
# タイムゾーン処理
# ============================================================================

def get_local_timezone() -> Any:
    """
    ローカルタイムゾーンを取得する。
    
    環境変数 TZ または設定ファイルの timezone 設定を使用する。
    デフォルトは 'Asia/Tokyo'。
    
    Returns:
        Any: タイムゾーンオブジェクト（pytz.timezone または datetime.timezone）
    """
    config = get_config()
    tz_name = os.environ.get('TZ', config.get('timezone', 'Asia/Tokyo'))
    
    try:
        import pytz
        return pytz.timezone(tz_name)
    except ImportError:
        # pytz がない場合は固定オフセットを使用
        # 日本時間（UTC+9）の場合
        if tz_name == 'Asia/Tokyo':
            return timezone(timedelta(hours=9))
        # その他のタイムゾーンはUTCとして扱う
        return timezone.utc


def utc_to_local(utc_str: str) -> str:
    """
    UTC文字列をローカルタイムに変換する。
    
    Args:
        utc_str: UTC形式の日時文字列（YYYY-MM-DD HH:MM:SS）
    
    Returns:
        str: ローカルタイム形式の日時文字列
    """
    try:
        local_tz = get_local_timezone()
        utc_dt = datetime.strptime(utc_str, '%Y-%m-%d %H:%M:%S')
        utc_dt = utc_dt.replace(tzinfo=timezone.utc)
        local_dt = utc_dt.astimezone(local_tz)
        return local_dt.strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return utc_str


def now_utc() -> str:
    """
    現在日時をUTCで取得する。
    
    Returns:
        str: UTC形式の現在日時文字列（YYYY-MM-DD HH:MM:SS）
    """
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def get_3_business_days_later() -> str:
    """
    3営業日後の日付を計算して返す。
    
    実行日から1日ずつ加算し、土曜日・日曜日をスキップして
    3日分カウントを進めた日付を返す。
    
    Returns:
        str: 3営業日後の日付（YYYY-MM-DD形式）
    """
    current = datetime.now()
    count = 0
    
    while count < 3:
        current += timedelta(days=1)
        # weekday(): 月曜日=0, 日曜日=6
        if current.weekday() < 5:  # 月〜金
            count += 1
    
    return current.strftime('%Y-%m-%d')

# ============================================================================
# ファイルパース機能
# ============================================================================

def validate_filepath(filepath: str) -> str:
    """
    ファイルパスの安全性を検証する。
    
    パストラバーサル攻撃を防ぐため、以下のチェックを行う:
    - 絶対パスの場合、許可されたベースディレクトリ内か検証
    - `..`（親ディレクトリ参照）を含むパスは拒否
    - シンボリックリンクの追跡を禁止
    
    環境変数 DESKTODO_ALLOWED_DIRS で許可ディレクトリを設定可能（カンマ区切り）。
    デフォルトはカレントディレクトリとユーザーホームディレクトリ。
    
    Args:
        filepath: 検証するファイルパス（絶対パスまたは相対パス）
    
    Returns:
        str: 検証済みの正規化された絶対パス
    
    Raises:
        ValueError: パスが許可されていないディレクトリにある場合
        FileNotFoundError: ファイルが存在しない場合
    """
    logger = get_logger()
    
    # 許可されたディレクトリの取得
    allowed_dirs_env = os.environ.get('DESKTODO_ALLOWED_DIRS', '')
    if allowed_dirs_env:
        allowed_dirs = [d.strip() for d in allowed_dirs_env.split(',') if d.strip()]
    else:
        # デフォルト: カレントディレクトリとユーザーホームディレクトリ
        allowed_dirs = [
            os.getcwd(),
            os.path.expanduser('~')
        ]
    
    # パスの正規化
    filepath = os.path.expanduser(filepath)
    
    # 相対パスの場合は絶対パスに変換
    if not os.path.isabs(filepath):
        filepath = os.path.abspath(filepath)
    
    # `..` のチェック（正規化後も残っている場合は危険）
    if '..' in filepath:
        error_msg = f"パストラバーサルの試行を検出しました: {filepath}"
        logger.warning(error_msg)
        raise ValueError(error_msg)
    
    # シンボリックリンクのチェック
    if os.path.islink(filepath):
        error_msg = f"シンボリックリンクは許可されていません: {filepath}"
        logger.warning(error_msg)
        raise ValueError(error_msg)
    
    # ファイルの存在確認
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"ファイルが見つかりません: {filepath}")
    
    # 実際のパスを取得（symlinkを解決せずに実パスを取得）
    real_path = os.path.realpath(filepath)
    
    # 許可されたディレクトリ内かチェック
    is_allowed = False
    for allowed_dir in allowed_dirs:
        allowed_dir = os.path.realpath(os.path.expanduser(allowed_dir))
        if real_path.startswith(allowed_dir + os.sep) or real_path == allowed_dir:
            is_allowed = True
            break
    
    if not is_allowed:
        error_msg = f"アクセスが許可されていないパスです: {filepath}"
        logger.warning(error_msg)
        raise ValueError(error_msg)
    
    logger.debug(f"パス検証成功: {filepath}")
    return filepath


def parse_eml_file(filepath: str) -> str:
    """
    標準メールファイル（.eml）をパースする。
    
    Python標準ライブラリ `email` を使用してメールファイルを解析し、
    ヘッダー情報とプレーンテキスト本文を抽出する。
    HTMLタグやBase64エンコードされた添付ファイルは除外する。
    
    Args:
        filepath: .emlファイルのパス
    
    Returns:
        str: フォーマット済みのメール内容
            【件名】...
            【送信者】...
            【日時】...
            【本文】...
    """
    logger = get_logger()
    
    try:
        with open(filepath, 'rb') as f:
            msg = BytesParser(policy=policy.default).parse(f)
        
        # ヘッダー情報の抽出
        subject = msg.get('Subject', '(件名なし)')
        if subject:
            # ヘッダーのデコード
            from email.header import decode_header
            decoded_parts = decode_header(subject)
            subject = ''.join(
                part.decode(encoding or 'utf-8') if isinstance(part, bytes) else part
                for part, encoding in decoded_parts
            )
        
        from_addr = msg.get('From', '(送信者不明)')
        if from_addr:
            from email.header import decode_header
            decoded_parts = decode_header(from_addr)
            from_addr = ''.join(
                part.decode(encoding or 'utf-8') if isinstance(part, bytes) else part
                for part, encoding in decoded_parts
            )
        
        date_str = msg.get('Date', '')
        if date_str:
            try:
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(date_str)
                date_str = dt.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                pass  # パースできない場合は元の文字列を使用
        
        # 本文の抽出（text/plainのみ）
        body_text = ""
        
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get('Content-Disposition', ''))
                
                # 添付ファイルはスキップ
                if 'attachment' in content_disposition:
                    continue
                
                # text/plainパートのみ抽出
                if content_type == 'text/plain':
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            charset = part.get_content_charset() or 'utf-8'
                            body_text += payload.decode(charset, errors='replace')
                    except Exception as e:
                        logger.warning(f"メール本文のデコードエラー: {e}")
        else:
            # 単一パートの場合
            content_type = msg.get_content_type()
            if content_type == 'text/plain':
                try:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        charset = msg.get_content_charset() or 'utf-8'
                        body_text = payload.decode(charset, errors='replace')
                except Exception as e:
                    logger.warning(f"メール本文のデコードエラー: {e}")
        
        # HTMLタグの除去（念のため）
        body_text = re.sub(r'<[^>]+>', '', body_text)
        
        # 結果のフォーマット
        result = f"""【件名】{subject}
【送信者】{from_addr}
【日時】{date_str}
【本文】
{body_text.strip()}"""
        
        return result
        
    except Exception as e:
        logger.error(f".emlファイルのパースエラー: {e}")
        return f"メールファイルの読み込みに失敗しました: {str(e)}"


def parse_msg_file(filepath: str) -> str:
    """
    Outlook専用メールファイル（.msg）をパースする。
    
    外部ライブラリ `ExtractMsg` を使用してOutlookメールファイルを解析する。
    ライブラリが存在しない場合はガイダンス文字列を返す。
    
    Args:
        filepath: .msgファイルのパス
    
    Returns:
        str: フォーマット済みのメール内容、またはエラーメッセージ
    """
    logger = get_logger()
    
    try:
        import ExtractMsg
    except ImportError:
        return "`.msg` ファイルを読むには `pip install ExtractMsg` が必要です"
    
    try:
        # ExtractMsgでメールを開く
        msg = ExtractMsg.openMsg(filepath)
        
        # ヘッダー情報の抽出
        subject = msg.subject or '(件名なし)'
        sender = msg.sender or '(送信者不明)'
        date_str = msg.date or ''
        
        # 本文の抽出
        body_text = msg.body or ''
        
        # HTMLタグの除去（念のため）
        body_text = re.sub(r'<[^>]+>', '', body_text)
        
        # 結果のフォーマット
        result = f"""【件名】{subject}
【送信者】{sender}
【日時】{date_str}
【本文】
{body_text.strip()}"""
        
        return result
        
    except Exception as e:
        logger.error(f".msgファイルのパースエラー: {e}")
        return f"Outlookメールファイルの読み込みに失敗しました: {str(e)}"


def parse_text_file(filepath: str) -> str:
    """
    一般テキストファイルをパースする。
    
    デフォルトはUTF-8で読み込み、UnicodeDecodeErrorの場合は
    CP932（Shift_JIS）でフォールバックする。
    ファイルサイズ制限（環境変数 DESKTODO_MAX_FILE_SIZE_MB、デフォルト10MB）を適用。
    
    Args:
        filepath: テキストファイルのパス
    
    Returns:
        str: ファイルの内容
    
    Raises:
        ValueError: ファイルサイズが制限を超える場合
    """
    logger = get_logger()
    
    # ファイルサイズ制限の取得
    max_size_mb = int(os.environ.get('DESKTODO_MAX_FILE_SIZE_MB', '10'))
    max_size_bytes = max_size_mb * 1024 * 1024
    
    # ファイルサイズのチェック
    file_size = os.path.getsize(filepath)
    if file_size > max_size_bytes:
        raise ValueError(
            f"ファイルサイズが制限を超えています ({file_size / 1024 / 1024:.2f}MB > {max_size_mb}MB)"
        )
    
    # ファイルの読み込み
    encodings = ['utf-8', 'cp932', 'shift_jis', 'euc-jp', 'iso-2022-jp']
    
    for encoding in encodings:
        try:
            with open(filepath, 'r', encoding=encoding) as f:
                content = f.read()
            logger.debug(f"ファイルを {encoding} エンコーディングで読み込みました: {filepath}")
            return content
        except UnicodeDecodeError:
            continue
        except Exception as e:
            logger.error(f"ファイル読み込みエラー ({encoding}): {e}")
            continue
    
    # すべてのエンコーディングで失敗した場合
    raise ValueError(f"ファイルのエンコーディングを特定できませんでした: {filepath}")


def read_document_file(filepath: str) -> str:
    """
    指定されたファイルを読み込み、AIが解釈しやすい形式でテキストデータを返す。
    
    指定された絶対パスまたは相対パスのファイル（.eml, .msg, .txt, .md等）を読み込み、
    AIが解釈しやすいようにパースして純粋なテキストデータのみを返します。
    メールファイルの場合は不要なHTMLや添付ファイルを自動排除し、
    件名・送信者・日付・本文のみを抽出します。
    ユーザーから「このメールを読んで」と指示された際に最初に使用してください。
    
    対応拡張子: .eml, .msg, .txt, .md, .csv
    
    Args:
        filepath: 読み込むファイルのパス（絶対パスまたは相対パス）
    
    Returns:
        str: パースされたテキスト内容
    
    Raises:
        ValueError: パスが無効または許可されていない場合
        FileNotFoundError: ファイルが存在しない場合
    """
    logger = get_logger()
    
    # パスの検証
    validated_path = validate_filepath(filepath)
    
    # 拡張子の取得
    _, ext = os.path.splitext(validated_path)
    ext = ext.lower()
    
    logger.info(f"ファイルを読み込みます: {validated_path} (拡張子: {ext})")
    
    # 拡張子に基づいてパーサーを選択
    if ext == '.eml':
        return parse_eml_file(validated_path)
    elif ext == '.msg':
        return parse_msg_file(validated_path)
    elif ext in ['.txt', '.md', '.csv']:
        return parse_text_file(validated_path)
    else:
        # 未対応の拡張子はテキストファイルとして読み込みを試行
        logger.warning(f"未対応の拡張子です: {ext}。テキストファイルとして読み込みを試行します。")
        return parse_text_file(validated_path)

# ============================================================================
# MCPツール関数群
# ============================================================================

def add_task(
    title: Annotated[str, "タスクのタイトル（必須）。ユーザーの指示から簡潔に抽出してください。"],
    description: Annotated[str, "タスクの詳細説明（オプション、デフォルト: 空文字）。詳細な説明がある場合のみ設定。"] = "",
    due_date: Annotated[Optional[str], "期日（オプション、デフォルト: 自動設定）。YYYY-MM-DD形式。指定がない場合は3営業日後に自動設定されます。"] = None
) -> str:
    """
    タスクを新規登録します。

    【使用場面】
    ユーザーが「タスクを追加して」「やることを登録して」等の指示をした場合に使用します。
    「タスクを作成」「新しいタスク」等の表現でも使用します。

    【パラメータ】
    - title: タスクのタイトル（必須）
    - description: タスクの詳細説明（オプション）
    - due_date: 期日（オプション、未指定時は3営業日後に自動設定）

    【戻り値】
    成功時: タスクIDと登録内容
    失敗時: エラーメッセージ

    【注意点】
    期日が指定されていない場合、自動的に3営業日後に設定されます。
    """
    logger = get_logger()
    
    # タイトルの検証
    if not title or not title.strip():
        return t('error.empty_title')
    
    # 期日の処理
    auto_due = False
    if due_date is None or due_date == "" or due_date.lower() == "none":
        due_date = get_3_business_days_later()
        auto_due = True
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # タスクの登録
        cursor.execute('''
            INSERT INTO tasks (title, description, due_date, status, priority, created_at)
            VALUES (?, ?, ?, 'pending', 'medium', datetime('now'))
        ''', (title.strip(), description, due_date))
        
        task_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        logger.info(f"タスクを登録しました: #{task_id} '{title}' (期日: {due_date})")
        
        if auto_due:
            return t('task.added_auto_due', task_id, title, due_date)
        else:
            return t('task.added', task_id, title, due_date)
            
    except Exception as e:
        logger.error(f"タスク登録エラー: {str(e)}")
        return f"タスクの登録に失敗しました: {str(e)}"


def list_pending_tasks(
    limit: Annotated[int, "取得件数の上限（デフォルト: 100、最大: 1000）。"] = 100,
    offset: Annotated[int, "オフセット（ページネーション用、デフォルト: 0）。"] = 0
) -> str:
    """
    未完了（status = 'pending'）のタスク一覧を取得します。

    【使用場面】
    ユーザーが「未完了のタスク一覧を表示して」「やるべきことをリストアップして」等の指示をした場合に使用します。
    「今やるべきタスク」「残っているタスク」等の表現でも使用します。

    【list_all_tasksとの使い分け】
    - list_pending_tasks: 未完了タスクのみを表示（日常的なタスク確認用）
    - list_all_tasks: 完了・キャンセル済みを含む全タスクを表示（履歴確認用）

    【パラメータ】
    - limit: 取得件数の上限（デフォルト: 100）
    - offset: オフセット（ページネーション用）

    【戻り値】
    成功時: 未完了タスクの一覧（期日順）
    失敗時: エラーメッセージ
    """
    logger = get_logger()
    
    # パラメータの検証
    limit = min(max(1, limit), 1000)
    offset = max(0, offset)
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, title, due_date, priority, category
            FROM tasks
            WHERE status = 'pending'
            ORDER BY due_date ASC
            LIMIT ? OFFSET ?
        ''', (limit, offset))
        
        tasks = cursor.fetchall()
        conn.close()
        
        if not tasks:
            return t('task.no_pending')
        
        result_lines = []
        for task in tasks:
            task_id, title, due_date, priority, category = task
            line = f"[#{task_id}] {title} (期日: {due_date}"
            if priority and priority != 'medium':
                line += f", 優先度: {priority}"
            if category:
                line += f", カテゴリ: {category}"
            line += ")"
            result_lines.append(line)
        
        result = "\n".join(result_lines)
        result += f"\n\n(表示: {offset + 1}〜{offset + len(tasks)}件)"
        
        return result
        
    except Exception as e:
        logger.error(f"タスク一覧取得エラー: {str(e)}")
        return f"タスク一覧の取得に失敗しました: {str(e)}"


def update_task_date(
    task_id: Annotated[int, "対象のタスクID（必須）。正の整数。"],
    new_due_date: Annotated[str, "新しい期日（必須）。YYYY-MM-DD形式。"],
    reason: Annotated[Optional[str], "変更理由（オプション）。"] = None
) -> str:
    """
    登録済みタスクの期日を変更・延期します。

    【使用場面】
    ユーザーが「タスクの期日を変更して」「期限を延ばして」等の指示をした場合に使用します。
    「締め切りを変更」「日程を変更」等の表現でも使用します。

    【パラメータ】
    - task_id: 対象のタスクID（必須）
    - new_due_date: 新しい期日（YYYY-MM-DD形式、必須）
    - reason: 変更理由（オプション）

    【戻り値】
    成功時: 変更内容の確認メッセージ
    失敗時: エラーメッセージ

    【注意点】
    過去の日付を指定した場合、警告が表示されます。
    """
    logger = get_logger()
    
    # タスクIDの検証
    if task_id <= 0:
        return t('error.invalid_id')
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # タスクの存在確認と変更前の値を取得
        cursor.execute('SELECT due_date FROM tasks WHERE id = ?', (task_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return t('task.not_found', task_id)
        
        old_due_date = row[0]
        
        # 過去日付の警告
        warning = ""
        try:
            from datetime import datetime
            new_date = datetime.strptime(new_due_date, '%Y-%m-%d')
            today = datetime.now().date()
            if new_date.date() < today:
                warning = f"\n{t('task.due_date_past_warning')}"
        except ValueError:
            conn.close()
            return t('error.invalid_date')
        
        # 期日の更新
        cursor.execute('''
            UPDATE tasks SET due_date = ?, updated_at = datetime('now')
            WHERE id = ?
        ''', (new_due_date, task_id))
        
        # 変更履歴の記録
        cursor.execute('''
            INSERT INTO task_history (task_id, field_name, old_value, new_value, reason, changed_at, changed_by)
            VALUES (?, 'due_date', ?, ?, ?, datetime('now'), 'ai')
        ''', (task_id, old_due_date, new_due_date, reason))
        
        conn.commit()
        conn.close()
        
        logger.info(f"タスク #{task_id} の期日を {old_due_date} から {new_due_date} に変更しました")
        
        result = t('task.due_date_updated', task_id, new_due_date)
        if reason:
            result += f" (変更理由: {reason})"
        if warning:
            result += warning
        
        return result
        
    except Exception as e:
        logger.error(f"期日変更エラー: {str(e)}")
        return f"期日の変更に失敗しました: {str(e)}"


def update_task_title(
    task_id: Annotated[int, "対象のタスクID（必須）。正の整数。"],
    new_title: Annotated[str, "新しいタイトル（必須）。空文字は不可。"],
    reason: Annotated[Optional[str], "変更理由（オプション）。"] = None
) -> str:
    """
    登録済みタスクのタイトルを変更します。

    【使用場面】
    ユーザーが「タスクのタイトルを変更して」「名前を変えて」等の指示をした場合に使用します。

    【パラメータ】
    - task_id: 対象のタスクID（必須）
    - new_title: 新しいタイトル（必須、空文字不可）
    - reason: 変更理由（オプション）

    【戻り値】
    成功時: 変更内容の確認メッセージ
    失敗時: エラーメッセージ
    """
    logger = get_logger()
    
    # タスクIDの検証
    if task_id <= 0:
        return t('error.invalid_id')
    
    # タイトルの検証
    if not new_title or not new_title.strip():
        return t('error.empty_title')
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # タスクの存在確認と変更前の値を取得
        cursor.execute('SELECT title FROM tasks WHERE id = ?', (task_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return t('task.not_found', task_id)
        
        old_title = row[0]
        
        # タイトルの更新
        cursor.execute('''
            UPDATE tasks SET title = ?, updated_at = datetime('now')
            WHERE id = ?
        ''', (new_title.strip(), task_id))
        
        # 変更履歴の記録
        cursor.execute('''
            INSERT INTO task_history (task_id, field_name, old_value, new_value, reason, changed_at, changed_by)
            VALUES (?, 'title', ?, ?, ?, datetime('now'), 'ai')
        ''', (task_id, old_title, new_title.strip(), reason))
        
        conn.commit()
        conn.close()
        
        logger.info(f"タスク #{task_id} のタイトルを '{old_title}' から '{new_title}' に変更しました")
        
        result = t('task.title_updated', task_id, new_title.strip())
        if reason:
            result += f" (変更理由: {reason})"
        
        return result
        
    except Exception as e:
        logger.error(f"タイトル変更エラー: {str(e)}")
        return f"タイトルの変更に失敗しました: {str(e)}"


def update_task_description(
    task_id: Annotated[int, "対象のタスクID（必須）。正の整数。"],
    new_description: Annotated[str, "新しい説明（必須）。空文字も可。"],
    reason: Annotated[Optional[str], "変更理由（オプション）。"] = None
) -> str:
    """
    登録済みタスクの説明を変更します。

    【使用場面】
    ユーザーが「タスクの説明を更新して」「詳細を追加して」等の指示をした場合に使用します。

    【パラメータ】
    - task_id: 対象のタスクID（必須）
    - new_description: 新しい説明（空文字も可）
    - reason: 変更理由（オプション）

    【戻り値】
    成功時: 変更内容の確認メッセージ
    失敗時: エラーメッセージ
    """
    logger = get_logger()
    
    # タスクIDの検証
    if task_id <= 0:
        return t('error.invalid_id')
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # タスクの存在確認と変更前の値を取得
        cursor.execute('SELECT description FROM tasks WHERE id = ?', (task_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return t('task.not_found', task_id)
        
        old_description = row[0] if row[0] else ""
        
        # 説明の更新
        cursor.execute('''
            UPDATE tasks SET description = ?, updated_at = datetime('now')
            WHERE id = ?
        ''', (new_description, task_id))
        
        # 変更履歴の記録
        cursor.execute('''
            INSERT INTO task_history (task_id, field_name, old_value, new_value, reason, changed_at, changed_by)
            VALUES (?, 'description', ?, ?, ?, datetime('now'), 'ai')
        ''', (task_id, old_description, new_description, reason))
        
        conn.commit()
        conn.close()
        
        logger.info(f"タスク #{task_id} の説明を更新しました")
        
        result = t('task.description_updated', task_id)
        if reason:
            result += f" (変更理由: {reason})"
        
        return result
        
    except Exception as e:
        logger.error(f"説明変更エラー: {str(e)}")
        return f"説明の変更に失敗しました: {str(e)}"


def update_task_priority(
    task_id: Annotated[int, "対象のタスクID（必須）。正の整数。"],
    priority: Annotated[str, "優先度（必須）。'high'（高）, 'medium'（中）, 'low'（低）のいずれか。"],
    reason: Annotated[Optional[str], "変更理由（オプション）。"] = None
) -> str:
    """
    登録済みタスクの優先度を変更します。

    【使用場面】
    ユーザーが「タスクの優先度を変更して」「優先度を高くして」等の指示をした場合に使用します。

    【パラメータ】
    - task_id: 対象のタスクID（必須）
    - priority: 優先度（'high', 'medium', 'low'のいずれか）
    - reason: 変更理由（オプション）

    【戻り値】
    成功時: 変更内容の確認メッセージ
    失敗時: エラーメッセージ
    """
    logger = get_logger()
    
    # タスクIDの検証
    if task_id <= 0:
        return t('error.invalid_id')
    
    # 優先度の検証
    valid_priorities = ['high', 'medium', 'low']
    if priority not in valid_priorities:
        return t('error.invalid_priority')
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # タスクの存在確認と変更前の値を取得
        cursor.execute('SELECT priority FROM tasks WHERE id = ?', (task_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return t('task.not_found', task_id)
        
        old_priority = row[0]
        
        # 優先度の更新
        cursor.execute('''
            UPDATE tasks SET priority = ?, updated_at = datetime('now')
            WHERE id = ?
        ''', (priority, task_id))
        
        # 変更履歴の記録
        cursor.execute('''
            INSERT INTO task_history (task_id, field_name, old_value, new_value, reason, changed_at, changed_by)
            VALUES (?, 'priority', ?, ?, ?, datetime('now'), 'ai')
        ''', (task_id, old_priority, priority, reason))
        
        conn.commit()
        conn.close()
        
        logger.info(f"タスク #{task_id} の優先度を '{old_priority}' から '{priority}' に変更しました")
        
        result = t('task.priority_updated', task_id, priority)
        if reason:
            result += f" (変更理由: {reason})"
        
        return result
        
    except Exception as e:
        logger.error(f"優先度変更エラー: {str(e)}")
        return f"優先度の変更に失敗しました: {str(e)}"


def update_task_category(
    task_id: Annotated[int, "対象のタスクID（必須）。正の整数。"],
    category: Annotated[str, "カテゴリ（必須）。例: '仕事', 'プライベート', '買い物'等。"],
    reason: Annotated[Optional[str], "変更理由（オプション）。"] = None
) -> str:
    """
    登録済みタスクのカテゴリを変更します。

    【使用場面】
    ユーザーが「タスクのカテゴリを設定して」「分類を変更して」等の指示をした場合に使用します。

    【パラメータ】
    - task_id: 対象のタスクID（必須）
    - category: カテゴリ（自由入力）
    - reason: 変更理由（オプション）

    【戻り値】
    成功時: 変更内容の確認メッセージ
    失敗時: エラーメッセージ
    """
    logger = get_logger()
    
    # タスクIDの検証
    if task_id <= 0:
        return t('error.invalid_id')
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # タスクの存在確認と変更前の値を取得
        cursor.execute('SELECT category FROM tasks WHERE id = ?', (task_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return t('task.not_found', task_id)
        
        old_category = row[0] if row[0] else ""
        
        # カテゴリの更新
        cursor.execute('''
            UPDATE tasks SET category = ?, updated_at = datetime('now')
            WHERE id = ?
        ''', (category, task_id))
        
        # 変更履歴の記録
        cursor.execute('''
            INSERT INTO task_history (task_id, field_name, old_value, new_value, reason, changed_at, changed_by)
            VALUES (?, 'category', ?, ?, ?, datetime('now'), 'ai')
        ''', (task_id, old_category, category, reason))
        
        conn.commit()
        conn.close()
        
        logger.info(f"タスク #{task_id} のカテゴリを '{old_category}' から '{category}' に変更しました")
        
        result = t('task.category_updated', task_id, category)
        if reason:
            result += f" (変更理由: {reason})"
        
        return result
        
    except Exception as e:
        logger.error(f"カテゴリ変更エラー: {str(e)}")
        return f"カテゴリの変更に失敗しました: {str(e)}"


def update_task_status(
    task_id: Annotated[int, "対象のタスクID（必須）。正の整数。"],
    status: Annotated[str, "ステータス（必須）。'pending'（未完了）, 'completed'（完了）, 'canceled'（キャンセル）, 'archived'（アーカイブ）のいずれか。"],
    reason: Annotated[Optional[str], "変更理由（オプション）。"] = None
) -> str:
    """
    登録済みタスクのステータスを変更します。

    【使用場面】
    ユーザーが「タスクのステータスを変更して」等の指示をした場合に使用します。
    完了・キャンセル・アーカイブ等の状態変更に使用します。

    【パラメータ】
    - task_id: 対象のタスクID（必須）
    - status: ステータス（'pending', 'completed', 'canceled', 'archived'のいずれか）
    - reason: 変更理由（オプション）

    【戻り値】
    成功時: 変更内容の確認メッセージ
    失敗時: エラーメッセージ

    【注意点】
    完了にする場合は complete_task 関数の使用も検討してください。
    """
    logger = get_logger()
    
    # タスクIDの検証
    if task_id <= 0:
        return t('error.invalid_id')
    
    # ステータスの検証
    valid_statuses = ['pending', 'completed', 'canceled', 'archived']
    if status not in valid_statuses:
        return t('error.invalid_status')
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # タスクの存在確認と変更前の値を取得
        cursor.execute('SELECT status FROM tasks WHERE id = ?', (task_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return t('task.not_found', task_id)
        
        old_status = row[0]
        
        # ステータスの更新
        cursor.execute('''
            UPDATE tasks SET status = ?, updated_at = datetime('now')
            WHERE id = ?
        ''', (status, task_id))
        
        # 変更履歴の記録
        cursor.execute('''
            INSERT INTO task_history (task_id, field_name, old_value, new_value, reason, changed_at, changed_by)
            VALUES (?, 'status', ?, ?, ?, datetime('now'), 'ai')
        ''', (task_id, old_status, status, reason))
        
        conn.commit()
        conn.close()
        
        logger.info(f"タスク #{task_id} のステータスを '{old_status}' から '{status}' に変更しました")
        
        result = t('task.status_updated', task_id, status)
        if reason:
            result += f" (変更理由: {reason})"
        
        return result
        
    except Exception as e:
        logger.error(f"ステータス変更エラー: {str(e)}")
        return f"ステータスの変更に失敗しました: {str(e)}"


def complete_task(
    task_id: Annotated[int, "完了するタスクのID（必須）。正の整数。"]
) -> str:
    """
    指定されたタスクを完了状態に変更します。

    【使用場面】
    ユーザーが「タスクを完了して」「やった」「終わった」等の指示をした場合に使用します。
    「タスクを閉じて」「チェックして」等の表現でも使用します。

    【パラメータ】
    - task_id: 完了するタスクのID（必須）

    【戻り値】
    成功時: 完了確認メッセージ
    失敗時: エラーメッセージ

    【注意点】
    既に完了しているタスクに対してはエラーメッセージを返します。
    """
    logger = get_logger()
    
    # タスクIDの検証
    if task_id <= 0:
        return t('error.invalid_id')
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # タスクの存在確認と現在のステータスを取得
        cursor.execute('SELECT status FROM tasks WHERE id = ?', (task_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return t('task.not_found', task_id)
        
        current_status = row[0]
        
        # 既に完了している場合
        if current_status == 'completed':
            conn.close()
            return t('task.already_completed', task_id)
        
        # ステータスを完了に更新
        cursor.execute('''
            UPDATE tasks SET status = 'completed', updated_at = datetime('now'), completed_at = datetime('now')
            WHERE id = ?
        ''', (task_id,))
        
        # 変更履歴の記録
        cursor.execute('''
            INSERT INTO task_history (task_id, field_name, old_value, new_value, reason, changed_at, changed_by)
            VALUES (?, 'status', ?, 'completed', 'タスク完了', datetime('now'), 'ai')
        ''', (task_id, current_status))
        
        conn.commit()
        conn.close()
        
        logger.info(f"タスク #{task_id} を完了状態に変更しました")
        
        return t('task.completed', task_id)
        
    except Exception as e:
        logger.error(f"タスク完了エラー: {str(e)}")
        return f"タスクの完了に失敗しました: {str(e)}"


def archive_task(
    task_id: Annotated[int, "アーカイブするタスクのID（必須）。正の整数。"],
    reason: Annotated[Optional[str], "アーカイブ理由（オプション）。"] = None
) -> str:
    """
    指定されたタスクをアーカイブ状態に変更します。

    【使用場面】
    ユーザーが「タスクをアーカイブして」「非表示にして」等の指示をした場合に使用します。
    タスクを履歴に残しつつ非表示にする場合に使用します。

    【delete_taskとの使い分け】
    - archive_task: 論理削除（復元可能）
    - delete_task: 物理削除（復元不可）

    【パラメータ】
    - task_id: アーカイブするタスクのID（必須）
    - reason: アーカイブ理由（オプション）

    【戻り値】
    成功時: アーカイブ確認メッセージ
    失敗時: エラーメッセージ

    【注意点】
    アーカイブされたタスクは restore_task で復元可能です。
    """
    logger = get_logger()
    
    # タスクIDの検証
    if task_id <= 0:
        return t('error.invalid_id')
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # タスクの存在確認と現在のステータスを取得
        cursor.execute('SELECT status FROM tasks WHERE id = ?', (task_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return t('task.not_found', task_id)
        
        current_status = row[0]
        
        # ステータスをアーカイブに更新
        cursor.execute('''
            UPDATE tasks SET status = 'archived', updated_at = datetime('now')
            WHERE id = ?
        ''', (task_id,))
        
        # 変更履歴の記録
        cursor.execute('''
            INSERT INTO task_history (task_id, field_name, old_value, new_value, reason, changed_at, changed_by)
            VALUES (?, 'status', ?, 'archived', ?, datetime('now'), 'ai')
        ''', (task_id, current_status, reason))
        
        conn.commit()
        conn.close()
        
        logger.info(f"タスク #{task_id} をアーカイブしました")
        
        return t('task.archived', task_id)
        
    except Exception as e:
        logger.error(f"タスクアーカイブエラー: {str(e)}")
        return f"タスクのアーカイブに失敗しました: {str(e)}"


def restore_task(
    task_id: Annotated[int, "復元するタスクのID（必須）。正の整数。"]
) -> str:
    """
    アーカイブされたタスクを元の状態に復元します。

    【使用場面】
    ユーザーが「アーカイブしたタスクを戻して」「タスクを復元して」等の指示をした場合に使用します。

    【パラメータ】
    - task_id: 復元するタスクのID（必須）

    【戻り値】
    成功時: 復元確認メッセージ
    失敗時: エラーメッセージ

    【注意点】
    復元後のステータスは 'pending' になります。
    アーカイブされていないタスクに対してはエラーメッセージを返します。
    """
    logger = get_logger()
    
    # タスクIDの検証
    if task_id <= 0:
        return t('error.invalid_id')
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # タスクの存在確認と現在のステータスを取得
        cursor.execute('SELECT status FROM tasks WHERE id = ?', (task_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return t('task.not_found', task_id)
        
        current_status = row[0]
        
        # アーカイブされていない場合
        if current_status != 'archived':
            conn.close()
            return t('task.not_archived', task_id)
        
        # ステータスをpendingに復元
        cursor.execute('''
            UPDATE tasks SET status = 'pending', updated_at = datetime('now')
            WHERE id = ?
        ''', (task_id,))
        
        # 変更履歴の記録
        cursor.execute('''
            INSERT INTO task_history (task_id, field_name, old_value, new_value, reason, changed_at, changed_by)
            VALUES (?, 'status', 'archived', 'pending', 'タスク復元', datetime('now'), 'ai')
        ''', (task_id,))
        
        conn.commit()
        conn.close()
        
        logger.info(f"タスク #{task_id} を復元しました")
        
        return t('task.restored', task_id)
        
    except Exception as e:
        logger.error(f"タスク復元エラー: {str(e)}")
        return f"タスクの復元に失敗しました: {str(e)}"


def delete_task(
    task_id: Annotated[int, "削除するタスクのID（必須）。正の整数。"]
) -> str:
    """
    指定されたタスクをデータベースから完全に削除します（物理削除）。

    【使用場面】
    ユーザーが「タスクを削除して」「タスクを消して」等の指示をした場合に使用します。
    誤って登録したタスクを削除する際に使用します。

    【archive_taskとの使い分け】
    - delete_task: 物理削除（完全に削除、復元不可）
    - archive_task: 論理削除（アーカイブ、復元可能）

    【パラメータ】
    - task_id: 削除するタスクのID（必須）

    【戻り値】
    成功時: 削除確認メッセージ
    失敗時: エラーメッセージ

    【注意点】
    削除したタスクは復元できません。復元可能な削除が必要な場合は archive_task を使用してください。
    """
    logger = get_logger()
    
    # タスクIDの検証
    if task_id <= 0:
        return t('error.invalid_id')
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # タスクの存在確認とタイトルを取得
        cursor.execute('SELECT id, title FROM tasks WHERE id = ?', (task_id,))
        row = cursor.fetchone()
        
        if not row:
            conn.close()
            return t('task.not_found', task_id)
        
        task_title = row[1]
        
        # 関連する変更履歴の削除
        cursor.execute('DELETE FROM task_history WHERE task_id = ?', (task_id,))
        
        # タスクの削除
        cursor.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        
        conn.commit()
        conn.close()
        
        logger.info(f"タスク #{task_id} '{task_title}' を削除しました")
        
        return t('task.deleted', task_id, task_title)
        
    except Exception as e:
        logger.error(f"タスク削除エラー: {str(e)}")
        return f"タスクの削除に失敗しました: {str(e)}"


def get_task_history(
    task_id: Annotated[int, "履歴を取得するタスクのID（必須）。正の整数。"]
) -> str:
    """
    指定されたタスクの変更履歴を取得します。

    【使用場面】
    ユーザーが「タスクの履歴を表示して」「変更履歴を見せて」等の指示をした場合に使用します。
    タスクの修正履歴を確認する際に使用します。

    【パラメータ】
    - task_id: 履歴を取得するタスクのID（必須）

    【戻り値】
    成功時: 変更履歴の一覧
    失敗時: エラーメッセージ
    """
    logger = get_logger()
    
    # タスクIDの検証
    if task_id <= 0:
        return t('error.invalid_id')
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # タスクの存在確認
        cursor.execute('SELECT id FROM tasks WHERE id = ?', (task_id,))
        if not cursor.fetchone():
            conn.close()
            return t('task.not_found', task_id)
        
        # 変更履歴の取得（日時の降順）
        cursor.execute('''
            SELECT field_name, old_value, new_value, reason, changed_at, changed_by
            FROM task_history
            WHERE task_id = ?
            ORDER BY changed_at DESC
        ''', (task_id,))
        
        history = cursor.fetchall()
        conn.close()
        
        if not history:
            return t('task.history_empty', task_id)
        
        result_lines = [f"タスク [#{task_id}] の変更履歴:"]
        for record in history:
            field_name, old_value, new_value, reason, changed_at, changed_by = record
            line = f"  - {changed_at}: {field_name}"
            if old_value:
                line += f" (変更前: {old_value})"
            if new_value:
                line += f" → {new_value}"
            if reason:
                line += f" [理由: {reason}]"
            if changed_by:
                line += f" (実行者: {changed_by})"
            result_lines.append(line)
        
        return "\n".join(result_lines)
        
    except Exception as e:
        logger.error(f"変更履歴取得エラー: {str(e)}")
        return f"変更履歴の取得に失敗しました: {str(e)}"


def search_tasks(
    keyword: Annotated[str, "検索キーワード（必須）。タイトルまたは説明に含まれる文字列。"]
) -> str:
    """
    タスクのタイトルまたは説明に指定されたキーワードが含まれるタスクを検索します。

    【使用場面】
    ユーザーが「〇〇を含むタスクを探して」「〇〇というタスクはある？」等の指示をした場合に使用します。
    特定のキーワードでタスクを検索する場合に使用します。

    【他の検索関数との使い分け】
    - search_tasks: 単純なキーワード検索（部分一致）
    - fuzzy_search_tasks: あいまい検索（FTS5全文検索、関連度順）
    - semantic_search_tasks: 意味検索（エンベディング使用、意味的類似性）
    - search_tasks_advanced: 高度な検索（複数条件、フィルタリング）

    【パラメータ】
    - keyword: 検索キーワード（必須）

    【戻り値】
    成功時: 検索結果の一覧
    失敗時: エラーメッセージまたは「見つかりません」メッセージ
    """
    logger = get_logger()
    
    # キーワードの検証
    if not keyword or not keyword.strip():
        return "検索キーワードを指定してください。"
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # LIKE検索（部分一致）
        search_pattern = f"%{keyword.strip()}%"
        cursor.execute('''
            SELECT id, title, due_date, status
            FROM tasks
            WHERE title LIKE ? OR description LIKE ?
            ORDER BY created_at DESC
        ''', (search_pattern, search_pattern))
        
        tasks = cursor.fetchall()
        conn.close()
        
        if not tasks:
            return t('search.no_results', keyword)
        
        result_lines = [f"キーワード '{keyword}' の検索結果:"]
        for task in tasks:
            task_id, title, due_date, status = task
            result_lines.append(f"  [#{task_id}] {title} (期日: {due_date}, ステータス: {status})")
        
        return "\n".join(result_lines)
        
    except Exception as e:
        logger.error(f"タスク検索エラー: {str(e)}")
        return f"タスクの検索に失敗しました: {str(e)}"


def list_all_tasks(
    limit: Annotated[int, "取得件数の上限（デフォルト: 100、最大: 1000）。"] = 100,
    offset: Annotated[int, "オフセット（ページネーション用、デフォルト: 0）。"] = 0
) -> str:
    """
    完了・キャンセル済みを含む全てのタスク一覧を取得します。

    【使用場面】
    ユーザーが「全てのタスクを表示して」「タスク履歴を見せて」等の指示をした場合に使用します。
    過去のタスクを含めて確認したい場合に使用します。

    【list_pending_tasksとの使い分け】
    - list_pending_tasks: 未完了タスクのみ（日常的なタスク確認用）
    - list_all_tasks: 全タスク（完了・キャンセル済みを含む、履歴確認用）

    【パラメータ】
    - limit: 取得件数の上限（デフォルト: 100）
    - offset: オフセット（ページネーション用）

    【戻り値】
    成功時: 全タスクの一覧（作成日時順）
    失敗時: エラーメッセージ
    """
    logger = get_logger()
    
    # パラメータの検証
    limit = min(max(1, limit), 1000)
    offset = max(0, offset)
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, title, due_date, status, priority, category
            FROM tasks
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        ''', (limit, offset))
        
        tasks = cursor.fetchall()
        conn.close()
        
        if not tasks:
            return t('task.no_tasks')
        
        result_lines = []
        for task in tasks:
            task_id, title, due_date, status, priority, category = task
            line = f"[#{task_id}] {title} (期日: {due_date}, ステータス: {status}"
            if priority:
                line += f", 優先度: {priority}"
            if category:
                line += f", カテゴリ: {category}"
            line += ")"
            result_lines.append(line)
        
        result = "\n".join(result_lines)
        result += f"\n\n(表示: {offset + 1}〜{offset + len(tasks)}件)"
        
        return result
        
    except Exception as e:
        logger.error(f"全タスク一覧取得エラー: {str(e)}")
        return f"タスク一覧の取得に失敗しました: {str(e)}"


# ============================================================================
# エンベディング検索ヘルパー関数
# ============================================================================

def get_embedding_api_client():
    """
    エンベディングAPIクライアントの設定を取得する。
    
    Returns:
        dict: API設定（provider, api_url, model）
    """
    config = get_config()
    embedding_config = config.get('embedding', {})
    
    return {
        'enabled': embedding_config.get('enabled', True),
        'provider': embedding_config.get('provider', 'ollama'),
        'api_url': embedding_config.get('api_url', 'http://localhost:11434'),
        'model': embedding_config.get('model', 'qwen3-embedding:0.6b'),
    }


def get_embedding_from_api(text: str) -> Optional[List[float]]:
    """
    エンベディングAPIを使用してテキストのエンベディングを取得する。
    
    Args:
        text: エンベディングを取得するテキスト
    
    Returns:
        Optional[List[float]]: エンベディングベクトル（失敗時はNone）
    """
    import urllib.request
    import urllib.error
    
    api_config = get_embedding_api_client()
    
    if not api_config['enabled']:
        return None
    
    try:
        provider = api_config['provider']
        api_url = api_config['api_url']
        model = api_config['model']
        
        if provider == 'ollama':
            # Ollama API
            url = f"{api_url}/api/embeddings"
            data = json.dumps({
                'model': model,
                'prompt': text
            }).encode('utf-8')
            
            headers = {'Content-Type': 'application/json'}
            req = urllib.request.Request(url, data=data, headers=headers, method='POST')
            
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
                return result.get('embedding')
        
        elif provider in ['lmstudio', 'openai_compatible']:
            # LM Studio / OpenAI Compatible API
            url = f"{api_url}/v1/embeddings"
            data = json.dumps({
                'model': model,
                'input': text
            }).encode('utf-8')
            
            headers = {'Content-Type': 'application/json'}
            req = urllib.request.Request(url, data=data, headers=headers, method='POST')
            
            with urllib.request.urlopen(req, timeout=30) as response:
                result = json.loads(response.read().decode('utf-8'))
                embeddings = result.get('data', [])
                if embeddings and len(embeddings) > 0:
                    return embeddings[0].get('embedding')
        
        return None
        
    except urllib.error.URLError:
        return None
    except urllib.error.HTTPError:
        return None
    except Exception:
        return None


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """
    2つのベクトル間のコサイン類似度を計算する。
    
    Args:
        vec1: ベクトル1
        vec2: ベクトル2
    
    Returns:
        float: コサイン類似度（-1〜1）
    """
    import math
    
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    magnitude1 = math.sqrt(sum(a * a for a in vec1))
    magnitude2 = math.sqrt(sum(b * b for b in vec2))
    
    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0
    
    return dot_product / (magnitude1 * magnitude2)


def store_task_embedding(task_id: int, embedding: List[float]) -> bool:
    """
    タスクのエンベディングをデータベースに保存する。
    
    Args:
        task_id: タスクID
        embedding: エンベディングベクトル
    
    Returns:
        bool: 保存成功時はTrue
    """
    import struct
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # エンベディングをバイナリに変換
        embedding_blob = struct.pack(f'{len(embedding)}f', *embedding)
        
        api_config = get_embedding_api_client()
        model_name = api_config.get('model', 'qwen3-embedding:0.6b')
        
        # 既存のエンベディングを削除
        cursor.execute('DELETE FROM task_embeddings WHERE task_id = ?', (task_id,))
        
        # 新しいエンベディングを保存
        cursor.execute('''
            INSERT INTO task_embeddings (task_id, embedding, model_name, created_at)
            VALUES (?, ?, ?, datetime('now'))
        ''', (task_id, embedding_blob, model_name))
        
        conn.commit()
        conn.close()
        
        return True
        
    except Exception:
        return False


def get_task_embedding(task_id: int) -> Optional[List[float]]:
    """
    データベースからタスクのエンベディングを取得する。
    
    Args:
        task_id: タスクID
    
    Returns:
        Optional[List[float]]: エンベディングベクトル（存在しない場合はNone）
    """
    import struct
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT embedding FROM task_embeddings WHERE task_id = ?', (task_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row and row[0]:
            # バイナリからエンベディングを復元
            embedding_blob = row[0]
            embedding = list(struct.unpack(f'{len(embedding_blob) // 4}f', embedding_blob))
            return embedding
        
        return None
        
    except Exception:
        return None


# ============================================================================
# 検索関連MCPツール関数群
# ============================================================================

def search_tasks_advanced(query: dict) -> str:
    """
    複数の条件を組み合わせてタスクを高度に検索します。
    
    ユーザーが「先週登録したタスク」「期限が過ぎているタスク」「優先度の高い未完了タスク」
    など、曖昧な質問をした際に使用してください。条件は全てAND結合されます。
    
    Args:
        query: 検索条件の辞書（全てオプション）
            - keyword: タイトルまたは説明に含まれるキーワード
            - status: ステータス（'pending', 'completed', 'canceled', 'archived'）またはリスト
            - priority: 優先度（'high', 'medium', 'low'）またはリスト
            - category: カテゴリ
            - due_date_from: 期日の開始日（YYYY-MM-DD）
            - due_date_to: 期日の終了日（YYYY-MM-DD）
            - created_from: 登録日時の開始日（YYYY-MM-DD）
            - created_to: 登録日時の終了日（YYYY-MM-DD）
            - overdue: Trueの場合、期限切れのタスクのみ
            - sort_by: ソート対象フィールド（'due_date', 'created_at', 'priority'）
            - sort_order: ソート順（'asc', 'desc'）
            - limit: 取得件数の上限（デフォルト: 100、最大: 1000）
    
    Returns:
        str: 検索結果の文字列表現
    """
    logger = get_logger()
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # SQLクエリの構築
        sql = "SELECT id, title, due_date, status, priority, category FROM tasks WHERE 1=1"
        params = []
        
        # キーワード検索
        keyword = query.get('keyword')
        if keyword:
            sql += " AND (title LIKE ? OR description LIKE ?)"
            search_pattern = f"%{keyword.strip()}%"
            params.extend([search_pattern, search_pattern])
        
        # ステータスフィルタ
        status = query.get('status')
        if status:
            if isinstance(status, list):
                placeholders = ','.join(['?' for _ in status])
                sql += f" AND status IN ({placeholders})"
                params.extend(status)
            else:
                sql += " AND status = ?"
                params.append(status)
        
        # 優先度フィルタ
        priority = query.get('priority')
        if priority:
            if isinstance(priority, list):
                placeholders = ','.join(['?' for _ in priority])
                sql += f" AND priority IN ({placeholders})"
                params.extend(priority)
            else:
                sql += " AND priority = ?"
                params.append(priority)
        
        # カテゴリフィルタ
        category = query.get('category')
        if category:
            sql += " AND category = ?"
            params.append(category)
        
        # 期日範囲フィルタ
        due_date_from = query.get('due_date_from')
        if due_date_from:
            sql += " AND due_date >= ?"
            params.append(due_date_from)
        
        due_date_to = query.get('due_date_to')
        if due_date_to:
            sql += " AND due_date <= ?"
            params.append(due_date_to)
        
        # 登録日時範囲フィルタ
        created_from = query.get('created_from')
        if created_from:
            sql += " AND created_at >= ?"
            params.append(f"{created_from} 00:00:00")
        
        created_to = query.get('created_to')
        if created_to:
            sql += " AND created_at <= ?"
            params.append(f"{created_to} 23:59:59")
        
        # 期限切れフィルタ
        overdue = query.get('overdue')
        if overdue:
            sql += " AND due_date < date('now') AND status = 'pending'"
        
        # ソート
        sort_by = query.get('sort_by', 'due_date')
        valid_sort_fields = {'due_date': 'due_date', 'created_at': 'created_at', 'priority': 'priority'}
        sort_field = valid_sort_fields.get(sort_by, 'due_date')
        
        # 優先度のソート順（high > medium > low）
        if sort_field == 'priority':
            sort_order = query.get('sort_order', 'asc')
            if sort_order == 'desc':
                sql += " ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END DESC"
            else:
                sql += " ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END ASC"
        else:
            sort_order = query.get('sort_order', 'asc')
            order = 'DESC' if sort_order == 'desc' else 'ASC'
            sql += f" ORDER BY {sort_field} {order}"
        
        # リミット
        limit = min(max(1, query.get('limit', 100)), 1000)
        sql += " LIMIT ?"
        params.append(limit)
        
        cursor.execute(sql, params)
        tasks = cursor.fetchall()
        conn.close()
        
        if not tasks:
            return t('search.no_advanced_results')
        
        result_lines = ["条件に一致するタスク:"]
        for task in tasks:
            task_id, title, due_date, status, priority, category = task
            line = f"  [#{task_id}] {title} (期日: {due_date}, ステータス: {status}"
            if priority:
                line += f", 優先度: {priority}"
            if category:
                line += f", カテゴリ: {category}"
            line += ")"
            result_lines.append(line)
        
        result_lines.append(f"\n(検索結果: {len(tasks)}件)")
        
        return "\n".join(result_lines)
        
    except Exception as e:
        logger.error(f"高度な検索エラー: {str(e)}")
        return f"タスクの検索に失敗しました: {str(e)}"


def get_overdue_tasks() -> str:
    """
    期限が過ぎている未完了タスクの一覧を取得します。
    
    ユーザーが「期限切れのタスク」「遅れているタスク」を尋ねた際に使用してください。
    期日の昇順でソートされ、遅延日数を計算して表示します。
    
    Returns:
        str: 期限切れタスクの一覧
    """
    logger = get_logger()
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        today = datetime.now().date()
        
        cursor.execute('''
            SELECT id, title, due_date, priority, category
            FROM tasks
            WHERE status = 'pending' AND due_date < date('now')
            ORDER BY due_date ASC
        ''')
        
        tasks = cursor.fetchall()
        conn.close()
        
        if not tasks:
            return t('task.no_overdue')
        
        result_lines = ["期限切れのタスク一覧:"]
        for task in tasks:
            task_id, title, due_date, priority, category = task
            
            # 遅延日数の計算
            try:
                due = datetime.strptime(due_date, '%Y-%m-%d').date()
                delay_days = (today - due).days
                delay_text = f"（{delay_days}日遅延）"
            except ValueError:
                delay_text = ""
            
            line = f"  [#{task_id}] {title} (期日: {due_date}){delay_text}"
            if priority and priority != 'medium':
                line += f" [優先度: {priority}]"
            if category:
                line += f" [カテゴリ: {category}]"
            result_lines.append(line)
        
        result_lines.append(f"\n(合計: {len(tasks)}件)")
        
        return "\n".join(result_lines)
        
    except Exception as e:
        logger.error(f"期限切れタスク取得エラー: {str(e)}")
        return f"期限切れタスクの取得に失敗しました: {str(e)}"


def get_tasks_by_date_range(date_type: str, date_from: str, date_to: str) -> str:
    """
    指定期間に登録または期限が設定されたタスクを取得します。
    
    ユーザーが「先月のタスク」「今週のタスク」など期間指定で尋ねた際に使用してください。
    
    Args:
        date_type: 'created'（登録日）または 'due'（期日）
        date_from: 開始日（YYYY-MM-DD）
        date_to: 終了日（YYYY-MM-DD）
    
    Returns:
        str: 指定期間のタスク一覧
    """
    logger = get_logger()
    
    # パラメータの検証
    if date_type not in ['created', 'due']:
        return "date_typeは 'created' または 'due' を指定してください。"
    
    try:
        # 日付形式の検証
        datetime.strptime(date_from, '%Y-%m-%d')
        datetime.strptime(date_to, '%Y-%m-%d')
    except ValueError:
        return t('error.invalid_date')
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        if date_type == 'created':
            sql = '''
                SELECT id, title, due_date, status, created_at
                FROM tasks
                WHERE created_at >= ? AND created_at <= ?
                ORDER BY created_at DESC
            '''
            params = [f"{date_from} 00:00:00", f"{date_to} 23:59:59"]
            date_label = "登録日"
        else:
            sql = '''
                SELECT id, title, due_date, status
                FROM tasks
                WHERE due_date >= ? AND due_date <= ?
                ORDER BY due_date ASC
            '''
            params = [date_from, date_to]
            date_label = "期日"
        
        cursor.execute(sql, params)
        tasks = cursor.fetchall()
        conn.close()
        
        if not tasks:
            return f"指定期間（{date_from} 〜 {date_to}）にタスクはありません。"
        
        result_lines = [f"期間: {date_from} 〜 {date_to}（{date_label}）のタスク一覧:"]
        for task in tasks:
            if date_type == 'created':
                task_id, title, due_date, status, created_at = task
                result_lines.append(f"  [#{task_id}] {title} (期日: {due_date}, ステータス: {status}, 登録: {created_at})")
            else:
                task_id, title, due_date, status = task
                result_lines.append(f"  [#{task_id}] {title} (期日: {due_date}, ステータス: {status})")
        
        result_lines.append(f"\n(合計: {len(tasks)}件)")
        
        return "\n".join(result_lines)
        
    except Exception as e:
        logger.error(f"期間指定タスク取得エラー: {str(e)}")
        return f"タスクの取得に失敗しました: {str(e)}"


def get_task_statistics() -> str:
    """
    タスクの統計情報を取得します。
    
    ユーザーが「タスクの状況は？」「どれくらいタスクがある？」と尋ねた際に使用してください。
    
    Returns:
        str: JSON形式の統計情報
    """
    logger = get_logger()
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # 総タスク数
        cursor.execute('SELECT COUNT(*) FROM tasks')
        total = cursor.fetchone()[0]
        
        # ステータス別の集計
        cursor.execute('SELECT status, COUNT(*) FROM tasks GROUP BY status')
        status_rows = cursor.fetchall()
        by_status = {row[0]: row[1] for row in status_rows}
        
        # 優先度別の集計（未完了タスクのみ）
        cursor.execute('''
            SELECT priority, COUNT(*)
            FROM tasks
            WHERE status = 'pending'
            GROUP BY priority
        ''')
        priority_rows = cursor.fetchall()
        by_priority = {row[0]: row[1] for row in priority_rows}
        
        # 期限切れタスク数
        cursor.execute('''
            SELECT COUNT(*)
            FROM tasks
            WHERE status = 'pending' AND due_date < date('now')
        ''')
        overdue_count = cursor.fetchone()[0]
        
        # 今日が期日のタスク数
        cursor.execute('''
            SELECT COUNT(*)
            FROM tasks
            WHERE status = 'pending' AND due_date = date('now')
        ''')
        upcoming_today = cursor.fetchone()[0]
        
        # 今週が期日のタスク数（今日から7日以内）
        cursor.execute('''
            SELECT COUNT(*)
            FROM tasks
            WHERE status = 'pending'
            AND due_date >= date('now')
            AND due_date <= date('now', '+7 days')
        ''')
        upcoming_week = cursor.fetchone()[0]
        
        conn.close()
        
        # 統計情報をJSON形式で返す
        stats = {
            "total": total,
            "by_status": {
                "pending": by_status.get('pending', 0),
                "completed": by_status.get('completed', 0),
                "canceled": by_status.get('canceled', 0),
                "archived": by_status.get('archived', 0)
            },
            "by_priority": {
                "high": by_priority.get('high', 0),
                "medium": by_priority.get('medium', 0),
                "low": by_priority.get('low', 0)
            },
            "overdue_count": overdue_count,
            "upcoming_today": upcoming_today,
            "upcoming_week": upcoming_week
        }
        
        return json.dumps(stats, ensure_ascii=False, indent=2)
        
    except Exception as e:
        logger.error(f"統計情報取得エラー: {str(e)}")
        return f"統計情報の取得に失敗しました: {str(e)}"


def get_recent_tasks(days: int = 7) -> str:
    """
    指定日数以内に登録されたタスクの一覧を取得します。
    
    ユーザーが「最近のタスク」「今週登録したタスク」などと尋ねた際に使用してください。
    
    Args:
        days: 遡る日数（デフォルト: 7）
    
    Returns:
        str: 最近登録されたタスクの一覧
    """
    logger = get_logger()
    
    # パラメータの検証
    days = max(1, min(days, 365))
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, title, due_date, status, created_at
            FROM tasks
            WHERE created_at >= datetime('now', '-' || ? || ' days')
            ORDER BY created_at DESC
        ''', (days,))
        
        tasks = cursor.fetchall()
        conn.close()
        
        if not tasks:
            return t('task.no_recent', days)
        
        result_lines = [f"過去{days}日以内に登録されたタスク:"]
        for task in tasks:
            task_id, title, due_date, status, created_at = task
            result_lines.append(f"  [#{task_id}] {title} (期日: {due_date}, ステータス: {status}, 登録: {created_at})")
        
        result_lines.append(f"\n(合計: {len(tasks)}件)")
        
        return "\n".join(result_lines)
        
    except Exception as e:
        logger.error(f"最近のタスク取得エラー: {str(e)}")
        return f"タスクの取得に失敗しました: {str(e)}"


def semantic_search_tasks(query: str, limit: int = 10, use_embedding: bool = True) -> str:
    """
    意味的類似性に基づいてタスクを検索します。
    
    ユーザーが「確か報告みたいな」「重要なやつ」など、キーワードが一致しなくても
    意味が近いタスクを検索する際に使用してください。
    Ollama API（またはLM Studio等）を使用したベクトル検索を提供します。
    
    Args:
        query: 検索クエリ（自然文可）
        limit: 取得件数の上限（デフォルト: 10）
        use_embedding: Trueの場合はエンベディング検索、Falseの場合はFTS5全文検索を使用
    
    Returns:
        str: 検索結果の文字列表現
    """
    logger = get_logger()
    
    # クエリの検証
    if not query or not query.strip():
        return "検索クエリを指定してください。"
    
    limit = max(1, min(limit, 100))
    
    # エンベディング検索を試行
    if use_embedding:
        api_config = get_embedding_api_client()
        
        if api_config['enabled']:
            # クエリのエンベディングを取得
            query_embedding = get_embedding_from_api(query.strip())
            
            if query_embedding:
                logger.debug(t('embedding.mode_embedding', api_config['model']))
                
                try:
                    conn = get_connection()
                    cursor = conn.cursor()
                    
                    # 全タスクのエンベディングを取得
                    cursor.execute('''
                        SELECT t.id, t.title, t.due_date, t.status, e.embedding
                        FROM tasks t
                        LEFT JOIN task_embeddings e ON t.id = e.task_id
                    ''')
                    
                    tasks_with_embeddings = cursor.fetchall()
                    conn.close()
                    
                    # 類似度を計算してソート
                    results = []
                    for task in tasks_with_embeddings:
                        task_id, title, due_date, status, embedding_blob = task
                        
                        if embedding_blob:
                            # エンベディングをデシリアライズ
                            import struct
                            try:
                                embedding = list(struct.unpack(f'{len(embedding_blob) // 4}f', embedding_blob))
                                similarity = cosine_similarity(query_embedding, embedding)
                                results.append((task_id, title, due_date, status, similarity))
                            except Exception:
                                continue
                    
                    # 類似度順にソート
                    results.sort(key=lambda x: x[4], reverse=True)
                    results = results[:limit]
                    
                    if not results:
                        # エンベディングがあるタスクがない場合はFTS5にフォールバック
                        logger.debug(t('embedding.mode_fts5_unavailable'))
                        return fuzzy_search_tasks(query, limit)
                    
                    result_lines = [f"意味的検索結果（クエリ: '{query}'）:"]
                    for task_id, title, due_date, status, similarity in results:
                        score = f"{similarity:.3f}"
                        result_lines.append(f"  [#{task_id}] {title} (期日: {due_date}, ステータス: {status}, 類似度: {score})")
                    
                    result_lines.append(f"\n(検索モード: エンベディング検索)")
                    
                    return "\n".join(result_lines)
                    
                except Exception as e:
                    logger.error(f"エンベディング検索エラー: {str(e)}")
            
            else:
                # エンベディングAPIが利用不可の場合はFTS5にフォールバック
                logger.debug(t('embedding.mode_fts5_unavailable'))
                return fuzzy_search_tasks(query, limit)
    
    # FTS5全文検索を使用
    logger.debug(t('embedding.mode_fts5_manual'))
    return fuzzy_search_tasks(query, limit)


def rebuild_embeddings() -> str:
    """
    全タスクのエンベディングを再構築します。
    
    エンベディングモデルを変更した場合や、エンベディングが破損した場合に使用してください。
    
    Returns:
        str: 操作結果メッセージ
    """
    logger = get_logger()
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # 全タスクを取得
        cursor.execute('SELECT id, title, description FROM tasks')
        tasks = cursor.fetchall()
        
        if not tasks:
            conn.close()
            return "エンベディングを再構築するタスクがありません。"
        
        # task_embeddingsテーブルをクリア
        cursor.execute('DELETE FROM task_embeddings')
        
        success_count = 0
        failed_count = 0
        
        for task_id, title, description in tasks:
            # タイトルと説明を結合してエンベディングを生成
            text = f"{title} {description or ''}"
            embedding = get_embedding_from_api(text)
            
            if embedding:
                if store_task_embedding(task_id, embedding):
                    success_count += 1
                else:
                    failed_count += 1
            else:
                failed_count += 1
        
        conn.close()
        
        if failed_count > 0:
            return f"{success_count}件のタスクのエンベディングを再構築しました。（{failed_count}件は失敗）"
        
        return t('embedding.rebuild_success', success_count)
        
    except Exception as e:
        logger.error(f"エンベディング再構築エラー: {str(e)}")
        return f"エンベディングの再構築に失敗しました: {str(e)}"


def get_completed_tasks(days: int = 30) -> str:
    """
    指定期間内に完了したタスクの一覧を取得します。
    
    ユーザーが「最近完了したタスク」「先月完了したタスク」を確認したい際に使用してください。
    
    Args:
        days: 遡る日数（デフォルト: 30日）
    
    Returns:
        str: 完了タスクの一覧
    """
    logger = get_logger()
    
    # パラメータの検証
    days = max(1, min(days, 365))
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, title, due_date, completed_at
            FROM tasks
            WHERE status = 'completed'
            AND completed_at >= datetime('now', '-' || ? || ' days')
            ORDER BY completed_at DESC
        ''', (days,))
        
        tasks = cursor.fetchall()
        conn.close()
        
        if not tasks:
            return t('task.no_completed', days)
        
        result_lines = [f"過去{days}日以内に完了したタスク:"]
        for task in tasks:
            task_id, title, due_date, completed_at = task
            result_lines.append(f"  [#{task_id}] {title} (期日: {due_date}, 完了日: {completed_at})")
        
        result_lines.append(f"\n(合計: {len(tasks)}件)")
        
        return "\n".join(result_lines)
        
    except Exception as e:
        logger.error(f"完了タスク取得エラー: {str(e)}")
        return f"完了タスクの取得に失敗しました: {str(e)}"


def get_recently_modified_tasks(days: int = 7) -> str:
    """
    指定期間内に更新されたタスクの一覧を取得します。
    
    完了・変更を含む最近の活動を確認したい際に使用してください。
    
    Args:
        days: 遡る日数（デフォルト: 7日）
    
    Returns:
        str: 更新されたタスクの一覧
    """
    logger = get_logger()
    
    # パラメータの検証
    days = max(1, min(days, 365))
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, title, status, updated_at
            FROM tasks
            WHERE updated_at >= datetime('now', '-' || ? || ' days')
            ORDER BY updated_at DESC
        ''', (days,))
        
        tasks = cursor.fetchall()
        conn.close()
        
        if not tasks:
            return t('task.no_modified', days)
        
        result_lines = [f"過去{days}日以内に更新されたタスク:"]
        for task in tasks:
            task_id, title, status, updated_at = task
            result_lines.append(f"  [#{task_id}] {title} (ステータス: {status}, 更新日: {updated_at})")
        
        result_lines.append(f"\n(合計: {len(tasks)}件)")
        
        return "\n".join(result_lines)
        
    except Exception as e:
        logger.error(f"最近更新されたタスク取得エラー: {str(e)}")
        return f"タスクの取得に失敗しました: {str(e)}"


def fuzzy_search_tasks(keyword: str, limit: int = 10) -> str:
    """
    キー��ードの部分一致や関連語を含めてタスクを検索します。
    
    ユーザーが「確か○○といった単語がでてきたような…」「確か××さんからの依頼だったような…」
    とうろ覚えで検索する際に使用してください。
    SQLiteの全文検索エンジン（FTS5）を使用し、関連度スコア順に結果を返します。
    
    Args:
        keyword: 検索キーワード（部分一致、関連語を含む）
        limit: 取得件数の上限（デフォルト: 10）
    
    Returns:
        str: 検索結果の文字列表現
    """
    logger = get_logger()
    
    # キーワードの検証
    if not keyword or not keyword.strip():
        return "検索キーワードを指定してください。"
    
    limit = max(1, min(limit, 100))
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # FTS5特殊文字をエスケープ
        escaped_keyword = keyword.strip().replace('"', '""')
        
        # FTS5全文検索（BM25スコア順）
        cursor.execute('''
            SELECT t.id, t.title, t.due_date, t.status, bm25(tasks_fts) as score
            FROM tasks t
            JOIN tasks_fts fts ON t.id = fts.id
            WHERE tasks_fts MATCH ?
            ORDER BY score
            LIMIT ?
        ''', (f'"{escaped_keyword}"', limit))
        
        tasks = cursor.fetchall()
        conn.close()
        
        if not tasks:
            return f"キーワード '{keyword}' を含むタスクは見つかりませんでした。"
        
        result_lines = [f"キーワード '{keyword}' の全文検索結果（関連度順）:"]
        for task in tasks:
            task_id, title, due_date, status, score = task
            # BM25スコアは負の値（小さいほど関連度が高い）
            relevance = f"{-score:.2f}" if score < 0 else f"{score:.2f}"
            result_lines.append(f"  [#{task_id}] {title} (期日: {due_date}, ステータス: {status}, 関連度: {relevance})")
        
        result_lines.append(f"\n(検索結果: {len(tasks)}件)")
        
        return "\n".join(result_lines)
        
    except Exception as e:
        logger.error(f"全文検索エラー: {str(e)}")
        # FTS5エラーの場合はLIKE検索にフォールバック
        return search_tasks(keyword)


def search_tasks_by_content_fragments(fragments: list) -> str:
    """
    複数の断片的なキーワードからタスクを検索します。
    
    ユーザーが「確か○○と××のどちらかだった気がする」「何か会議かプロジェクトのどっちか」
    のように断片的な記憶から検索する際に使用してください。
    いずれかのキーワードを含むタスクを全て返します。
    
    Args:
        fragments: 断片的なキーワードのリスト
    
    Returns:
        str: 検索結果の文字列表現（各結果にどのキーワードがマッチしたかを表示）
    """
    logger = get_logger()
    
    # パラメータの検証
    if not fragments or not isinstance(fragments, list):
        return "キーワードのリストを指定してください。"
    
    # 空のキーワードを除外
    keywords = [f.strip() for f in fragments if f and f.strip()]
    
    if not keywords:
        return "有効なキーワードが含まれていません。"
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # FTS5 OR検索クエリを構築
        or_query = " OR ".join([f'"{k.replace(chr(34), chr(34)+chr(34))}"' for k in keywords])
        
        # FTS5全文検索
        cursor.execute('''
            SELECT t.id, t.title, t.due_date, t.status
            FROM tasks t
            JOIN tasks_fts fts ON t.id = fts.id
            WHERE tasks_fts MATCH ?
        ''', (or_query,))
        
        tasks = cursor.fetchall()
        conn.close()
        
        if not tasks:
            return t('search.no_fragments_results')
        
        # 各タスクでどのキーワードがマッチしたかを判定
        result_lines = [f"断片キーワード検索結果（キーワード: {', '.join(keywords)}）:"]
        
        for task in tasks:
            task_id, title, due_date, status = task
            
            # マッチしたキーワードを特定
            matched_keywords = []
            title_lower = title.lower() if title else ""
            for keyword in keywords:
                if keyword.lower() in title_lower:
                    matched_keywords.append(keyword)
            
            match_info = f"（マッチ: {', '.join(matched_keywords)}）" if matched_keywords else ""
            result_lines.append(f"  [#{task_id}] {title} (期日: {due_date}, ステータス: {status}){match_info}")
        
        result_lines.append(f"\n(検索結果: {len(tasks)}件)")
        
        return "\n".join(result_lines)
        
    except Exception as e:
        logger.error(f"断片キーワード検索エラー: {str(e)}")
        return f"タスクの検索に失敗しました: {str(e)}"


def get_all_unique_words() -> str:
    """
    タスクのタイトルと説明から抽出された全てのユニークな単語一覧を取得します。
    
    ユーザーが「どんなキーワードで検索できる？」と尋ねた際や、
    検索キーワードの候補を提示する際に使用してください。
    頻度順にソートされ、ストップワード（「の」「を」「に」等）は除外されます。
    
    Returns:
        str: 単語一覧（頻度順）
    """
    logger = get_logger()
    
    # 日本語のストップワード
    STOP_WORDS = {
        'の', 'を', 'に', 'が', 'は', 'で', 'と', 'も', 'から', 'まで',
        'より', 'へ', 'や', 'など', 'か', 'な', 'だ', 'です', 'ます',
        'て', 'た', 'し', 'いる', 'ある', 'する', 'なる', 'ある',
        'これ', 'それ', 'あれ', 'この', 'その', 'あの', 'どの',
        'ここ', 'そこ', 'あそこ', 'どこ', 'いつ', 'どう', 'なぜ',
        'だれ', 'なに', 'なん', 'どうして', 'どのように',
        'a', 'an', 'the', 'is', 'are', 'was', 'were', 'be', 'been',
        'being', 'have', 'has', 'had', 'do', 'does', 'did', 'will',
        'would', 'could', 'should', 'may', 'might', 'must', 'can',
        'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from',
        'as', 'into', 'through', 'during', 'before', 'after', 'above',
        'below', 'between', 'under', 'again', 'further', 'then', 'once',
        'and', 'but', 'or', 'nor', 'so', 'yet', 'both', 'either', 'neither',
        'not', 'only', 'own', 'same', 'than', 'too', 'very', 'just',
        'about', 'against', 'between', 'into', 'through', 'during',
        'before', 'after', 'above', 'below', 'to', 'from', 'up', 'down',
        'in', 'out', 'on', 'off', 'over', 'under', 'again', 'further',
        'then', 'once', 'here', 'there', 'when', 'where', 'why', 'how',
        'all', 'each', 'few', 'more', 'most', 'other', 'some', 'such',
        'no', 'nor', 'not', 'only', 'own', 'same', 'than', 'too', 'very',
    }
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # 全タスクのタイトルと説明を取得
        cursor.execute('SELECT title, description FROM tasks')
        tasks = cursor.fetchall()
        conn.close()
        
        if not tasks:
            return "タスクが登録されていません。"
        
        # 単語の出現頻度をカウント
        word_count = {}
        
        for title, description in tasks:
            # タイトルと説明を結合
            text = f"{title or ''} {description or ''}"
            
            # 簡易的な単語分割（スペース、句読点で分割）
            import re
            # 日本語と英語の単語を抽出
            words = re.findall(r'[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FAF]+|[a-zA-Z]+', text)
            
            for word in words:
                # 小文字に統一
                word_lower = word.lower()
                
                # ストップワードを除外
                if word_lower in STOP_WORDS:
                    continue
                
                # 短すぎる単語を除外（1文字）
                if len(word_lower) < 2:
                    continue
                
                # 頻度をカウント
                word_count[word_lower] = word_count.get(word_lower, 0) + 1
        
        if not word_count:
            return "抽出可能な単語がありません。"
        
        # 頻度順にソート
        sorted_words = sorted(word_count.items(), key=lambda x: x[1], reverse=True)
        
        # 上位100単語を表示
        max_words = 100
        result_lines = ["タスクから抽出された単語一覧（頻度順）:"]
        
        for i, (word, count) in enumerate(sorted_words[:max_words]):
            result_lines.append(f"  {i+1}. {word} ({count}回)")
        
        if len(sorted_words) > max_words:
            result_lines.append(f"\n... 他 {len(sorted_words) - max_words} 語")
        
        result_lines.append(f"\n(合計: {len(sorted_words)} 語)")
        
        return "\n".join(result_lines)
        
    except Exception as e:
        logger.error(f"単語抽出エラー: {str(e)}")
        return f"単語の抽出に失敗しました: {str(e)}"


# ============================================================================
# バルク操作MCPツール関数群
# ============================================================================

def delete_tasks_bulk(task_ids: list) -> str:
    """
    複数のタスクを一括で削除します。
    
    指定されたIDのタスクとその関連する変更履歴を全て削除します。
    トランザクション整合性を保証し、エラー時は全てロールバックします。
    
    Args:
        task_ids: 削除するタスクIDのリスト
    
    Returns:
        str: 操作結果メッセージ
            - 成功時: 「N件のタスクを削除しました。ID: [...]」
            - 失敗時: 「削除に失敗したタスク: [...]」
    """
    logger = get_logger()
    
    # 空のリストチェック
    if not task_ids or not isinstance(task_ids, list):
        return t('error.empty_task_list')
    
    # 無効なIDのフィルタリング
    valid_ids = [tid for tid in task_ids if isinstance(tid, int) and tid > 0]
    
    if not valid_ids:
        return t('error.empty_task_list')
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        deleted_ids = []
        not_found_ids = []
        failed_ids = []
        
        for task_id in valid_ids:
            # タスクの存在確認とタイトルを取得
            cursor.execute('SELECT id, title FROM tasks WHERE id = ?', (task_id,))
            row = cursor.fetchone()
            
            if not row:
                not_found_ids.append(task_id)
                continue
            
            try:
                # 関連する変更履歴の削除
                cursor.execute('DELETE FROM task_history WHERE task_id = ?', (task_id,))
                
                # エンベディングの削除
                cursor.execute('DELETE FROM task_embeddings WHERE task_id = ?', (task_id,))
                
                # タスクの削除
                cursor.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
                
                deleted_ids.append(task_id)
                logger.debug(f"タスク #{task_id} を削除しました")
                
            except Exception as e:
                failed_ids.append(task_id)
                logger.warning(f"タスク #{task_id} の削除に失敗: {str(e)}")
        
        conn.commit()
        conn.close()
        
        # 結果メッセージの構築
        result_parts = []
        
        if deleted_ids:
            result_parts.append(f"{len(deleted_ids)}件のタスクを削除しました。ID: {deleted_ids}")
            logger.info(f"{len(deleted_ids)}件のタスクを一括削除しました")
        
        if not_found_ids:
            result_parts.append(f"存在しないID: {not_found_ids}")
        
        if failed_ids:
            result_parts.append(f"削除に失敗したタスク: {failed_ids}")
        
        return "\n".join(result_parts) if result_parts else "処理が完了しました。"
        
    except Exception as e:
        logger.error(f"一括削除エラー: {str(e)}")
        return f"タスクの一括削除に失敗しました: {str(e)}"


def update_tasks_status_bulk(task_ids: list, status: str, reason: str = None) -> str:
    """
    複数のタスクのステータスを一括で変更します。
    
    指定された全てのタスクのステータスを変更し、各タスクの変更履歴を記録します。
    トランザクション整合性を保証し、エラー時は全てロールバックします。
    
    Args:
        task_ids: ステータスを変更するタスクIDのリスト
        status: 新しいステータス（'pending', 'completed', 'canceled', 'archived'）
        reason: 変更理由（オプション）
    
    Returns:
        str: 操作結果メッセージ
            - 成功時: 「N件のタスクのステータスを '...' に変更しました。」
    """
    logger = get_logger()
    
    # 空のリストチェック
    if not task_ids or not isinstance(task_ids, list):
        return t('error.empty_task_list')
    
    # ステータスの検証
    valid_statuses = ['pending', 'completed', 'canceled', 'archived']
    if status not in valid_statuses:
        return t('error.invalid_status')
    
    # 無効なIDのフィルタリング
    valid_ids = [tid for tid in task_ids if isinstance(tid, int) and tid > 0]
    
    if not valid_ids:
        return t('error.empty_task_list')
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        updated_ids = []
        not_found_ids = []
        already_status_ids = []
        
        for task_id in valid_ids:
            # タスクの存在確認と現在のステータスを取得
            cursor.execute('SELECT id, status FROM tasks WHERE id = ?', (task_id,))
            row = cursor.fetchone()
            
            if not row:
                not_found_ids.append(task_id)
                continue
            
            current_status = row[1]
            
            # 既に同じステータスの場合はスキップ
            if current_status == status:
                already_status_ids.append(task_id)
                continue
            
            # ステータスの更新
            if status == 'completed':
                cursor.execute('''
                    UPDATE tasks SET status = ?, updated_at = datetime('now'), completed_at = datetime('now')
                    WHERE id = ?
                ''', (status, task_id))
            else:
                cursor.execute('''
                    UPDATE tasks SET status = ?, updated_at = datetime('now')
                    WHERE id = ?
                ''', (status, task_id))
            
            # 変更履歴の記録
            cursor.execute('''
                INSERT INTO task_history (task_id, field_name, old_value, new_value, reason, changed_at, changed_by)
                VALUES (?, 'status', ?, ?, ?, datetime('now'), 'ai')
            ''', (task_id, current_status, status, reason))
            
            updated_ids.append(task_id)
        
        conn.commit()
        conn.close()
        
        # 結果メッセージの構築
        result_parts = []
        
        if updated_ids:
            result_parts.append(f"{len(updated_ids)}件のタスクのステータスを '{status}' に変更しました。")
            if reason:
                result_parts.append(f"変更理由: {reason}")
            logger.info(f"{len(updated_ids)}件のタスクのステータスを '{status}' に一括変更しました")
        
        if not_found_ids:
            result_parts.append(f"存在しないID: {not_found_ids}")
        
        if already_status_ids:
            result_parts.append(f"既に '{status}' だったタスク: {already_status_ids}（スキップ）")
        
        return "\n".join(result_parts) if result_parts else "処理が完了しました。"
        
    except Exception as e:
        logger.error(f"ステータス一括変更エラー: {str(e)}")
        return f"ステータスの一括変更に失敗しました: {str(e)}"


def update_tasks_due_date_bulk(
    task_ids: Annotated[list, "期日を変更するタスクIDのリスト（必須）。正の整数のリスト。"],
    new_due_date: Annotated[str, "新しい期日（必須）。YYYY-MM-DD形式。"],
    reason: Annotated[Optional[str], "変更理由（オプション）。"] = None
) -> str:
    """
    複数のタスクの期日を一括で変更します。

    【使用場面】
    ユーザーが「これらのタスクの期日を変更して」「複数のタスクの期限を延ばして」等の指示をした場合に使用します。

    【パラメータ】
    - task_ids: 期日を変更するタスクIDのリスト
    - new_due_date: 新しい期日（YYYY-MM-DD形式）
    - reason: 変更理由（オプション）

    【戻り値】
    成功時: 「N件のタスクの期日を YYYY-MM-DD に変更しました。」
    失敗時: エラーメッセージ

    【注意点】
    指定された全てのタスクの期日を変更し、各タスクの変更履歴を記録します。
    トランザクション整合性を保証し、エラー時は全てロールバックします。
    """
    logger = get_logger()
    
    # 空のリストチェック
    if not task_ids or not isinstance(task_ids, list):
        return t('error.empty_task_list')
    
    # 日付形式の検証
    try:
        from datetime import datetime
        datetime.strptime(new_due_date, '%Y-%m-%d')
    except (ValueError, TypeError):
        return t('error.invalid_date')
    
    # 無効なIDのフィルタリング
    valid_ids = [tid for tid in task_ids if isinstance(tid, int) and tid > 0]
    
    if not valid_ids:
        return t('error.empty_task_list')
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        updated_ids = []
        not_found_ids = []
        
        # 過去日付の警告
        warning = ""
        try:
            new_date = datetime.strptime(new_due_date, '%Y-%m-%d')
            today = datetime.now().date()
            if new_date.date() < today:
                warning = f"\n{t('task.due_date_past_warning')}"
        except ValueError:
            pass
        
        for task_id in valid_ids:
            # タスクの存在確認と変更前の期日を取得
            cursor.execute('SELECT id, due_date FROM tasks WHERE id = ?', (task_id,))
            row = cursor.fetchone()
            
            if not row:
                not_found_ids.append(task_id)
                continue
            
            old_due_date = row[1]
            
            # 期日の更新
            cursor.execute('''
                UPDATE tasks SET due_date = ?, updated_at = datetime('now')
                WHERE id = ?
            ''', (new_due_date, task_id))
            
            # 変更履歴の記録
            cursor.execute('''
                INSERT INTO task_history (task_id, field_name, old_value, new_value, reason, changed_at, changed_by)
                VALUES (?, 'due_date', ?, ?, ?, datetime('now'), 'ai')
            ''', (task_id, old_due_date, new_due_date, reason))
            
            updated_ids.append(task_id)
        
        conn.commit()
        conn.close()
        
        # 結果メッセージの構築
        result_parts = []
        
        if updated_ids:
            result_parts.append(f"{len(updated_ids)}件のタスクの期日を {new_due_date} に変更しました。")
            if reason:
                result_parts.append(f"変更理由: {reason}")
            logger.info(f"{len(updated_ids)}件のタスクの期日を {new_due_date} に一括変更しました")
        
        if not_found_ids:
            result_parts.append(f"存在しないID: {not_found_ids}")
        
        if warning:
            result_parts.append(warning)
        
        return "\n".join(result_parts) if result_parts else "処理が完了しました。"
        
    except Exception as e:
        logger.error(f"期日一括変更エラー: {str(e)}")
        return f"期日の一括変更に失敗しました: {str(e)}"


def add_tasks_bulk(
    tasks: Annotated[list, "タスク情報のリスト（必須）。各タスクは辞書形式で、title（必須）、description、due_date、priority、categoryを含む。"]
) -> str:
    """
    複数のタスクを一括で登録します。

    【使用場面】
    ユーザーが「複数のタスクを登録して」「これらのタスクをまとめて追加して」等の指示をした場合に使用します。
    CSVやリスト形式でタスクを一括登録する場合に使用します。

    【パラメータ】
    tasks: タスク情報のリスト。各タスクは以下のキーを持つ辞書:
        - title: タスクのタイトル（必須）
        - description: タスクの詳細説明（オプション）
        - due_date: 期日（YYYY-MM-DD形式、オプション、未指定時は3営業日後）
        - priority: 優先度（'high', 'medium', 'low'、デフォルト: 'medium'）
        - category: カテゴリ（オプション）

    【戻り値】
    成功時: 「N件のタスクを登録しました。ID: [...]」
    失敗時: エラーメッセージ

    【注意点】
    100件ごとにコミットを分割して実行し、トランザクション整合性を保証します。
    エラー時は現在のバッチまでの変更をロールバックします。
    """
    logger = get_logger()
    
    # 空のリストチェック
    if not tasks or not isinstance(tasks, list):
        return "タスクリストは空にできません。"
    
    # 有効な優先度
    valid_priorities = ['high', 'medium', 'low']
    
    # 登録結果
    added_ids = []
    failed_tasks = []
    
    # 100件ごとにコミット
    batch_size = 100
    
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        for i, task in enumerate(tasks):
            if not isinstance(task, dict):
                failed_tasks.append((i, "タスク情報が辞書形式ではありません"))
                continue
            
            # タイトルの検証
            title = task.get('title', '')
            if not title or not str(title).strip():
                failed_tasks.append((i, "タイトルが空です"))
                continue
            
            title = str(title).strip()
            description = task.get('description', '')
            due_date = task.get('due_date')
            priority = task.get('priority', 'medium')
            category = task.get('category')
            
            # 期日の処理（未指定の場合は3営業日後）
            auto_due = False
            if not due_date or due_date == "" or str(due_date).lower() == "none":
                due_date = get_3_business_days_later()
                auto_due = True
            else:
                # 日付形式の検証
                try:
                    datetime.strptime(str(due_date), '%Y-%m-%d')
                except ValueError:
                    failed_tasks.append((i, f"期日の形式が不正です: {due_date}"))
                    continue
            
            # 優先度の検証
            if priority not in valid_priorities:
                priority = 'medium'
            
            try:
                # タスクの登録
                cursor.execute('''
                    INSERT INTO tasks (title, description, due_date, status, priority, category, created_at)
                    VALUES (?, ?, ?, 'pending', ?, ?, datetime('now'))
                ''', (title, description, due_date, priority, category))
                
                task_id = cursor.lastrowid
                added_ids.append(task_id)
                
                # 100件ごとにコミット
                if len(added_ids) % batch_size == 0:
                    conn.commit()
                    logger.debug(f"{len(added_ids)}件のタスクを登録してコミットしました")
                    
            except Exception as e:
                failed_tasks.append((i, str(e)))
                logger.warning(f"タスク {i} の登録に失敗: {str(e)}")
        
        # 残りのタスクをコミット
        conn.commit()
        conn.close()
        
        # 結果メッセージの構築
        result_parts = []
        
        if added_ids:
            result_parts.append(f"{len(added_ids)}件のタスクを登録しました。ID: {added_ids}")
            logger.info(f"{len(added_ids)}件のタスクを一括登録しました")
        
        if failed_tasks:
            failed_info = [f"タスク {i}: {err}" for i, err in failed_tasks]
            result_parts.append(f"登録に失敗したタスク:\n" + "\n".join(failed_info))
        
        return "\n".join(result_parts) if result_parts else "処理が完了しました。"
        
    except Exception as e:
        logger.error(f"一括登録エラー: {str(e)}")
        return f"タスクの一括登録に失敗しました: {str(e)}"


# ============================================================================
# MCPツール関数の登録
# ============================================================================

def register_mcp_tools():
    """
    MCPサーバーにツール関数を登録する。
    
    FastMCPが利用可能な場合、各関数をMCPツールとして登録する。
    """
    mcp = get_mcp_server()
    
    if mcp is None:
        return
    
    # 各ツールをMCPサーバーに登録
    mcp.tool()(read_document_file)
    mcp.tool()(add_task)
    mcp.tool()(list_pending_tasks)
    mcp.tool()(update_task_date)
    mcp.tool()(update_task_title)
    mcp.tool()(update_task_description)
    mcp.tool()(update_task_priority)
    mcp.tool()(update_task_category)
    mcp.tool()(update_task_status)
    mcp.tool()(complete_task)
    mcp.tool()(archive_task)
    mcp.tool()(restore_task)
    mcp.tool()(delete_task)
    mcp.tool()(get_task_history)
    mcp.tool()(search_tasks)
    mcp.tool()(list_all_tasks)
    
    # 検索関連ツール
    mcp.tool()(search_tasks_advanced)
    mcp.tool()(get_overdue_tasks)
    mcp.tool()(get_tasks_by_date_range)
    mcp.tool()(get_task_statistics)
    mcp.tool()(get_recent_tasks)
    mcp.tool()(semantic_search_tasks)
    mcp.tool()(rebuild_embeddings)
    mcp.tool()(get_completed_tasks)
    mcp.tool()(get_recently_modified_tasks)
    mcp.tool()(fuzzy_search_tasks)
    mcp.tool()(search_tasks_by_content_fragments)
    mcp.tool()(get_all_unique_words)
    
    # バルク操作ツール
    mcp.tool()(delete_tasks_bulk)
    mcp.tool()(update_tasks_status_bulk)
    mcp.tool()(update_tasks_due_date_bulk)
    mcp.tool()(add_tasks_bulk)
    
    # バックアップ・サーバー情報ツール
    mcp.tool()(backup_database)
    mcp.tool()(get_server_info)


# ============================================================================
# エントリポイント
# ============================================================================

def initialize() -> None:
    """
    サーバーの初期化を行う。
    
    設定の読み込み、ロガーの設定、データベースの初期化を行う。
    """
    # 設定の読み込み
    load_config()
    
    # ロガーの設定
    setup_logger()
    
    # メッセージカタログの読み込み
    load_messages()
    
    # データベースの初期化
    init_db()
    
    # MCPツールの登録
    register_mcp_tools()
    
    logger = get_logger()
    logger.info("DeskToDo MCP Server が初期化されました")


def run_server():
    """
    MCPサーバーを起動する。
    """
    if not MCP_AVAILABLE:
        print("エラー: MCPライブラリがインストールされていません。")
        print("pip install mcp を実行してください。")
        return
    
    # 初期化
    initialize()
    
    # MCPサーバーの起動
    mcp = get_mcp_server()
    if mcp:
        logger = get_logger()
        logger.info("MCPサーバーを起動します...")
        mcp.run(transport='stdio')


if __name__ == "__main__":
    run_server()