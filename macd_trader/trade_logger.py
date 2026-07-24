"""
trade_logger.py
取引履歴をCSVに保存し、コンソールに見やすく出力する
"""
import csv
import logging
import os
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class TradeLogger:
    def __init__(self, log_path: str = "logs/trades.csv", enabled: bool = True):
        self.enabled = enabled
        self.log_path = Path(log_path)
        if self.enabled:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_csv()

    def _init_csv(self):
        """CSVファイルのヘッダーを作成（既存の場合はスキップ）"""
        if not self.log_path.exists():
            with open(self.log_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "action", "symbol", "price", "quantity",
                    "entry_price", "pnl_usd", "pnl_pct",
                    "hold_minutes", "exit_reason", "gc_duration", "daily_trades"
                ])

    def log_entry(self, symbol: str, price: float, quantity: int,
                  gc_duration: float, timestamp: datetime):
        """買いエントリーを記録"""
        print(f"\n{'='*60}")
        print(f"  📥 BUY  {symbol}  {quantity}株 @ ${price:.4f}")
        print(f"     GC継続: {gc_duration:.1f}分  |  {timestamp.strftime('%H:%M:%S')}")
        print(f"{'='*60}\n")

        if self.enabled:
            with open(self.log_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    timestamp.isoformat(), "BUY", symbol, price, quantity,
                    price, "", "", "", "", gc_duration, ""
                ])

    def log_exit(self, symbol: str, price: float, quantity: int,
                 entry_price: float, hold_minutes: float,
                 exit_reason: str, daily_trades: int, timestamp: datetime):
        """売りエグジットを記録"""
        pnl = (price - entry_price) * quantity
        pnl_pct = (price - entry_price) / entry_price * 100
        emoji = "🟢" if pnl >= 0 else "🔴"

        print(f"\n{'='*60}")
        print(f"  📤 SELL {symbol}  {quantity}株 @ ${price:.4f}")
        print(f"     {emoji} PnL: {pnl:+.2f}USD ({pnl_pct:+.2f}%)")
        print(f"     保有時間: {hold_minutes:.1f}分  理由: {exit_reason}")
        print(f"     本日取引: {daily_trades}回  |  {timestamp.strftime('%H:%M:%S')}")
        print(f"{'='*60}\n")

        if self.enabled:
            with open(self.log_path, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    timestamp.isoformat(), "SELL", symbol, price, quantity,
                    entry_price, f"{pnl:.2f}", f"{pnl_pct:.2f}",
                    f"{hold_minutes:.1f}", exit_reason, "", daily_trades
                ])

    def log_skip(self, reason: str):
        """取引スキップ理由を記録（DEBUGレベル）"""
        logger.debug(f"⏭️  スキップ: {reason}")
