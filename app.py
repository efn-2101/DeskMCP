"""
DeskMCP UI Layer - Chainlit Entry Point
=======================================
責務:
- セッション初期化とMCPクライアント設定
- ユーザー入力の受付とAgent.run()への委譲
- Chainlitのライフサイクル管理
- ローカルチャット履歴の管理
"""

# .envファイルから環境変数を読み込む（Chainlit CLI用）
# chainlit run app.pyで実行する場合、if __name__ == "__main__":ブロックは実行されないため、
# モジュールレベルでload_dotenv()を呼び出す必要がある
from dotenv import load_dotenv
load_dotenv()

import asyncio
import logging
import json
import os
import shutil

import chainlit as cl
from chainlit.context import context_var
import chainlit.data as cl_data
from chainlit.input_widget import Switch, Select

from agent import Agent, AgentConfig, load_system_config, ToolCall
from tools import MCPClientManager
from data_layer import SQLiteDataLayer

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# ============================================
# データレイヤーの登録
# ============================================
# Chainlit 1.0.0では cl_data._data_layer にカスタム実装を設定
# これによりシステムがデータ永続化をONと認識する
data_layer = SQLiteDataLayer(db_path="data/chat_history.db")
cl_data._data_layer = data_layer


# ============================================
# 設定ファイル読み込み関数
# ============================================
async def _load_buttons_config() -> list[dict]:
    """
    buttons_config.jsonを読み込む。
    config/buttons_config.jsonが存在しない場合は
    resources/default_configs/buttons_config.jsonからコピーして使用する。
    
    Returns:
        list[dict]: action_buttonsのリスト
    """
    config_path = "config/buttons_config.json"
    default_config_path = "resources/default_configs/buttons_config.json"
    
    # 設定ファイルのフォールバック・自動復旧機構
    if not os.path.exists(config_path):
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        try:
            shutil.copy2(default_config_path, config_path)
            logger.info(f"デフォルトの設定ファイルをコピーしました: {config_path}")
        except Exception as e:
            logger.error(f"デフォルト設定ファイルのコピーに失敗しました: {e}")
            
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            btn_config = json.load(f)
    except Exception as e:
        logger.error(f"ボタン設定のパースに失敗しました。デフォルト設定で復旧を試みます: {e}")
        try:
            shutil.copy2(default_config_path, config_path)
            with open(config_path, "r", encoding="utf-8") as f:
                btn_config = json.load(f)
            logger.info("デフォルト設定で復旧しました。")
        except Exception as recover_e:
            logger.error(f"復旧に失敗しました: {recover_e}")
            btn_config = {}
    
    return btn_config.get("action_buttons", [])



# ============================================
# アクションメニュー描画関数
# ============================================
async def send_action_menu():
    """アクションメニューをチャットの最下部に描画する"""
    old_menu = cl.user_session.get("action_menu_msg")
    if old_menu:
        try:
            await old_menu.remove()
        except Exception:
            pass
            
    actions = []
    action_buttons = await _load_buttons_config()
    
    # MCPサーバの接続状態を取得
    mcp_manager = cl.user_session.get("mcp_manager")
    health_status = {}
    if mcp_manager:
        try:
            health_status = await mcp_manager.health_check()
        except Exception:
            pass  # ヘルスチェック失敗時は空辞書のまま（サーバ依存ボタンは非表示）
    
    # MCP接続状態でフィルタリング
    filtered_buttons = []
    for btn in action_buttons:
        server_name = btn.get("mcp_server")
        if not server_name or health_status.get(server_name, False):
            filtered_buttons.append(btn)
    
    # 表示設定を取得（visible_macrosに含まれるマクロのみ表示）
    visible_macros = cl.user_session.get("visible_macros")
    
    for btn in filtered_buttons:
        macro_id = btn.get("id", btn.get("ui_label", ""))
        # visible_macrosが設定されている場合、表示フィルタを適用
        if visible_macros is not None and macro_id not in visible_macros:
            continue
        
        actions.append(
            cl.Action(
                name="macro_button",
                payload=btn,  # Chainlit v1.0では辞書を直接渡す
                label=btn.get("ui_label", "未定義ラベル"),
                description=btn.get("ui_description", "")
            )
        )
    
    if actions:
        menu_msg = cl.Message(
            author="ActionMenu",
            content="👇 実行したいアクションを選択、または自由に入力してください\n*(※ボタンが増えた場合は将来的に折りたたみUIに変更予定です)*",
            actions=actions
        )
        await menu_msg.send()
        cl.user_session.set("action_menu_msg", menu_msg)


# ============================================
# ChatSettings（歯車アイコンメニュー）設定関数
# ============================================
async def setup_chat_settings() -> None:
    """
    ChatSettings（歯車アイコンメニュー）を設定・表示する。
    
    - 直接実行用ドロップダウン（Select）: 全マクロから選択して実行
    - 表示切替トグル（Switch）: 各マクロの表示/非表示を切り替え
    """
    # マクロ設定を取得
    action_buttons = await _load_buttons_config()
    
    # MCPサーバの接続状態を取得
    mcp_manager = cl.user_session.get("mcp_manager")
    health_status = {}
    if mcp_manager:
        try:
            health_status = await mcp_manager.health_check()
        except Exception:
            pass
    
    # MCP接続状態でフィルタリング
    filtered_buttons = []
    for btn in action_buttons:
        server_name = btn.get("mcp_server")
        if not server_name or health_status.get(server_name, False):
            filtered_buttons.append(btn)
    
    # 現在の表示設定を取得（初回は全て表示＝True）
    visible_macros = cl.user_session.get("visible_macros")
    if visible_macros is None:
        # 初回はフィルタリング後のマクロIDをリスト化
        visible_macros = [btn.get("id", btn.get("ui_label", "")) for btn in filtered_buttons]
        cl.user_session.set("visible_macros", visible_macros)
    
    # ドロップダウンの選択肢を作成（文字列リスト形式）
    # ChainlitのSelectウィジェットはvaluesパラメータに文字列リストを渡す必要がある
    options = ["-- 選択してください --"]
    for btn in filtered_buttons:
        ui_label = btn.get("ui_label", "未定義ラベル")
        options.append(ui_label)
    
    # Selectウィジェットを作成
    macro_select = Select(
        id="macro_select",
        label="マクロを直接実行",
        initial_value="-- 選択してください --",
        values=options
    )
    
    # 各マクロ用のSwitchウィジェットを作成
    switches = []
    for btn in filtered_buttons:
        macro_id = btn.get("id", btn.get("ui_label", ""))
        ui_label = btn.get("ui_label", "未定義ラベル")
        is_visible = macro_id in visible_macros
        switches.append(
            Switch(
                id=f"switch_{macro_id}",
                label=ui_label,
                initial=is_visible
            )
        )
    
    # ChatSettingsに全ウィジェットを設定
    await cl.ChatSettings(
        [macro_select] + switches
    ).send()


# ============================================
# ChatSettings変更コールバック
# ============================================
@cl.on_settings_update
async def on_settings_update(settings: dict) -> None:
    """
    歯車メニューの設定変更時のコールバック。
    
    - ドロップダウンでマクロが選択された場合: マクロを実行し、ドロップダウンをリセット
    - トグルが切り替えられた場合: visible_macrosを更新し、アクションメニューを再描画
    """
    # ドロップダウンでマクロが選択された場合
    selected_label = settings.get("macro_select")
    if selected_label and selected_label != "-- 選択してください --":
        # 選択肢のラベルからマクロを逆引き
        action_buttons = await _load_buttons_config()
        btn_config = None
        for btn in action_buttons:
            ui_label = btn.get("ui_label", "未定義ラベル")
            if ui_label == selected_label:
                btn_config = btn
                break
        
        if btn_config:
            # マクロの指示をユーザーメッセージとして注入し、on_message経由で通常フローに乗せる
            await _inject_macro_prompt(btn_config)
        
        # ドロップダウンをリセット（選択状態をクリア）
        asyncio.create_task(setup_chat_settings())
        return
    
    # 表示設定の更新（トグルが切り替えられた場合）
    action_buttons = await _load_buttons_config()
    visible_macros = []
    
    for btn in action_buttons:
        macro_id = btn.get("id", btn.get("ui_label", ""))
        switch_key = f"switch_{macro_id}"
        if settings.get(switch_key, True):
            visible_macros.append(macro_id)
    
    # セッションに保存
    cl.user_session.set("visible_macros", visible_macros)
    
    # アクションメニューを再描画
    asyncio.create_task(send_action_menu())


# ============================================
# 自動認証（履歴UI有効化のため）
# ============================================
# ChainlitのデータレイヤーをUIで有効化するには認証フックが必須
# 環境変数 CHAINLIT_AUTH_SECRET は設定済み
# header_auth_callbackを使用することで、ログイン画面をスキップして
# 自動的にローカルユーザーとして認証する
@cl.header_auth_callback
async def header_auth_callback(headers: dict):
    """
    HTTPヘッダーからユーザーを認証する。
    ヘッダーに認証情報がない場合はデフォルトユーザーを返す。
    
    これにより、ログイン画面をスキップして直接チャット画面にアクセス可能。
    履歴機能（データレイヤー）も有効なまま維持される。
    
    Args:
        headers: HTTPリクエストヘッダー
        
    Returns:
        cl.User: ローカルユーザー識別子を持つUserオブジェクト
    """
    # ヘッダーに認証情報がある場合はそれを使用（将来の拡張用）
    # 現在は常にデフォルトユーザーを返す
    return cl.User(identifier="local_user")


# ============================================
# セッション初期化
# ============================================
@cl.on_chat_start
async def on_chat_start():
    logger.info("=== セッション初期化開始 ===")
    
    agent = cl.user_session.get("agent")
    if agent is not None:
        agent.history.messages = []
        agent.history._tool_call_history = []
    
    cl.user_session.set("user", cl.User(identifier="local_user"))
    
    try:
        # 【重要】Chainlitのネイティブなスレッド管理に任せるため、手動のcreate_threadやthread_idの上書きは絶対に行わない
        mcp_manager = MCPClientManager()
        await mcp_manager.connect_servers()
        system_config = load_system_config()
        agent_config = AgentConfig.from_dict(system_config)
        
        agent = Agent(mcp_manager=mcp_manager, config=agent_config)
        
        cl.user_session.set("agent", agent)
        cl.user_session.set("mcp_manager", mcp_manager)
        
        logger.info("セッション初期化完了")
        await cl.Message(
            author="SystemWelcome",
            content="🤖 エージェントを起動しました。何かお手伝いしましょうか？"
        ).send()
        
        # ChatSettings（歯車メニュー）を初期化
        await setup_chat_settings()
        
        # アクションメニューを表示
        await send_action_menu()
        
    except Exception as e:
        logger.error(f"セッション初期化エラー: {e}")
        await cl.Message(
            content=f"❌ 初期化エラーが発生しました: {str(e)}"
        ).send()


# ============================================
# チャット再開（Resume）ハンドラ
# ============================================
@cl.on_chat_resume
async def on_chat_resume(thread: dict):
    logger.info(f"=== スレッド再開: {thread.get('id')} ===")
    
    try:
        # 1. MCPマネージャーと設定の再初期化
        mcp_manager = MCPClientManager()
        await mcp_manager.connect_servers()
        system_config = load_system_config()
        agent_config = AgentConfig.from_dict(system_config)
        
        # 2. 必須引数を渡してAgentを作成（ここがエラーの原因でした）
        agent = Agent(mcp_manager=mcp_manager, config=agent_config)
        
        # 3. セッションへの確実な登録
        cl.user_session.set("agent", agent)
        cl.user_session.set("mcp_manager", mcp_manager)
        cl.user_session.set("thread_id", thread.get("id"))
        cl.context.session.thread_id = thread.get("id")  # 追加: コアシステムへの同期
        
        # 4. 履歴の復元とAgentへの記憶装填
        steps = await cl_data._data_layer.get_steps(thread.get("id"))
        restored_messages = []
        for step in steps:
            role = "user" if step["type"] == "user_message" else "assistant"
            restored_messages.append({"role": role, "content": step.get("output", "")})
        
        agent.history.messages = restored_messages
        logger.info(f"履歴復元完了: {len(restored_messages)}件のメッセージ")
        
        # ChatSettings（歯車メニュー）を初期化
        await setup_chat_settings()
        
        # アクションメニューを表示（フロントエンドのDOM復元完了を待つため少し遅延させる）
        await asyncio.sleep(0.5)
        await send_action_menu()
        
    except Exception as e:
        logger.error(f"スレッド再開エラー: {e}", exc_info=True)
        await cl.Message(
            content=f"❌ 会話の復元に失敗しました: {str(e)}"
        ).send()


# ============================================
# ユーザー入力処理コアロジック
# ============================================
async def _process_user_input(user_input: str) -> None:
    """
    ユーザー入力の処理コアロジック
    
    on_messageからも_inject_macro_promptからも呼び出される、
    デコレータなしのプレーンな非同期関数。
    @cl.on_message デコレータ付き関数を直接呼び出すと
    Chainlitの内部状態を破壊するため、ビジネスロジックは
    この関数に抽出している。
    
    Args:
        user_input: ユーザーからの入力テキスト
    """
    logger.info(f"ユーザーメッセージ処理: {user_input[:50]}...")
    
    # セッションからAgentを取得
    agent: Agent = cl.user_session.get("agent")
    
    if agent is None:
        await cl.Message(
            content="❌ セッションが初期化されていません。ページを再読み込みしてください。"
        ).send()
        return
    
    try:
        # Agentの自律ループを実行
        # すべてのロジック（推論、ツール実行、履歴管理）はAgent内で完結
        final_response = None
        
        async for step in agent.run(user_input=user_input):
            # Agentからの応答を順次UIに表示
            # stepはChainlitのStepオブジェクト
            if hasattr(step, 'output') and step.output:
                # Stepの内容は自動的にアコーディオンとして表示される
                pass
        
        # 最終的な応答を取得
        # Agent.run()はStepをyieldするため、最終的な応答は履歴から取得
        if agent.history.messages:
            last_message = agent.history.messages[-1]
            if last_message.get("role") == "assistant" and last_message.get("content"):
                final_response = last_message["content"]
        
        # 最終応答があれば表示
        if final_response:
            await cl.Message(content=final_response).send()
        else:
            # 応答がない場合（ツール実行のみなど）
            await cl.Message(content="処理が完了しました。").send()
            
    except Exception as e:
        logger.error(f"メッセージ処理エラー: {e}", exc_info=True)
        await cl.Message(
            content=f"❌ エラーが発生しました: {str(e)}"
        ).send()
    finally:
        # fire-and-forget: ハンドラを即座に返しストップボタンを消す
        asyncio.create_task(send_action_menu())


# ============================================
# メッセージ受信ハンドラ
# ============================================
@cl.on_message
async def on_message(message: cl.Message):
    """
    ユーザーメッセージ受信時の処理
    
    責務:
    - メニュー消去処理
    - _process_user_inputへの委譲
    
    【重要】メッセージのDB保存はChainlitが自動で行うため、
    手動保存処理は不要です。cl.Message送信時にcreate_stepが呼ばれます。
    
    Args:
        message: ユーザーからの入力メッセージ
    """
    # 古いアクションメニューを削除
    old_menu = cl.user_session.get("action_menu_msg")
    if old_menu:
        try:
            await old_menu.remove()
        except Exception:
            pass
        cl.user_session.set("action_menu_msg", None)
    
    # コアロジックはデコレータなしの関数に委譲
    await _process_user_input(message.content)


# ============================================
# マクロプロンプト注入ヘルパー
# ============================================
async def _inject_macro_prompt(btn_config: dict) -> None:
    """マクロの指示をユーザーメッセージとしてチャットに送信し、通常の処理フローに乗せる"""
    instruction = btn_config.get("task_instruction", "")
    prompt = instruction
    
    # 古いメニューを消去
    old_menu = cl.user_session.get("action_menu_msg")
    if old_menu:
        try:
            await old_menu.remove()
        except Exception:
            pass
        cl.user_session.set("action_menu_msg", None)

    # ユーザーの発言としてメッセージを送信
    msg = cl.Message(content=prompt, author="User")
    await msg.send()
    
    # 【重要】アクションコールバックを即座に終了させるため、
    # Chainlitのコンテキストを保持したまま _process_user_input をバックグラウンドタスクとして起動する
    # デコレータ付きの on_message を直接呼ぶとChainlitの内部状態を破壊するため、
    # プレーンな _process_user_input を呼び出す
    ctx = context_var.get()
    
    async def run_message_in_background():
        context_var.set(ctx)
        # Chainlitのタスクライフサイクル管理: フロントエンドのローディング状態を正しく制御する
        emitter = ctx.emitter
        await emitter.task_start()
        try:
            await _process_user_input(prompt)
        except Exception as e:
            logger.error(f"バックグラウンドマクロ処理エラー: {e}")
        finally:
            await emitter.task_end()
            
    asyncio.create_task(run_message_in_background())


# ============================================
# セッション終了ハンドラ
# ============================================
@cl.on_chat_end
async def on_chat_end():
    logger.info("=== セッション終了 ===")
    mcp_manager = cl.user_session.get("mcp_manager")
    
    if mcp_manager:
        try:
            # anyio/MCPのキャンセルスコープは作成タスク内で閉じる必要があるため直接await
            await mcp_manager.disconnect_servers()
            logger.info("MCPサーバーとの切断完了")
        except Exception as e:
            logger.error(f"接続切断エラー: {e}")


# ============================================
# 停止ボタンハンドラ（キルスイッチ）
# ============================================
@cl.on_stop
def on_stop():
    """ユーザーが停止ボタンを押した際の強制終了ハンドラ"""
    logger.info("ユーザーにより処理の強制停止が要求されました")
    
    agent = cl.user_session.get("agent")
    if agent:
        agent.cancel()
        logger.info("Agentにキャンセル要求を送信しました")


# ============================================
# アクションボタン（マクロ）コールバック
# ============================================
@cl.action_callback("macro_button")
async def on_macro_button(action: cl.Action):
    """
    マクロボタン押下時の処理
    
    マクロの指示をユーザーメッセージとして注入し、on_message経由で通常フローに乗せる。
    """
    await _inject_macro_prompt(action.payload)


# ============================================
# エントリーポイント（開発用）
# ============================================
if __name__ == "__main__":
    # .envファイルから環境変数を読み込む
    from dotenv import load_dotenv
    load_dotenv()
    
    # 認証シークレットが未設定の場合は自動生成
    import os
    import secrets
    if not os.getenv("CHAINLIT_AUTH_SECRET"):
        os.environ["CHAINLIT_AUTH_SECRET"] = secrets.token_urlsafe(32)
        logger.info("CHAINLIT_AUTH_SECRET を自動生成しました")
    
    # ポートが未設定の場合はデフォルト値2000を使用
    if not os.getenv("CHAINLIT_PORT"):
        os.environ["CHAINLIT_PORT"] = "2000"
        logger.info("CHAINLIT_PORT をデフォルト値 2000 に設定しました")
    
    # Chainlit 2.x の正しい起動方法
    from chainlit.cli import run_chainlit
    run_chainlit("app.py")