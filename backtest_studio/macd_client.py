"""
macd_client.py
MACD Trader（本番アプリ、別プロセス）のREST APIを呼ぶ薄いクライアント。

Backtest Studioは data/symbols.json を直接読み書きしない。
SymbolStoreはMACD Trader側のプロセス内シングルトンでファイルロックが無いため、
別プロセスから直接ファイルに触れると設定が食い違う（詳細はplan参照）。
すべての銘柄設定の読み書きは、この薄いHTTPクライアント経由で行う。

標準ライブラリ（urllib）のみを使用し、新規の外部依存を追加しない
（discord_notifier.pyと同じ方針）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ.get("MACD_TRADER_API", "http://127.0.0.1:5001")
TIMEOUT = 10


def _request(method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    url = f"{BASE_URL}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        return e.code, (json.loads(body) if body else {"error": str(e)})


def get_symbols() -> list:
    status, body = _request("GET", "/api/symbols")
    return body


def get_symbol(symbol_id: str) -> dict | None:
    status, body = _request("GET", f"/api/symbols/{symbol_id}")
    return None if status == 404 else body


def get_defaults() -> dict:
    status, body = _request("GET", "/api/defaults")
    return body


def get_status() -> dict:
    status, body = _request("GET", "/api/status")
    return body


def is_running(symbol_id: str) -> bool:
    return bool(get_status().get(symbol_id, {}).get("running"))


def create_symbol(payload: dict) -> tuple[bool, dict, int]:
    status, body = _request("POST", "/api/symbols", payload)
    return (200 <= status < 300), body, status


def update_symbol(symbol_id: str, payload: dict) -> tuple[bool, dict, int]:
    status, body = _request("PUT", f"/api/symbols/{symbol_id}", payload)
    return (200 <= status < 300), body, status
