"""
swing_web_app.py
Swing Trader GUI - Flask Webアプリ（macd_trader/web_app.py のスイング版）
起動: python3 swing_web_app.py
ブラウザで http://localhost:5003 を開く

macd_trader（ポート5001）・backtest_studio（ポート5002）とは完全に別プロセス・
別ポート・別データファイル（swing_trader/data/symbols.json）。銘柄スクリーニング
機能（screen_manager.py相当）はスコープ外のため実装しない。
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "macd_trader"))

from flask import Flask, jsonify, request, send_from_directory

from swing_symbol_store import SwingSymbolStore
from swing_config import SWING_DEFAULT_CONFIG
from swing_bot_manager import BotManager, get_logs_since
from order_manager import get_market_state_for_symbol
from daily_pnl import compute_daily_pnl

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"))
store = SwingSymbolStore(os.path.join(BASE_DIR, "data", "symbols.json"))
manager = BotManager()


# ─── Static ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/daily_pnl")
def daily_pnl_page():
    return send_from_directory(app.static_folder, "daily_pnl.html")


@app.route("/api/daily_pnl")
def api_daily_pnl():
    days = request.args.get("days", default=30, type=int)
    log_dir = Path(BASE_DIR) / "logs"
    return jsonify(compute_daily_pnl(log_dir, days))


# ─── Symbol CRUD ─────────────────────────────────────────────────

@app.route("/api/symbols", methods=["GET"])
def list_symbols():
    return jsonify(store.list())


@app.route("/api/symbols", methods=["POST"])
def add_symbol():
    data = request.json or {}
    sid = data.get("symbol", "").upper().strip()
    if not sid:
        return jsonify({"error": "symbol は必須です"}), 400
    if store.get(sid):
        return jsonify({"error": f"{sid} はすでに登録されています"}), 409
    return jsonify(store.add(data)), 201


@app.route("/api/symbols/<symbol_id>", methods=["GET"])
def get_symbol(symbol_id):
    cfg = store.get(symbol_id)
    return jsonify(cfg) if cfg else (jsonify({"error": "not found"}), 404)


@app.route("/api/symbols/<symbol_id>", methods=["PUT"])
def update_symbol(symbol_id):
    s = manager.get_state(symbol_id)
    if s and s.running:
        return jsonify({"error": "ボットを停止してから編集してください"}), 400
    cfg = store.update(symbol_id, request.json or {})
    return jsonify(cfg) if cfg else (jsonify({"error": "not found"}), 404)


@app.route("/api/symbols/<symbol_id>", methods=["DELETE"])
def delete_symbol(symbol_id):
    s = manager.get_state(symbol_id)
    if s and s.running:
        return jsonify({"error": "ボットを停止してから削除してください"}), 400
    store.delete(symbol_id)
    return "", 204


# ─── Bot Control ─────────────────────────────────────────────────

@app.route("/api/bots/<symbol_id>/start", methods=["POST"])
def start_bot(symbol_id):
    cfg = store.get(symbol_id)
    if not cfg:
        return jsonify({"error": "銘柄が見つかりません"}), 404
    ok, msg = manager.start(symbol_id, cfg)
    return (jsonify({"status": msg}) if ok else (jsonify({"error": msg}), 400))


@app.route("/api/bots/<symbol_id>/stop", methods=["POST"])
def stop_bot(symbol_id):
    ok, msg = manager.stop(symbol_id)
    return (jsonify({"status": msg}) if ok else (jsonify({"error": msg}), 400))


@app.route("/api/bots/stop_all", methods=["POST"])
def stop_all():
    manager.stop_all()
    return jsonify({"status": "全ボットに停止を要求しました"})


@app.route("/api/bots/<symbol_id>/force_sell", methods=["POST"])
def force_sell_bot(symbol_id):
    ok, msg = manager.force_sell(symbol_id)
    return (jsonify({"status": msg}) if ok else (jsonify({"error": msg}), 400))


@app.route("/api/bots/force_sell_all", methods=["POST"])
def force_sell_all():
    targets = manager.force_sell_all()
    return jsonify({"status": f"{len(targets)}銘柄に即時売却を要求しました", "symbols": targets})


@app.route("/api/bots/<symbol_id>/market_state")
def market_state(symbol_id):
    cfg = store.get(symbol_id)
    if not cfg:
        return jsonify({"error": "銘柄が見つかりません"}), 404
    result = get_market_state_for_symbol(
        symbol_id, cfg["opend"]["host"], cfg["opend"]["port"]
    )
    if result is None:
        return jsonify({"error": "市場状態を取得できませんでした"}), 502
    return jsonify(result)


# ─── Status / Trades / Logs ──────────────────────────────────────

@app.route("/api/status")
def get_status():
    return jsonify(manager.all_states())


@app.route("/api/trades")
def get_trades():
    return jsonify(manager.all_trades())


@app.route("/api/defaults")
def get_defaults():
    return jsonify(SWING_DEFAULT_CONFIG)


@app.route("/api/logs")
def get_logs():
    since = int(request.args.get("since", 0))
    return jsonify(get_logs_since(since))


# ─── Entry Point ─────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    os.makedirs(os.path.join(BASE_DIR, "static"), exist_ok=True)
    print("=" * 50)
    print("  Swing Trader GUI 起動")
    print("  http://localhost:5003 をブラウザで開いてください")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5003, debug=False, threaded=True)
