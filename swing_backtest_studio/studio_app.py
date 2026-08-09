"""
studio_app.py
Swing Backtest Studio — Swing Trader（本番アプリ、ポート5003）とは別プロセスで動く、
複数銘柄・複数パラメータのスイングバックテストGUI。

発注機能・swing_trader/data/symbols.jsonへの直接書き込みは一切行わない
（読み書きはswing_client.py経由でSwing TraderのREST APIを呼ぶだけ）。
起動: python3 studio_app.py
ブラウザで http://localhost:5004 を開く
"""
from __future__ import annotations

import os

from flask import Flask, jsonify, request, send_from_directory
import futu as ft

from batch_manager import BatchBacktestManager
import screener
import swing_client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OPEND_HOST = "127.0.0.1"
DEFAULT_OPEND_PORT = 11111

# moomooアプリの自選株（ウォッチリスト）に銘柄発見の候補を追加する先のグループ名。
# このグループはmoomooアプリ側で事前に手動作成しておく必要がある
# （futu APIには自選股グループを新規作成する機能が無く、存在しないグループ名を
# 指定すると「不明なお気に入りリスト」エラーになることを実機で確認済み）。
WATCHLIST_GROUP_NAME = "Backtest Studio候補"

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"))
batch_mgr = BatchBacktestManager()


# ─── Static ──────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


# ─── Swing Trader プロキシ（読み取り専用） ──────────────────────────

@app.route("/api/registered-symbols")
def registered_symbols():
    try:
        return jsonify(swing_client.get_symbols())
    except Exception as e:
        return jsonify({"error": f"Swing Traderに接続できません: {e}"}), 502


@app.route("/api/defaults")
def defaults():
    try:
        return jsonify(swing_client.get_defaults())
    except Exception as e:
        return jsonify({"error": f"Swing Traderに接続できません: {e}"}), 502


# ─── OpenD 過去K線クォータ ────────────────────────────────────────

@app.route("/api/opend-quota")
def opend_quota():
    """
    moomoo/FutuのOpenDに直接接続し、過去K線クォータ（使用済み・残り）を取得する。
    history_loader.py が実際のデータ取得直前に呼んでいるのと同じAPI
    （get_history_kl_quota）を、単体で呼ぶだけの読み取り専用エンドポイント。
    """
    try:
        quote_ctx = ft.OpenQuoteContext(host=DEFAULT_OPEND_HOST, port=DEFAULT_OPEND_PORT)
        try:
            ret, quota = quote_ctx.get_history_kl_quota(get_detail=False)
        finally:
            quote_ctx.close()
        if ret != ft.RET_OK:
            return jsonify({"error": f"クォータ取得に失敗しました: {quota}"}), 502
        used, remain, _detail = quota
        return jsonify({"used": used, "remain": remain})
    except Exception as e:
        return jsonify({"error": f"OpenDに接続できません: {e}"}), 502


# ─── moomooアプリの自選株（ウォッチリスト）へ追加 ───────────────────

@app.route("/api/watchlist/groups")
def watchlist_groups():
    """
    moomooアプリ側に存在する自選株グループ（CUSTOM、ユーザー作成分のみ）の一覧を返す。
    グループの新規作成はfutu APIに無いため、既存グループから選ばせるための一覧取得。
    """
    try:
        quote_ctx = ft.OpenQuoteContext(host=DEFAULT_OPEND_HOST, port=DEFAULT_OPEND_PORT)
        try:
            ret, data = quote_ctx.get_user_security_group()
        finally:
            quote_ctx.close()
        if ret != ft.RET_OK:
            return jsonify({"error": f"グループ一覧の取得に失敗しました: {data}"}), 502
        names = [
            n for n in data.loc[data["group_type"] == "CUSTOM", "group_name"].tolist()
            if n
        ]
        seen = set()
        unique_names = [n for n in names if not (n in seen or seen.add(n))]
        return jsonify({"groups": unique_names, "default": WATCHLIST_GROUP_NAME})
    except Exception as e:
        return jsonify({"error": f"OpenDに接続できません: {e}"}), 502


@app.route("/api/watchlist/add", methods=["POST"])
def watchlist_add():
    """
    銘柄発見の候補をmoomooアプリの自選株グループへ追加する（groupパラメータで指定、
    未指定ならWATCHLIST_GROUP_NAME既定）。グループが存在しない場合はfutu側が
    エラーを返す（自動作成はされない）ため、呼び出し側にその旨を分かりやすく伝える。
    """
    data = request.json or {}
    symbols = [s.strip().upper() for s in data.get("symbols", []) if s.strip()]
    group = (data.get("group") or "").strip() or WATCHLIST_GROUP_NAME
    if not symbols:
        return jsonify({"error": "symbols は必須です"}), 400

    try:
        quote_ctx = ft.OpenQuoteContext(host=DEFAULT_OPEND_HOST, port=DEFAULT_OPEND_PORT)
        try:
            ret, result = quote_ctx.modify_user_security(
                group, ft.ModifyUserSecurityOp.ADD, symbols)
        finally:
            quote_ctx.close()
        if ret != ft.RET_OK:
            detail = str(result)
            if "不明" in detail or "unknown" in detail.lower():
                detail = (f"グループ「{group}」がmoomooアプリ側に存在しません。"
                           f"アプリの自選株画面で同名のグループを先に作成してください。（元エラー: {detail}）")
            return jsonify({"error": detail}), 502
        return jsonify({"status": "ok", "group": group, "symbols": symbols})
    except Exception as e:
        return jsonify({"error": f"OpenDに接続できません: {e}"}), 502


# ─── 銘柄発見（Stage 1: OpenD市場スクリーニング） ───────────────────

@app.route("/api/discover", methods=["POST"])
def discover():
    data = request.json or {}
    try:
        result = screener.discover_candidates(
            market=data.get("market", "US"),
            market_cap_min=float(data.get("market_cap_min", 10_000_000_000.0)),
            avg_turnover_min=float(data.get("avg_turnover_min", 20_000_000.0)),
            amplitude_min=float(data.get("amplitude_min", 0.05)),
            amplitude_days=int(data.get("amplitude_days", 5)),
            require_gold_cross=bool(data.get("require_gold_cross", False)),
            gold_cross_timeframe=data.get("gold_cross_timeframe", "K_DAY"),
            max_results=int(data.get("max_results", 50)),
            host=DEFAULT_OPEND_HOST,
            port=DEFAULT_OPEND_PORT,
        )
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": f"スクリーニングに失敗しました: {e}"}), 502


# ─── バッチバックテスト ──────────────────────────────────────────

@app.route("/api/batch", methods=["POST"])
def start_batch():
    data = request.json or {}
    symbols = data.get("symbols", [])
    start_date = (data.get("start_date") or "").strip()
    end_date = (data.get("end_date") or "").strip()
    grid_spec = data.get("grid_spec", {})
    timeframe = (data.get("timeframe") or "").strip() or None

    if not symbols:
        return jsonify({"error": "対象銘柄を1つ以上指定してください"}), 400
    if not start_date or not end_date:
        return jsonify({"error": "start_date, end_date は必須です"}), 400
    if timeframe and timeframe not in ("K_60M", "K_DAY"):
        return jsonify({"error": "timeframe は K_60M か K_DAY を指定してください"}), 400

    job_id = batch_mgr.start(symbols, start_date, end_date, grid_spec, timeframe)
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


# ─── Swing Traderへの反映 ────────────────────────────────────────

@app.route("/api/apply", methods=["POST"])
def apply_to_swing_trader():
    data = request.json or {}
    symbol_id = (data.get("symbol_id") or "").upper().strip()
    is_new = bool(data.get("is_new", False))
    entry_overrides = data.get("entry_overrides", {})
    timeframe = data.get("timeframe")

    if not symbol_id:
        return jsonify({"error": "symbol_id は必須です"}), 400

    payload = {"entry": entry_overrides}
    if timeframe:
        payload["macd"] = {"timeframe": timeframe}

    if is_new:
        ok, body, status = swing_client.create_symbol({"symbol": symbol_id, **payload})
        return jsonify(body), status

    try:
        if swing_client.is_running(symbol_id):
            return jsonify({"error": f"{symbol_id} は現在稼働中です。Swing Trader側で先にボットを停止してください。"}), 409
    except Exception as e:
        return jsonify({"error": f"Swing Traderに接続できません: {e}"}), 502

    ok, body, status = swing_client.update_symbol(symbol_id, payload)
    return jsonify(body), status


# ─── Entry Point ─────────────────────────────────────────────────

if __name__ == "__main__":
    os.makedirs(os.path.join(BASE_DIR, "static"), exist_ok=True)
    print("=" * 50)
    print("  Swing Backtest Studio 起動")
    print("  http://localhost:5004 をブラウザで開いてください")
    print("  ※ Swing Trader (http://localhost:5003) が起動している必要があります")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5004, debug=False, threaded=True)
