"""
DeskMCP Agent Layer - 自律エージェントロジック
==============================================
責務:
- 自律ループ（推論 → ツール実行判定 → ツール実行 → 履歴追加 → 推論）の実装
- 履歴（messages）の手動管理
- ツール出力のPruning（剪定）処理
- Chainlit StepによるUI更新
"""

import json
import asyncio
import logging
import os
import shutil
import ipaddress
import base64
from typing import AsyncGenerator, Optional, Any
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse
import httpx

import chainlit as cl

from tools import MCPClientManager

logger = logging.getLogger(__name__)


# ============================================
# ツール実行承認設定
# ============================================
# 承認が必要なツール名のキーワード（小文字で判定）
DANGEROUS_KEYWORDS = ["create", "update", "delete", "write", "remove", "post", "put", "add", "archive", "clear", "drop", "move"]

# 安全なツールプレフィックス（DANGEROUS_KEYWORDSより優先で承認不要と判定）
# add_ を追加: add_task, add_tasks_bulk 等はタスク登録・追加で破壊的操作ではない
SAFE_TOOL_PREFIXES = ["get_", "list_", "check_", "read_", "fetch_", "search_", "query_", "status", "add_"]

# ポーリング系ツールのプレフィックス（ループ検知の閾値を緩和する対象）
SAFE_POLLING_PREFIXES = ["get_", "list_", "check_", "status", "read_", "fetch_", "query_"]


# ============================================
# 設定読み込み
# ============================================
def load_system_config() -> dict:
    """
    システム設定ファイルを読み込む。
    config/system_config.jsonが存在しない場合は
    resources/default_configs/system_config.jsonからコピーして使用する。
    
    Returns:
        dict: 設定内容
    """
    config_path = Path("config/system_config.json")
    default_config_path = Path("resources/default_configs/system_config.json")
    
    # 設定ファイルのフォールバック・自動復旧機構
    if not config_path.exists():
        os.makedirs(config_path.parent, exist_ok=True)
        try:
            shutil.copy2(default_config_path, config_path)
            logger.info(f"デフォルトの設定ファイルをコピーしました: {config_path}")
        except Exception as e:
            logger.error(f"デフォルト設定ファイルのコピーに失敗しました: {e}")
            return _get_default_config()
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"設定ファイル読み込みエラー: {e}")
        # パースエラー時もデフォルト設定で復旧を試みる
        try:
            shutil.copy2(default_config_path, config_path)
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            logger.info("デフォルト設定で復旧しました。")
            return config
        except Exception as recover_e:
            logger.error(f"復旧に失敗しました: {recover_e}")
            return _get_default_config()


def _get_default_config() -> dict:
    """デフォルト設定を返す（新スキーマ: シンプル版）"""
    return {
        "llm_settings": {
            "provider": "ollama",
            "base_url": "http://localhost:11434/v1",
            "model_name": "gemma3:latest",
            "api_key": "optional_key_here",
            "temperature": 0.2,
            "max_tokens": 32768
        },
        "context_management": {
            "max_context_tokens": 128000,
            "tool_result_max_chars": 4000
        },
        "agent_safeguards": {
            "max_repeated_loops": 3,
            "max_iterations": 10,
            "inference_timeout_seconds": 180,
            "tool_execution_timeout_seconds": 60,
            "max_llm_retries": 3
        },
        "tool_filter_settings": {
            "enabled": True,
            "max_tools": 15,
            "always_include": ["get_server_info"],
            "compression_mode": "compact"
        },
        "system_prompt_settings": {
            "use_enhanced_prompt": True,
            "include_tool_guidelines": True
        },
        "system_prompt": "あなたは親切で有能なAIアシスタントです。"
    }


# ============================================
# プロキシバイパス設定ヘルパー関数
# ============================================
def get_proxy_bypass_hosts() -> set:
    """プロキシバイパス対象のホスト名セットを取得
    
    以下の順序で設定をマージ:
    1. デフォルトのローカルホスト
    2. NO_PROXY/no_proxy環境変数
    3. system_config.jsonのproxy_bypass_hosts
    """
    bypass_hosts = {"localhost", "127.0.0.1", "::1"}
    
    # 環境変数から取得
    no_proxy = os.environ.get("NO_PROXY", "") or os.environ.get("no_proxy", "")
    if no_proxy:
        bypass_hosts.update(h.strip() for h in no_proxy.split(",") if h.strip())
    
    # 設定ファイルから取得
    config_path = Path("config/system_config.json")
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
            hosts = config.get("network_settings", {}).get("proxy_bypass_hosts", [])
            bypass_hosts.update(hosts)
        except Exception:
            pass
    
    return bypass_hosts


def is_private_ip(hostname: str) -> bool:
    """プライベートIPアドレスかどうかを判定
    
    プライベートIP範囲:
    - 10.0.0.0/8
    - 172.16.0.0/12
    - 192.168.0.0/16
    """
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_private
    except ValueError:
        return False


def should_bypass_proxy(hostname: str) -> bool:
    """プロキシをバイパスすべきかどうかを判定"""
    if not hostname:
        return False
    
    bypass_hosts = get_proxy_bypass_hosts()
    
    # ホスト名がバイパスリストに含まれるか
    if hostname in bypass_hosts:
        return True
    
    # .localドメイン
    if hostname.endswith(".local"):
        return True
    
    # ワイルドカードマッチ (*.example.com形式)
    for pattern in bypass_hosts:
        if pattern.startswith("*."):
            domain = pattern[2:]
            if hostname.endswith(domain) or hostname == domain[1:]:
                return True
    
    # プライベートIPアドレス
    if is_private_ip(hostname):
        return True
    
    return False


# ============================================
# 強化版システムプロンプト
# ============================================
ENHANCED_SYSTEM_PROMPT_TEMPLATE = """
あなたは親切で有能なAIアシスタントです。ユーザーの質問に丁寧かつ正確に回答してください。

## 行動規範

1. **応答フォーマット**:
   - コードや設定値は必ずMarkdownのコードブロック（```）で囲んでください
   - 表形式のデータは**必ずMarkdownテーブル**を使用してください
   - **HTMLテーブル（`<table>`タグ等）は絶対に使用しないでください**
   - 箇条書きは `- ` または `1. ` を使用してください
   - 重要な注意事項は **太字** で強調してください

2. **ツール使用の絶対原則（最優先）**:
    - **【最重要】ユーザーの要求にタスク操作（追加・一覧・検索・変更・削除・完了）やデータ操作が含まれる場合、必ず対応するツールを呼び出してください**
    - **【最重要】ツールが利用可能な場合は、自分の知識や記憶に頼らず、必ずツールを使用して情報を取得・操作してください**
    - **【最重要】絶対にツールを使わずに「処理しました」「完了しました」と答えることは避けてください。自然言語での説明だけで済ませないでください**
    - **【最重要】ツールを使わずに「〜は提供されていません」「〜はできません」と断定的に否定しないでください。まずツールを試行してください**
    - ツール実行結果に基づいて回答を生成してください。推測や想像で答えないでください
    - ツール実行結果が空やエラーの場合のみ、代替案や説明を提供してください
    - 複数のツールを順次実行する必要がある場合は、1つずつ順番に呼び出してください
    - ツール呼び出しの引数は必須パラメータをすべて含めてください
    - ツール実行結果に基づいて、ユーザーに分かりやすく要約して報告してください
    - ツールが見つからない場合やエラーが発生した場合は、エラーの内容を説明し、代替案を提案してください
    - 最新情報や外部情報が必要な場合、検索・取得系ツールを優先的に使用してください

3. **ツール結果受取後の行動指針（ループ防止）**:
    - **ツール実行結果を受け取ったら、まずその内容を評価してください**
    - **結果が十分であれば、即座にユーザーに回答を生成してください。追加ツールを呼び出さないでください**
    - **同じツールを同じ引数で繰り返し呼び出さないでください**
    - **結果が不明確な場合は、ユーザーに確認するか、異なるツールを試行してください**
    - **ツール呼び出し後は必ず自然言語応答を生成し、ツール結果をユーザーに伝えてください**

3. **推論と回答**:
   - 推論過程をユーザーに見せる必要はありません。結論と根拠を簡潔に述べてください
   - 不確かな情報は「確信が持てません」と正直に伝えてください
   - ユーザーの質問に直接関係ない情報は省略してください

## ツール使用ガイドライン

利用可能なツールカテゴリと使い分け基準：

### タスク管理
- **作成**: `add_task` - 新しいタスクを登録
- **一覧**: `list_pending_tasks`（未完了のみ）, `list_all_tasks`（全て）
- **更新**: `update_task_*` - 各フィールドを個別に更新
- **完了**: `complete_task` - タスクを完了状態に
- **削除**: `delete_task`（完全削除）, `archive_task`（アーカイブ）

### 検索
- **キーワード検索**: `search_tasks` - 単純な部分一致検索
- **あいまい検索**: `fuzzy_search_tasks` - FTS5全文検索（関連度順）
- **意味検索**: `semantic_search_tasks` - エンベディング使用（意味的類似性）
- **高度な検索**: `search_tasks_advanced` - 複数条件・フィルタリング

### 一括操作
- `*_bulk` 系ツール - 複数タスクの一括処理

### ファイル操作
- `read_document_file` - メール・テキストファイルの読み込み

## サーバー別ツール選択ガイド

ユーザーが特定のサーバーとの通信確認や操作を求めた場合、適切なサーバーのツールを選択してください。

### Redmine（チケット管理）
- **通信確認・接続テスト**: `getIssues` または `getProjects` を使用
- **チケット検索**: `getIssues` を使用
- **プロジェクト一覧**: `getProjects` を使用
- **チケット詳細取得**: `getIssue` を使用

### DeskToDo（タスク管理）
- **通信確認**: `list_pending_tasks` を使用
- **タスク追加**: `add_task` を使用
- **タスク一覧**: `list_pending_tasks` または `list_all_tasks` を使用

### local-rag（ドキュメント検索）
- **通信確認**: `list_roots` または `list_documents` を使用
- **ドキュメント検索**: `search_documents` を使用

**重要**: ユーザーが「Redmine」と明示的に言及した場合、Redmineサーバーのツール（getIssues, getProjects等）を優先的に使用してください。

## 重要な判断基準

1. **検索ツールの選択**:
   - 曖昧な表現・うろ覚え → `fuzzy_search_tasks` または `semantic_search_tasks`
   - 明確なキーワード → `search_tasks`
   - 複雑な条件 → `search_tasks_advanced`

2. **一覧表示の選択**:
   - 日常的な確認 → `list_pending_tasks`
   - 履歴確認 → `list_all_tasks`
   - 期限切れ確認 → `get_overdue_tasks`

3. **削除の選択**:
   - 復元不要 → `delete_task`
   - 復元可能性あり → `archive_task`

## 現在のシステム時刻
{current_time}
"""


def deep_merge(base: dict, override: dict) -> dict:
    """辞書を深くマージ（プリセット適用用）"""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        elif key not in ["llm_presets", "active_preset"]:
            result[key] = value
    return result


# ============================================
# 動的システムプロンプト生成
# ============================================
class DynamicSystemPromptGenerator:
    """システムプロンプト動的生成クラス"""
    
    def __init__(self, mcp_manager):
        """
        初期化
        
        Args:
            mcp_manager: MCPClientManagerインスタンス
        """
        self.mcp_manager = mcp_manager
    
    async def generate(self, base_prompt: str = None) -> str:
        """
        接続済みサーバのツール一覧から動的にプロンプトを生成
        
        Args:
            base_prompt: ベースとなるプロンプト（Noneの場合はデフォルト）
            
        Returns:
            生成されたシステムプロンプト
        """
        tools = await self.mcp_manager.get_all_tools()
        server_names = self.mcp_manager.get_server_names()
        
        # サーバ別にツールを分類
        tools_by_server = {}
        for tool in tools:
            if tool.server_name not in tools_by_server:
                tools_by_server[tool.server_name] = []
            tools_by_server[tool.server_name].append(tool)
        
        # プロンプト構築
        sections = []
        
        # ベースプロンプト
        if base_prompt:
            sections.append(base_prompt)
        else:
            sections.append("あなたは親切で有能なAIアシスタントです。")
        
        # 応答フォーマット指示
        sections.append("\n## 応答フォーマット\n")
        sections.append("- 表形式のデータは**必ずMarkdownテーブル**を使用してください\n")
        sections.append("- **HTMLテーブル（`<table>`タグ等）は絶対に使用しないでください**\n")
        sections.append("- コードや設定値は必ずMarkdownのコードブロック（```）で囲んでください\n")
        
        # ツール使用ガイドライン
        sections.append("\n## 利用可能なMCPサーバとツール\n")
        
        for server_name in server_names:
            server_tools = tools_by_server.get(server_name, [])
            if not server_tools:
                continue
            
            sections.append(f"\n### {server_name}\n")
            
            # ツールをカテゴリ別に分類
            categories = self._categorize_tools(server_tools)
            for category_name, category_tools in categories.items():
                sections.append(f"- **{category_name}**: ")
                tool_names = [f"`{t.name}`" for t in category_tools[:5]]
                sections.append(", ".join(tool_names))
                if len(category_tools) > 5:
                    sections.append(f" 他{len(category_tools) - 5}件")
                sections.append("\n")
        
        # 現在時刻
        current_time = datetime.now(timezone(timedelta(hours=9))).strftime('%Y年%m月%d日 %H時%M分%S秒')
        sections.append(f"\n## 現在のシステム時刻\n{current_time}\n")
        
        return "".join(sections)
    
    def _categorize_tools(self, tools: list) -> dict:
        """ツールをカテゴ���別に分類"""
        categories = {
            "作成・追加": [],
            "参照・検索": [],
            "更新・変更": [],
            "削除・アーカイブ": [],
            "その他": []
        }
        
        for tool in tools:
            name_lower = tool.name.lower()
            if any(kw in name_lower for kw in ["add", "create", "new", "追加", "作成", "register"]):
                categories["作成・追加"].append(tool)
            elif any(kw in name_lower for kw in ["get", "list", "search", "find", "一覧", "検索", "fetch", "query"]):
                categories["参照・検索"].append(tool)
            elif any(kw in name_lower for kw in ["update", "change", "modify", "更新", "変更"]):
                categories["更新・変更"].append(tool)
            elif any(kw in name_lower for kw in ["delete", "remove", "archive", "削除", "アーカイブ"]):
                categories["削除・アーカイブ"].append(tool)
            else:
                categories["その他"].append(tool)
        
        # 空カテゴリを除外
        return {k: v for k, v in categories.items() if v}


# ============================================
# データクラス定義
# ============================================
@dataclass
class AgentConfig:
    """エージェント設定"""
    # セーフガード設定
    max_repeated_loops: int = 3
    max_iterations: int = 10
    inference_timeout_seconds: int = 180
    tool_execution_timeout_seconds: int = 60
    max_llm_retries: int = 3
    
    # コンテキスト管理設定（シンプル化: 1つのmax_context_tokensで統一）
    max_context_tokens: int = 128000  # コンテキスト全体の上限
    tool_result_max_chars: int = 100000  # ツール結果安全弁閾値（1ツール結果の最大文字数、超過時のみ切り詰め）
    
    # ツール定義予算設定
    tool_definition_budget_ratio: float = 0.25  # max_context_tokensに対するツール定義の割合（デフォルト25%）
    
    # 内部計算用（max_context_tokensから自動導出、直接設定不要）
    hard_limit_tokens: int = field(init=False)
    soft_limit_tokens: int = field(init=False)
    tool_definition_budget_tokens: int = field(init=False)
    message_history_budget_tokens: int = field(init=False)
    
    # ツール結果Pruning設定（後方互換、内部計算用）
    tool_result_read_max_chars: int = field(init=False)
    tool_result_write_max_chars: int = field(init=False)
    tool_result_info_max_chars: int = field(init=False)
    tool_result_default_max_chars: int = field(init=False)
    pruning_soft_limit_tokens: int = field(init=False)
    
    # LLM設定
    base_url: str = "http://localhost:11434/v1"
    model_name: str = "gemma3:latest"
    api_key: str = "optional_key_here"
    temperature: float = 0.2
    max_tokens: int = 32768
    
    # システムプロンプト設定
    system_prompt: str = "あなたは親切で有能なAIアシスタントです。"
    use_enhanced_prompt: bool = True
    include_tool_guidelines: bool = True
    
    # ツールフィルタ設定
    tool_filter_enabled: bool = True
    max_tools: int = 30
    always_include: list = field(default_factory=lambda: ["get_server_info"])
    compression_mode: str = "compact"  # full, compact, minimal
    
    def __post_init__(self):
        """派生値を自動計算"""
        # コンテキスト予算の自動配分
        self.hard_limit_tokens = self.max_context_tokens
        self.soft_limit_tokens = int(self.max_context_tokens * 0.75)
        # ツール定義予算: max_context_tokensの指定割合（デフォルト25%）
        self.tool_definition_budget_tokens = int(self.max_context_tokens * self.tool_definition_budget_ratio)
        self.message_history_budget_tokens = int(self.max_context_tokens * 0.5)
        
        # ツール結果Pruningの自動配分（後方互換・内部計算用）
        # 即座切り詰めは廃止され、安全弁のみ使用
        self.tool_result_read_max_chars = self.tool_result_max_chars
        self.tool_result_write_max_chars = self.tool_result_max_chars
        self.tool_result_info_max_chars = self.tool_result_max_chars
        self.tool_result_default_max_chars = self.tool_result_max_chars
        self.pruning_soft_limit_tokens = self.tool_result_max_chars  # MessageHistoryの即座Pruning閾値（大きな値に）
    
    @classmethod
    def from_dict(cls, config: dict) -> "AgentConfig":
        """設定辞書からAgentConfigを作成（旧設定からの自動変換対応）"""
        llm_settings = config.get("llm_settings", {})
        context_mgmt = config.get("context_management", {})
        safeguards = config.get("agent_safeguards", {})
        tool_filter = config.get("tool_filter_settings", {})
        prompt_settings = config.get("system_prompt_settings", {})
        
        # --- 新設定（シンプル版）の読み込み ---
        max_context_tokens = context_mgmt.get("max_context_tokens")
        tool_result_max_chars = context_mgmt.get("tool_result_max_chars")
        tool_definition_budget_ratio = context_mgmt.get("tool_definition_budget_ratio")
        
        # --- 旧設定からの自動変換（後方互換） ---
        if max_context_tokens is None:
            # 旧: hard_limit_tokens / soft_limit_tokens から変換
            old_hard = context_mgmt.get("hard_limit_tokens")
            old_soft = context_mgmt.get("soft_limit_tokens")
            if old_hard is not None:
                max_context_tokens = old_hard
                logger.info(f"旧設定 'hard_limit_tokens={old_hard}' を 'max_context_tokens' に自動変換しました")
            elif old_soft is not None:
                max_context_tokens = int(old_soft / 0.75)
                logger.info(f"旧設定 'soft_limit_tokens={old_soft}' を 'max_context_tokens={max_context_tokens}' に自動変換しました")
            else:
                max_context_tokens = 128000  # デフォルト
        
        if tool_result_max_chars is None:
            # 旧: tool_result_pruning から変換
            tool_pruning = context_mgmt.get("tool_result_pruning", {})
            old_read = tool_pruning.get("read_max_chars")
            if old_read is not None:
                tool_result_max_chars = old_read
                logger.info(f"旧設定 'tool_result_pruning.read_max_chars={old_read}' を 'tool_result_max_chars' に自動変換しました")
            else:
                tool_result_max_chars = 4000  # デフォルト
        
        if tool_definition_budget_ratio is None:
            tool_definition_budget_ratio = 0.25  # デフォルト25%
        
        return cls(
            # セーフガード
            max_repeated_loops=safeguards.get("max_repeated_loops", 3),
            max_iterations=safeguards.get("max_iterations", 10),
            inference_timeout_seconds=safeguards.get("inference_timeout_seconds", 180),
            tool_execution_timeout_seconds=safeguards.get("tool_execution_timeout_seconds", 60),
            max_llm_retries=safeguards.get("max_llm_retries", 3),
            # コンテキスト管理（シンプル版）
            max_context_tokens=max_context_tokens,
            tool_result_max_chars=tool_result_max_chars,
            tool_definition_budget_ratio=tool_definition_budget_ratio,
            # LLM設定
            base_url=llm_settings.get("base_url", "http://localhost:11434/v1"),
            model_name=llm_settings.get("model_name", "gemma3:latest"),
            api_key=llm_settings.get("api_key", "optional_key_here"),
            temperature=llm_settings.get("temperature", 0.2),
            max_tokens=llm_settings.get("max_tokens", 4096),
            # システムプロンプト
            system_prompt=config.get("system_prompt", "あなたは親切で有能なAIアシスタントです。"),
            use_enhanced_prompt=prompt_settings.get("use_enhanced_prompt", True),
            include_tool_guidelines=prompt_settings.get("include_tool_guidelines", True),
            # ツールフィルタ
            tool_filter_enabled=tool_filter.get("enabled", True),
            max_tools=tool_filter.get("max_tools", 30),
            always_include=tool_filter.get("always_include", ["get_server_info"]),
            compression_mode=tool_filter.get("compression_mode", "compact")
        )


@dataclass
class ToolCall:
    """ツール呼び出し情報"""
    id: str
    name: str
    arguments: dict


@dataclass
class LLMResponse:
    """LLM応答"""
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    thinking: str = ""  # <thinking>タグ内の推論内容


# ============================================
# メッセージ履歴管理クラス
# ============================================
class MessageHistory:
    """
    メッセージ履歴管理クラス
    
    仕様書6.3に基づき、Chainlitの自動管理に任せず手動でリスト管理
    巨大なツール実行結果は「結果の要約」に置換するPruningを実装
    """
    
    def __init__(self, system_prompt: str = "",
                 hard_limit_tokens: int = 8192,
                 soft_limit_tokens: int = 6000,
                 message_history_budget_tokens: int = 2000,
                 pruning_soft_limit_tokens: int = 2000):
        """
        初期化
        
        Args:
            system_prompt: システムプロンプト
            hard_limit_tokens: ハードリミット（トークン数）
            soft_limit_tokens: ソフトリミット（トークン数）
            message_history_budget_tokens: メッセージ履歴の予算（トークン数）
            pruning_soft_limit_tokens: ツール結果Pruningのソフトリミット（トークン数）
        """
        self.messages: list[dict] = []
        self._system_prompt = system_prompt
        self._tool_call_history: list[dict] = []  # ループ検知用
        
        # コンテキスト予算
        self._hard_limit_tokens = hard_limit_tokens
        self._soft_limit_tokens = soft_limit_tokens
        self._message_history_budget_tokens = message_history_budget_tokens
        self._pruning_soft_limit_tokens = pruning_soft_limit_tokens
        
        # システムプロンプトを追加
        if system_prompt:
            self.messages.append({
                "role": "system",
                "content": system_prompt
            })
    
    def add_user_message(self, content: str | list) -> None:
        """
        ユーザーメッセージを追加
        
        Args:
            content: ユーザー入力テキスト、またはマルチモーダルcontentリスト
        """
        self.messages.append({
            "role": "user",
            "content": content
        })
        if isinstance(content, str):
            logger.debug(f"ユーザーメッセージ追加: {content[:50]}...")
        else:
            logger.debug(f"ユーザーメッセージ追加（マルチモーダル）: {len(content)}要素")
    
    def add_assistant_message(self, content: str) -> None:
        """
        アシスタントメッセージを追加
        
        Args:
            content: アシスタントの応答テキスト
        """
        self.messages.append({
            "role": "assistant",
            "content": content
        })
        logger.debug(f"アシスタントメッセージ追加: {content[:50]}...")
    
    def add_tool_call_message(self, tool_calls: list[ToolCall]) -> None:
        """
        ツール呼び出しメッセージを追加
        
        Args:
            tool_calls: ツール呼び出しリスト
        """
        # OpenAI形式のtool_callsを作成
        openai_tool_calls = []
        for tc in tool_calls:
            openai_tool_calls.append({
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                }
            })
            # ループ検知用に履歴に記録
            self._tool_call_history.append({
                "name": tc.name,
                "arguments": tc.arguments
            })
        
        self.messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": openai_tool_calls
        })
        logger.debug(f"ツール呼び出しメッセージ追加: {len(tool_calls)}件")
    
    def add_tool_result(self, tool_call_id: str, tool_name: str, raw_result: dict, summary: str) -> None:
        """
        ツール実行結果を追加
        
        ツール結果は原則そのまま保持。コンテキスト全体の圧縮は
        get_context_for_llm() -> _trim_to_budget() で行う。
        
        Args:
            tool_call_id: ツール呼び出しID
            tool_name: ツール名
            raw_result: 生のツール実行結果
            summary: 要約テキスト（安全弁適用済みのテキスト）
        """
        # summaryは既にAgent._summarize_tool_resultで安全弁処理済み
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": summary
        })
        logger.debug(f"ツール結果追加: {tool_name} -> {summary[:50]}...")
    
    def _extract_content(self, raw_result: dict) -> str:
        """生の結果からコンテンツを抽出"""
        if isinstance(raw_result, dict):
            # MCP形式のコンテンツを抽出
            content_list = raw_result.get("content", [])
            if isinstance(content_list, list):
                texts = []
                for item in content_list:
                    if isinstance(item, dict) and item.get("type") == "text":
                        texts.append(item.get("text", ""))
                return "\n".join(texts)
            return str(raw_result)
        return str(raw_result)
    
    def _prune_content(self, content: str, summary: str) -> str:
        """
        コンテンツのPruning処理
        
        仕様書6.3: 即時OOMの防止（上限ガード）
        - ソフトリミット超過時は先頭保持の切り詰め
        - ハードリミットはprune_large_contentで対応
        
        Args:
            content: 対象コンテンツ
            summary: 要約テキスト（後方互換のため残すが使用しない）
        """
        # 簡易的なトークン数推定（日本語は約2文字/トークン、英語は約4文字/トークン）
        estimated_tokens = len(content) / 3
        
        # ソフトリミット超過時は先頭保持の切り詰め
        soft_limit_chars = self._pruning_soft_limit_tokens * 3  # ≈文字数
        
        if estimated_tokens > self._pruning_soft_limit_tokens:
            logger.info(f"コンテンツをPruning: {estimated_tokens:.0f}トークン -> {self._pruning_soft_limit_tokens}トークンに切り詰め")
            return content[:soft_limit_chars] + "\n... [コンテキスト予算のため省略]"
        
        return content
    
    def prune_large_content(self, content: str, max_tokens: int = 8192) -> str:
        """
        上限ガード: 想定外に巨大なレスポンスを事前に切り詰め
        
        仕様書6.3: 即時OOMの防止（上限ガード）
        
        Args:
            content: 対象のコンテンツ
            max_tokens: 最大トークン数
            
        Returns:
            切り詰められたコンテンツ
        """
        estimated_tokens = len(content) / 3
        
        if estimated_tokens > max_tokens:
            # 文字数ベースで切り詰め
            max_chars = max_tokens * 3
            truncated = content[:max_chars]
            return truncated + "\n... [コンテンツが切り詰められました]"
        
        return content
    
    def get_context_for_llm(self) -> list:
        """
        LLMに渡すためのコンテキストを取得（予算ベースのトリミング付き）
        
        優先度順にメッセージを保持:
        1. system プロンプト（最優先、常に保持）
        2. 最新の user メッセージ（現在のタスク）
        3. 最新の assistant メッセージ
        4. 古いメッセージから順に予算内で保持
        
        Returns:
            OpenAI形式のメッセージリスト
        """
        # 現在の総トークン数を推定
        total_tokens = self._estimate_total_tokens()
        
        # 予算内ならそのまま返す
        if total_tokens <= self._soft_limit_tokens:
            return self.messages.copy()
        
        # 予算超過時のトリミング
        return self._trim_to_budget()
    
    def _estimate_total_tokens(self) -> int:
        """メッセージ履歴の総トークン数を推定（日本語対応）"""
        total = 0
        for msg in self.messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self._estimate_text_tokens(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        total += self._estimate_text_tokens(item.get("text", ""))
                    elif isinstance(item, dict) and item.get("type") == "image_url":
                        # 画像のトークン数を推定（base64データサイズに基づく簡易推定）
                        image_url = item.get("image_url", {})
                        url = image_url.get("url", "") if isinstance(image_url, dict) else str(image_url)
                        if url.startswith("data:"):
                            # base64データ部分を抽出して推定
                            try:
                                b64_part = url.split(",")[-1]
                                # base64文字数 -> バイト数 -> ピクセル数（概算）
                                image_bytes = len(b64_part) * 3 // 4
                                # 画像トークン: 約1000トークン/画像（簡易推定）
                                total += 1000
                            except Exception:
                                total += 1000
                        else:
                            total += 1000
            
            # tool_callsのトークン数も推定
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                func = tc.get("function", {})
                total += self._estimate_text_tokens(func.get("name", ""))
                total += self._estimate_text_tokens(func.get("arguments", ""))
        
        return int(total)
    
    def _estimate_messages_tokens(self, messages: list) -> int:
        """メッセージリストのトークン数を推定（日本語対応）"""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += self._estimate_text_tokens(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        total += self._estimate_text_tokens(item.get("text", ""))
                    elif isinstance(item, dict) and item.get("type") == "image_url":
                        total += 1000  # 画像トークン概算
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                func = tc.get("function", {})
                total += self._estimate_text_tokens(func.get("name", ""))
                total += self._estimate_text_tokens(func.get("arguments", ""))
        return int(total)
    
    @staticmethod
    def _estimate_text_tokens(text: str) -> float:
        """
        テキストのトークン数を推定
        
        日本語: 約1.0文字/トークン（SentencePieceベースのモデルではCJKも1文字程度）
        英語: 約4文字/トークン
        混合テキスト: 文字種に応じて重み付け
        
        Note: Gemma3, Qwen, Llama3等のSentencePieceベースモデルでは、
        日本語文字は1文字≒1トークン程度になることが多い。
        過去の1.5文字/トークンはtiktoken等のBPEベースの推定値であり、
        ローカルLLMでは過小評価になりがちだった。
        """
        if not text:
            return 0.0
        
        # 日本語文字（CJK統合漢字、ひらがな、カタカナ等）をカウント
        import re
        japanese_chars = len(re.findall(r'[\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF\u3400-\u4DBF\u3000-\u303F]', text))
        other_chars = len(text) - japanese_chars
        
        # 日本語部分は1.0文字/トークン、それ以外は4文字/トークン
        return (japanese_chars / 1.0) + (other_chars / 4.0)
    
    def _summarize_message(self, msg: dict, aggressive: bool = False) -> dict | None:
        """
        メッセージを要約版に変換
        
        - tool メッセージ: 先頭500文字（通常）/ 100文字（積極的圧縮）に切り詰め
          または事実要約（ツール名と結果の要約）
        - assistant メッセージ: 先頭400文字に切り詰め
        - user メッセージ: 先頭400文字に切り詰め（マルチモーダルは要約しない）
        - tool_calls を含む assistant メッセージ: 要約しない（構造を保持）
        
        Args:
            msg: 要約対象メッセージ
            aggressive: Trueの場合、toolメッセージをより積極的に圧縮（事実要約）
        """
        role = msg.get("role", "")
        content = msg.get("content", "")
        
        # tool_calls を含む assistant メッセージは要約しない（構造保持）
        if role == "assistant" and msg.get("tool_calls"):
            return None
        
        # マルチモーダルcontent（リスト）は要約しない（構造を保持）
        if isinstance(content, list):
            return None
        
        if not isinstance(content, str):
            return None
        
        if role == "tool":
            if aggressive:
                # 積極的圧縮: ツール結果を事実要約
                tool_name = msg.get("name", "")
                fact_summary = self._summarize_tool_fact(tool_name, content)
                return {**msg, "content": fact_summary}
            else:
                limit = 500
                if len(content) > limit:
                    return {**msg, "content": content[:limit] + f"\n... [要約: 全{len(content)}文字]"}
        elif role == "assistant":
            if len(content) > 400:
                return {**msg, "content": content[:400] + "\n... [要約]"}
        elif role == "user":
            if len(content) > 400:
                return {**msg, "content": content[:400] + "\n... [要約]"}
        
        return None
    
    def _summarize_tool_fact(self, tool_name: str, content: str) -> str:
        """
        ツール実行結果を1行の事実に要約
        
        Args:
            tool_name: ツール名
            content: ツール実行結果のテキスト
            
        Returns:
            1行の事実要約
        """
        # JSON形式のエラーレスポンスの場合
        try:
            data = json.loads(content)
            if isinstance(data, dict) and data.get("status") == "error":
                return f"[要約] {tool_name}: エラー発生 ({data.get('error_code', 'UNKNOWN')})"
        except (json.JSONDecodeError, ValueError):
            pass
        
        # 行数で要約
        lines = content.strip().split('\n')
        if len(lines) <= 3:
            return f"[要約] {tool_name}: {content[:100]}"
        
        # 結果の種類を推測して要約
        content_lower = content.lower()
        if "件" in content or "count" in content_lower or "total" in content_lower:
            # 件数系結果
            return f"[要約] {tool_name}: {len(lines)}行の結果を取得"
        elif "success" in content_lower or "完了" in content or "完了しました" in content:
            return f"[要約] {tool_name}: 処理完了"
        else:
            return f"[要約] {tool_name}: {len(lines)}行の結果"
    
    def _trim_to_budget(self) -> list:
        """
        予算内に収まるようメッセージを段階的に圧縮
        
        戦略:
        1. system メッセージは常に保持
        2. 最新の user メッセージは常に保持
        3. 最新3往復分（最大6メッセージ）を保護
        4. 古いメッセージから順に、段階的に圧縮→削除
           - まず通常要約（先頭切り詰め）
           - それでも予算超過なら積極的要約（事実要約）
           - それでも超過なら削除
        5. 最後に hard_limit を適用
        
        【修正点】
        - breakを削除し、要約できたメッセージの次の古いメッセージも処理を継続
        - 積極圧縮のフラグ管理を廃止し、予算に基づいて各メッセージを個別に判断
        - 診断ログを強化
        """
        # メッセージを分類
        system_msgs = []
        other_msgs = []
        
        for msg in self.messages:
            if msg.get("role") == "system":
                system_msgs.append(msg)
            else:
                other_msgs.append(msg)
        
        # 最新の user メッセージのインデックスを特定
        latest_user_idx = None
        for i, msg in enumerate(other_msgs):
            if msg.get("role") == "user":
                latest_user_idx = i
        
        # 保護メッセージ（最新user以降の直近文脈）
        # tool_calls と tool result は OpenAI 互換履歴で対になるため、
        # 先頭から6件ではなく末尾側を優先して最新のツール結果を守る。
        protected_msgs = []
        if latest_user_idx is not None:
            latest_turn_msgs = other_msgs[latest_user_idx:]
            max_protected = 8
            protected_msgs = [latest_turn_msgs[0]]
            if len(latest_turn_msgs) > 1:
                protected_msgs.extend(latest_turn_msgs[-(max_protected - 1):])
            protected_msgs = self._expand_tool_pair_protection(other_msgs, protected_msgs)
        
        # 圧縮対象メッセージ（古い順）
        protected_ids = {id(m) for m in protected_msgs}
        compressible_msgs = [m for m in other_msgs if id(m) not in protected_ids]
        
        # 固定メッセージのトークン数
        fixed_tokens = self._estimate_messages_tokens(system_msgs)
        fixed_tokens += self._estimate_messages_tokens(protected_msgs)
        
        # 圧縮対象に使える予算
        remaining_budget = self._soft_limit_tokens - fixed_tokens
        
        # Phase 1: 古いメッセージから順に、段階的に圧縮
        processed_msgs = []
        for msg in compressible_msgs:
            msg_tokens = self._estimate_messages_tokens([msg])
            
            if remaining_budget >= msg_tokens:
                # 予算内ならそのまま追加
                processed_msgs.append(msg)
                remaining_budget -= msg_tokens
            else:
                # 予算不足: 通常要約を試行
                summarized = self._summarize_message(msg, aggressive=False)
                if summarized:
                    summarized_tokens = self._estimate_messages_tokens([summarized])
                    if remaining_budget >= summarized_tokens:
                        processed_msgs.append(summarized)
                        remaining_budget -= summarized_tokens
                        continue  # 次のメッセージへ（breakしない！）
                
                # 通常要約でも予算不足: 積極的要約を試行（toolメッセージのみ）
                if msg.get("role") == "tool":
                    aggressive_summary = self._summarize_message(msg, aggressive=True)
                    if aggressive_summary:
                        aggressive_tokens = self._estimate_messages_tokens([aggressive_summary])
                        if remaining_budget >= aggressive_tokens:
                            processed_msgs.append(aggressive_summary)
                            remaining_budget -= aggressive_tokens
                            continue  # 次のメッセージへ
                
                # それ以外は削除（ループを継続して次のメッセージを試行）
        
        # 結果を構築: system + 圧縮済み古いメッセージ + 保護メッセージ
        result = self._repair_tool_message_pairs(system_msgs + processed_msgs + protected_msgs)
        
        # Phase 2: hard_limit適用（それでも超過なら古いメッセージから削除）
        total_tokens = self._estimate_messages_tokens(result)
        if total_tokens > self._hard_limit_tokens:
            # protected_msgsの先頭がuserメッセージの場合はそれをlatest_userとして渡す
            latest_user_msg = protected_msgs[0] if protected_msgs and protected_msgs[0].get("role") == "user" else None
            result = self._enforce_hard_limit(result, system_msgs, latest_user_msg)
            result = self._repair_tool_message_pairs(result)
        
        # 診断ログ強化
        original_tokens = self._estimate_total_tokens()
        final_tokens = self._estimate_messages_tokens(result)
        removed_count = len(self.messages) - len(result)
        
        # 圧縮/削除されたメッセージの詳細をログ
        compressed_details = []
        for i, msg in enumerate(self.messages):
            if msg not in result:
                role = msg.get("role", "unknown")
                content_preview = ""
                if isinstance(msg.get("content"), str):
                    content_preview = msg["content"][:30].replace("\n", " ")
                compressed_details.append(f"[{i}]{role}:{content_preview}...")
        
        logger.info(
            f"コンテキスト圧縮: {original_tokens}→{final_tokens}トoken "
            f"({removed_count}件のメッセージを圧縮/削除, "
            f"予算: soft={self._soft_limit_tokens}, hard={self._hard_limit_tokens})"
        )
        if compressed_details:
            logger.debug(f"圧縮/削除されたメッセージ: {' | '.join(compressed_details[:10])}")
        
        return result
    
    def _expand_tool_pair_protection(self, all_msgs: list[dict], protected_msgs: list[dict]) -> list[dict]:
        """保護対象に含まれる tool_call/tool_result の対応相手も保護する"""
        protected_ids = {id(m) for m in protected_msgs}
        wanted_tool_call_ids = set()
        
        for msg in protected_msgs:
            if msg.get("role") == "tool" and msg.get("tool_call_id"):
                wanted_tool_call_ids.add(msg.get("tool_call_id"))
            for tc in msg.get("tool_calls", []) or []:
                if tc.get("id"):
                    wanted_tool_call_ids.add(tc.get("id"))
        
        if not wanted_tool_call_ids:
            return protected_msgs
        
        expanded = []
        for msg in all_msgs:
            include = id(msg) in protected_ids
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                ids = {tc.get("id") for tc in msg.get("tool_calls", []) if tc.get("id")}
                include = include or bool(ids & wanted_tool_call_ids)
            elif msg.get("role") == "tool":
                include = include or msg.get("tool_call_id") in wanted_tool_call_ids
            
            if include:
                expanded.append(msg)
        
        return expanded
    
    def _repair_tool_message_pairs(self, messages: list[dict]) -> list[dict]:
        """assistant(tool_calls) と tool(result) の片割れだけが残らないように整える"""
        tool_result_ids = {
            msg.get("tool_call_id")
            for msg in messages
            if msg.get("role") == "tool" and msg.get("tool_call_id")
        }
        
        kept = []
        pending_tool_ids = set()
        for msg in messages:
            role = msg.get("role")
            if role == "assistant" and msg.get("tool_calls"):
                call_ids = {tc.get("id") for tc in msg.get("tool_calls", []) if tc.get("id")}
                if call_ids and call_ids.issubset(tool_result_ids):
                    kept.append(msg)
                    pending_tool_ids.update(call_ids)
                else:
                    logger.debug("コンテキスト圧縮: 対応するtool結果がないassistant(tool_calls)を除外")
            elif role == "tool":
                tool_call_id = msg.get("tool_call_id")
                if tool_call_id in pending_tool_ids:
                    kept.append(msg)
                    pending_tool_ids.discard(tool_call_id)
                else:
                    logger.debug("コンテキスト圧縮: 対応するassistant(tool_calls)がないtool結果を除外")
            else:
                kept.append(msg)
        
        return kept
    
    def _enforce_hard_limit(self, messages: list, system_msgs: list, latest_user_msg: dict | None) -> list:
        """hard_limitを強制適用: system + latest_user以外の古いメッセージから削除"""
        # system_msgsとlatest_user_msgは固定
        fixed = system_msgs + ([latest_user_msg] if latest_user_msg else [])
        fixed_tokens = self._estimate_messages_tokens(fixed)
        
        remaining = self._hard_limit_tokens - fixed_tokens
        other = [m for m in messages if m not in fixed]
        
        # 新しい順に予算内で保持
        kept = []
        for msg in reversed(other):
            msg_tokens = self._estimate_messages_tokens([msg])
            if remaining >= msg_tokens:
                kept.append(msg)
                remaining -= msg_tokens
            else:
                break
        
        kept.reverse()
        return fixed + kept
    
    def check_loop_detection(self, current_tool_calls: list[ToolCall], max_loops: int) -> bool:
        """
        反復ループ検知（仕様書5.3.1）
        
        同じツールを同じ引数で連続呼び出しを検知
        
        Args:
            current_tool_calls: 現在のツール呼び出しリスト
            max_loops: 最大繰り返し回数
            
        Returns:
            ループが検知された場合True
        """
        if len(self._tool_call_history) < max_loops:
            return False
        
        # 直近の履歴を確認
        for tc in current_tool_calls:
            same_call_count = 0
            for history in self._tool_call_history[-max_loops:]:
                if history["name"] == tc.name and history["arguments"] == tc.arguments:
                    same_call_count += 1
            
            if same_call_count >= max_loops:
                logger.warning(f"ループ検知: {tc.name} が {same_call_count}回連続呼び出し")
                return True
        
        return False
    
    def clear_tool_call_history(self) -> None:
        """ツール呼び出し履歴をクリア（ループ検知リセット）"""
        self._tool_call_history.clear()


# ============================================
# エージェントクラス
# ============================================
class Agent:
    """
    自律エージェントクラス
    
    仕様書2（システムアーキテクチャ）に基づくUI・統括層のロジック実装
    """
    
    def __init__(
        self,
        mcp_manager: MCPClientManager,
        config: Optional[AgentConfig] = None
    ):
        """
        エージェントの初期化
        
        Args:
            mcp_manager: MCPクライアントマネージャー
            config: エージェント設定（Noneの場合はデフォルト値を使用）
        """
        self.mcp_manager = mcp_manager
        
        # 設定を読み込む
        if config is None:
            system_config = load_system_config()
            config = AgentConfig.from_dict(system_config)
        
        self.config = config
        
        # 動的プロンプト生成器
        self._prompt_generator = DynamicSystemPromptGenerator(mcp_manager)
        self._dynamic_prompt_generated = False  # 動的プロンプト生成済みフラグ
        
        # 初期システムプロンプトを構築（後で動的に更新）
        current_time = datetime.now(timezone(timedelta(hours=9))).strftime('%Y年%m月%d日 %H時%M分%S秒')
        initial_prompt = f"""あなたは親切で有能なAIアシスタントです。ユーザーの質問に丁寧かつ正確に回答してください。

## 行動規範
1. コードや設定値は必ずMarkdownのコードブロック（```）で囲んでください
2. 表形式のデータはMarkdownテーブルを使用してください
3. ツールを使用する場合は必須パラメータをすべて含めてください
4. ツール実行結果に基づいて、ユーザーに分かりやすく要約して報告してください
5. 推論過程をユーザーに見せる必要はありません。結論と根拠を簡潔に述べてください
6. 不確かな情報は「確信が持てません」と正直に伝えてください

## 推論とツール使用の原則
- ツールを使用する前、または複雑な回答を行う前に、必ず `<thinking>` タグ内で推論過程を記述してください
- `<thinking>` タグの内容はユーザーには表示されません（内部処理用）
- 推論内容: ユーザーの意図分析 → 必要な情報の特定 → ツール選択の理由 → 実行計画
- `</thinking>` タグを閉じた後に、実際のツール呼び出しまたはユーザー応答を行ってください

## 現在のシステム時刻
{current_time}

※ 接続中のMCPサーバのツール一覧は、初回の会話時に動的に追加されます。
"""
        
        # 【バグ修正】MessageHistoryにAgentConfigの値を正しく渡す
        self.history = MessageHistory(
            system_prompt=initial_prompt,
            hard_limit_tokens=self.config.hard_limit_tokens,
            soft_limit_tokens=self.config.soft_limit_tokens,
            message_history_budget_tokens=self.config.message_history_budget_tokens,
            pruning_soft_limit_tokens=self.config.pruning_soft_limit_tokens
        )
        
        self._cancel_requested = False  # キルスイッチ用フラグ
        self._tool_call_counter = {}  # 連続呼び出し検知用
        self._rejection_occurred = False  # ユーザー拒否発生フラグ（LLM暴走防止用）
        self._initial_user_input = None  # 初回ユーザー入力保存用（ツールフィルタリング用）
        self._empty_response_retry_count = 0  # 空応答リトライカウンター
        self._max_empty_retries = 3  # 空応答最大リトライ回数
        self._llm_error_retry_count = 0  # LLM接続エラーリトライカウンター
        self._tool_expectation_retry_count = 0  # ツール使用期待再試行カウンター
        self._max_tool_expectation_retries = 1  # ツール使用期待最大再試行回数
        self._length_retry_count = 0  # max_tokens不足リトライカウンター
        
        logger.info(f"エージェント初期化完了: model={config.model_name}, base_url={config.base_url}")
    
    async def _ensure_dynamic_prompt(self):
        """
        動的システムプロンプトが生成されていない場合に生成する
        
        MCPサーバ接続後にツール一覧を取得してプロンプトを動的に更新
        """
        if self._dynamic_prompt_generated:
            return
        
        try:
            # 動的プロンプトを生成
            base_prompt = self.config.system_prompt if self.config.system_prompt else None
            
            if self.config.use_enhanced_prompt:
                # 強化版プロンプトを使用
                dynamic_prompt = await self._prompt_generator.generate(base_prompt)
            else:
                # シンプルなプロンプト
                current_time = datetime.now(timezone(timedelta(hours=9))).strftime('%Y年%m月%d日 %H時%M分%S秒')
                dynamic_prompt = f"""{base_prompt or 'あなたは親切で有能なAIアシスタントです。'}

## 現在のシステム時刻
{current_time}
"""
            
            # 履歴のシステムプロンプトを更新
            self.history.messages[0] = {"role": "system", "content": dynamic_prompt}
            self._dynamic_prompt_generated = True
            
            logger.info("動的システムプロンプトを生成しました")
            
        except Exception as e:
            logger.warning(f"動的プロンプト生成に失敗、デフォルトプロンプトを使用: {e}")
            # フォールバック: 静的プロンプトを使用
            current_time = datetime.now(timezone(timedelta(hours=9))).strftime('%Y年%m月%d日 %H時%M分%S秒')
            if self.config.use_enhanced_prompt and self.config.include_tool_guidelines:
                fallback_prompt = ENHANCED_SYSTEM_PROMPT_TEMPLATE.format(current_time=current_time)
            else:
                fallback_prompt = self.config.system_prompt + f"\n\n[System Info]\n現在のシステム時刻は {current_time} です。"
            
            self.history.messages[0] = {"role": "system", "content": fallback_prompt}
    
    # ============================================
    # メインループ
    # ============================================
    async def run(self, user_input: str, server_name: str = None, file_attachments: list[dict] = None) -> AsyncGenerator[Any, None]:
        """
        自律エージェントのメインループ
        
        仕様書5.3に基づく自律ループ:
        推論 → ツール実行判定 → ツール実行 → 履歴追加 → 推論
        
        Args:
            user_input: ユーザーからの入力テキスト
            server_name: MCPサーバー名（指定時は該当サーバーのツールのみを使用）
            file_attachments: 添付ファイル情報リスト（辞書のリスト）
            
        Yields:
            Chainlit Step/Message オブジェクト
        """
        # サーバー名をインスタンス変数に保存
        self._server_name = server_name
        
        # 動的システムプロンプトが未生成の場合は生成
        await self._ensure_dynamic_prompt()
        
        # 添付ファイルがあれば処理してマルチモーダルメッセージを構築
        if file_attachments:
            user_content = await self._build_multimodal_user_message(user_input, file_attachments)
        else:
            user_content = user_input
        
        # ユーザー入力を履歴に追加
        self.history.add_user_message(user_content)
        self._cancel_requested = False
        self._tool_call_counter.clear()
        self.history.clear_tool_call_history()
        self._rejection_occurred = False  # 拒否フラグをリセット
        self._empty_response_retry_count = 0  # 空応答リトライカウンターをリセット
        self._llm_error_retry_count = 0  # LLM接続エラーリトライカウンターをリセット
        self._tool_expectation_retry_count = 0  # ツール使用期待再試行カウンターをリセット
        self._tools_executed_this_turn = False  # 現在のターンでツールが実行されたか
        
        loop_count = 0
        max_iterations = self.config.max_iterations  # 無限ループ防止の安全策
        
        while not self._cancel_requested and loop_count < max_iterations and not self._rejection_occurred:
            loop_count += 1
            # 各推論ターン開始時にツール実行フラグをリセット
            self._tools_executed_this_turn = False
            logger.info(f"=== 推論ループ {loop_count} 回目 ===")
            
            # ========================================
            # Step 1: LLM推論
            # ========================================
            async with cl.Step(name="推論") as step:
                step.output = "LLMに問い合わせ中..."
                yield step
                
                try:
                    # LLMにコンテキストを送信し、応答を取得
                    # 初回のユーザー入力を保存し、2回目以降もツールフィルタリングに使用
                    if loop_count == 1:
                        self._initial_user_input = user_input
                        filter_input = user_input
                    else:
                        filter_input = self._initial_user_input
                    llm_response = await self._call_llm(user_input=filter_input)
                    
                    # LLM呼び出し成功時はエラーリトライカウンターをリセット
                    self._llm_error_retry_count = 0
                    
                    # 【追加】空応答リトライで上昇したtemperatureを元に戻す
                    if hasattr(self, '_original_temperature'):
                        self.config.temperature = self._original_temperature
                        logger.info(f"正常応答検知: temperatureを {self.config.temperature} に復元")
                        delattr(self, '_original_temperature')
                    
                    # 【追加】max_tokens不足リトライで上昇したmax_tokensを元に戻す
                    if hasattr(self, '_original_max_tokens'):
                        self.config.max_tokens = self._original_max_tokens
                        logger.info(f"正常応答検知: max_tokensを {self.config.max_tokens} に復元")
                        delattr(self, '_original_max_tokens')
                        self._length_retry_count = 0
                    
                    # 【診断ログ】LLM応答の詳細を記録
                    logger.debug(f"[診断] LLM応答: content={llm_response.content[:100] if llm_response.content else 'None'}, tool_calls={len(llm_response.tool_calls)}, finish_reason={llm_response.finish_reason}")
                    
                    if llm_response.content:
                        step.output = f"応答: {llm_response.content[:100]}..."
                    else:
                        step.output = "ツール呼び出しを検知"
                    
                except Exception as e:
                    logger.error(f"LLM呼び出しエラー: {e}")
                    self._llm_error_retry_count += 1
                    
                    if self._llm_error_retry_count < self.config.max_llm_retries:
                        # リトライ可能: エラー内容を履歴に追加して再推論
                        retry_msg = f"🔄 LLM接続エラーが発生しました。再試行 {self._llm_error_retry_count}/{self.config.max_llm_retries} 回目です。エラー: {str(e)}"
                        logger.warning(retry_msg)
                        step.output = retry_msg
                        yield step
                        
                        # 【修正】エラー内容を履歴に追加（systemメッセージを直接追加せず、assistantメッセージとして追加）
                        # これにより _trim_to_budget() の保護メッセージ判定に影響を与えない
                        self.history.add_assistant_message(
                            f"【システム通知】LLM接続エラーが発生しました。エラー内容: {str(e)}。再度推論を試行してください。"
                        )
                        continue  # ループを継続して再推論
                    else:
                        # 最大リトライ回数に達した: エラーをユーザーに通知して終了
                        error_msg = f"❌ LLM接続エラーが{self.config.max_llm_retries}回連続で発生しました。エラー: {str(e)}"
                        logger.error(error_msg)
                        step.output = error_msg
                        yield step
                        
                        # エラーメッセージを履歴に追加
                        self.history.add_assistant_message(error_msg)
                        
                        # 最終的なエラーメッセージをユーザーに表示
                        await cl.Message(content=error_msg).send()
                        
                        break  # ループ終了
            
            # ========================================
            # Step 2: ツール実行判定
            # ========================================
            if self._has_tool_calls(llm_response):
                tool_calls = self._extract_tool_calls(llm_response)
                
                # 【診断ログ】ツール呼び出しの詳細を記録
                logger.info(f"[診断] ツール呼び出し検知: {len(tool_calls)}件, ツール名: {[tc.name for tc in tool_calls]}")
                
                # 1ターン1ツール制限: 複数ツール呼び出し時は最初の1件のみ実行
                if len(tool_calls) > 1:
                    logger.warning(f"1ターン1ツール制限: {len(tool_calls)}件のツール呼び出しを検知 → 最初の1件のみ実行 ({tool_calls[0].name})")
                    tool_calls = [tool_calls[0]]
                
                # 異常挙動検知（仕様書5.3.1）
                if self._detect_loop(tool_calls):
                    async with cl.Step(name="⚠️ 異常検知") as warn_step:
                        warn_step.output = "同じツールが繰り返し呼び出されています。処理を中断します。"
                        yield warn_step
                    break
                
                # ツール呼び出しメッセージを履歴に追加
                self.history.add_tool_call_message(tool_calls)
                
                # ========================================
                # Step 3: ツール実行
                # ========================================
                for tool_call in tool_calls:
                    # キャンセル要求の監視
                    if self._cancel_requested:
                        async with cl.Step(name="System", type="system_message") as cancel_step:
                            cancel_step.output = "🛑 ユーザーによって処理が強制停止されました。"
                            yield cancel_step
                        break
                    
                    
                    # ========================================
                    # Step 3-1: ツール実行承認チェック
                    # ========================================
                    if self._requires_approval(tool_call.name):
                        # 承認が必要なツールの場合、ユーザーに確認
                        approved, rejection_msg = await self._request_tool_approval(tool_call)
                        
                        if not approved:
                            # 拒否された場合：ツール結果としてエラーを記録し、次の推論へ
                            # LLMに拒否されたことを伝え、代替案を提案させる
                            self.history.add_tool_result(
                                tool_call_id=tool_call.id,
                                tool_name=tool_call.name,
                                raw_result={"rejected": True, "error": rejection_msg},
                                summary=rejection_msg
                            )
                            # ツール実行ループを抜け、次の推論でLLMが対応を決定
                            break  # for tool_call in tool_calls ループを抜ける
                    
                    async with cl.Step(name=f"🛠️ {tool_call.name}") as tool_step:
                        tool_step.output = f"実行中..."
                        yield tool_step
                        
                        try:
                            # MCPサーバーでツールを実行
                            raw_result = await self._execute_tool(tool_call)
                            
                            # 仕様書6.3: Pruning（剪定）処理
                            summary = self._summarize_tool_result(tool_call.name, raw_result)
                            
                            # 履歴に追加（Pruning適用）
                            self.history.add_tool_result(
                                tool_call_id=tool_call.id,
                                tool_name=tool_call.name,
                                raw_result=raw_result,
                                summary=summary
                            )
                            
                            tool_step.output = summary
                            
                        except asyncio.TimeoutError:
                            from tools import ToolExecutionErrorHandler
                            error_msg = f"ツール実行がタイムアウトしました（{self.config.tool_execution_timeout_seconds}秒）"
                            tool_step.output = f"❌ {error_msg}"
                            structured_error = ToolExecutionErrorHandler.generate_structured_error(
                                Exception(error_msg), tool_call.name
                            )
                            self.history.add_tool_result(
                                tool_call_id=tool_call.id,
                                tool_name=tool_call.name,
                                raw_result=structured_error,
                                summary=json.dumps(structured_error, ensure_ascii=False)
                            )
                            
                        except Exception as e:
                            from tools import ToolExecutionErrorHandler
                            error_msg = f"ツール実行エラー: {str(e)}"
                            tool_step.output = f"❌ {error_msg}"
                            logger.error(f"ツール実行エラー: {e}")
                            structured_error = ToolExecutionErrorHandler.generate_structured_error(
                                e, tool_call.name
                            )
                            self.history.add_tool_result(
                                tool_call_id=tool_call.id,
                                tool_name=tool_call.name,
                                raw_result=structured_error,
                                summary=json.dumps(structured_error, ensure_ascii=False)
                            )
                        
                        yield tool_step
                
                # 【修正】ツール実行後、LLMがcontentを同時に返していた場合の処理
                # contentには思考・計画テキストが含まれることがあるが、
                # assistantメッセージとして追加すると次のターンで空応答を誘発するため、
                # 履歴には追加せずログのみ出力する
                if llm_response.content:
                    logger.info(f"[診断] ツール実行後の思考テキストを検知（履歴には追加しません）: {llm_response.content[:100]}...")
                
                # 【修正】1ターン1ツール制限で無視されたツール呼び出しはログに記録するのみ
                # assistantメッセージとして追加すると空応答を誘発するため、履歴には追加しない
                if len(llm_response.tool_calls) > 1:
                    skipped_tools = llm_response.tool_calls[1:]
                    skipped_names = [tc.name for tc in skipped_tools]
                    logger.info(f"[診断] スキップされたツール呼び出し: {skipped_names}")
                
                # ツール実行済みフラグをセット
                self._tools_executed_this_turn = True
                
                # 【修正】ツール実行後、空応答リトライカウンターをリセット
                # ツール結果を受け取った状態で新しい推論を開始するため、前のターンの空応答カウントは引き継がない
                self._empty_response_retry_count = 0
                
                # 【追加】ツール実行後、max_tokens不足リトライカウンターもリセット
                self._length_retry_count = 0
                
                # ツール結果を渡して再度推論へ（ループ継続）
                continue
            
            # ========================================
            # ツール呼び出しなし → 自然言語応答
            # ========================================
            if not llm_response.tool_calls and llm_response.finish_reason == "length":
                # 【追加】max_tokens不足で応答が切り詰められた
                logger.warning(f"[診断] max_tokens不足（finish_reason=length）。現在のmax_tokens={self.config.max_tokens}")
                
                if self._length_retry_count < 1:  # 1回までリトライ
                    self._length_retry_count += 1
                    # max_tokensを32768に一時的に増加
                    if not hasattr(self, '_original_max_tokens'):
                        self._original_max_tokens = self.config.max_tokens
                    self.config.max_tokens = 65536
                    logger.info(f"max_tokens不足: {self._original_max_tokens} → {self.config.max_tokens} に一時的に増加")
                    
                    retry_msg = "🔄 応答が長すぎて切り詰められました。より大きなmax_tokensで再試行します。"
                    logger.warning(retry_msg)
                    
                    async with cl.Step(name="⚠️ 応答切り詰め検知") as retry_step:
                        retry_step.output = retry_msg
                        yield retry_step
                    
                    # ループを継続して再推論
                    continue
                else:
                    # 既にリトライ済み
                    error_msg = "❌ 応答が長すぎて生成できませんでした。より短い要求を試してください。"
                    logger.error(error_msg)
                    
                    async with cl.Step(name="❌ 応答生成失敗") as error_step:
                        error_step.output = error_msg
                        yield error_step
                    
                    # エラーメッセージを履歴に追加
                    self.history.add_assistant_message(error_msg)
                    await cl.Message(content=error_msg).send()
                    
                    break  # ループ終了
            elif llm_response.content:
                # 【診断ログ】自然言語応答を検知
                logger.info(f"[診断] 自然言語応答を検知: {llm_response.content[:100]}...")
                
                # 【削除】ツール使用期待再試行機能を廃止
                # 理由: 偽陽性が高く、空応答を誘発する主要な要因となっていた
                # システムプロンプトでのツール使用促進で十分なため、追加の再試行メカニズムは不要
                
                self.history.add_assistant_message(llm_response.content)
                
                async with cl.Step(name="応答") as response_step:
                    response_step.output = llm_response.content
                    yield response_step
                break  # ループ終了
            elif not llm_response.tool_calls and llm_response.finish_reason == "stop":
                # 【診断ログ】自然言語応答なし（空応答）
                logger.warning(
                    f"[診断] 自然言語応答なし（空応答）。"
                    f"finish_reason={llm_response.finish_reason}, "
                    f"tool_calls={len(llm_response.tool_calls)}, "
                    f"messages_count={len(self.history.messages)}, "
                    f"retry_count={self._empty_response_retry_count}, "
                    f"thinking_len={len(llm_response.thinking)}"
                )
                
                # ========================================
                # 空応答リトライ処理
                # ========================================
                if self._empty_response_retry_count < self._max_empty_retries:
                    self._empty_response_retry_count += 1
                    retry_msg = f"🔄 LLMから空の応答を受信しました。再試行 {self._empty_response_retry_count}/{self._max_empty_retries} 回目です。"
                    logger.warning(retry_msg)
                    
                    async with cl.Step(name="⚠️ 空応答検知") as retry_step:
                        retry_step.output = retry_msg
                        yield retry_step
                    
                    # 【修正】空応答リトライ時に、直前のツール結果があれば文脈を保持
                    # assistantメッセージとして追加すると空応答を誘発するため、
                    # userメッセージとして追加して「ツール結果を受け取った状態で回答を求める」文脈を保持
                    last_tool_result = None
                    last_tool_name = None
                    for msg in reversed(self.history.messages):
                        if msg.get("role") == "tool":
                            last_tool_result = msg.get("content", "")
                            last_tool_name = msg.get("name", "")
                            break
                    
                    if last_tool_result and last_tool_name:
                        # ツール結果の要約を含む文脈保持メッセージ
                        result_summary = last_tool_result[:300] if len(last_tool_result) > 300 else last_tool_result
                        context_msg = f"【システム: 前回のツール「{last_tool_name}」の実行結果】\n{result_summary}"
                        if len(last_tool_result) > 300:
                            context_msg += "..."
                        # 【修正】userメッセージとして追加（assistantではない）
                        self.history.add_user_message(context_msg)
                        logger.info(f"[診断] 空応答リトライ時にツール結果文脈をuserメッセージとして追加: {last_tool_name}")
                    
                    if llm_response.thinking:
                        self.history.add_user_message(
                            "【システム通知】前回のLLM応答は内部推論のみで、ユーザーに返す本文やツール呼び出しがありませんでした。"
                            "内部推論を続けず、最終回答本文または必要なツール呼び出しだけを返してください。"
                        )
                        logger.info("[診断] reasoning/thinkingのみの空応答に対する最終回答要求を追加")
                    
                    # 【修正】temperature上昇幅を0.2→0.1に減らし、過度なランダム性を抑制
                    if not hasattr(self, '_original_temperature'):
                        self._original_temperature = self.config.temperature
                    self.config.temperature = min(self.config.temperature + 0.1, 0.8)
                    logger.info(f"空応答リトライ: temperatureを {self._original_temperature} → {self.config.temperature} に一時的に上昇")
                    
                    # ループを継続して再推論
                    continue
                else:
                    # 最大リトライ回数に達した
                    error_msg = f"❌ LLMからの応答生成に失敗しました（空応答が{self._max_empty_retries}回連続しました）。ページを再読み込みして再度お試しください。"
                    logger.error(error_msg)
                    
                    async with cl.Step(name="❌ 応答生成失敗") as error_step:
                        error_step.output = error_msg
                        yield error_step
                    
                    # エラーメッセージを履歴に追加
                    self.history.add_assistant_message(error_msg)
                    
                    # 最終的なエラーメッセージをユーザーに表示
                    await cl.Message(content=error_msg).send()
                    
                    break  # ループ終了
            else:
                # その他の異常終了（finish_reasonがstop以外、またはtool_callsがあるのにcontentもない）
                error_msg = (
                    f"❌ LLMから予期しない応答を受信しました。"
                    f"(finish_reason={llm_response.finish_reason}, "
                    f"tool_calls={len(llm_response.tool_calls)}, "
                    f"content={'あり' if llm_response.content else 'なし'})"
                )
                logger.error(error_msg)
                
                async with cl.Step(name="❌ 異常応答検知") as error_step:
                    error_step.output = error_msg
                    yield error_step
                
                # エラーメッセージを履歴に追加
                self.history.add_assistant_message(error_msg)
                await cl.Message(content=error_msg).send()
                
                break  # ループ終了
        
        if loop_count >= max_iterations:
            async with cl.Step(name="⚠️ 最大反復回数") as warn_step:
                warn_step.output = f"最大反復回数（{max_iterations}回）に達しました。処理を終了します。"
                yield warn_step
    
    # ============================================
    # 内部メソッド（プライベート）
    # ============================================
    async def _build_multimodal_user_message(self, user_input: str, file_attachments: list[dict]) -> list[dict]:
        """
        添付ファイルを含むマルチモーダルユーザーメッセージを構築
        
        Args:
            user_input: ユーザー入力テキスト
            file_attachments: 添付ファイル情報リスト
            
        Returns:
            OpenAI互換マルチモーダルcontentリスト
        """
        content_parts = [{"type": "text", "text": user_input}]
        
        for file_info in file_attachments:
            file_path = file_info.get("path", "")
            file_name = file_info.get("name", "")
            mime_type = file_info.get("mime", "")
            
            if not file_path or not os.path.exists(file_path):
                logger.warning(f"ファイルが見つかりません: {file_path}")
                content_parts.append({
                    "type": "text",
                    "text": f"[添付ファイル '{file_name}' が見つかりません]"
                })
                continue
            
            ext = Path(file_name).suffix.lower()
            
            try:
                # 画像ファイル: base64エンコードしてマルチモーダル形式で追加
                if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
                    with open(file_path, "rb") as f:
                        image_data = f.read()
                    
                    # MIMEタイプの推定
                    if not mime_type:
                        mime_map = {
                            ".png": "image/png",
                            ".jpg": "image/jpeg",
                            ".jpeg": "image/jpeg",
                            ".gif": "image/gif",
                            ".webp": "image/webp",
                            ".bmp": "image/bmp"
                        }
                        mime_type = mime_map.get(ext, "image/png")
                    
                    b64_data = base64.b64encode(image_data).decode("utf-8")
                    content_parts.append({
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{b64_data}"
                        }
                    })
                    logger.info(f"画像ファイルをbase64エンコード: {file_name} ({len(image_data)} bytes)")
                
                # テキストファイル: 内容を読み込んでテキストとして追加
                elif ext in {".txt", ".md", ".csv", ".json", ".py", ".yaml", ".yml", ".xml", ".html", ".htm", ".log", ".ini", ".cfg", ".toml", ".rst"}:
                    # エンコーディングを推定（UTF-8優先、失敗したらshift-jis）
                    file_text = ""
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            file_text = f.read()
                    except UnicodeDecodeError:
                        try:
                            with open(file_path, "r", encoding="shift_jis") as f:
                                file_text = f.read()
                        except UnicodeDecodeError:
                            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                                file_text = f.read()
                    
                    # 長すぎる場合は切り詰め
                    max_text_chars = 8000
                    if len(file_text) > max_text_chars:
                        file_text = file_text[:max_text_chars] + f"\n... [ファイル '{file_name}' の内容が長いため切り詰め: 全{len(file_text)}文字]"
                    
                    content_parts.append({
                        "type": "text",
                        "text": f"\n\n--- 添付ファイル: {file_name} ---\n{file_text}\n--- ファイル終了 ---\n"
                    })
                    logger.info(f"テキストファイルを読み込み: {file_name} ({len(file_text)} chars)")
                
                # その他のファイル: ファイルパスを含めてテキストとして追加
                else:
                    file_size = os.path.getsize(file_path)
                    content_parts.append({
                        "type": "text",
                        "text": f"\n\n--- 添付ファイル: {file_name} ---\nファイルパス: {file_path}\n（read_document_file ツールで読み込んでください。）\n--- ファイル終了 ---\n"
                    })
                    logger.info(f"その他のファイル情報を追加: {file_name} ({file_size} bytes)")
            
            except Exception as e:
                logger.error(f"ファイル読み込みエラー ({file_name}): {e}")
                content_parts.append({
                    "type": "text",
                    "text": f"\n[添付ファイル '{file_name}' の読み込み中にエラーが発生しました: {str(e)}]\n"
                })
        
        return content_parts
    
    async def _call_llm(self, user_input: str = None) -> LLMResponse:
        """
        LLMへのAPI呼び出し
        
        OpenAI互換API（Ollama/vLLM/LM Studio）へのリクエスト実装
        
        Args:
            user_input: ユーザー入力（ツールフィルタリング用、オプション）
        
        Returns:
            LLM応答
        """
        # ツール定義を取得（フィルタリング設定を適用）
        # サーバー名が指定されている場合は該当サーバーのツールのみを取得
        # ツール定義予算を渡して動的調整を有効化
        tools = await self.mcp_manager.get_tools_for_llm(
            user_input=user_input,
            max_tools=self.config.max_tools if self.config.tool_filter_enabled else None,
            compression_mode=self.config.compression_mode if self.config.tool_filter_enabled else "full",
            always_include=self.config.always_include if self.config.tool_filter_enabled else None,
            server_name=getattr(self, '_server_name', None),
            tool_definition_budget_tokens=self.config.tool_definition_budget_tokens
        )
        
        # リクエストボディを構築
        messages = self.history.get_context_for_llm()
        
        # 【修正】システムプロンプト内の時刻を最新のJST時刻に更新
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = self._inject_current_time(messages[0]["content"])
        
        # マルチモーダルメッセージのcontentがリストの場合、OpenAI互換形式に変換
        # 一部のローカルLLM（Ollama等）はimage_urlではなくimageで受け取る場合があるが、
        # OpenAI互換APIではimage_urlが標準
        request_body = {
            "model": self.config.model_name,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens
        }
        
        # ツールが存在する場合は追加
        if tools:
            request_body["tools"] = tools
            request_body["tool_choice"] = "auto"
        
        # 【診断】リクエスト全体の推定トークン数を計算してログ出力
        try:
            from tools import _estimate_tool_definition_tokens
            tool_tokens = _estimate_tool_definition_tokens(tools)
            message_tokens = self.history._estimate_total_tokens()
            total_estimated = tool_tokens + message_tokens
            logger.info(
                f"LLMリクエスト推定トークン数: ツール定義={tool_tokens}, "
                f"メッセージ履歴={message_tokens}, 合計={total_estimated} "
                f"（予算: ツール={self.config.tool_definition_budget_tokens}, "
                f"メッセージ={self.config.message_history_budget_tokens}, "
                f"全体={self.config.max_context_tokens}）"
            )
        except Exception as e:
            logger.debug(f"リクエストトークン数推定に失敗: {e}")
        
        headers = {
            "Content-Type": "application/json"
        }
        
        if self.config.api_key and self.config.api_key != "optional_key_here":
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        
        # タイムアウト設定
        timeout = httpx.Timeout(self.config.inference_timeout_seconds)
        
        # ローカル接続先かどうかを判定し、ローカルの場合はプロキシを無効化
        _client_kwargs = {"timeout": timeout}
        _base_hostname = urlparse(self.config.base_url).hostname if self.config.base_url else None
        if should_bypass_proxy(_base_hostname):
            _client_kwargs["proxy"] = None
            _client_kwargs["trust_env"] = False  # 環境変数のプロキシ設定を無視

        async with httpx.AsyncClient(**_client_kwargs) as client:
            try:
                logger.debug(f"LLMリクエスト送信: {self.config.base_url}/chat/completions")
                response = await client.post(
                    f"{self.config.base_url}/chat/completions",
                    headers=headers,
                    json=request_body
                )
                
                response.raise_for_status()
                data = response.json()
                
                return self._parse_llm_response(data)
                
            except httpx.HTTPStatusError as e:
                logger.error(f"LLM API HTTPエラー: {e}")
                raise Exception(f"LLM API エラー: {e.response.status_code}")
            
            except httpx.RequestError as e:
                error_detail = f"{type(e).__name__}: {repr(e)}"
                logger.error(f"LLM API 接続エラー: {error_detail}")
                raise Exception(f"LLM接続エラー: {error_detail}")
    
    def _parse_llm_response(self, data: dict) -> LLMResponse:
        """
        LLM応答をパース
        
        Args:
            data: API応答データ
            
        Returns:
            パースされたLLM応答
        """
        choices = data.get("choices", [])
        
        if not choices:
            return LLMResponse(content="", finish_reason="stop")
        
        choice = choices[0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "stop")
        
        # コンテンツを取得
        raw_content = message.get("content") or ""
        raw_reasoning = (
            message.get("reasoning")
            or message.get("reasoning_content")
            or choice.get("reasoning")
            or ""
        )
        
        # <thinking> タグの抽出
        thinking_parts = []
        content = raw_content
        import re
        thinking_match = re.search(r'<thinking>(.*?)</thinking>', raw_content, re.DOTALL)
        if thinking_match:
            thinking_parts.append(thinking_match.group(1).strip())
            # thinkingタグを除去したコンテンツ
            content = re.sub(r'<thinking>.*?</thinking>', '', raw_content, flags=re.DOTALL).strip()
        else:
            content = raw_content.strip() if isinstance(raw_content, str) else ""
        
        if raw_reasoning:
            # Ollama/OpenAI互換APIの一部モデル（Qwen系など）は内部推論を
            # contentではなくreasoning/reasoning_contentに分離して返す。
            thinking_parts.append(str(raw_reasoning).strip())
        
        thinking = "\n\n".join(part for part in thinking_parts if part)
        if thinking:
            logger.debug(f"[thinking] 推論内容を抽出: {thinking[:100]}...")
            if not content and not message.get("tool_calls", []):
                logger.warning(
                    "[診断] LLM応答はreasoning/thinkingのみで、content/tool_callsが空です。"
                    f"finish_reason={finish_reason}, thinking_len={len(thinking)}"
                )
        
        # ツール呼び出しを取得
        tool_calls = []
        raw_tool_calls = message.get("tool_calls", [])
        
        for tc in raw_tool_calls:
            function = tc.get("function", {})
            arguments_str = function.get("arguments", "{}")
            
            try:
                arguments = json.loads(arguments_str) if arguments_str else {}
            except json.JSONDecodeError:
                arguments = {}
            
            tool_calls.append(ToolCall(
                id=tc.get("id", ""),
                name=function.get("name", ""),
                arguments=arguments
            ))
        
        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            thinking=thinking
        )
    
    def _has_tool_calls(self, llm_response: LLMResponse) -> bool:
        """
        LLM応答にツール呼び出しが含まれるか判定
        
        Args:
            llm_response: LLM応答
            
        Returns:
            ツール呼び出しがある場合True
        """
        return len(llm_response.tool_calls) > 0
    
    def _extract_tool_calls(self, llm_response: LLMResponse) -> list[ToolCall]:
        """
        LLM応答からツール呼び出しを抽出
        
        Args:
            llm_response: LLM応答
            
        Returns:
            ツール呼び出しリスト
        """
        return llm_response.tool_calls
    
    def _requires_approval(self, tool_name: str) -> bool:
        """
        ツール実行にユーザー承認が必要かを判定
        
        安全なプレフィックス（get_, list_, check_ 等）で始まるツールは
        DANGEROUS_KEYWORDS に合致しても承認不要と判定する。
        
        Args:
            tool_name: ツール名
            
        Returns:
            承認が必要な場合True
        """
        tool_name_lower = tool_name.lower()
        # 安全プレフィックスを持つツールは読み取り専用とみなし承認不要（優先判定）
        if any(tool_name_lower.startswith(prefix) for prefix in SAFE_TOOL_PREFIXES):
            return False
        return any(keyword in tool_name_lower for keyword in DANGEROUS_KEYWORDS)
    
    async def _request_tool_approval(self, tool_call: ToolCall) -> tuple[bool, str]:
        """
        ユーザーにツール実行の承認を求める
        
        ChainlitのAskActionMessageを使用して、ツール名と引数を提示し
        ユーザーに許可を求める。
        
        Args:
            tool_call: ツール呼び出し情報
            
        Returns:
            (承認されたかどうか, 結果メッセージ)
        """
        # 引数を整形して表示
        try:
            args_json = json.dumps(tool_call.arguments, ensure_ascii=False, indent=2)
        except Exception:
            args_json = str(tool_call.arguments)
        
        # インスタンスを変数に格納
        ask_msg = cl.AskActionMessage(
            content=f"⚠️ **承認待ち**: AIがツール `{tool_call.name}` を実行しようとしています。\n\n**引数:**\n```json\n{args_json}\n```\n\n実行を許可しますか？",
            actions=[
                cl.Action(name="approve", payload={"action": "approve"}, label="✅ 許可"),
                cl.Action(name="reject", payload={"action": "reject"}, label="❌ 拒否")
            ],
            timeout=60
        )
        
        # ユーザーの応答を待機
        res = await ask_msg.send()
        
        # ユーザーの応答を判定
        if res and res.get("payload", {}).get("action") == "approve":
            await cl.Message(content=f"✅ ツール `{tool_call.name}` の実行を許可しました。").send()
            return True, ""
        else:
            rejection_msg = f"❌ ツール `{tool_call.name}` の実行が拒否されました（またはタイムアウト）。"
            await cl.Message(content=rejection_msg).send()
            return False, "【重要: システムによる強制キャンセル】ユーザーがこの操作を明示的に拒否しました。代替手段を探さずに「キャンセルされました」とだけ回答して処理を終了してください。"
    
    async def _execute_tool(self, tool_call: ToolCall) -> dict:
        """
        MCPサーバーでツールを実行
        
        Args:
            tool_call: ツール呼び出し情報
            
        Returns:
            ツール実行結果
        """
        # タイムアウト付きでツール実行
        # サーバー名はcall_tool内で自動的に検索される
        result = await asyncio.wait_for(
            self.mcp_manager.call_tool(
                server_name="",  # call_tool内で適切なサーバーを自動検索
                tool_name=tool_call.name,
                arguments=tool_call.arguments
            ),
            timeout=self.config.tool_execution_timeout_seconds
        )
        
        return result
    
    def _summarize_tool_result(self, tool_name: str, raw_result: dict) -> str:
        """
        ツール実行結果の安全弁処理
        
        原則としてツール結果はそのまま保持し、コンテキスト全体の圧縮は
        MessageHistory._trim_to_budget() で行う。
        本メソッドは極端に巨大な結果に対する安全弁のみを提供する。
        
        Args:
            tool_name: ツール名
            raw_result: 生のツール実行結果
            
        Returns:
            処理後のテキスト
        """
        # 結果からテキストを抽出
        if isinstance(raw_result, dict):
            content = raw_result.get("content", [])
            if isinstance(content, list):
                texts = []
                for item in content:
                    if isinstance(item, dict) and item.get("type") == "text":
                        texts.append(item.get("text", ""))
                result_text = "\n".join(texts)
            else:
                result_text = str(content)
        else:
            result_text = str(raw_result)
        
        # 安全弁: tool_result_max_chars を超える場合のみ切り詰め
        safety_limit = self.config.tool_result_max_chars
        
        if len(result_text) > safety_limit:
            logger.warning(f"ツール結果が安全弁を超過: {tool_name} ({len(result_text)}文字 -> {safety_limit}文字)")
            return result_text[:safety_limit] + f"\n... [結果が長すぎるため切り詰め: 全{len(result_text)}文字中{safety_limit}文字表示]"
        
        return result_text
    
    def _inject_current_time(self, prompt: str) -> str:
        """
        システムプロンプト内の時刻を現在のJST時刻で更新
        
        Args:
            prompt: 更新対象のシステムプロンプト
            
        Returns:
            時刻が更新されたプロンプト
        """
        import re
        jst_now = datetime.now(timezone(timedelta(hours=9))).strftime('%Y年%m月%d日 %H時%M分%S秒')
        
        # 「現在のシステム時刻」に続く時刻表記を置換
        # 複数のフォーマットに対応:
        # - "## 現在のシステム時刻\n2025年05月23日 11時37分50秒"
        # - "現在のシステム時刻は 2025年05月23日 11時37分50秒 です。"
        time_pattern = r'(現在のシステム時刻[は:\s]*(?:\n\s*)?)(\d{4}年\d{2}月\d{2}日\s+\d{2}時\d{2}分\d{2}秒)'
        
        if re.search(time_pattern, prompt):
            # \g<1> を使用: \1 の直後に数字が続くと「グループ12026」と誤解釈されるため
            return re.sub(time_pattern, r'\g<1>' + jst_now, prompt, count=1)
        
        # 時刻セクションが見つからない場合は末尾に追加
        return prompt + f"\n\n## 現在のシステム時刻\n{jst_now}\n"
    
    def _detect_loop(self, tool_calls: list[ToolCall]) -> bool:
        """
        反復ループ検知（仕様書5.3.1）
        
        4段階の検知を実施:
        1. 同じツール＋同じ引数の連続呼び出し（厳密一致）
        2. 同じツール名の連続呼び出し（引数違いも検知）
        3. ツール呼び出し総数の上限チェック（ポーリング全般を検知）
        4. 直前のツール結果後の同ツール名＋同引数の即座再呼び出し
        
        Args:
            tool_calls: 現在のツール呼び出しリスト
            
        Returns:
            ループが検知された場合True
        """
        # --- 検知1: 同じツール＋同じ引数の連続呼び出し（厳密一致）---
        if self.history.check_loop_detection(tool_calls, self.config.max_repeated_loops):
            return True
        
        # --- 検知2: 同じツール名の連続呼び出し（引数違いも検知）---
        # ポーリング（例: get_sync_status を引数を変えて繰り返し呼ぶ）を検知
        for tc in tool_calls:
            # ツール名のみをキーとするカウンター
            name_key = f"_name:{tc.name}"
            self._tool_call_counter[name_key] = self._tool_call_counter.get(name_key, 0) + 1
            
            # ポーリング系ツール（get_, list_, check_ 等）は閾値を緩和する
            tc_lower = tc.name.lower()
            is_polling_tool = any(tc_lower.startswith(prefix) for prefix in SAFE_POLLING_PREFIXES)
            if is_polling_tool:
                # ポーリング系は max_repeated_loops * 2 回まで許容（緩和）
                name_threshold = self.config.max_repeated_loops * 2
            else:
                # 通常ツールは max_repeated_loops 回まで（厳格化）
                name_threshold = self.config.max_repeated_loops
            
            if self._tool_call_counter[name_key] >= name_threshold:
                logger.warning(f"ループ検知（ツール名）: {tc.name} が {self._tool_call_counter[name_key]}回呼び出し")
                return True
        
        # --- 検知3: ツール呼び出し総数の上限チェック ---
        # あらゆるパターンのポーリング・ループを包括的に検知
        total_calls = sum(
            v for k, v in self._tool_call_counter.items()
            if k.startswith("_name:")
        )
        # 1セッション中のツール呼び出し総数が max_repeated_loops * 4 を超えたら検知（緩和）
        total_threshold = self.config.max_repeated_loops * 4
        if total_calls > total_threshold:
            logger.warning(f"ループ検知（総数）: ツール呼び出し総数 {total_calls} が閾値 {total_threshold} を超過")
            return True
        
        # --- 検知4: 直前のツール結果後の同ツール名＋同引数の即座再呼び出し ---
        # ツール結果後、同じツールを同じ引数で即座に再呼び出しした場合を検知
        last_tool_name = None
        last_tool_args = None
        for msg in reversed(self.history.messages):
            if msg.get("role") == "tool":
                last_tool_name = msg.get("name", "")
                # 直前のassistantメッセージからtool_callsを探す
                for prev_msg in reversed(self.history.messages):
                    if prev_msg.get("role") == "assistant" and prev_msg.get("tool_calls"):
                        for tc_info in prev_msg.get("tool_calls", []):
                            func = tc_info.get("function", {})
                            if func.get("name") == last_tool_name:
                                try:
                                    last_tool_args = json.loads(func.get("arguments", "{}"))
                                except (json.JSONDecodeError, ValueError):
                                    last_tool_args = {}
                                break
                        if last_tool_args is not None:
                            break
                break
        
        if last_tool_name and last_tool_args is not None:
            for tc in tool_calls:
                if tc.name == last_tool_name and tc.arguments == last_tool_args:
                    logger.warning(f"ループ検知（即座再呼び出し）: {tc.name} が直前の結果後に同じ引数で再呼び出し")
                    return True
        
        # --- 検知1のカウンターベース（厳密一致: ツール名+引数）---
        for tc in tool_calls:
            key = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True)}"
            self._tool_call_counter[key] = self._tool_call_counter.get(key, 0) + 1
            
            if self._tool_call_counter[key] >= self.config.max_repeated_loops:
                logger.warning(f"ループ検知（カウンター）: {key} が {self._tool_call_counter[key]}回")
                return True
        
        return False
    
    async def _should_retry_for_tool_expectation(self, user_input: str, llm_content: str) -> bool:
        """
        ツール使用が期待されるが使われなかった場合に再試行すべきかを判定
        
        Args:
            user_input: ユーザー入力テキスト
            llm_content: LLMの応答テキスト
            
        Returns:
            再試行すべき場合True
        """
        # 現在のターンでツールが既に実行されている場合は再試行しない（偽陽性防止）
        if self._tools_executed_this_turn:
            logger.debug("ツール使用期待再試行: 現在のターンでツールが既に実行されているためスキップします")
            return False
        
        # 最大再試行回数に達した場合は再試行しない
        if self._tool_expectation_retry_count >= self._max_tool_expectation_retries:
            logger.debug(
                f"ツール使用期待再試行: 最大回数({self._max_tool_expectation_retries})に達したため再試行しません"
            )
            return False
        
        # ツールフィルタからツール使用期待値を判定
        from tools import ToolFilter
        tool_filter = ToolFilter()
        should_expect, matched_keywords = tool_filter.should_expect_tool_calls(user_input)
        
        if not should_expect:
            logger.debug(f"ツール使用期待再試行: ツール使用は期待されません")
            return False
        
        # 利用可能なツールがあるか確認
        tools = await self.mcp_manager.get_all_tools()
        if not tools:
            logger.debug(f"ツール使用期待再試行: 利用可能なツールがないため再試行しません")
            return False
        
        # LLMの応答に「処理しました」「完了しました」などの虚偽完了表現が含まれるか確認
        completion_phrases = [
            "処理しました", "完了しました", "実行しました", "登録しました",
            "変更しました", "削除しました", "更新しました", "追加しました",
            "完了", "処理", "実行", "登録", "変更", "削除", "更新", "追加",
            "しました", "してあります", "しておきました",
        ]
        has_completion_phrase = any(phrase in llm_content for phrase in completion_phrases)
        
        # ツール使用が期待され、かつ虚偽完了表現がある場合に再試行
        if has_completion_phrase:
            logger.warning(
                f"ツール使用期待再試行: ツール使用が期待されるが虚偽完了表現を検知。"
                f"keywords={matched_keywords}, content={llm_content[:100]}..."
            )
            return True
        
        logger.debug(
            f"ツール使用期待再試行: ツール使用は期待されますが、虚偽完了表現は検知されませんでした。"
            f"keywords={matched_keywords}"
        )
        return False
    
    def cancel(self):
        """外部から処理の強制停止を要求する"""
        self._cancel_requested = True
        logger.info("キャンセル要求を受け付けました")
