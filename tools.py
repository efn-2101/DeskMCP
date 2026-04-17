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
import ipaddress
from typing import Optional, Any, List, Tuple
from enum import Enum
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
    keywords: dict = field(default_factory=dict)  # サーバ固有キーワード設定
    
    @property
    def transport_type(self) -> str:
        """トランスポート種別を判定（'sse' or 'stdio'）"""
        if self.url:
            return "sse"
        return "stdio"
    
    def get_keywords(self) -> List[str]:
        """サーバ固有のキーワードリストを取得"""
        if self.keywords:
            return self.keywords.get("include", [])
        return []


# ============================================
# サーバ推定関連の定数・クラス定義
# ============================================

# 明示的サーバ指定パターン（正規表現）
EXPLICIT_SERVER_PATTERNS = {
    "DeskToDo": [
        r"DeskToDo[でのを]?",
        r"デスクトゥドゥ[でのを]?",
        r"タスク管理[でのを]?",
        r"やること[でのを]?",
        r"ToDo[でのを]?",
        r"todo[でのを]?"
    ],
    "local-rag": [
        r"RAG[でのを]?",
        r"ラグ[でのを]?",
        r"ドキュメント[でのを]?",
        r"文書[でのを]?",
        r"ナレッジ[でのを]?"
    ],
    "redmine": [
        r"Redmine[でのを]?",
        r"レッドマイン[でのを]?",
        r"チケット[でのを]?",
        r"issue[でのを]?",
        r"プロジェクト管理[でのを]?"
    ]
}

# サーバ固有の除外キーワード（他サーバを排除）
SERVER_EXCLUSIVE_KEYWORDS = {
    "DeskToDo": {
        "exclusive": ["やること", "ToDo", "todo", "タスク管理", "デスクトゥドゥ"],
        "weight": 2.0
    },
    "local-rag": {
        "exclusive": ["RAG", "ラグ", "ドキュメント", "文書", "インデックス", "embedding", "ベクトル", "ナレッジ"],
        "weight": 2.0
    },
    "redmine": {
        "exclusive": ["チケット", "issue", "Redmine", "レッドマイン", "tracker", "プロジェクト管理"],
        "weight": 2.0
    }
}

# デフォルトサーバキーワード定義
DEFAULT_SERVER_KEYWORDS = {
    "DeskToDo": {
        "keywords": [
            # 日本語キーワード
            "タスク", "やること", "ToDo", "todo", "とど",
            "期日", "期限", "完了", "進捗", "管理",
            # 英語キーワード
            "task", "pending", "complete", "due",
            # アクション系
            "追加して", "登録して", "一覧", "表示して"
        ],
        "weight": 1.0
    },
    "local-rag": {
        "keywords": [
            # 日本語キーワード
            "ドキュメント", "文書", "RAG", "らぐ", "検索",
            "インデックス", "同期", "ナレッジ",
            # 英語キーワード
            "document", "rag", "search", "index", "sync",
            "knowledge", "embedding", "ベクトル"
        ],
        "weight": 1.0
    },
    "redmine": {
        "keywords": [
            # 日本語キーワード
            "Redmine", "レッドマイン", "チケット", "issue",
            "プロジェクト管理", "課題", "バグ", "不具合",
            # 英語キーワード
            "redmine", "ticket", "issue", "project",
            "tracker", "status", "priority", "assign"
        ],
        "weight": 1.0
    }
}


@dataclass
class ServerEstimationResult:
    """サーバ推定結果"""
    server_name: str
    confidence: float  # 0.0 - 1.0
    matched_keywords: List[str] = field(default_factory=list)
    match_type: str = "keyword"  # "explicit", "exclusive", "context", "keyword", "default"


class ServerContext:
    """サーバ使用コンテキスト管理"""
    
    def __init__(self):
        self._last_used_server: Optional[str] = None
        self._server_usage_history: List[str] = []
        self._max_history = 5
    
    def record_tool_usage(self, server_name: str):
        """ツール使用を記録"""
        self._last_used_server = server_name
        self._server_usage_history.append(server_name)
        if len(self._server_usage_history) > self._max_history:
            self._server_usage_history.pop(0)
    
    def get_context_server(self) -> Optional[str]:
        """コンテキストから推定されるサーバ"""
        if not self._server_usage_history:
            return None
        
        # 直近3回の使用履歴から最も頻繁なサーバを返す
        recent = self._server_usage_history[-3:]
        from collections import Counter
        counter = Counter(recent)
        most_common = counter.most_common(1)
        if most_common and most_common[0][1] >= 2:
            return most_common[0][0]
        return self._last_used_server
    
    def clear(self):
        """コンテキストをクリア"""
        self._last_used_server = None
        self._server_usage_history.clear()


class ServerEstimator:
    """ユーザ入力からMCPサーバを推定するクラス"""
    
    def __init__(self, server_keywords: dict = None):
        self.server_keywords = server_keywords or DEFAULT_SERVER_KEYWORDS
    
    def estimate(self, user_input: str) -> List[ServerEstimationResult]:
        """
        ユーザ入力から対象サーバを推定
        
        Args:
            user_input: ユーザ入力テキスト
            
        Returns:
            信頼度順にソートされたサーバ推定結果リスト
        """
        input_lower = user_input.lower()
        results = []
        
        for server_name, config in self.server_keywords.items():
            keywords = config.get("keywords", [])
            weight = config.get("weight", 1.0)
            
            # キーワードマッチング
            matched = []
            for kw in keywords:
                if kw.lower() in input_lower:
                    matched.append(kw)
            
            if matched:
                # スコア計算: マッチ数 * 重み / 正規化係数
                score = len(matched) * weight / max(len(keywords), 1)
                results.append(ServerEstimationResult(
                    server_name=server_name,
                    confidence=min(score, 1.0),
                    matched_keywords=matched,
                    match_type="keyword"
                ))
        
        # 信頼度順にソート
        return sorted(results, key=lambda x: x.confidence, reverse=True)


class EnhancedServerEstimator:
    """拡張版サーバ推定クラス（キーワード重複対応）"""
    
    def __init__(self, context: ServerContext = None):
        self.context = context or ServerContext()
        self.keyword_estimator = ServerEstimator()
    
    def detect_explicit_server(self, user_input: str) -> Optional[str]:
        """明示的なサーバ指定を検出"""
        for server_name, patterns in EXPLICIT_SERVER_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, user_input, re.IGNORECASE):
                    return server_name
        return None
    
    def estimate_by_exclusive_keywords(self, user_input: str) -> Optional[str]:
        """除外キーワードによるサーバ推定"""
        input_lower = user_input.lower()
        
        for server_name, config in SERVER_EXCLUSIVE_KEYWORDS.items():
            for keyword in config["exclusive"]:
                if keyword.lower() in input_lower:
                    return server_name
        return None
    
    def estimate(self, user_input: str) -> List[ServerEstimationResult]:
        """
        優先度順にサーバを推定
        
        優先度:
        1. 明示的指定
        2. 除外キーワード
        3. コンテキスト継承
        4. キーワードマッチング
        """
        results = []
        
        # 1. 明示的指定を検出
        explicit = self.detect_explicit_server(user_input)
        if explicit:
            results.append(ServerEstimationResult(
                server_name=explicit,
                confidence=1.0,
                matched_keywords=["explicit"],
                match_type="explicit"
            ))
            return results  # 明示的指定は最優先
        
        # 2. 除外キーワードによる推定
        exclusive = self.estimate_by_exclusive_keywords(user_input)
        if exclusive:
            results.append(ServerEstimationResult(
                server_name=exclusive,
                confidence=0.9,
                matched_keywords=["exclusive"],
                match_type="exclusive"
            ))
            return results  # 除外キーワードも高信頼度
        
        # 3. コンテキスト継承
        context_server = self.context.get_context_server()
        if context_server:
            results.append(ServerEstimationResult(
                server_name=context_server,
                confidence=0.7,
                matched_keywords=["context"],
                match_type="context"
            ))
        
        # 4. キーワードマッチング（従来ロジック）
        keyword_results = self.keyword_estimator.estimate(user_input)
        
        # コンテキスト結果と統合
        for kr in keyword_results:
            # 既にコンテキストで推定されている場合は信頼度を加算
            existing = next((r for r in results if r.server_name == kr.server_name), None)
            if existing:
                existing.confidence = min(existing.confidence + kr.confidence * 0.3, 1.0)
                existing.matched_keywords.extend(kr.matched_keywords)
            else:
                results.append(kr)
        
        # 信頼度順にソート
        results.sort(key=lambda x: x.confidence, reverse=True)
        
        # デフォルト（DeskToDo）を追加
        if not results:
            results.append(ServerEstimationResult(
                server_name="DeskToDo",
                confidence=0.5,
                matched_keywords=["default"],
                match_type="default"
            ))
        
        return results


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


# ============================================
# スコアリング・クオータ管理クラス
# ============================================

@dataclass
class ScoringRule:
    """スコアリングルール"""
    name: str
    keywords: list
    tool_patterns: list
    weight: float
    
    def matches_keyword(self, text: str) -> bool:
        """キーワードがテキストにマッチするか"""
        text_lower = text.lower()
        return any(kw in text_lower for kw in self.keywords)
    
    def matches_tool(self, tool_name: str) -> bool:
        """ツール名がパターンにマッチするか"""
        return any(fnmatch.fnmatch(tool_name.lower(), pattern.lower()) for pattern in self.tool_patterns)
    
    def calculate_score(self, text: str, tool_name: str) -> float:
        """
        スコアを計算
        
        Args:
            text: ユーザー入力テキスト
            tool_name: ツール名
            
        Returns:
            スコア（キーワードとツールの両方がマッチした場合はweight、それ以外は0）
        """
        if self.matches_keyword(text) and self.matches_tool(tool_name):
            return self.weight
        return 0.0


class ScoringRuleRegistry:
    """スコアリングルールのレジストリ"""
    
    DEFAULT_RULES = [
        ScoringRule("communication_check", ["通信確認", "接続確認", "通信テスト", "接続テスト", "通信", "接続", "確認して", "確認", "テスト"], ["get*", "list*", "status*", "check*"], 2.5),
        ScoringRule("create_action", ["作成", "追加", "新規", "登録", "作って", "追加して"], ["create*", "add*", "new*", "insert*"], 2.0),
        ScoringRule("update_action", ["更新", "変更", "修正", "編集", "変えて", "修正して"], ["update*", "edit*", "modify*", "change*"], 2.0),
        ScoringRule("delete_action", ["削除", "消去", "削除して", "消して", "削る"], ["delete*", "remove*", "destroy*"], 2.0),
        ScoringRule("search_action", ["検索", "探して", "探す", "検索して", "見つけて"], ["search*", "find*", "get*", "list*"], 1.5),
    ]
    
    def __init__(self, config_path: str = None):
        """
        初期化
        
        Args:
            config_path: 設定ファイルのパス（指定しない場合はデフォルトルールを使用）
        """
        self.rules = self.DEFAULT_RULES.copy()
        if config_path:
            self.load_from_file(config_path)
    
    def load_from_file(self, config_path: str) -> bool:
        """
        設定ファイルからルールをロード
        
        Args:
            config_path: 設定ファイルのパス
            
        Returns:
            ロード成功フラグ
        """
        try:
            path = Path(config_path)
            if not path.exists():
                logger.warning(f"スコアリングルール設定ファイルが見つかりません: {config_path}")
                return False
            
            with open(path, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            rules_data = config.get("scoring_rules", [])
            for rule_data in rules_data:
                rule = ScoringRule(
                    name=rule_data.get("name", ""),
                    keywords=rule_data.get("keywords", []),
                    tool_patterns=rule_data.get("tool_patterns", []),
                    weight=rule_data.get("weight", 1.0)
                )
                self.rules.append(rule)
            
            logger.info(f"スコアリングルールをロードしました: {len(rules_data)}件")
            return True
        except Exception as e:
            logger.error(f"スコアリングルールのロードに失敗: {e}")
            return False
    
    def calculate_total_score(self, text: str, tool_name: str) -> float:
        """
        全ルールのスコアを合計
        
        Args:
            text: ユーザー入力テキスト
            tool_name: ツール名
            
        Returns:
            合計スコア
        """
        return sum(rule.calculate_score(text, tool_name) for rule in self.rules)


@dataclass
class ScoredTool:
    """スコア付きツール"""
    tool: ToolSchema
    score: float
    match_reasons: List[str] = field(default_factory=list)


class ToolScorer:
    """ツールスコアリングクラス"""
    
    # スコアリング重み
    WEIGHTS = {
        "server_match": 3.0,
        "category_match": 2.0,
        "tool_name_match": 1.5,
        "description_match": 1.0,
        "pattern_match": 1.2,
        "communication_check": 2.5  # 通信確認キーワード用の重み
    }
    
    # 通信確認・接続確認関連のキーワード
    COMMUNICATION_KEYWORDS = [
        "通信確認", "接続確認", "通信テスト", "接続テスト",
        "通信", "接続", "確認して", "確認", "テスト",
        "communication", "connect", "connection", "test", "check"
    ]
    
    # 通信確認に適したツール名パターン（get*, list*, status系）
    COMMUNICATION_TOOL_PATTERNS = [
        "get*", "list*", "status*", "check*", "fetch*", "query*"
    ]
    
    def __init__(self, scoring_config_path: str = None):
        """
        初期化
        
        Args:
            scoring_config_path: スコアリングルール設定ファイルのパス（指定しない場合はデフォルトルールを使用）
        """
        self._rule_registry = ScoringRuleRegistry(scoring_config_path)
        logger.debug(f"ToolScorer初期化: ルール数={len(self._rule_registry.rules)}")
    
    def score_tools(
        self,
        tools: List[ToolSchema],
        user_input: str,
        estimated_servers: List[ServerEstimationResult],
        categories: List[ToolCategory]
    ) -> List[ScoredTool]:
        """
        ツールをスコアリング
        
        Args:
            tools: ツールリスト
            user_input: ユーザ入力
            estimated_servers: 推定サーバリスト
            categories: カテゴリリスト
            
        Returns:
            スコア順にソートされたツールリスト
        """
        scored_tools = []
        input_lower = user_input.lower()
        
        # 優先サーバ名を取得（信頼度が閾値以上）
        priority_servers = {
            s.server_name for s in estimated_servers
            if s.confidence >= 0.3
        }
        
        # 通信確認キーワードが含まれているかチェック
        has_communication_keyword = any(
            kw in input_lower for kw in self.COMMUNICATION_KEYWORDS
        )
        
        for tool in tools:
            score = 0.0
            reasons = []
            
            # 1. サーバ一致ボーナス
            if tool.server_name in priority_servers:
                score += self.WEIGHTS["server_match"]
                reasons.append(f"server:{tool.server_name}")
            
            # 2. カテゴリ一致
            for category in categories:
                for keyword in category.keywords:
                    if keyword.lower() in input_lower:
                        for pattern in category.tool_patterns:
                            if fnmatch.fnmatch(tool.name, pattern):
                                score += self.WEIGHTS["category_match"]
                                reasons.append(f"category:{category.id}")
                                break
            
            # 3. ツール名一致
            tool_name_lower = tool.name.lower()
            words = self._extract_words(user_input)
            for word in words:
                if word in tool_name_lower:
                    score += self.WEIGHTS["tool_name_match"]
                    reasons.append(f"name:{word}")
            
            # 4. 説明一致
            if tool.description:
                desc_lower = tool.description.lower()
                for word in words:
                    if word in desc_lower:
                        score += self.WEIGHTS["description_match"]
                        reasons.append(f"desc:{word}")
            
            # 5. 通信確認キーワード + get*/list*ツールのボーナス
            # 「通信確認」などのキーワードがある場合、get*/list*系ツールを優先
            if has_communication_keyword:
                for pattern in self.COMMUNICATION_TOOL_PATTERNS:
                    if fnmatch.fnmatch(tool.name, pattern):
                        score += self.WEIGHTS["communication_check"]
                        reasons.append(f"communication_check:{tool.name}")
                        break
            
            # 6. ルールベースのスコアリング（ScoringRuleRegistryを使用）
            rule_score = self._rule_registry.calculate_total_score(user_input, tool.name)
            if rule_score > 0:
                score += rule_score
                reasons.append(f"rule_score:{rule_score:.1f}")
            
            scored_tools.append(ScoredTool(
                tool=tool,
                score=score,
                match_reasons=reasons
            ))
        
        # スコア順にソート
        return sorted(scored_tools, key=lambda x: x.score, reverse=True)
    
    def _extract_words(self, text: str) -> List[str]:
        """テキストから単語を抽出"""
        words = [w for w in text.lower().split() if len(w) >= 2]
        # ツール名パターンも抽出
        tool_patterns = re.findall(r'[a-zA-Z_][a-zA-Z0-9_]*', text)
        words.extend([p.lower() for p in tool_patterns if len(p) >= 2])
        return list(set(words))


class QuotaManager:
    """サーバ間クオータ管理クラス"""
    
    def calculate_quotas(
        self,
        server_names: List[str],
        max_tools: int,
        priority_server: str = None,
        priority_ratio: float = 0.5,
        min_quota: int = 2
    ) -> dict:
        """
        サーバ別のツール割当数を計算
        
        Args:
            server_names: サーバ名リスト
            max_tools: 最大ツール数
            priority_server: 優先サーバ名（Noneの場合は均等）
            priority_ratio: 優先サーバへの割当比率（0.0-1.0）
            min_quota: 各サーバの最低保証ツール数
            
        Returns:
            サーバ名→割当数の辞書
        """
        num_servers = len(server_names)
        if num_servers == 0:
            return {}
        
        quotas = {}
        
        if priority_server and priority_server in server_names:
            # 優先サーバがある場合
            priority_slots = int(max_tools * priority_ratio)
            remaining_slots = max_tools - priority_slots
            other_servers = [s for s in server_names if s != priority_server]
            other_slots_per_server = remaining_slots // max(len(other_servers), 1)
            
            # 最低保証を確保
            for name in server_names:
                if name == priority_server:
                    quotas[name] = max(priority_slots, min_quota)
                else:
                    quotas[name] = max(other_slots_per_server, min_quota)
        else:
            # 均等配分
            slots_per_server = max_tools // num_servers
            remainder = max_tools % num_servers
            
            for i, name in enumerate(server_names):
                base = slots_per_server + (1 if i < remainder else 0)
                quotas[name] = max(base, min_quota)
        
        return quotas
    
    def apply_round_robin(
        self,
        scored_tools: List[ScoredTool],
        quotas: dict,
        max_tools: int
    ) -> List[ToolSchema]:
        """
        ラウンドロビン方式でツールを配置
        
        各サーバからクオータ数まで順番にツールを取り出す
        """
        # サーバ別にツールをグループ化
        tools_by_server: dict = {}
        for st in scored_tools:
            if st.tool.server_name not in tools_by_server:
                tools_by_server[st.tool.server_name] = []
            tools_by_server[st.tool.server_name].append(st)
        
        # 各サーバ内でスコア順にソート
        for server_name in tools_by_server:
            tools_by_server[server_name].sort(key=lambda x: x.score, reverse=True)
        
        # ラウンドロビンで配置
        result = []
        server_names = list(tools_by_server.keys())
        indices = {name: 0 for name in server_names}
        
        while len(result) < max_tools:
            added = False
            for server_name in server_names:
                if len(result) >= max_tools:
                    break
            
                quota = quotas.get(server_name, 0)
                current_count = sum(1 for t in result if t.server_name == server_name)
            
                if current_count < quota and indices[server_name] < len(tools_by_server[server_name]):
                    tool = tools_by_server[server_name][indices[server_name]].tool
                    result.append(tool)
                    indices[server_name] += 1
                    added = True
            
            if not added:
                break  # すべてのサーバでクオータに到達
        
        return result


class ToolFilter:
    """ツールフィルタリングクラス（スコアリング対応版）"""
    
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
    
    def __init__(self, categories: List[ToolCategory] = None, server_context: ServerContext = None, scoring_config_path: str = None):
        self.categories = categories or DEFAULT_CATEGORIES
        self.server_estimator = EnhancedServerEstimator(server_context)
        self.tool_scorer = ToolScorer(scoring_config_path)
        self.quota_manager = QuotaManager()
    
    def filter_by_user_input(
        self,
        user_input: str,
        all_tools: List[ToolSchema],
        max_tools: int = 15,
        always_include: List[str] = None,
        server_quota: int = None,
        enable_server_boost: bool = True,
        use_scoring: bool = True
    ) -> List[ToolSchema]:
        """
        ユーザー入力に基づいてツールをフィルタリング（スコアリング対応版）
        
        Args:
            user_input: ユーザー入力テキスト
            all_tools: 全ツールのリスト
            max_tools: 最大ツール数
            always_include: 常に含めるツール名のリスト
            server_quota: 各サーバの最低保証ツール数（Noneで自動計算）
            enable_server_boost: サーバ優先度ブーストの有効/無効
            use_scoring: スコアリングを使用するかどうか
            
        Returns:
            フィルタリングされたツールリスト
        """
        # 【診断ログ】入力と全ツールを記録
        logger.info(f"[診断] filter_by_user_input 呼び出し:")
        logger.info(f"  user_input: {user_input[:100] if user_input else 'None'}...")
        logger.info(f"  all_tools数: {len(all_tools)}")
        logger.info(f"  all_tools名一覧: {[t.name for t in all_tools]}")
        logger.info(f"  enable_server_boost={enable_server_boost}, use_scoring={use_scoring}")
        
        if not user_input:
            return self._apply_round_robin_fallback(all_tools, max_tools)
        
        always_include = always_include or []
        
        # 新しいスコアリングベースのフィルタリング
        if use_scoring and enable_server_boost:
            return self._filter_with_scoring(
                user_input, all_tools, max_tools, always_include, server_quota
            )
        
        # 従来のフィルタリング（フォールバック）
        return self._legacy_filter(user_input, all_tools, max_tools, always_include)
    
    def _filter_with_scoring(
        self,
        user_input: str,
        all_tools: List[ToolSchema],
        max_tools: int,
        always_include: List[str],
        server_quota: int = None
    ) -> List[ToolSchema]:
        """スコアリングベースのフィルタリング"""
        
        # Step 1: サーバ推定
        estimated_servers = self.server_estimator.estimate(user_input)
        logger.info(f"[診断] Step1 サーバ推定: {[(s.server_name, s.confidence, s.match_type) for s in estimated_servers]}")
        
        # Step 2: ツールスコアリング
        scored_tools = self.tool_scorer.score_tools(
            all_tools, user_input, estimated_servers, self.categories
        )
        logger.info(f"[診断] Step2 スコアリング完了: 上位5件 {[(st.tool.name, st.score) for st in scored_tools[:5]]}")
        
        # Step 3: サーバ名リストを取得
        server_names = list(set(t.server_name for t in all_tools if t.server_name))
        
        # Step 4: 優先サーバを決定
        priority_server = None
        if estimated_servers and estimated_servers[0].confidence >= 0.7:
            priority_server = estimated_servers[0].server_name
        
        # Step 5: クオータ計算
        min_quota = server_quota if server_quota else max(2, max_tools // max(len(server_names), 1))
        quotas = self.quota_manager.calculate_quotas(
            server_names, max_tools, priority_server, min_quota=min_quota
        )
        logger.info(f"[診断] Step3 クオータ計算: {quotas}")
        
        # Step 6: ラウンドロビン配置
        final_tools = self.quota_manager.apply_round_robin(scored_tools, quotas, max_tools)
        logger.info(f"[診断] Step4 ラウンドロビン配置: {[t.name for t in final_tools]}")
        
        # Step 7: always_includeツールを追加
        always_tools = [t for t in all_tools if t.name in always_include]
        if always_tools:
            # 重複を排除しながら追加
            seen = set(t.name for t in final_tools)
            for tool in always_tools:
                if tool.name not in seen:
                    final_tools.insert(0, tool)
                    seen.add(tool.name)
            logger.info(f"[診断] Step5 always_include追加: {[t.name for t in always_tools]}")
        
        # max_tools制限を適用
        final_tools = final_tools[:max_tools]
        logger.info(f"[診断] 最終結果: {[t.name for t in final_tools]}")
        
        # フォールバック: 結果が空の場合
        if not final_tools and all_tools:
            logger.info(f"スコアリング結果が空のため、ラウンドロビンフォールバックを使用")
            return self._apply_round_robin_fallback(all_tools, max_tools)
        
        return final_tools
    
    def _legacy_filter(
        self,
        user_input: str,
        all_tools: List[ToolSchema],
        max_tools: int,
        always_include: List[str]
    ) -> List[ToolSchema]:
        """従来のフィルタリングロジック（フォールバック用）"""
        
        # Step 1: カテゴリマッチング
        matched_categories = self._match_categories(user_input)
        logger.info(f"[診断] Legacy Step1 カテゴリマッチング: {[c.id for c in matched_categories]}")
        
        # Step 2: ツール名パターンマッチング
        candidate_tools = self._match_tool_patterns(all_tools, matched_categories)
        logger.info(f"[診断] Legacy Step2 パターンマッチング結果: {[t.name for t in candidate_tools]}")
        
        # Step 3: キーワードベースの追加マッチング
        keyword_tools = self._match_by_keywords(user_input, all_tools)
        logger.info(f"[診断] Legacy Step3 キーワードマッチング結果: {[t.name for t in keyword_tools]}")
        
        # Step 4: 動的カテゴリマッチング（ツール説明から抽出）
        dynamic_tools = self._match_by_description(user_input, all_tools)
        logger.info(f"[診断] Legacy Step4 動的マッチング結果: {[t.name for t in dynamic_tools]}")
        
        # Step 5: 常に含めるツールを追加
        always_tools = [t for t in all_tools if t.name in always_include]
        logger.info(f"[診断] Legacy Step5 always_include結果: {[t.name for t in always_tools]}")
        
        # Step 6: 結合・重複排除・制限
        final_tools = self._merge_and_limit(
            candidate_tools,
            keyword_tools,
            always_tools,
            max_tools,
            dynamic_tools
        )
        logger.info(f"[診断] Legacy Step6 最終結果: {[t.name for t in final_tools]}")
        
        # フォールバック: マッチするツールがない場合は全ツールから返す
        if not final_tools and all_tools:
            logger.info(f"カテゴリマッチング結果が空のため、全ツールから{max_tools}件を返します")
            return all_tools[:max_tools]
        
        return final_tools
    
    def _apply_round_robin_fallback(
        self,
        all_tools: List[ToolSchema],
        max_tools: int
    ) -> List[ToolSchema]:
        """ラウンドロビン方式でツールを配置（フォールバック用）"""
        # サーバ別にツールをグループ化
        tools_by_server: dict = {}
        for tool in all_tools:
            if tool.server_name not in tools_by_server:
                tools_by_server[tool.server_name] = []
            tools_by_server[tool.server_name].append(tool)
        
        server_names = list(tools_by_server.keys())
        quotas = self.quota_manager.calculate_quotas(server_names, max_tools)
        
        result = []
        indices = {name: 0 for name in server_names}
        
        while len(result) < max_tools:
            added = False
            for server_name in server_names:
                if len(result) >= max_tools:
                    break
                
                quota = quotas.get(server_name, 0)
                current_count = sum(1 for t in result if t.server_name == server_name)
                
                if current_count < quota and indices[server_name] < len(tools_by_server[server_name]):
                    result.append(tools_by_server[server_name][indices[server_name]])
                    indices[server_name] += 1
                    added = True
            
            if not added:
                break
        
        return result
    
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
# デフォルトパラメータ値生成クラス
# ============================================
class DefaultParameterValueGenerator:
    """必須パラメータのデフォルト値を自動生成するクラス"""
    
    @staticmethod
    def generate_default_value(prop_schema: dict) -> Any:
        """
        プロパティスキーマに基づいてデフォルト値を生成
        
        Args:
            prop_schema: JSON Schema形式のプロパティ定義
            
        Returns:
            デフォルト値
        """
        # const制約がある場合、その値を使用
        if "const" in prop_schema:
            return prop_schema["const"]
        
        prop_type = prop_schema.get("type", "string")
        
        if prop_type == "string":
            # enumがある場合は最初の値を使用
            enum_values = prop_schema.get("enum")
            if enum_values and len(enum_values) > 0:
                return enum_values[0]
            return ""
        elif prop_type in ("number", "integer"):
            # minimum/maximumがある場合は中間値
            minimum = prop_schema.get("minimum")
            maximum = prop_schema.get("maximum")
            if minimum is not None and maximum is not None:
                return (minimum + maximum) // 2 if prop_type == "integer" else (minimum + maximum) / 2
            if minimum is not None:
                return minimum
            if maximum is not None:
                return maximum
            return 0
        elif prop_type == "boolean":
            return False
        elif prop_type == "array":
            return []
        elif prop_type == "object":
            # ネストされた必須プロパティを処理
            return DefaultParameterValueGenerator.generate_object_defaults(prop_schema)
        
        return None
    
    @staticmethod
    def generate_object_defaults(object_schema: dict) -> dict:
        """
        オブジェクト型のデフォルト値を生成
        
        Args:
            object_schema: オブジェクト型のスキーマ定義
            
        Returns:
            デフォルト値を持つオブジェクト
        """
        result = {}
        properties = object_schema.get("properties", {})
        required = object_schema.get("required", [])
        
        for prop_name in required:
            if prop_name in properties:
                result[prop_name] = DefaultParameterValueGenerator.generate_default_value(properties[prop_name])
        
        return result
    
    @staticmethod
    def fill_missing_required_params(tool_name: str, arguments: dict, tool_schema: dict) -> dict:
        """
        欠けている必須パラメータにデフォルト値を設定
        
        Args:
            tool_name: ツール名
            arguments: 現在の引数
            tool_schema: ツールの入力スキーマ
            
        Returns:
            デフォルト値が設定された引数
        """
        if not tool_schema:
            return arguments
            
        result = arguments.copy() if arguments else {}
        properties = tool_schema.get("properties", {})
        required = tool_schema.get("required", [])
        
        for param_name in required:
            if param_name not in result or result[param_name] is None:
                if param_name in properties:
                    result[param_name] = DefaultParameterValueGenerator.generate_default_value(properties[param_name])
                    logger.info(f"[{tool_name}] 必須パラメータ '{param_name}' にデフォルト値を設定: {result[param_name]}")
        
        return result


# ============================================
# エラーハンドリング
# ============================================
class ErrorType(Enum):
    """ツール実行エラーの種類"""
    MISSING_REQUIRED = "missing_required"
    TYPE_ERROR = "type_error"
    PERMISSION_ERROR = "permission_error"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    UNKNOWN = "unknown"


class ToolExecutionErrorHandler:
    """ツール実行エラーを処理するクラス"""
    
    @staticmethod
    def classify_error(error: Exception) -> ErrorType:
        """
        エラーを分類
        
        Args:
            error: 発生した例外
            
        Returns:
            エラーの種類
        """
        error_message = str(error).lower()
        
        # 必須パラメータ不足
        if any(keyword in error_message for keyword in ["required", "missing", "mandatory"]):
            return ErrorType.MISSING_REQUIRED
        
        # 型エラー
        if any(keyword in error_message for keyword in ["type", "invalid", "expected"]):
            return ErrorType.TYPE_ERROR
        
        # 権限エラー
        if any(keyword in error_message for keyword in ["permission", "forbidden", "unauthorized", "access denied"]):
            return ErrorType.PERMISSION_ERROR
        
        # タイムアウト
        if any(keyword in error_message for keyword in ["timeout", "timed out"]):
            return ErrorType.TIMEOUT
        
        # 接続エラー
        if any(keyword in error_message for keyword in ["connection", "connect", "network", "unreachable"]):
            return ErrorType.CONNECTION_ERROR
        
        return ErrorType.UNKNOWN
    
    @staticmethod
    def generate_user_feedback(error: Exception, tool_name: str, missing_params: list = None) -> str:
        """
        ユーザーへのフィードバックメッセージを生成
        
        Args:
            error: 発生した例外
            tool_name: ツール名
            missing_params: 欠けているパラメータのリスト
            
        Returns:
            ユーザーへのフィードバックメッセージ
        """
        error_type = ToolExecutionErrorHandler.classify_error(error)
        
        if error_type == ErrorType.MISSING_REQUIRED:
            if missing_params:
                return f"ツール '{tool_name}' の実行には以下のパラメータが必要です: {', '.join(missing_params)}"
            return f"ツール '{tool_name}' の実行に必要なパラメータが不足しています"
        
        if error_type == ErrorType.TYPE_ERROR:
            return f"ツール '{tool_name}' に渡されたパラメータの型が正しくありません"
        
        if error_type == ErrorType.PERMISSION_ERROR:
            return f"ツール '{tool_name}' を実行する権限がありません"
        
        if error_type == ErrorType.TIMEOUT:
            return f"ツール '{tool_name}' の実行がタイムアウトしました"
        
        if error_type == ErrorType.CONNECTION_ERROR:
            return f"ツール '{tool_name}' の実行中に接続エラーが発生しました"
        
        return f"ツール '{tool_name}' の実行中にエラーが発生しました: {str(error)}"


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
        
        # SSE接続用のカスタムHTTPクライアントファクトリ
        _httpx_client_factory = None
        if transport == "sse" and self.config.url:
            # SSE接続先がローカルネットワークの場合、プロキシを無効化したクライアントを使用
            from urllib.parse import urlparse as _urlparse
            import httpx
            _parsed_url = _urlparse(self.config.url)
            _hostname = _parsed_url.hostname
            if _hostname and should_bypass_proxy(_hostname):
                # プロキシを無効化するカスタムクライアントファクトリを作成
                def _create_no_proxy_client(
                    headers: dict[str, str] | None = None,
                    timeout: httpx.Timeout | None = None,
                    auth: httpx.Auth | None = None,
                ) -> httpx.AsyncClient:
                    kwargs: dict[str, Any] = {
                        "follow_redirects": True,
                        "proxy": None,
                        "trust_env": False,  # 環境変数のプロキシ設定を無視
                    }
                    if timeout is not None:
                        kwargs["timeout"] = timeout
                    if headers is not None:
                        kwargs["headers"] = headers
                    if auth is not None:
                        kwargs["auth"] = auth
                    return httpx.AsyncClient(**kwargs)
                _httpx_client_factory = _create_no_proxy_client
                logger.debug(f"SSE接続でプロキシを無効化: {_hostname}")
        
        if transport == "sse":
            # SSEトランスポート接続
            logger.debug(f"  url: {self.config.url}")
            logger.debug(f"  headers: {list(self.config.headers.keys())}")
            
            self._cm = sse_client(
                url=self.config.url,
                headers=self.config.headers if self.config.headers else None,
                httpx_client_factory=_httpx_client_factory
            )
        else:
            # Stdioトランスポート接続
            logger.debug(f"  command: {self.config.command}")
            logger.debug(f"  args: {self.config.args}")
            logger.debug(f"  cwd: {self.config.cwd}")
            
            # 【診断ログ】環境変数の確認
            logger.info(f"[診断] MCPサーバー '{self.config.name}' の環境変数設定:")
            logger.info(f"  config.env: {self.config.env}")
            logger.info(f"  config.env type: {type(self.config.env)}")
            
            # 環境変数のマージ: 親プロセスの環境変数を継承しつつ、設定値を追加
            import os
            merged_env = os.environ.copy()
            if self.config.env:
                merged_env.update(self.config.env)
            logger.info(f"  merged_env keys: {list(merged_env.keys())[:10]}... (showing first 10)")
            logger.info(f"  REDMINE_URL in merged_env: {'REDMINE_URL' in merged_env}")
            if 'REDMINE_URL' in merged_env:
                logger.info(f"  REDMINE_URL value: {merged_env.get('REDMINE_URL')}")
            
            server_params = StdioServerParameters(
                command=self.config.command,
                args=self.config.args,
                env=merged_env,  # マージした環境変数を使用
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
    
    def _normalize_arguments(self, arguments: dict, tool_name: str = None) -> dict:
        """
        引数をinput_schemaに基づいて正規化
        
        LLMが文字列として値を渡してしまう問題を回避するため、
        input_schemaでtype: "object"として定義されている引数が、
        文字列やNoneで渡された場合、空のオブジェクト{}に変換する。
        
        また、requiredプロパティに基づいて必須引数が渡されていない場合、
        空のオブジェクト{}を設定する。
        
        Args:
            arguments: ツール引数（Noneまたはundefinedの場合は空オブジェクトとして扱う）
            tool_name: ツール名（ログ用）
            
        Returns:
            正規化された引数
        """
        # 引数がNone、undefined、または空の場合は空オブジェクトを初期値とする
        if arguments is None or arguments is False:
            arguments = {}
        
        # 対象ツールのinput_schemaを取得
        tool_schema = None
        for tool in self.tools:
            if tool.name == tool_name:
                tool_schema = tool.input_schema
                break
        
        if not tool_schema or "properties" not in tool_schema:
            return arguments if arguments else {}
        
        normalized = {}
        schema_props = tool_schema.get("properties", {})
        required_props = tool_schema.get("required", [])
        
        # スキーマで定義された全てのプロパティを処理
        for key, prop_schema in schema_props.items():
            prop_type = prop_schema.get("type")
            value = arguments.get(key) if arguments else None
            
            # type: "object"のプロパティに対して正規化
            if prop_type == "object":
                if value is None:
                    # None、undefined、または引数が渡されていない場合
                    if key in required_props:
                        # 必須引数の場合は空オブジェクトを設定し、ネストされた必須プロパティも処理
                        logger.info(f"[正規化] 必須引数 '{key}' が未指定のため空オブジェクトで初期化")
                        normalized[key] = self._normalize_nested_object({}, prop_schema)
                    else:
                        # オプション引数の場合はNoneのまま
                        normalized[key] = None
                elif isinstance(value, str):
                    # 文字列の場合は空オブジェクトに変換
                    if value.strip():
                        logger.info(f"[正規化] 引数 '{key}' を空オブジェクトに変換 (元の値: 文字列'{value}')")
                    else:
                        logger.info(f"[正規化] 引数 '{key}' を空オブジェクトに変換 (元の値: 空文字)")
                    normalized[key] = self._normalize_nested_object({}, prop_schema)
                elif isinstance(value, dict):
                    # 辞書の場合は再帰的に正規化
                    normalized[key] = self._normalize_nested_object(value, prop_schema)
                else:
                    normalized[key] = value
            else:
                normalized[key] = value
        
        # argumentsに含まれる追加のプロパティがあればコピー
        if arguments:
            for key, value in arguments.items():
                if key not in normalized:
                    normalized[key] = value
        
        return normalized
    
    def _normalize_nested_object(self, obj: dict, schema: dict) -> dict:
        """
        ネストされたオブジェクトの正規化
        
        Args:
            obj: 正規化対象のオブジェクト
            schema: プロパティのスキーマ定義
            
        Returns:
            正規化されたオブジェクト
        """
        if obj is None:
            return {}
        
        if not isinstance(obj, dict):
            return {}
        
        schema_props = schema.get("properties", {})
        if not schema_props:
            return obj
        
        required_props = schema.get("required", [])
        normalized = {}
        
        # スキーマで定義された全てのプロパティを処理
        for key, prop_schema in schema_props.items():
            prop_type = prop_schema.get("type")
            value = obj.get(key)
            
            # const制約がある場合、その値を使用
            if "const" in prop_schema:
                const_value = prop_schema["const"]
                if value is None:
                    logger.info(f"[正規化] 引数 '{key}' が未指定のためconst制約の値 '{const_value}' を設定")
                    normalized[key] = const_value
                else:
                    normalized[key] = value
                continue
            
            elif prop_type == "object":
                # ネストされたオブジェクトの再帰処理
                if value is None:
                    # object型のプロパティで値がnullの場合、空オブジェクトを設定
                    # これにより、ネストされたオブジェクト内のnull値が適切に処理される
                    if key in required_props:
                        logger.info(f"[正規化] 引数 '{key}' が未指定のため空オブジェクトを設定")
                    else:
                        logger.info(f"[正規化] オプション引数 '{key}' がnullのため空オブジェクトを設定")
                    normalized[key] = {}
                elif isinstance(value, dict):
                    # 再帰的に処理
                    normalized[key] = self._normalize_nested_object(value, prop_schema)
                else:
                    normalized[key] = value
            elif prop_type == "string":
                # 文字列型のプロパティに対するデフォルト値設定
                if value is None:
                    # enumがある場合は最初の値を使用
                    enum_values = prop_schema.get("enum")
                    if enum_values and len(enum_values) > 0:
                        default_value = enum_values[0]
                        logger.info(f"[正規化] 引数 '{key}' が未指定のためenumの最初の値 '{default_value}' を設定")
                        normalized[key] = default_value
                    else:
                        # enumがない場合は空文字を設定
                        logger.info(f"[正規化] 引数 '{key}' が未指定のため空文字を設定")
                        normalized[key] = ""
                else:
                    normalized[key] = value
            elif prop_type in ("array",):
                # 配列型のプロパティに対するデフォルト値設定
                if value is None:
                    logger.info(f"[正規化] 引数 '{key}' が未指定のため空配列を設定")
                    normalized[key] = []
                else:
                    normalized[key] = value
            elif prop_type in ("number", "integer"):
                # 数値型のプロパティに対するデフォルト値設定
                if value is None:
                    logger.info(f"[正規化] 引数 '{key}' が未指定のため0を設定")
                    normalized[key] = 0
                else:
                    normalized[key] = value
            elif prop_type == "boolean":
                # 真偽型のプロパティに対するデフォルト値設定
                if value is None:
                    logger.info(f"[正規化] 引数 '{key}' が未指定のためFalseを設定")
                    normalized[key] = False
                else:
                    normalized[key] = value
            else:
                normalized[key] = value
        
        # objに含まれる追加のプロパティがあればコピー
        for key, value in obj.items():
            if key not in normalized:
                normalized[key] = value
        
        return normalized
    
    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """ツールを実行"""
        if not self.session:
            raise RuntimeError(f"サーバー '{self.config.name}' に接続されていません")
        
        # ツールの入力スキーマを取得
        tool_schema = None
        for tool in self.tools:
            if tool.name == tool_name:
                tool_schema = tool.input_schema
                break
        
        # 必須パラメータのデフォルト値を設定
        filled_args = DefaultParameterValueGenerator.fill_missing_required_params(
            tool_name, arguments, tool_schema
        )
        
        # 引数を正規化（LLMが文字列を渡す問題を回避）
        normalized_args = self._normalize_arguments(filled_args, tool_name)
        
        logger.info(f"ツール実行: {tool_name} (サーバー: {self.config.name})")
        logger.debug(f"引数: {arguments} -> デフォルト値設定後: {filled_args} -> 正規化後: {normalized_args}")
        
        try:
            result = await self.session.call_tool(tool_name, arguments=normalized_args)
            
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
    
    async def call_tool_with_retry(self, tool_name: str, arguments: dict, max_retries: int = 1) -> Tuple[bool, Any, Optional[str]]:
        """
        エラー時にリトライを行うツール実行
        
        Args:
            tool_name: ツール名
            arguments: ツール引数
            max_retries: 最大リトライ回数
            
        Returns:
            (成功フラグ, 結果, エラーメッセージ)
        """
        retry_count = 0
        
        while retry_count <= max_retries:
            try:
                result = await self.call_tool(tool_name, arguments)
                
                # エラーレスポンスかどうかを確認
                if isinstance(result, dict) and result.get("isError"):
                    # エラーコンテンツからエラーメッセージを抽出
                    error_text = ""
                    for content in result.get("content", []):
                        if content.get("type") == "text":
                            error_text = content.get("text", "")
                            break
                    
                    if error_text:
                        error = Exception(error_text)
                        error_type = ToolExecutionErrorHandler.classify_error(error)
                        
                        # 必須パラメータ不足の場合はリトライ（デフォルト値が設定されている可能性）
                        if error_type == ErrorType.MISSING_REQUIRED and retry_count < max_retries:
                            logger.warning(f"[{tool_name}] 必須パラメータ不足エラー、リトライします ({retry_count + 1}/{max_retries})")
                            retry_count += 1
                            continue
                        
                        # その他のエラーはリトライしない
                        user_feedback = ToolExecutionErrorHandler.generate_user_feedback(error, tool_name)
                        return (False, None, user_feedback)
                
                # 成功した場合
                return (True, result, None)
                
            except Exception as e:
                error_type = ToolExecutionErrorHandler.classify_error(e)
                
                # 必須パラメータ不足の場合はリトライ（デフォルト値が設定されている可能性）
                if error_type == ErrorType.MISSING_REQUIRED and retry_count < max_retries:
                    logger.warning(f"[{tool_name}] 必須パラメータ不足エラー、リトライします ({retry_count + 1}/{max_retries})")
                    retry_count += 1
                    continue
                
                # その他のエラーはリトライしない
                user_feedback = ToolExecutionErrorHandler.generate_user_feedback(e, tool_name)
                return (False, None, user_feedback)
        
        return (False, None, "最大リトライ回数を超えました")


# ============================================
# MCPクライアントマネージャー
# ============================================
class MCPClientManager:
    """
    MCPサーバー接続管理クラス
    
    仕様書2（システムアーキテクチャ）のMCP機能層との連携を担当
    公式のmcpパッケージを使用してMCPサーバーに接続
    """
    
    def __init__(self, tool_filter_settings: dict = None, scoring_config_path: str = None):
        """
        マネージャーの初期化
        
        Args:
            tool_filter_settings: ツールフィルタリング設定
                - enabled: フィルタリング有効/無効
                - max_tools: 最大ツール数
                - always_include: 常に含めるツール名リスト
                - compression_mode: 説明圧縮モード ("full", "compact", "minimal")
                - enable_server_boost: サーバ優先度ブーストの有効/無効
            scoring_config_path: スコアリングルール設定ファイルのパス
        """
        self._connections: dict[str, MCPServerConnection] = {}
        self._connected = False
        self._server_context = ServerContext()  # サーバ使用コンテキスト
        
        # scoring_rules.jsonの自動検出ロジック
        if scoring_config_path is None:
            config_path = Path("config/scoring_rules.json")
            default_path = Path("resources/default_configs/scoring_rules.json")
            
            if config_path.exists():
                scoring_config_path = str(config_path)
                logger.info(f"カスタムスコアリングルールを使用: {scoring_config_path}")
            else:
                scoring_config_path = str(default_path)
        
        self._tool_filter = ToolFilter(
            server_context=self._server_context,
            scoring_config_path=scoring_config_path
        )
        self._tool_filter_settings = tool_filter_settings or {}
        self._server_keywords: dict = {}  # サーバ固有キーワード設定
    
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
            # サーバコンテキストに記録
            self._server_context.record_tool_usage(actual_server_name)
            return await target_connection.call_tool(actual_tool_name, arguments)
        
        # サーバー名がない場合は全サーバーから検索
        target_connection = None
        found_server_name = None
        for sname, connection in self._connections.items():
            for tool in connection.tools:
                if tool.name == actual_tool_name:
                    target_connection = connection
                    found_server_name = sname
                    break
            if target_connection:
                break
        
        if not target_connection:
            logger.error(f"ツール '{actual_tool_name}' が見つかりません")
            return {
                "content": [{"type": "text", "text": f"エラー: ツール '{actual_tool_name}' が見つかりません"}],
                "isError": True
            }
        
        # サーバコンテキストに記録
        if found_server_name:
            self._server_context.record_tool_usage(found_server_name)
        
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
                
                # keywords設定を取得
                keywords = server_config.get("keywords", {})
                
                configs.append(MCPServerConfig(
                    name=name,
                    command=server_config.get("command", ""),
                    args=server_config.get("args", []),
                    env=server_config.get("env", {}),
                    cwd=cwd,
                    url=server_config.get("url"),
                    headers=server_config.get("headers", {}),
                    keywords=keywords
                ))
                
                # サーバキーワード設定を保存
                if keywords:
                    self._server_keywords[name] = keywords
            
            logger.info(f"{len(configs)}件のMCPサーバー設定を読み込みました")
            for config in configs:
                if config.transport_type == "sse":
                    logger.info(f"  - {config.name}: SSE {config.url}")
                else:
                    cmd_str = f"{config.command} {' '.join(config.args)}" if config.args else config.command
                    logger.info(f"  - {config.name}: {cmd_str}")
            
            # サーバ推定器にキーワード設定を反映
            self._update_server_estimator_keywords()
            
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
                    
                    keywords = server_config.get("keywords", {})
                    
                    configs.append(MCPServerConfig(
                        name=name,
                        command=server_config.get("command", ""),
                        args=server_config.get("args", []),
                        env=server_config.get("env", {}),
                        cwd=cwd,
                        url=server_config.get("url"),
                        headers=server_config.get("headers", {}),
                        keywords=keywords
                    ))
                    
                    if keywords:
                        self._server_keywords[name] = keywords
                
                logger.info("デフォルト設定で復旧しました。")
                self._update_server_estimator_keywords()
                return configs
            except Exception as recover_e:
                logger.error(f"復旧に失敗しました: {recover_e}")
                return []
    
    def _update_server_estimator_keywords(self):
        """サーバ推定器にキーワード設定を反映"""
        if not self._server_keywords:
            return
        
        # デフォルトキーワードとマージ
        merged_keywords = DEFAULT_SERVER_KEYWORDS.copy()
        
        for server_name, kw_config in self._server_keywords.items():
            if server_name not in merged_keywords:
                merged_keywords[server_name] = {"keywords": [], "weight": 1.0}
            
            include_keywords = kw_config.get("include", [])
            weight = kw_config.get("weight", 1.0)
            
            # キーワードを追加
            merged_keywords[server_name]["keywords"] = list(set(
                merged_keywords[server_name].get("keywords", []) + include_keywords
            ))
            merged_keywords[server_name]["weight"] = weight
        
        # サーバ推定器を更新
        if hasattr(self._tool_filter, 'server_estimator') and self._tool_filter.server_estimator:
            self._tool_filter.server_estimator.keyword_estimator = ServerEstimator(merged_keywords)
            logger.info(f"サーバ推定器のキーワード設定を更新しました: {list(merged_keywords.keys())}")
    
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
    
    def get_server_keywords(self) -> dict:
        """
        サーバ固有キーワード設定を返す
        
        Returns:
            サーバ名→キーワード設定の辞書
        """
        return self._server_keywords.copy()
    
    def get_server_context(self) -> ServerContext:
        """
        サーバ使用コンテキストを返す
        
        Returns:
            ServerContextインスタンス
        """
        return self._server_context
    
    def clear_server_context(self):
        """サーバ使用コンテキストをクリア"""
        self._server_context.clear()