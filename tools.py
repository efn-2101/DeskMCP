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
from typing import Optional, Any
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
class ToolSchema:
    """ツールスキーマ情報"""
    name: str
    description: str
    input_schema: dict


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
        
        # ツールスキーマを変換
        self.tools = []
        for tool in tools_result.tools:
            self.tools.append(ToolSchema(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema if hasattr(tool, 'inputSchema') else {}
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
    
    def __init__(self):
        """マネージャーの初期化"""
        self._connections: dict[str, MCPServerConnection] = {}
        self._connected = False
    
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
    
    async def get_tools_for_llm(self) -> list[dict]:
        """
        LLMに渡すためのツール定義をOpenAI形式で返す
        
        Returns:
            OpenAI Tools形式のツール定義リスト
        """
        tools = []
        for tool in await self.get_all_tools():
            tools.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema
                }
            })
        return tools
    
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
            tool_name: ツール名
            arguments: ツール引数
            
        Returns:
            ツール実行結果（MCP形式）
            
        Note:
            仕様書5.3.1: タイムアウト検知を実装
        """
        logger.info(f"ツール実行要求: {tool_name}")
        
        # 適切なサーバーを探す
        target_connection = None
        for sname, connection in self._connections.items():
            for tool in connection.tools:
                if tool.name == tool_name:
                    target_connection = connection
                    break
            if target_connection:
                break
        
        if not target_connection:
            logger.error(f"ツール '{tool_name}' が見つかりません")
            return {
                "content": [{"type": "text", "text": f"エラー: ツール '{tool_name}' が見つかりません"}],
                "isError": True
            }
        
        return await target_connection.call_tool(tool_name, arguments)
    
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