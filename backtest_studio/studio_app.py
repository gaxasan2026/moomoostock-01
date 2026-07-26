"""
studio_app.py
MACD Backtest Studio — MACD Trader（本番アプリ）とは別プロセスで動く、
複数銘柄・複数パラメータのバックテストGUI。

発注機能・data/symbols.jsonへの直接書き込みは一切行わない（読み書きは
macd_client.py経由でMACD TraderのREST APIを呼ぶだけ）。
起動: python3 studio_app.py
ブラウザで http://localhost:5002 を開く
"""
from __future__ import annotations

import os
from datetime import time as dt_time

from flask import Flask, jsonify, request, send_from_directory

from batch_manager import BatchBacktestManager
import macd_client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"))
batch_mgr = BatchBacktestManager()


def _parse_hours(data: dict) -> tuple[dt_time, dt_time] | None:
    start_s = (data.get("hours_start") or "").strip()
    end_s = (data.get("hours_end") or "").strip()
    if not start_s or not end_s:
        return None
    return dt_time.fromisoformat(start_s), dt_time.fromisoformat(end_s)


# ─── Static ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ─── MACD Trader プロキシ（読み取り専用） ──────────────────────────

@app.route("/api/registered-symbols")
def registered_symbols():
    try:
        return jsonify(macd_client.get_symbols())
    except Exception as e:
        return jsonify({"error": f"MACD Traderに接続できません: {e}"}), 502


@app.route("/api/defaults")
def defaults():
    try:
        return jsonify(macd_client.get_defaults())
    except Exception as e:
        return jsonify({"error": f"MACD Traderに接続できません: {e}"}), 502


# ─── バッチバックテスト ──────────────────────────────────────────

@app.route("/api/batch", methods=["POST"])
def start_batch():
    data = request.json or {}
    symbols = data.get("symbols", [])
    start_date = (data.get("start_date") or "").strip()
    end_date = (data.get("end_date") or "").strip()
    grid_spec = data.get("grid_spec", {})

    if not symbols:
        return jsonify({"error": "対象銘柄を1つ以上指定してください"}), 400
    if not start_date or not end_date:
        return jsonify({"error": "start_date, end_date は必須です"}), 400

    try:
        hours_filter = _parse_hours(data)
    except ValueError:
        return jsonify({"error": "取引時間帯の形式が不正です（HH:MM）"}), 400

    job_id = batch_mgr.start(symbols, start_date, end_date, grid_spec, hours_filter)
    return jsonify({"job_id": job_id}), 202


@app.route("/api/batch/<job_id>")
def get_batch_status(job_id):
    job = batch_mgr.get(job_id)
    if not job:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "status": job.status,
        "progress": job.progress,
        "results": job.results if job.status == "done" else [],
        "error": job.error,
    })


# ─── MACD Traderへの反映 ────────────────────────────────────────

@app.route("/api/apply", methods=["POST"])
def apply_to_macd_trader():
    data = request.json or {}
    symbol_id = (data.get("symbol_id") or "").upper().strip()
    is_new = bool(data.get("is_new", False))
    entry_overrides = data.get("entry_overrides", {})

    if not symbol_id:
        return jsonify({"error": "symbol_id は必須です"}), 400

    if is_new:
        ok, body, status = macd_client.create_symbol({"symbol": symbol_id, "entry": entry_overrides})
        return jsonify(body), status

    try:
        if macd_client.is_running(symbol_id):
            return jsonify({"error": f"{symbol_id} は現在稼働中です。MACD Trader側で先にボットを停止してください。"}), 409
    except Exception as e:
        return jsonify({"error": f"MACD Traderに接続できません: {e}"}), 502

    ok, body, status = macd_client.update_symbol(symbol_id, {"entry": entry_overrides})
    return jsonify(body), status


# ─── Entry Point ─────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(os.path.join(BASE_DIR, "static"), exist_ok=True)
    print("=" * 50)
    print("  MACD Backtest Studio 起動")
    print("  http://localhost:5002 をブラウザで開いてください")
    print("  ※ MACD Trader (http://localhost:5001) が起動している必要があります")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5002, debug=False, threaded=True)
