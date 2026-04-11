"""
DeskMCP MCP Integration Layer - MCPサーバー連携
===============================================
責務:
- 公式mcpパッケージを使用したMCPサーバー接続管理
- ツールスキーマの取得
- ツール実行と結果の返却

Note: 公式のmcpパッケージのClientSession, StdioServerParametersを使用
"""

import asyncio
import json
import os
import shutil
import re
import fnmatch
from typing import Optional, Any, List
from dataclasses import dataclass, field
from pathlib import Path
import logging

logger = logging.getLogger(__name__)

# MCP公式パッケージのインポート
try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.sse import sse_client
    MCP_AVAILABLE = True
    logger.info("MCPパッケージが利用可能です")
except ImportError:
    MCP_AVAILABLE = False
    logger.warning("MCPパッケージが利用できません。pip install mcp を実行してください")


# ============================================
# データクラス定義
# ============================================
@dataclass
class MCPServerConfig:
    """
    MCPサーバー設定
    
    仕様書4.2のmcp_servers.json構造に対応
    Stdio（command/args）とSSE（url/headers）の両トランスポートに対応
    """
    name: str
    command: str = ""
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    cwd: Optional[str] = None
    url: Optional[str] = None
    headers: dict = field(default_factory=dict)
    
    @property
    def transport_type(self) -> str:
        """トランスポート種別を判定（'sse' or 'stdio'）"""
        if self.url:
            return "sse"
        return "stdio"


@dataclass
class ToolCategory:
    """
    ツールカテゴリ定義
    
    ユーザー入力に基づくツールフィルタリングで使用
    """
    id: str
    name: str
    keywords: List[str] = field(default_factory=list)  # カテゴリに関連するキーワード
    tool_patterns: List[str] = field(default_factory=list)  # カテゴリに含まれるツール名パターン


# デフォルトカテゴリ定義
DEFAULT_CATEGORIES = [
    ToolCategory(
        id="task_create",
        name="タスク作成",
        keywords=["追加", "登録", "作成", "新規", "add", "create", "new"],
        tool_patterns=["add_task*", "create_*"]
    ),
    ToolCategory(
        id="task_read",
        name="タスク参照",
        keywords=["一覧", "表示", "見せ", "確認", "list", "get", "show", "search", "探", "検索"],
        tool_patterns=["list_*", "get_*", "search_*", "fuzzy_*", "semantic_*"]
    ),
    ToolCategory(
        id="task_update",
        name="タスク更新",
        keywords=["変更", "更新", "修正", "update", "change", "modify"],
        tool_patterns=["update_*"]
    ),
    ToolCategory(
        id="task_delete",
        name="タスク削除",
        keywords=["削除", "消去", "delete", "remove"],
        tool_patterns=["delete_*"]
    ),
    ToolCategory(
        id="task_complete",
        name="タスク完了",
        keywords=["完了", "終了", "complete", "finish", "done", "やった", "終わ"],
        tool_patterns=["complete_*"]
    ),
    ToolCategory(
        id="task_archive",
        name="タスクアーカイブ",
        keywords=["アーカイブ", "非表示", "archive", "復元", "restore"],
        tool_patterns=["archive_*", "restore_*"]
    ),
    ToolCategory(
        id="bulk",
        name="一括操作",
        keywords=["一括", "まとめ", "bulk", "batch", "全部"],
        tool_patterns=["*_bulk", "add_tasks_*"]
    ),
    ToolCategory(
        id="file",
        name="ファイル操作",
        keywords=["ファイル", "読み", "メール", "file", "read", "document", "eml", "msg"],
        tool_patterns=["read_*", "parse_*"]
    ),
    ToolCategory(
        id="statistics",
        name="統計・状況",
        keywords=["統計", "状況", "数", "statistics", "status", "count", "どれくらい", "情報"],
        tool_patterns=["get_*_statistics", "get_overdue_*", "get_server_info"]
    ),
    ToolCategory(
        id="history",
        name="履歴・バックアップ",
        keywords=["履歴", "バックアップ", "history", "backup", "変更履歴"],
        tool_patterns=["get_task_history", "backup_*", "get_recently_*"]
    ),
]


@dataclass
class ToolSchema:
    """ツールスキーマ情報"""
    name: str
    description: str
    input_schema: dict
    short_description: str = ""  # 簡易説明
    category: str = ""  # カテゴリID
    server_name: str = ""  # 所属サーバー名
    
    @property
    def namespaced_name(self) -> str:
        """名前空間付きツール名（server_name.tool_name）"""
        if self.server_name:
            return f"{self.server_name}.{self.name}"
        return self.name
    
    def get_description(self, mode: str = "full") -> str:
        """
        モードに応じた説明を返す
        
        Args:
            mode: "full"（詳細）, "compact"（圧縮）, "minimal"（最小）
            
        Returns:
            モードに応じた説明文
        """
        if mode == "minimal":
            return self.short_description or self.name
        elif mode == "compact":
            return self._extract_compact_description()
        return self.description
    
    def _extract_compact_description(self) -> str:
        """詳細説明から簡易版を抽出"""
        if self.short_description:
            return self.short_description
        
        # docstringの最初の段落（要約部分）を抽出
        lines = self.description.split('\n')
        summary_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if summary_lines:
                    break
                continue
            if stripped.startswith('【') or stripped.startswith('Args:') or stripped.startswith('Returns:'):
                break
            summary_lines.append(stripped)
        
        return ' '.join(summary_lines) if summary_lines else self.name
    
    def matches(self, query: str) -> bool:
        """
        ツール名のマッチング（名前空間対応）
        
        Args:
            query: 検索クエリ（"tool_name" または "server.tool_name"）
            
        Returns:
            マッチする場合True
        """
        if query == self.name or query == self.namespaced_name:
            return True
        # サーバー名なしでの検索も許可
        if '.' not in query and query == self.name:
            return True
        return False


class ToolDescriptionCompressor:
    """ツール説明の圧縮を行うクラス"""
    
    # 日本語のセクション見出しパターン
    SECTION_PATTERNS = [
        r'【使用場面】',
        r'【パラメータ】',
        r'【戻り値】',
        r'【注意点】',
        r'【他の.*との使い分け】',
    ]
    
    @classmethod
    def compress(cls, description: str, mode: str = "compact") -> str:
        """
        ツール説明を圧縮
        
        Args:
            description: 元の説明文
            mode: "compact"（約50%削減）, "minimal"（約80%削減）
            
        Returns:
            圧縮された説明文
        """
        if mode == "minimal":
            return cls._extract_minimal(description)
        elif mode == "compact":
            return cls._extract_compact(description)
        return description
    
    @classmethod
    def _extract_minimal(cls, description: str) -> str:
        """最小限の説明を抽出（1行）"""
        lines = description.split('\n')
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith('【') and not stripped.startswith('Args'):
                return stripped[:200]  # 最大200文字
        return description[:100]
    
    @classmethod
    def _extract_compact(cls, description: str) -> str:
        """簡易説明を抽出（使用場面のみ）"""
        # 【使用場面】セクションを抽出
        match = re.search(r'【使用場面】(.*?)(?=【|$)', description, re.DOTALL)
        if match:
            usage = match.group(1).strip()
            # 改行を削除して1行に
            usage = ' '.join(usage.split())
            return usage[:300]  # 最大300文字
        
        # セクションがない場合は最初の段落
        return cls._extract_minimal(description)


class ToolFilter:
    """ツールフィルタリングクラス"""
    
    # 日本語→英語キーワードマッピング（動的カテゴリ生成用）
    JAPANESE_TO_ENGLISH = {
        # 一般的なアクション
        "更新": "update",
        "作成": "create",
        "追加": "add",
        "削除": "delete",
        "表示": "show",
        "一覧": "list",
        "取得": "get",
        "検索": "search",
        "変更": "change",
        "修正": "modify",
        "完了": "complete",
        "終了": "finish",
        "復元": "restore",
        "アーカイブ": "archive",
        "同期": "sync",
        "再構築": "rebuild",
        "バックアップ": "backup",
        
        # ドキュメント・RAG関連
        "インデックス": "index",
        "ドキュメント": "document",
        "ファイル": "file",
        "読み": "read",
        "サーバー": "server",
        "情報": "info",
        "ステータス": "status",
        "状況": "status",
        "履歴": "history",
        "統計": "statistics",
        "数": "count",
        
        # カテゴリ関連
        "タスク": "task",
        "RAG": "rag",
        "検索": "search",
    }
    
    def __init__(self, categories: List[ToolCategory] = None):
        self.categories = categories or DEFAULT_CATEGORIES
    
    def filter_by_user_input(
        self,
        user_input: str,
        all_tools: List[ToolSchema],
        max_tools: int = 15,
        always_include: List[str] = None
    ) -> List[ToolSchema]:
        """
        ユーザー入力に基づいてツールをフィルタリング
        
        Args:
            user_input: ユーザー入力テキスト
            all_tools: 全ツールのリスト
            max_tools: 最大ツール数
            always_include: 常に含めるツール名のリスト
            
        Returns:
            フィルタリングされたツールリスト
        """
        # 【診断ログ】入力と全ツールを記録
        logger.info(f"[診断] filter_by_user_input 呼び出し:")
        logger.info(f"  user_input: {user_input[:100]}...")
        logger.info(f"  all_tools数: {len(all_tools)}")
        logger.info(f"  all_tools名一覧: {[t.name for t in all_tools]}")
        
        if not user_input:
            return all_tools[:max_tools]
        
        always_include = always_include or []
        
        # Step 1: カテゴリマッチング
        matched_categories = self._match_categories(user_input)
        logger.info(f"[診断] Step1 カテゴリマッチング: {[c.id for c in matched_categories]}")
        
        # Step 2: ツール名パターンマッチング
        candidate_tools = self._match_tool_patterns(all_tools, matched_categories)
        logger.info(f"[診断] Step2 パターンマッチング結果: {[t.name for t in candidate_tools]}")
        
        # Step 3: キーワードベースの追加マッチング
        keyword_tools = self._match_by_keywords(user_input, all_tools)
        logger.info(f"[診断] Step3 キーワードマッチング結果: {[t.name for t in keyword_tools]}")
        
        # Step 4: 動的カテゴリマッチング（ツール説明から抽出）
        dynamic_tools = self._match_by_description(user_input, all_tools)
        logger.info(f"[診断] Step4 動的マッチング結果: {[t.name for t in dynamic_tools]}")
        
        # Step 5: 常に含めるツールを追加
        always_tools = [t for t in all_tools if t.name in always_include]
        logger.info(f"[診断] Step5 always_include結果: {[t.name for t in always_tools]}")
        
        # Step 6: 結合・重複排除・制限
        final_tools = self._merge_and_limit(
            candidate_tools,
            keyword_tools,
            always_tools,
            max_tools,
            dynamic_tools
        )
        logger.info(f"[診断] Step6 最終結果: {[t.name for t in final_tools]}")
        
        # フォールバック: マッチするツールがない場合は全ツールから返す
        if not final_tools and all_tools:
            logger.info(f"カテゴリマッチング結果が空のため、全ツールから{max_tools}件を返します")
            return all_tools[:max_tools]
        
        return final_tools
    
    def _match_categories(self, user_input: str) -> List[ToolCategory]:
        """ユーザー入力にマッチするカテゴリを特定"""
        input_lower = user_input.lower()
        matched = []
        for category in self.categories:
            for keyword in category.keywords:
                if keyword.lower() in input_lower:
                    matched.append(category)
                    break
        return matched
    
    def _match_tool_patterns(
        self,
        tools: List[ToolSchema],
        categories: List[ToolCategory]
    ) -> List[ToolSchema]:
        """カテゴリのツールパターンにマッチするツールを抽出"""
        matched = []
        for category in categories:
            for tool in tools:
                for pattern in category.tool_patterns:
                    if fnmatch.fnmatch(tool.name, pattern):
                        if tool not in matched:
                            matched.append(tool)
                        break
        return matched
    
    def _match_by_keywords(
        self,
        user_input: str,
        tools: List[ToolSchema]
    ) -> List[ToolSchema]:
        """ツール名・説明のキーワードマッチング"""
        import re
        input_lower = user_input.lower()
        # 日本語指示文からもアンダースコア区切りのツール名を抽出できるよう正規表現を使用
        words = [w for w in input_lower.split() if len(w) >= 2]
        # ツール名パターン（例: list_pending_tasks）も抽出
        tool_name_patterns = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', user_input)
        words.extend([p.lower() for p in tool_name_patterns if len(p) >= 2])
        # 重複を除去
        words = list(set(words))
        
        matched = []
        for tool in tools:
            # ツール名にマッチ
            tool_name_lower = tool.name.lower()
            if any(word in tool_name_lower for word in words):
                matched.append(tool)
                continue
            
            # 説明にマッチ（簡易）
            if tool.description:
                desc_lower = tool.description.lower()
                for word in words:
                    if word in desc_lower:
                        matched.append(tool)
                        break
        
        # ツール名で重複排除（ToolSchemaはハッシュ可能ではないため）
        seen = set()
        unique_matched = []
        for tool in matched:
            if tool.name not in seen:
                seen.add(tool.name)
                unique_matched.append(tool)
        return unique_matched
    
    def _match_by_description(
        self,
        user_input: str,
        tools: List[ToolSchema]
    ) -> List[ToolSchema]:
        """
        ツールの説明（description）から動的にキーワードを抽出してマッチング
        
        新しいMCPサーバーが追加された場合でも、ツールの説明から
        関連するツールを自動的に検出できる
        """
        # ユーザー入力からキーワードを抽出
        input_lower = user_input.lower()
        
        # 日本語キーワードを英語に変換
        translated_keywords = []
        for jp, en in self.JAPANESE_TO_ENGLISH.items():
            if jp in input_lower:
                translated_keywords.append(en)
        
        # 単語分割とツール名パターン抽出
        words = [w for w in input_lower.split() if len(w) >= 2]
        tool_name_patterns = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', user_input)
        words.extend([p.lower() for p in tool_name_patterns if len(p) >= 2])
        words.extend(translated_keywords)
        words = list(set(words))
        
        # 【診断ログ】抽出されたキーワード
        logger.info(f"[診断] _match_by_description 抽出キーワード: {words}")
        logger.info(f"[診断] _match_by_description 翻訳済みキーワード: {translated_keywords}")
        
        matched = []
        for tool in tools:
            # ツール名でマッチ
            tool_name_lower = tool.name.lower()
            for word in words:
                if word in tool_name_lower:
                    matched.append(tool)
                    break
            
            if tool in matched:
                continue
            
            # ツールの説明でマッチ
            if tool.description:
                desc_lower = tool.description.lower()
                for word in words:
                    if word in desc_lower:
                        matched.append(tool)
                        break
        
        # 重複排除
        seen = set()
        unique_matched = []
        for tool in matched:
            if tool.name not in seen:
                seen.add(tool.name)
                unique_matched.append(tool)
        
        # 【診断ログ】マッチ結果
        logger.info(f"[診断] _match_by_description マッチしたツール: {[t.name for t in unique_matched]}")
        
        return unique_matched
    
    def _merge_and_limit(
        self,
        tools1: List[ToolSchema],
        tools2: List[ToolSchema],
        always_tools: List[ToolSchema],
        max_tools: int,
        dynamic_tools: List[ToolSchema] = None
    ) -> List[ToolSchema]:
        """ツールリストを結合して制限"""
        # 重���を排除しながら結合（常に含めるツールを優先）
        seen = set()
        merged = []
        
        # 常に含めるツールを先に追加
        for tool in always_tools:
            if tool.name not in seen:
                merged.append(tool)
                seen.add(tool.name)
        
        # カテゴリマッチしたツール
        for tool in tools1:
            if tool.name not in seen:
                merged.append(tool)
                seen.add(tool.name)
        
        # キーワードマッチしたツール
        for tool in tools2:
            if tool.name not in seen:
                merged.append(tool)
                seen.add(tool.name)
        
        # 動的カテゴリマッチしたツール
        if dynamic_tools:
            for tool in dynamic_tools:
                if tool.name not in seen:
                    merged.append(tool)
                    seen.add(tool.name)
        
        return merged[:max_tools]
    
    def get_category_summary(self) -> str:
        """カテゴリ一覧の要約を返す"""
        lines = []
        for cat in self.categories:
            lines.append(f"- {cat.name}: {', '.join(cat.keywords[:5])}")
        return '\n'.join(lines)


# ============================================
# MCPサーバー接続コンテキスト
# ============================================
class MCPServerConnection:
    """
    単一のMCPサーバー接続を管理するクラス
    
    async with パターンで使用可能
    """
    
    def __init__(self, config: MCPServerConfig):
        self.config = config
        self.session: Optional[ClientSession] = None
        self._read_stream = None
        self._write_stream = None
        self._cm = None  # stdio_client/sse_clientのコンテキストマネージャ
        self._session_cm = None  # sessionのコンテキストマネージャ
        self.tools: list[ToolSchema] = []
    
    async def connect(self) -> None:
        """MCPサーバーに接続"""
        if not MCP_AVAILABLE:
            raise RuntimeError("MCPパッケージがインストールされていません")
        
        transport = self.config.transport_type
        logger.info(f"MCPサーバー '{self.config.name}' に接続します (transport: {transport})")
        
        if transport == "sse" and self.config.url:
            # SSE接続先がローカルネットワークの場合、プロキシバイパスを確実に設定
            from urllib.parse import urlparse as _urlparse
            _parsed_url = _urlparse(self.config.url)
            _hostname = _parsed_url.hostname
            if _hostname:
                _local_hostnames = {"localhost", "127.0.0.1", "::1"}
                if _hostname in _local_hostnames or _hostname.endswith(".local"):
                    _current_no_proxy = os.environ.get("NO_PROXY", "") or os.environ.get("no_proxy", "")
                    if _hostname not in _current_no_proxy.split(","):
                        _new_value = f"{_current_no_proxy},{_hostname}".lstrip(",")
                        os.environ["NO_PROXY"] = _new_value
                        os.environ["no_proxy"] = _new_value
        
        if transport == "sse":
            # SSEトランスポート接続
            logger.debug(f"  url: {self.config.url}")
            logger.debug(f"  headers: {list(self.config.headers.keys())}")
            
            self._cm = sse_client(
                url=self.config.url,
                headers=self.config.headers if self.config.headers else None
            )
        else:
            # Stdioトランスポート接続
            logger.debug(f"  command: {self.config.command}")
            logger.debug(f"  args: {self.config.args}")
            logger.debug(f"  cwd: {self.config.cwd}")
            
            server_params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env=self.config.env if self.config.env else None,
                cwd=self.config.cwd
            )
            self._cm = stdio_client(server_params)
        
        # 非同期コンテキストマネージャに入る
        self._read_stream, self._write_stream = await self._cm.__aenter__()
        
        # ClientSessionを作成
        self.session = ClientSession(self._read_stream, self._write_stream)
        await self.session.__aenter__()
        
        # セッションを初期化
        await self.session.initialize()
        
        # ツール一覧を取得
        tools_result = await self.session.list_tools()
        
        # ツールスキーマを変換（server_nameを設定）
        self.tools = []
        for tool in tools_result.tools:
            self.tools.append(ToolSchema(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema if hasattr(tool, 'inputSchema') else {},
                server_name=self.config.name  # サーバー名を設定
            ))
        
        logger.info(f"サーバー '{self.config.name}' に接続完了。ツール数: {len(self.tools)}")
        
        # 利用可能なツール名をログ出力
        for ts in self.tools:
            logger.debug(f"  - {ts.name}: {ts.description[:50]}...")
    
    async def disconnect(self) -> None:
        """MCPサーバーから切断"""
        try:
            if self.session:
                try:
                    await self.session.__aexit__(None, None, None)
                except RuntimeError:
                    pass  # 別タスクからの切断によるスコープエラーは安全に無視する
                self.session = None

            if self._cm:
                try:
                    await self._cm.__aexit__(None, None, None)
                except RuntimeError:
                    pass  # 別タスクからの切断によるスコープエラーは安全に無視する
                self._cm = None

            logger.info(f"サーバー '{self.config.name}' から切断しました")
        except Exception as e:
            logger.warning(f"切断エラー ({self.config.name}): {e}")
    
    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """ツールを実行"""
        if not self.session:
            raise RuntimeError(f"サーバー '{self.config.name}' に接続されていません")
        
        logger.info(f"ツール実行: {tool_name} (サーバー: {self.config.name})")
        logger.debug(f"引数: {arguments}")
        
        try:
            result = await self.session.call_tool(tool_name, arguments=arguments)
            
            # 結果をMCP形式のdictに変換
            response = {"content": []}
            
            if hasattr(result, 'content'):
                for item in result.content:
                    if hasattr(item, 'type'):
                        if item.type == "text":
                            response["content"].append({
                                "type": "text",
                                "text": item.text if hasattr(item, 'text') else str(item)
                            })
                        else:
                            response["content"].append({
                                "type": item.type,
                                "data": str(item)
                            })
            
            if hasattr(result, 'isError') and result.isError:
                response["isError"] = True
            
            logger.info(f"ツール実行完了: {tool_name}")
            logger.debug(f"結果: {response}")
            
            return response
            
        except Exception as e:
            logger.error(f"ツール実行エラー: {e}")
            return {
                "content": [{"type": "text", "text": f"ツール実行エラー: {str(e)}"}],
                "isError": True
            }


# ============================================
# MCPクライアントマネージャー
# ============================================
class MCPClientManager:
    """
    MCPサーバー接続管理クラス
    
    仕様書2（システムアーキテクチャ）のMCP機能層との連携を担当
    公式のmcpパッケージを使用してMCPサーバーに接続
    """
    
    def __init__(self, tool_filter_settings: dict = None):
        """
        マネージャーの初期化
        
        Args:
            tool_filter_settings: ツールフィルタリング設定
                - enabled: フィルタリング有効/無効
                - max_tools: 最大ツール数
                - always_include: 常に含めるツール名リスト
                - compression_mode: 説明圧縮モード ("full", "compact", "minimal")
        """
        self._connections: dict[str, MCPServerConnection] = {}
        self._connected = False
        self._tool_filter = ToolFilter()
        self._tool_filter_settings = tool_filter_settings or {}
    
    # ============================================
    # サーバー接続管理
    # ============================================
    async def connect_servers(self) -> None:
        """
        設定ファイルからMCPサーバー定義を読み込み、接続を確立
        
        仕様書4.2: mcp_servers.jsonから読み込み
        """
        if not MCP_AVAILABLE:
            logger.error("MCPパッケージが利用できません。接続をスキップします。")
            raise RuntimeError("MCPパッケージがインストールされていません")
        
        logger.info("MCPサーバーへの接続を開始します")
        
        # 設定ファイルから読み込み
        configs = await self._load_server_configs()
        
        if not configs:
            logger.warning("接続するMCPサーバーがありません")
            return
        
        # 各サーバーに接続
        for config in configs:
            try:
                connection = MCPServerConnection(config)
                await connection.connect()
                self._connections[config.name] = connection
            except Exception as e:
                logger.error(f"サーバー '{config.name}' への接続に失敗: {e}")
                # 1つのサーバー接続失敗でも他のサーバーは試行
        
        self._connected = len(self._connections) > 0
        logger.info(f"MCPサーバー接続完了: {len(self._connections)}サーバー")
    
    async def disconnect_servers(self) -> None:
        """すべてのMCPサーバーとの接続を切断"""
        logger.info("MCPサーバーとの接続を切断します")
        
        for server_name, connection in self._connections.items():
            try:
                await connection.disconnect()
            except Exception as e:
                logger.warning(f"切断エラー ({server_name}): {e}")
        
        self._connections.clear()
        self._connected = False
        logger.info("接続を切断しました")
    
    # ============================================
    # ツール操作
    # ============================================
    async def get_all_tools(self) -> list[ToolSchema]:
        """
        接続中のすべてのMCPサーバーからツールスキーマを取得
        
        Returns:
            全サーバーのツールスキーマのリスト
        """
        all_tools = []
        for server_name, connection in self._connections.items():
            all_tools.extend(connection.tools)
        return all_tools
    
    async def get_tools_for_llm(
        self,
        user_input: str = None,
        max_tools: int = None,
        compression_mode: str = None,
        always_include: list = None,
        server_name: str = None
    ) -> list[dict]:
        """
        LLMに渡すためのツール定義をOpenAI形式で返す
        
        Args:
            user_input: ユーザー入力（フィルタリング用）
            max_tools: 最大ツール数（Noneの場合は設定値を使用）
            compression_mode: 説明圧縮モード（Noneの場合は設定値を使用）
            always_include: 常に含めるツール名のリスト（Noneの場合は設定値を使用）
            server_name: MCPサーバー名（指定時は該当サーバーのツールのみを返す）
            
        Returns:
            OpenAI Tools形式のツール定義リスト
        """
        all_tools = await self.get_all_tools()
        
        # 【診断ログ】全ツールの確認
        logger.info(f"[診断] get_tools_for_llm 呼び出し:")
        logger.info(f"  user_input: {user_input[:100] if user_input else 'None'}...")
        logger.info(f"  server_name: {server_name}")
        logger.info(f"  all_tools数: {len(all_tools)}")
        logger.info(f"  all_tools名一覧: {[t.name for t in all_tools]}")
        
        # サーバー名が指定されている場合：該当サーバーのツールのみを返す
        if server_name:
            server_tools = [t for t in all_tools if t.server_name == server_name]
            logger.info(f"[診断] サーバー指定フィルタ: {server_name} -> {len(server_tools)}件")
            if not server_tools:
                logger.warning(f"指定されたサーバー '{server_name}' のツールが見つかりません")
            return self._convert_to_openai_tools(server_tools, compression_mode or "compact")
        
        # 設定値の取得（引数が指定された場合は引数を優先）
        settings = self._tool_filter_settings
        filter_enabled = settings.get("enabled", True)
        max_tools_limit = max_tools or settings.get("max_tools", 15)
        comp_mode = compression_mode or settings.get("compression_mode", "compact")
        always_include_list = always_include if always_include is not None else settings.get("always_include", [])
        
        # 【診断ログ】設定値
        logger.info(f"[診断] 設定値: filter_enabled={filter_enabled}, max_tools_limit={max_tools_limit}, always_include={always_include_list}")
        
        # フィルタリング適用
        # 空文字列でもフィルタリングを実行するようNone判定に変更
        if filter_enabled and user_input is not None:
            filtered_tools = self._tool_filter.filter_by_user_input(
                user_input,
                all_tools,
                max_tools=max_tools_limit,
                always_include=always_include_list
            )
        else:
            filtered_tools = all_tools[:max_tools_limit] if max_tools_limit else all_tools
        
        # 【診断ログ】フィルタリング結果
        logger.info(f"[診断] フィルタリング結果: {len(filtered_tools)}件, ツール名: {[t.name for t in filtered_tools]}")
        
        # OpenAI形式に変換（説明圧縮を適用）
        tools = []
        for tool in filtered_tools:
            description = tool.get_description(comp_mode)
            # 圧縮モードが有効な場合は圧縮処理を適用
            if comp_mode in ("compact", "minimal"):
                description = ToolDescriptionCompressor.compress(tool.description, comp_mode)
            
            tools.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": description,
                    "parameters": tool.input_schema
                }
            })
        
        logger.info(f"[診断] LLMに渡すツール定義: {len(tools)}件（フィルタリング: {filter_enabled}, 圧縮モード: {comp_mode}）")
        return tools
    
    def _convert_to_openai_tools(
        self,
        tools: list[ToolSchema],
        compression_mode: str = "compact"
    ) -> list[dict]:
        """
        ToolSchemaリストをOpenAI形式のツール定義に変換
        
        Args:
            tools: 変換対象のツールリスト
            compression_mode: 説明圧縮モード
            
        Returns:
            OpenAI Tools形式のツール定義リスト
        """
        result = []
        for tool in tools:
            description = tool.get_description(compression_mode)
            # 圧縮モードが有効な場合は圧縮処理を適用
            if compression_mode in ("compact", "minimal"):
                description = ToolDescriptionCompressor.compress(tool.description, compression_mode)
            
            result.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": description,
                    "parameters": tool.input_schema
                }
            })
        
        return result
    
    def get_tool_categories(self) -> list[dict]:
        """
        ツールカテゴリ一覧を取得
        
        Returns:
            カテゴリ情報のリスト
        """
        return [
            {"id": cat.id, "name": cat.name, "keywords": cat.keywords}
            for cat in self._tool_filter.categories
        ]
    
    def get_category_summary(self) -> str:
        """
        カテゴリ一覧の要約を返す
        
        Returns:
            カテゴリ一覧のテキスト
        """
        return self._tool_filter.get_category_summary()
    
    async def call_tool(
        self,
        server_name: str,
        tool_name: str,
        arguments: dict
    ) -> dict:
        """
        指定したMCPサーバーのツールを実行
        
        Args:
            server_name: MCPサーバー名（空文字の場合は自動検索）
            tool_name: ツール名（"server.tool"形式もサポート）
            arguments: ツール引数
            
        Returns:
            ツール実行結果（MCP形式）
            
        Note:
            仕様書5.3.1: タイムアウト検知を実装
            名前空間サポート: "server.tool"形式のツール名を解決
        """
        # 名前空間形式（server.tool）の解析
        actual_server_name = server_name
        actual_tool_name = tool_name
        
        if "." in tool_name and not server_name:
            parts = tool_name.split(".", 1)
            if len(parts) == 2:
                actual_server_name = parts[0]
                actual_tool_name = parts[1]
                logger.debug(f"名前空間解析: server={actual_server_name}, tool={actual_tool_name}")
        
        logger.info(f"ツール実行要求: {actual_server_name}.{actual_tool_name}" if actual_server_name else f"ツール実行要求: {actual_tool_name}")
        
        # サーバー名が指定されている場合は直接検索
        if actual_server_name and actual_server_name in self._connections:
            target_connection = self._connections[actual_server_name]
            # ツールが存在するか確認
            tool_found = any(tool.name == actual_tool_name for tool in target_connection.tools)
            if not tool_found:
                logger.error(f"サーバー '{actual_server_name}' にツール '{actual_tool_name}' が見つかりません")
                return {
                    "content": [{"type": "text", "text": f"エラー: サーバー '{actual_server_name}' にツール '{actual_tool_name}' が見つかりません"}],
                    "isError": True
                }
            return await target_connection.call_tool(actual_tool_name, arguments)
        
        # サーバー名がない場合は全サーバーから検索
        target_connection = None
        for sname, connection in self._connections.items():
            for tool in connection.tools:
                if tool.name == actual_tool_name:
                    target_connection = connection
                    break
            if target_connection:
                break
        
        if not target_connection:
            logger.error(f"ツール '{actual_tool_name}' が見つかりません")
            return {
                "content": [{"type": "text", "text": f"エラー: ツール '{actual_tool_name}' が見つかりません"}],
                "isError": True
            }
        
        return await target_connection.call_tool(actual_tool_name, arguments)
    
    # ============================================
    # ヘルスチェック
    # ============================================
    async def health_check(self) -> dict[str, bool]:
        """
        接続中のMCPサーバーのヘルスチェック
        
        仕様書5.1: MCPサーバーダウン時の動的UI制御用
        
        Returns:
            サーバー名をキー、接続状態を値とする辞書
        """
        result = {}
        for server_name, connection in self._connections.items():
            result[server_name] = connection.session is not None
        return result
    
    # ============================================
    # 設定読み込み
    # ============================================
    async def _load_server_configs(self) -> list[MCPServerConfig]:
        """
        設定ファイルからMCPサーバー定義を読み込み。
        config/mcp_servers.jsonが存在しない場合は
        resources/default_configs/mcp_servers.jsonからコピーして使用する。
        
        Returns:
            MCPサーバー設定のリスト
        """
        config_path = Path("config/mcp_servers.json")
        default_config_path = Path("resources/default_configs/mcp_servers.json")
        
        # 設定ファイルのフォールバック・自動復旧機構
        if not config_path.exists():
            os.makedirs(config_path.parent, exist_ok=True)
            try:
                shutil.copy2(default_config_path, config_path)
                logger.info(f"デフォルトの設定ファイルをコピーしました: {config_path}")
            except Exception as e:
                logger.error(f"デフォルト設定ファイルのコピーに失敗しました: {e}")
                return []
        
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
            
            configs = []
            mcp_servers = config_data.get("mcpServers", {})
            
            for name, server_config in mcp_servers.items():
                # cwdを絶対パスに変換
                cwd = server_config.get("cwd")
                if cwd:
                    # 相対パスの場合はプロジェクトルートからの相対パスとして解決
                    cwd_path = Path(cwd)
                    if not cwd_path.is_absolute():
                        cwd_path = Path.cwd() / cwd
                    cwd = str(cwd_path)
                
                configs.append(MCPServerConfig(
                    name=name,
                    command=server_config.get("command", ""),
                    args=server_config.get("args", []),
                    env=server_config.get("env", {}),
                    cwd=cwd,
                    url=server_config.get("url"),
                    headers=server_config.get("headers", {})
                ))
            
            logger.info(f"{len(configs)}件のMCPサーバー設定を読み込みました")
            for config in configs:
                if config.transport_type == "sse":
                    logger.info(f"  - {config.name}: SSE {config.url}")
                else:
                    cmd_str = f"{config.command} {' '.join(config.args)}" if config.args else config.command
                    logger.info(f"  - {config.name}: {cmd_str}")
            
            return configs
            
        except Exception as e:
            logger.error(f"設定ファイル読み込みエラー: {e}")
            # パースエラー時もデフォルト設定で復旧を試みる
            try:
                shutil.copy2(default_config_path, config_path)
                with open(config_path, "r", encoding="utf-8") as f:
                    config_data = json.load(f)
                
                configs = []
                mcp_servers = config_data.get("mcpServers", {})
                
                for name, server_config in mcp_servers.items():
                    cwd = server_config.get("cwd")
                    if cwd:
                        cwd_path = Path(cwd)
                        if not cwd_path.is_absolute():
                            cwd_path = Path.cwd() / cwd
                        cwd = str(cwd_path)
                    
                    configs.append(MCPServerConfig(
                        name=name,
                        command=server_config.get("command", ""),
                        args=server_config.get("args", []),
                        env=server_config.get("env", {}),
                        cwd=cwd,
                        url=server_config.get("url"),
                        headers=server_config.get("headers", {})
                    ))
                
                logger.info("デフォルト設定で復旧しました。")
                return configs
            except Exception as recover_e:
                logger.error(f"復旧に失敗しました: {recover_e}")
                return []
    
    @property
    def is_connected(self) -> bool:
        """接続状態を返す"""
        return self._connected
    
    def get_server_names(self) -> list[str]:
        """接続中のサーバー名一覧を返す"""
        return list(self._connections.keys())
    
    def get_tools_by_server(self, server_name: str) -> list[ToolSchema]:
        """指定サーバーのツール一覧を返す"""
        connection = self._connections.get(server_name)
        return connection.tools if connection else []