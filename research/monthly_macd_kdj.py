"""
monthly_macd_kdj.py
MACD_KDJ側（現行の実運用設定）の月次内訳を、MA_RSIとの比較用に取得する一回限りのスクリプト。
backtest_studio/batch_manager.py と同じ月次集計パターン（on_exitコールバック）を使う。

使い方: python3 monthly_macd_kdj.py US.SPCX,US.NVDA,... 2025-08-08 2026-08-08
"""
from __future__ import annotations

import sys
from pathlib import Path

MACD_TRADER_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/macd_trader")
sys.path.insert(0, str(MACD_TRADER_DIR))

from config_loader import MacdConfig, EntryConfig, ExitConfig, OrderConfig, RiskConfig, OpendConfig  # noqa: E402
from fast_replay import fast_replay  # noqa: E402
from backtest import _load_data, load_symbol_config  # noqa: E402


def main():
    symbols = sys.argv[1].split(",")
    start_date, end_date = sys.argv[2], sys.argv[3]

    combined_monthly: dict[str, float] = {}
    for symbol_id in symbols:
        cfg_dict = load_symbol_config(symbol_id)
        macd_cfg = MacdConfig(**cfg_dict["macd"])
        entry_cfg = EntryConfig(**cfg_dict["entry"])
        exit_cfg = ExitConfig(**cfg_dict["exit"])
        order_cfg = OrderConfig(**cfg_dict["order"])
        risk_cfg = RiskConfig(**cfg_dict["risk"])
        opend_cfg = OpendConfig(**cfg_dict["opend"])

        df = _load_data(symbol_id, start_date, end_date, macd_cfg, opend_cfg)

        monthly: dict[str, float] = {}

        def on_exit(price, qty, entry, hold, reason, daily_trades, bar_time, pnl):
            key = bar_time.strftime("%Y-%m")
            monthly[key] = monthly.get(key, 0.0) + pnl

        closed_trades, total_pnl = fast_replay(
            df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg, on_exit=on_exit,
        )
        print(f"  {symbol_id}: {closed_trades}取引 | 損益 {total_pnl:+.2f}")
        for month_key, month_pnl in monthly.items():
            combined_monthly[month_key] = combined_monthly.get(month_key, 0.0) + month_pnl

    print("\n  月次内訳（全銘柄合算）:")
    profitable_months = 0
    for month_key in sorted(combined_monthly.keys()):
        m_pnl = combined_monthly[month_key]
        mark = "✅" if m_pnl > 0 else ("➖" if m_pnl == 0 else "⚠️")
        if m_pnl > 0:
            profitable_months += 1
        print(f"    {month_key}: {mark} {m_pnl:+.2f}")
    total_months = len(combined_monthly)
    print(f"  {total_months}ヶ月中{profitable_months}ヶ月が黒字（{profitable_months / total_months * 100:.0f}%）")


if __name__ == "__main__":
    main()
