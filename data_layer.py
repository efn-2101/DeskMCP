"""
data_layer.py - ローカルチャット履歴データレイヤー
================================================
責務:
- ChainlitのBaseDataLayerを継承したカスタム実装
- SQLiteへの非同期アクセス（aiosqlite使用）
- スレッド・ステップの永続化

【重要】agent.pyのロジックは一切変更しない
"""

import json
import logging
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone
import uuid

import aiosqlite
import chainlit as cl
from chainlit.data import BaseDataLayer
from chainlit.types import Pagination, ThreadFilter, PaginatedResponse, PageInfo
from chainlit.user import PersistedUser

logger = logging.getLogger(__name__)


class SQLiteDataLayer(BaseDataLayer):
    """
    SQLiteベースのチャット履歴データレイヤー
    
    仕様書6.1「ローカルチャット履歴」の実装
    - 会話履歴をローカルSQLiteに保存
    - アプリ再起動時にも過去スレッドを読み込み可能
    """
    
    def __init__(self, db_path: str = "data/chat_history.db"):
        """
        初期化
        
        Args:
            db_path: データベースファイルのパス
        """
        self.db_path = db_path
        self._ensure_data_dir()
        logger.info(f"SQLiteDataLayer初期化: {db_path}")
    
    def _ensure_data_dir(self) -> None:
        """データディレクトリが存在しない場合は作成"""
        db_file = Path(self.db_path)
        db_file.parent.mkdir(parents=True, exist_ok=True)
    
    async def _init_db(self) -> None:
        """データベースの初期化（テーブル作成 + WALモード有効化）"""
        async with aiosqlite.connect(self.db_path) as db:
            # WALモードを有効化: 複数セッションの読み書き競合を大幅緩和
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA synchronous=NORMAL")  # WAL時はNORMALで十分
            await db.executescript("""
                CREATE TABLE IF NOT EXISTS threads (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    metadata TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                
                CREATE TABLE IF NOT EXISTS steps (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    name TEXT,
                    type TEXT,
                    output TEXT,
                    metadata TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (thread_id) REFERENCES threads(id) ON DELETE CASCADE
                );
                
                CREATE INDEX IF NOT EXISTS idx_steps_thread_id 
                    ON steps(thread_id);
            """)
            await db.commit()
    
    async def _connect(self) -> aiosqlite.Connection:
        """WALモードを有効にしてSQLite接続を返す共通ヘルパー"""
        db = await aiosqlite.connect(self.db_path)
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA synchronous=NORMAL")
        return db
    
    # ============================================
    # BaseDataLayer オーバーライドメソッド
    # ============================================
    
    async def get_user(self, identifier: str) -> Optional[PersistedUser]:
        """
        ユーザーを取得（Chainlitはidentifier引数で呼び出す）
        
        Args:
            identifier: ユーザー識別子
            
        Returns:
            PersistedUserオブジェクト
        """
        # ローカル実装では常にダミーユーザーを返す
        return PersistedUser(
            id=identifier,
            identifier=identifier,
            createdAt=datetime.now(timezone.utc).isoformat()
        )
    
    async def get_thread(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """
        スレッドを取得（ThreadDict形式で返却）
        
        Args:
            thread_id: スレッドID
            
        Returns:
            スレッド情報（存在しない場合はNone）
        """
        await self._init_db()
        
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM threads WHERE id = ?",
                (thread_id,)
            )
            row = await cursor.fetchone()
            
            if row is None:
                return None
            
            # DBから実際のステップ一覧を取得
            steps = await self.get_steps(thread_id)
            
            # ThreadDict形式（キャメルケース）で返却
            return {
                "id": row["id"],
                "createdAt": row["created_at"],  # キャメルケース
                "name": row["name"],
                "metadata": json.loads(row["metadata"]) if row["metadata"] else {},  # Noneは絶対不可
                "steps": steps,  # 実際のステップを設定
                "elements": [],  # 空リスト（Chainlit要件）
                "userId": "local_user",  # ローカルユーザー
                "userIdentifier": "local_user"  # Chainlitが検証に使用
            }
    
    
    async def create_thread(
        self,
        metadata: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        新規スレッドを作成（ThreadDict形式で返却）
        
        Args:
            metadata: スレッドのメタデータ
            
        Returns:
            作成されたスレッド情報
        """
        await self._init_db()
        
        thread_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        metadata_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
        
        # nameをmetadataから抽出（存在する場合）
        name = None
        if metadata and isinstance(metadata, dict):
            name = metadata.get("name")
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO threads (id, name, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (thread_id, name, metadata_json, now, now)
            )
            await db.commit()
        
        logger.info(f"スレッド作成: {thread_id}")
        
        # ThreadDict形式（キャメルケース）で返却
        return {
            "id": thread_id,
            "createdAt": now,  # キャメルケース
            "name": name,
            "metadata": metadata if metadata else {},  # Noneは絶対不可
            "steps": [],  # 空リスト
            "userId": "local_user",  # ローカルユーザー
            "userIdentifier": "local_user"  # Chainlitが検証に使用
        }
    
    async def delete_thread(self, thread_id: str):
        """
        スレッドを削除
        
        Args:
            thread_id: 削除するスレッドID
            
        Returns:
            True（成功シグナル）
        """
        await self._init_db()
        
        async with aiosqlite.connect(self.db_path) as db:
            # 関連するステップを先に削除
            await db.execute("DELETE FROM steps WHERE thread_id = ?", (thread_id,))
            # スレッドを削除
            await db.execute("DELETE FROM threads WHERE id = ?", (thread_id,))
            await db.commit()
        
        logger.info(f"スレッド削除: {thread_id}")
        return True
    
    async def update_thread(self, thread_id: str, **kwargs):
        """
        スレッド情報を更新（UPSERT対応）
        
        Chainlitは手動作成を待たず、名前更新時に初めてスレッドが
        存在する前提で動く場合があるため、存在確認と挿入を同時に行う。
        
        Args:
            thread_id: スレッドID
            **kwargs: 更新パラメータ（name, metadata等）
            
        Returns:
            True（成功シグナル）
        """
        async with aiosqlite.connect(self.db_path) as db:
            # スレッドの存在確認
            cursor = await db.execute("SELECT id FROM threads WHERE id = ?", (thread_id,))
            if not await cursor.fetchone():
                # スレッドが存在しない場合は新規作成（UPSERT）
                now = datetime.now(timezone.utc).isoformat()
                await db.execute(
                    "INSERT INTO threads (id, name, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (thread_id, kwargs.get("name", "新しいチャット"), json.dumps(kwargs.get("metadata", {})), now, now)
                )
            else:
                # 既存スレッドの更新
                if "name" in kwargs and kwargs["name"] is not None:
                    await db.execute("UPDATE threads SET name = ? WHERE id = ?", (kwargs["name"], thread_id))
                if "metadata" in kwargs and kwargs["metadata"] is not None:
                    await db.execute("UPDATE threads SET metadata = ? WHERE id = ?", (json.dumps(kwargs["metadata"]), thread_id))
            await db.commit()
        return True
    
    async def create_step(self, step_dict: dict):
        """
        ステップを作成（ツール実行履歴を含む）
        
        【重要】Chainlitから渡されるstep辞書のキーはキャメルケース
        （threadId, createdAt）であるため、キャメルケースで取得する
        
        【Foreign Key保護】親スレッドがDBに無いとエラーになるため、
        親スレッドを自動生成（UPSERT）する保護を追加。
        
        Args:
            step_dict: ステップデータ
                - id: ステップID
                - threadId: スレッドID（キャメルケース）
                - name: ステップ名
                - type: ステップタイプ
                - output: 出力内容
                - metadata: メタデータ
                - createdAt: 作成日時（キャメルケース）
                
        Returns:
            作成されたステップ情報
        """
        # 【変更】アクションメニューやウェルカムメッセージは一時UIのためDBに保存しない
        if step_dict.get("name") in ["ActionMenu", "SystemWelcome"]:
            return step_dict
        
        # threadIdの確実なフェイルセーフ
        thread_id = step_dict.get("threadId")
        if not thread_id:
            thread_id = cl.context.session.thread_id  # コアから取得
            if not thread_id:
                thread_id = cl.user_session.get("thread_id")
                if not thread_id:
                    return step_dict

        # テキスト内容の確実な取得（output と content の両対応）
        output_text = step_dict.get("output")
        if output_text is None or output_text == "":
            output_text = step_dict.get("content", "")

        # 【超強力フィルター】テキスト内容で確実に一時UI（挨拶とメニュー）を弾き、DB保存をブロックする
        if output_text and ("エージェントを起動しました" in output_text or "実行したいアクションを選択" in output_text):
            return step_dict

        async with aiosqlite.connect(self.db_path) as db:
            # 親スレッドが存在しない場合は自動作成（Foreign Key保護）
            cursor = await db.execute("SELECT id FROM threads WHERE id = ?", (thread_id,))
            if not await cursor.fetchone():
                now = datetime.now(timezone.utc).isoformat()
                await db.execute(
                    "INSERT INTO threads (id, name, metadata, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (thread_id, "新しいチャット", "{}", now, now)
                )

            await db.execute(
                "INSERT OR REPLACE INTO steps (id, name, type, output, thread_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    step_dict.get("id"),
                    step_dict.get("name", "System"),
                    step_dict.get("type", "assistant_message"),
                    output_text,  # 補完したテキスト
                    thread_id,
                    step_dict.get("createdAt", datetime.now(timezone.utc).isoformat())
                )
            )
            await db.commit()
        return step_dict
    
    async def update_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        """
        ステップを更新
        
        Args:
            step: 更新するステップデータ
                - id: ステップID（必須）
                - output: 新しい出力内容
                - metadata: 更新するメタデータ
                
        Returns:
            更新されたステップ情報
        """
        await self._init_db()
        
        step_id = step.get("id")
        if not step_id:
            raise ValueError("step_id is required for update")
        
        output = step.get("output", "")
        metadata = json.dumps(step.get("metadata"), ensure_ascii=False) if step.get("metadata") else None
        
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                UPDATE steps 
                SET output = ?, metadata = ?
                WHERE id = ?
                """,
                (output, metadata, step_id)
            )
            await db.commit()
        
        logger.debug(f"ステップ更新: {step_id}")
        
        return step
    
    async def delete_step(self, step_id: str) -> bool:
        """
        ステップを削除
        
        Args:
            step_id: 削除するステップID
            
        Returns:
            削除成功時True
        """
        await self._init_db()
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "DELETE FROM steps WHERE id = ?",
                (step_id,)
            )
            await db.commit()
            
            deleted = cursor.rowcount > 0
            
            if deleted:
                logger.debug(f"ステップ削除: {step_id}")
            
            return deleted
    
    async def get_steps(self, thread_id: str):
        """
        スレッド内のステップを取得
        
        Args:
            thread_id: スレッドID
            
        Returns:
            ステップリスト（時系列順）
            厳格なキャメルケース形式:
            - id: ステップID
            - name: ステップ名
            - type: ステップタイプ（user_message, assistant_message等）
            - output: 出力内容
            - threadId: スレッドID
            - createdAt: 作成日時
            - isError: エラーフラグ（デフォルトFalse）
            - showInput: 入力表示フラグ（デフォルトFalse）
        """
        steps = []
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM steps WHERE thread_id = ? ORDER BY created_at ASC", (thread_id,)) as cursor:
                async for row in cursor:
                    output_text = row["output"] if row["output"] else ""
                    
                    # 【超強力フィルター】過去のゴミデータ（挨拶とメニュー）をテキスト判定で非表示にする
                    if "エージェントを起動しました" in output_text or "実行したいアクションを選択" in output_text:
                        continue
                    
                    # 【変更】過去に保存された一時メッセージを復元対象から除外
                    if row["name"] in ["ActionMenu", "SystemWelcome"]:
                        continue
                    
                    steps.append({
                        "id": row["id"],
                        "name": row["name"],
                        "type": row["type"],
                        "output": row["output"] if row["output"] else "",
                        "threadId": row["thread_id"],
                        "createdAt": row["created_at"],
                        "isError": False,
                        "showInput": False
                    })
        return steps
    
    # ============================================
    # BaseDataLayer 必須抽象メソッドの実装
    # ============================================
    
    async def build_debug_url(self) -> str:
        """デバッグURLを構築（ローカル実装では空文字を返す）"""
        return ""
    
    async def close(self) -> None:
        """リソースの解放（SQLite接続は都度閉じているので何もしない）"""
        pass
    
    async def create_element(
        self,
        thread_id: str,
        element: Dict[str, Any]
    ) -> Dict[str, Any]:
        """要素を作成（ファイル添付等、現在は未使用）"""
        logger.warning(f"create_element is not implemented: thread_id={thread_id}")
        return element
    
    async def create_user(self, user: Dict[str, Any]) -> Dict[str, Any]:
        """ユーザーを作成（ローカル実装ではダミーを返す）"""
        return user
    
    async def delete_element(self, element_id: str) -> bool:
        """要素を削除"""
        logger.warning(f"delete_element is not implemented: element_id={element_id}")
        return False
    
    async def delete_feedback(self, feedback_id: str) -> bool:
        """フィードバックを削除"""
        logger.warning(f"delete_feedback is not implemented: feedback_id={feedback_id}")
        return False
    
    async def get_element(self, element_id: str) -> Optional[Dict[str, Any]]:
        """要素を取得"""
        logger.warning(f"get_element is not implemented: element_id={element_id}")
        return None
    
    async def get_favorite_steps(self, thread_id: str) -> List[Dict[str, Any]]:
        """お気に入りステップを取得"""
        return []
    
    async def get_thread_author(self, thread_id: str) -> str:
        """
        スレッドの作成者を取得
        Chainlitの編集・削除の権限チェックを通過させるため、常にローカルユーザーを返す
        """
        return "local_user"
    
    async def list_threads(
        self,
        pagination: Pagination,
        filter: ThreadFilter
    ) -> PaginatedResponse[dict]:
        """
        スレッド一覧を取得（Chainlit 1.0.0以降の仕様・ThreadDict形式）
        
        Args:
            pagination: ページネーション情報（pagination.firstをlimitとして使用）
            filter: スレッドフィルター（現在は未使用）
            
        Returns:
            PaginatedResponse形式のスレッド一覧
        """
        await self._init_db()
        
        # pagination.firstをlimitとして使用
        limit = pagination.first if pagination.first else 100
        
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM threads
                WHERE EXISTS (
                    SELECT 1 FROM steps WHERE steps.thread_id = threads.id
                )
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,)
            )
            rows = await cursor.fetchall()
            
            threads = []
            for row in rows:
                # ThreadDict形式（キャメルケース）で返却
                threads.append({
                    "id": row["id"],
                    "createdAt": row["created_at"],  # キャメルケース
                    "name": row["name"],
                    "metadata": json.loads(row["metadata"]) if row["metadata"] else {},  # Noneは絶対不可
                    "steps": [],  # 空リスト
                    "userId": "local_user",  # ローカルユーザー
                    "userIdentifier": "local_user"  # Chainlitが検証に使用
                })
            
            logger.info(f"UIに返却するスレッド数: {len(threads)}")
            
            return PaginatedResponse(
                pageInfo=PageInfo(hasNextPage=False, startCursor=None, endCursor=None),
                data=threads
            )
    
    async def upsert_feedback(
        self,
        feedback: Dict[str, Any]
    ) -> Dict[str, Any]:
        """フィードバックを登録/更新"""
        logger.warning("upsert_feedback is not implemented")
        return feedback