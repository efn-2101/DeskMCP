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
from typing import AsyncGenerator, Optional, Any
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
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
SAFE_TOOL_PREFIXES = ["get_", "list_", "check_", "read_", "fetch_", "search_", "query_", "status"]

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
    """デフォルト設定を返す"""
    return {
        "llm_settings": {
            "provider": "ollama",
            "base_url": "http://localhost:11434/v1",
            "model_name": "gemma3:latest",
            "api_key": "optional_key_here"
        },
        "context_management": {
            "hard_limit_tokens": 8192,
            "soft_limit_tokens": 6000,
            "tool_definition_budget_tokens": 4000,
            "message_history_budget_tokens": 2000
        },
        "agent_safeguards": {
            "max_repeated_loops": 3,
            "inference_timeout_seconds": 180,
            "tool_execution_timeout_seconds": 60
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
# データクラス定義
# ============================================
@dataclass
class AgentConfig:
    """エージェント設定"""
    # セーフガード設定
    max_repeated_loops: int = 3
    inference_timeout_seconds: int = 180
    tool_execution_timeout_seconds: int = 60
    
    # コンテキスト管理設定
    hard_limit_tokens: int = 8192
    soft_limit_tokens: int = 6000
    tool_definition_budget_tokens: int = 4000
    message_history_budget_tokens: int = 2000
    
    # LLM設定
    base_url: str = "http://localhost:11434/v1"
    model_name: str = "gemma3:latest"
    api_key: str = "optional_key_here"
    
    # システムプロンプト設定
    system_prompt: str = "あなたは親切で有能なAIアシスタントです。"
    use_enhanced_prompt: bool = True
    include_tool_guidelines: bool = True
    
    # ツールフィルタ設定
    tool_filter_enabled: bool = True
    max_tools: int = 15
    always_include: list = field(default_factory=lambda: ["get_server_info"])
    compression_mode: str = "compact"  # full, compact, minimal
    
    @classmethod
    def from_dict(cls, config: dict) -> "AgentConfig":
        """設定辞書からAgentConfigを作成"""
        llm_settings = config.get("llm_settings", {})
        context_mgmt = config.get("context_management", {})
        safeguards = config.get("agent_safeguards", {})
        tool_filter = config.get("tool_filter_settings", {})
        prompt_settings = config.get("system_prompt_settings", {})
        
        return cls(
            # セーフガード
            max_repeated_loops=safeguards.get("max_repeated_loops", 3),
            inference_timeout_seconds=safeguards.get("inference_timeout_seconds", 180),
            tool_execution_timeout_seconds=safeguards.get("tool_execution_timeout_seconds", 60),
            # コンテキスト管理
            hard_limit_tokens=context_mgmt.get("hard_limit_tokens", 8192),
            soft_limit_tokens=context_mgmt.get("soft_limit_tokens", 6000),
            tool_definition_budget_tokens=context_mgmt.get("tool_definition_budget_tokens", 4000),
            message_history_budget_tokens=context_mgmt.get("message_history_budget_tokens", 2000),
            # LLM設定
            base_url=llm_settings.get("base_url", "http://localhost:11434/v1"),
            model_name=llm_settings.get("model_name", "gemma3:latest"),
            api_key=llm_settings.get("api_key", "optional_key_here"),
            # システムプロンプト
            system_prompt=config.get("system_prompt", "あなたは親切で有能なAIアシスタントです。"),
            use_enhanced_prompt=prompt_settings.get("use_enhanced_prompt", True),
            include_tool_guidelines=prompt_settings.get("include_tool_guidelines", True),
            # ツールフィルタ
            tool_filter_enabled=tool_filter.get("enabled", True),
            max_tools=tool_filter.get("max_tools", 15),
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


# ============================================
# メッセージ履歴管理クラス
# ============================================
class MessageHistory:
    """
    メッセージ履歴管理クラス
    
    仕様書6.3に基づき、Chainlitの自動管理に任せず手動でリスト管理
    巨大なツール実行結果は「結果の要約」に置換するPruningを実装
    """
    
    def __init__(self, system_prompt: str = ""):
        """
        初期化
        
        Args:
            system_prompt: システムプロンプト
        """
        self.messages: list[dict] = []
        self._system_prompt = system_prompt
        self._tool_call_history: list[dict] = []  # ループ検知用
        
        # システムプロンプトを追加
        if system_prompt:
            self.messages.append({
                "role": "system",
                "content": system_prompt
            })
    
    def add_user_message(self, content: str) -> None:
        """
        ユーザーメッセージを追加
        
        Args:
            content: ユーザー入力テキスト
        """
        self.messages.append({
            "role": "user",
            "content": content
        })
        logger.debug(f"ユーザーメッセージ追加: {content[:50]}...")
    
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
            "tool_calls": openai_tool_calls
        })
        logger.debug(f"ツール呼び出しメッセージ追加: {len(tool_calls)}件")
    
    def add_tool_result(self, tool_call_id: str, tool_name: str, raw_result: dict, summary: str) -> None:
        """
        ツール実行結果を追加（Pruning適用）
        
        仕様書6.3: ツール出力データの即時剪定
        - LLMが回答生成後に、巨大なツール実行結果を極小テキストに置換
        - AIの思考結果のみを履歴に残す
        
        Args:
            tool_call_id: ツール呼び出しID
            tool_name: ツール名
            raw_result: 生のツール実行結果
            summary: 要約テキスト
        """
        # 生の結果からコンテンツを抽出
        content = self._extract_content(raw_result)
        
        # Pruning: 大きなコンテンツは要約に置換
        pruned_content = self._prune_content(content, summary)
        
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": pruned_content
        })
        logger.debug(f"ツール結果追加: {tool_name} -> {pruned_content[:50]}...")
    
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
        - 大きなコンテンツは要約に置換
        - トークン数の概算（1トークン ≈ 2-4文字）
        """
        # 簡易的なトークン数推定（日本語は約2文字/トークン、英語は約4文字/トークン）
        estimated_tokens = len(content) / 3
        
        # ソフトリミットを超える場合は要約を使用
        if estimated_tokens > 1000:  # 約1000トークン以上の場合
            logger.info(f"コンテンツをPruning: {estimated_tokens:.0f}トークン -> 要約")
            return summary
        
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
        LLMに渡すためのコンテキストを取得
        
        Returns:
            OpenAI形式のメッセージリスト
        """
        return self.messages.copy()
    
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
        
        # 強化版システムプロンプトを構築
        current_time = datetime.now().strftime('%Y年%m月%d日 %H時%M分%S秒')
        
        if config.use_enhanced_prompt:
            # 強化版プロンプトを使用（ツールガイドライン付き）
            if config.include_tool_guidelines:
                dynamic_system_prompt = ENHANCED_SYSTEM_PROMPT_TEMPLATE.format(current_time=current_time)
            else:
                # ツールガイドラインなしの強化版
                dynamic_system_prompt = f"""あなたは親切で有能なAIアシスタントです。
ユーザーの質問に丁寧かつ正確に回答してください。

## 現在のシステム時刻
{current_time}
"""
        else:
            # 従来のプロンプトを使用
            dynamic_system_prompt = config.system_prompt + f"\n\n[System Info]\n現在のシステム時刻は {current_time} です。時間に関する質問にはこの時刻を基準に回答してください。"
        
        self.history = MessageHistory(system_prompt=dynamic_system_prompt)
        self._cancel_requested = False  # キルスイッチ用フラグ
        self._tool_call_counter = {}  # 連続呼び出し検知用
        self._rejection_occurred = False  # ユーザー拒否発生フラグ（LLM暴走防止用）
        self._initial_user_input = None  # 初回ユーザー入力保存用（ツールフィルタリング用）
        
        logger.info(f"エージェント初期化完了: model={config.model_name}, base_url={config.base_url}")
    
    # ============================================
    # メインループ
    # ============================================
    async def run(self, user_input: str, server_name: str = None) -> AsyncGenerator[Any, None]:
        """
        自律エージェントのメインループ
        
        仕様書5.3に基づく自律ループ:
        推論 → ツール実行判定 → ツール実行 → 履歴追加 → 推論
        
        Args:
            user_input: ユーザーからの入力テキスト
            server_name: MCPサーバー名（指定時は該当サーバーのツールのみを使用）
            
        Yields:
            Chainlit Step/Message オブジェクト
        """
        # サーバー名をインスタンス変数に保存
        self._server_name = server_name
        
        # ユーザー入力を履歴に追加
        self.history.add_user_message(user_input)
        self._cancel_requested = False
        self._tool_call_counter.clear()
        self._rejection_occurred = False  # 拒否フラグをリセット
        
        loop_count = 0
        max_iterations = 10  # 無限ループ防止の安全策
        
        while not self._cancel_requested and loop_count < max_iterations and not self._rejection_occurred:
            loop_count += 1
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
                    
                    # 【診断ログ】LLM応答の詳細を記録
                    logger.debug(f"[診断] LLM応答: content={llm_response.content[:100] if llm_response.content else 'None'}, tool_calls={len(llm_response.tool_calls)}, finish_reason={llm_response.finish_reason}")
                    
                    if llm_response.content:
                        step.output = f"応答: {llm_response.content[:100]}..."
                    else:
                        step.output = "ツール呼び出しを検知"
                    
                except Exception as e:
                    logger.error(f"LLM呼び出しエラー: {e}")
                    step.output = f"エラーが発生しました: {str(e)}"
                    yield step
                    break
            
            # ========================================
            # Step 2: ツール実行判定
            # ========================================
            if self._has_tool_calls(llm_response):
                tool_calls = self._extract_tool_calls(llm_response)
                
                # 【診断ログ】ツール呼び出しの詳細を記録
                logger.info(f"[診断] ツール呼び出し検知: {len(tool_calls)}件, ツール名: {[tc.name for tc in tool_calls]}")
                
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
                            # 【重要】拒否された場合：強制終了フラグを設定し、ループを抜ける
                            # LLMが代替手段を模索して勝手に実行することを防ぐ
                            self._rejection_occurred = True
                            self.history.add_tool_result(
                                tool_call_id=tool_call.id,
                                tool_name=tool_call.name,
                                raw_result={"rejected": True},
                                summary=rejection_msg
                            )
                            # ただちにツール実行ループを抜け、メインループも終了
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
                            error_msg = f"ツール実行がタイムアウトしました（{self.config.tool_execution_timeout_seconds}秒）"
                            tool_step.output = f"❌ {error_msg}"
                            self.history.add_tool_result(
                                tool_call_id=tool_call.id,
                                tool_name=tool_call.name,
                                raw_result={"error": "timeout"},
                                summary=error_msg
                            )
                            
                        except Exception as e:
                            error_msg = f"ツール実行エラー: {str(e)}"
                            tool_step.output = f"❌ {error_msg}"
                            logger.error(f"ツール実行エラー: {e}")
                            self.history.add_tool_result(
                                tool_call_id=tool_call.id,
                                tool_name=tool_call.name,
                                raw_result={"error": str(e)},
                                summary=error_msg
                            )
                        
                        yield tool_step
                
                # ツール実行後、LLMがcontentを同時に返していた場合はループ終了
                # （LLMが「このツール実行が最後」として報告を含めて返すケース）
                if llm_response.content and not self._rejection_occurred:
                    logger.info(f"[診断] ツール実行後のcontent応答を検知、ループ終了: {llm_response.content[:100]}...")
                    self.history.add_assistant_message(llm_response.content)
                    async with cl.Step(name="応答") as response_step:
                        response_step.output = llm_response.content
                        yield response_step
                    break  # whileループを抜けて終了
                
                # 履歴追加後、再度推論へ（ループ継続）
                continue
            
            # ========================================
            # ツール呼び出しなし → 自然言語応答
            # ========================================
            if llm_response.content:
                # 【診断ログ】自然言語応答を検知
                logger.info(f"[診断] 自然言語応答を検知、ループ終了: {llm_response.content[:100]}...")
                self.history.add_assistant_message(llm_response.content)
                
                async with cl.Step(name="応答") as response_step:
                    response_step.output = llm_response.content
                    yield response_step
            else:
                # 【診断ログ】自然言語応答なし（異常終了の可能性）
                logger.warning(f"[診断] 自然言語応答なし、contentが空です。finish_reason={llm_response.finish_reason}")
            
            break  # ループ終了
        
        if loop_count >= max_iterations:
            async with cl.Step(name="⚠️ 最大反復回数") as warn_step:
                warn_step.output = f"最大反復回数（{max_iterations}回）に達しました。処理を終了します。"
                yield warn_step
    
    # ============================================
    # 内部メソッド（プライベート）
    # ============================================
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
        tools = await self.mcp_manager.get_tools_for_llm(
            user_input=user_input,
            max_tools=self.config.max_tools if self.config.tool_filter_enabled else None,
            compression_mode=self.config.compression_mode if self.config.tool_filter_enabled else "full",
            always_include=self.config.always_include if self.config.tool_filter_enabled else None,
            server_name=getattr(self, '_server_name', None)
        )
        
        # リクエストボディを構築
        request_body = {
            "model": self.config.model_name,
            "messages": self.history.get_context_for_llm(),
            "temperature": 0.7,
            "max_tokens": 4096
        }
        
        # ツールが存在する場合は追加
        if tools:
            request_body["tools"] = tools
            request_body["tool_choice"] = "auto"
        
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
                logger.error(f"LLM API 接続エラー: {e}")
                raise Exception(f"LLM接続エラー: {str(e)}")
    
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
        content = message.get("content", "")
        
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
            finish_reason=finish_reason
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
        ツール実行結果の要約（Pruning用）
        
        仕様書6.3に基づく要約生成
        
        Args:
            tool_name: ツール名
            raw_result: 生のツール実行結果
            
        Returns:
            要約テキスト
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
        
        # 長い場合は要約形式に変換
        if len(result_text) > 500:
            return f"[{tool_name}] 実行完了（データは読み込み済み）"
        
        return result_text
    
    def _detect_loop(self, tool_calls: list[ToolCall]) -> bool:
        """
        反復ループ検知（仕様書5.3.1）
        
        3段階の検知を実施:
        1. 同じツール＋同じ引数の連続呼び出し（厳密一致）
        2. 同じツール名の連続呼び出し（引数違いも検知）
        3. ツール呼び出し総数の上限チェック（ポーリング全般を検知）
        
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
                # ポーリング系は max_repeated_loops * 2 + 1 回まで許容
                name_threshold = self.config.max_repeated_loops * 2 + 1
            else:
                # 通常ツールは max_repeated_loops + 1 回まで
                name_threshold = self.config.max_repeated_loops + 1
            
            if self._tool_call_counter[name_key] >= name_threshold:
                logger.warning(f"ループ検知（ツール名）: {tc.name} が {self._tool_call_counter[name_key]}回呼び出し")
                return True
        
        # --- 検知3: ツール呼び出し総数の上限チェック ---
        # あらゆるパターンのポーリング・ループを包括的に検知
        total_calls = sum(
            v for k, v in self._tool_call_counter.items()
            if k.startswith("_name:")
        )
        # 1セッション中のツール呼び出し総数が max_repeated_loops * 3 を超えたら検知
        total_threshold = self.config.max_repeated_loops * 3
        if total_calls > total_threshold:
            logger.warning(f"ループ検知（総数）: ツール呼び出し総数 {total_calls} が閾値 {total_threshold} を超過")
            return True
        
        # --- 検知1のカウンターベース（厳密一致: ツール名+引数）---
        for tc in tool_calls:
            key = f"{tc.name}:{json.dumps(tc.arguments, sort_keys=True)}"
            self._tool_call_counter[key] = self._tool_call_counter.get(key, 0) + 1
            
            if self._tool_call_counter[key] >= self.config.max_repeated_loops:
                logger.warning(f"ループ検知（カウンター）: {key} が {self._tool_call_counter[key]}回")
                return True
        
        return False
    
    def cancel(self):
        """外部から処理の強制停止を要求する"""
        self._cancel_requested = True
        logger.info("キャンセル要求を受け付けました")