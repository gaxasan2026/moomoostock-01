"""
web_app.py
MACD Trader GUI - Flask Webアプリ
起動: python web_app.py
ブラウザで http://localhost:5000 を開く
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from flask import Flask, jsonify, request, send_from_directory

from symbol_store import SymbolStore
from bot_manager import BotManager, get_logs_since

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"))
store = SymbolStore(os.path.join(BASE_DIR, "data", "symbols.json"))
manager = BotManager()


# ─── Static ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


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


# ─── Status / Trades / Logs ──────────────────────────────────────

@app.route("/api/status")
def get_status():
    return jsonify(manager.all_states())


@app.route("/api/trades")
def get_trades():
    return jsonify(manager.all_trades())


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
    print("  MACD Trader GUI 起動")
    print("  http://localhost:5001 をブラウザで開いてください")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5001, debug=False, threaded=True)
