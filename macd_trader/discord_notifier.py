"""
discord_notifier.py
取引発生時にDiscord Webhookへ通知を送る（無人稼働時のモニタリング用）。

セキュリティ設計:
- Webhook URLは data/notification_config.json（Git管理外）に保存する
- 通知内容は銘柄・売買・価格・数量・損益のみ。口座番号・残高等は一切含めない
- 標準ライブラリ（urllib）のみ使用し、新規の外部依存を追加しない
- 通知の失敗は取引処理をブロックしない（例外を握りつぶし、ログにwarningを出すのみ）
"""
import json
import logging
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "data" / "notification_config.json"

# このモジュールはmacd_trader/swing_trader共通で使う（read-only import）。
# Webhook自体の表示名設定に関わらず、送信元がどちらのアプリか区別できるよう
# ペイロードのusernameで毎回明示的に上書きする。呼び出し元を指定しない場合の
# 既定値はこのモジュールの置き場所（macd_trader）に合わせてDay Traderとする。
DEFAULT_APP_NAME = "Day Trader"


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"enabled": False, "webhook_url": ""}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"enabled": False, "webhook_url": ""}


def _post_to_discord(webhook_url: str, content: str, username: str = DEFAULT_APP_NAME):
    payload = json.dumps({"content": content, "username": username}).encode("utf-8")
    req = urllib.request.Request(
        webhook_url, data=payload,
        # urllibのデフォルトUser-Agentは、DiscordのWAF（Cloudflare）にbotとしてブロックされる（403 error code 1010）
        headers={"Content-Type": "application/json", "User-Agent": "MacdTrader-DiscordNotifier/1.0"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5)


def notify_trade(symbol_id: str, action: str, price: float, quantity: int,
                  reason: str = "", pnl: float = None, pnl_pct: float = None,
                  app_name: str = DEFAULT_APP_NAME):
    """
    取引発生をDiscordへ通知する。設定が無効、またはWebhook URL未設定の場合は何もしない。
    通知の失敗は例外を外に投げず、ログにwarningを出すのみ（取引処理はブロックしない）。
    app_name: 通知の送信元表示名（例: "Day Trader" / "Swing Trader"）
    """
    cfg = _load_config()
    if not cfg.get("enabled") or not cfg.get("webhook_url"):
        return

    if action == "BUY":
        content = f"📥 **BUY** {symbol_id}  {quantity}株 @ ${price:.4f}"
        if reason:
            content += f"\n　理由: {reason}"
    else:
        emoji = "🟢" if (pnl is not None and pnl >= 0) else "🔴"
        content = f"📤 **SELL** {symbol_id}  {quantity}株 @ ${price:.4f}"
        if pnl is not None:
            content += f"\n　{emoji} 損益: {pnl:+.2f} USD ({pnl_pct:+.2f}%)"
        if reason:
            content += f"\n　理由: {reason}"

    try:
        _post_to_discord(cfg["webhook_url"], content, username=app_name)
    except Exception as e:
        logger.warning(f"[{symbol_id}] Discord通知に失敗しました: {e}")


def notify_position_mismatch(symbol_id: str, detail: str, app_name: str = DEFAULT_APP_NAME):
    """
    実口座の保有数量とTrader管理数量の不一致を新規検知した際に通知する
    （継続中の同一不一致では毎回呼ばない — 呼び出し側で新規検知時のみ呼ぶこと）。
    app_name: 通知の送信元表示名（例: "Day Trader" / "Swing Trader"）
    """
    cfg = _load_config()
    if not cfg.get("enabled") or not cfg.get("webhook_url"):
        return

    content = f"⚠️ **ポジション不一致検知** {symbol_id}\n　{detail}"

    try:
        _post_to_discord(cfg["webhook_url"], content, username=app_name)
    except Exception as e:
        logger.warning(f"[{symbol_id}] Discord通知に失敗しました: {e}")
