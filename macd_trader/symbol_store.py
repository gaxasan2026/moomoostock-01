"""
symbol_store.py
銘柄設定を data/symbols.json で永続管理する
"""
import json
from pathlib import Path
from typing import Optional

DEFAULT_CONFIG: dict = {
    "market": "US",
    "macd": {"fast_period": 12, "slow_period": 26, "signal_period": 9, "timeframe": "K_1M"},
    "entry": {"gc_duration_minutes": 3.0, "macd_histogram_min": 0.0,
              "volume_surge_ratio": 1.0, "max_spread_pct": 0.1},
    "exit": {"peak_drop_pct": 1.5, "peak_drop_duration_minutes": 5.0,
             "peak_confirmation_bars": 3, "dead_cross": True, "dc_duration_minutes": 0.0,
             "take_profit_pct": 3.0, "stop_loss_pct": 1.5, "max_hold_minutes": 60.0},
    "order": {"quantity": 5, "order_type": "market", "limit_offset_pct": 0.02, "paper_trading": True, "mock_data": False},
    "risk": {"max_daily_trades": 10, "max_daily_loss_pct": 3.0,
             "cooldown_minutes": 5.0, "max_position_value": 0.0},
    "logging": {"level": "INFO", "save_trade_log": True, "show_realtime_price": True},
    "opend": {"host": "127.0.0.1", "port": 11111},
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


class SymbolStore:
    def __init__(self, path: str):
        self._path = Path(path)
        self._symbols: dict = {}
        if self._path.exists():
            self._load()

    def _load(self):
        with open(self._path, "r", encoding="utf-8") as f:
            self._symbols = json.load(f).get("symbols", {})

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump({"symbols": self._symbols}, f, ensure_ascii=False, indent=2)

    def list(self) -> list:
        return list(self._symbols.values())

    def get(self, symbol_id: str) -> Optional[dict]:
        return self._symbols.get(symbol_id)

    def add(self, data: dict) -> dict:
        symbol_id = data["symbol"].upper().strip()
        cfg = _deep_merge(DEFAULT_CONFIG, data)
        cfg["symbol"] = symbol_id
        safe = symbol_id.replace(".", "_")
        cfg["logging"]["trade_log_path"] = f"logs/trades_{safe}.csv"
        self._symbols[symbol_id] = cfg
        self._save()
        return cfg

    def update(self, symbol_id: str, data: dict) -> Optional[dict]:
        if symbol_id not in self._symbols:
            return None
        self._symbols[symbol_id] = _deep_merge(self._symbols[symbol_id], data)
        self._save()
        return self._symbols[symbol_id]

    def delete(self, symbol_id: str):
        self._symbols.pop(symbol_id, None)
        self._save()
