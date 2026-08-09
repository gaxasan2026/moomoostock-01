"""
position_store.py
Trader自身が売買した数量・建値・ピーク価格を data/positions.json に永続化する。

signal_tracker.PositionInfo はメモリ上にしか存在しないため、bot停止→再開をまたぐと
消えてしまう。これを避けるため、BUY/SELL確定のたびにこのストアへ保存し、
起動時にSignalTracker.restore_position()で復元する（position_reconciler.py経由）。

symbols.json（銘柄設定）とは完全に別ファイルにすることで、この機能に不具合があっても
銘柄設定そのものは壊さない。複数のbotスレッドが同一ファイルへ書き込むため、
スレッドロックで保護する。
"""
import json
import threading
from pathlib import Path
from typing import Optional


class PositionStore:
    def __init__(self, path: str):
        self._path = Path(path)
        self._lock = threading.Lock()
        self._data: dict = {}
        if self._path.exists():
            self._load()

    def _load(self):
        with open(self._path, "r", encoding="utf-8") as f:
            self._data = json.load(f)

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)
        tmp.replace(self._path)

    def get(self, symbol_id: str) -> Optional[dict]:
        with self._lock:
            return self._data.get(symbol_id)

    def save(self, symbol_id: str, entry_price: float, entry_time: str,
             quantity: int, peak_price: float, peak_time: str,
             bars_since_peak: int = 0):
        with self._lock:
            self._data[symbol_id] = {
                "entry_price": entry_price,
                "entry_time": entry_time,
                "quantity": quantity,
                "peak_price": peak_price,
                "peak_time": peak_time,
                "bars_since_peak": bars_since_peak,
            }
            self._save()

    def clear(self, symbol_id: str):
        with self._lock:
            if symbol_id in self._data:
                del self._data[symbol_id]
                self._save()
