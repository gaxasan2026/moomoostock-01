"""
swing_symbol_store.py
macd_trader/symbol_store.py の SymbolStore をそのまま再利用し、
新規銘柄追加時のデフォルト値だけ SWING_DEFAULT_CONFIG に差し替えたサブクラス。

list/get/update/delete は親クラスのまま（汎用CRUDで日付ロジックを含まないため
フォーク不要）。独自の data/symbols.json を持つため、macd_trader側の
symbols.json とは完全に独立している（読み書きの競合なし）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "macd_trader"))
from symbol_store import SymbolStore, _deep_merge  # noqa: E402

from swing_config import SWING_DEFAULT_CONFIG  # noqa: E402


class SwingSymbolStore(SymbolStore):
    def add(self, data: dict) -> dict:
        symbol_id = data["symbol"].upper().strip()
        cfg = _deep_merge(SWING_DEFAULT_CONFIG, data)
        cfg["symbol"] = symbol_id
        safe = symbol_id.replace(".", "_")
        cfg["logging"]["trade_log_path"] = f"logs/trades_{safe}.csv"
        self._symbols[symbol_id] = cfg
        self._save()
        return cfg
