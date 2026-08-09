"""
swing_client.py
Swing Trader（本番アプリ、別プロセス、ポート5003）のREST APIを呼ぶ薄いクライアント。

backtest_studio/macd_client.py と全く同じ設計。Swing Backtest Studioは
swing_trader/data/symbols.json を直接読み書きしない。すべての銘柄設定の
読み書きは、このHTTPクライアント経由で行う。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE_URL = os.environ.get("SWING_TRADER_API", "http://127.0.0.1:5003")
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
