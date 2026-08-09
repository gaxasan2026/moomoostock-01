"""
monthly_macd_kdj_swing.py
Swing Trader側MACD_KDJ（現行の実運用設定＝各銘柄の登録済み設定そのまま）の
月次内訳を、MA_RSIとの比較用に取得する一回限りのスクリプト。
swing_backtest.py の swing_replay() をそのまま使う（登録済み銘柄の実設定を
読むため、SWING_DEFAULT_CONFIG固定のrun_swing_backtest()は使わない）。

使い方: python3 monthly_macd_kdj_swing.py US.WMT,US.MRVL,... 2023-08-08 2026-08-08
"""
from __future__ import annotations

import sys
from pathlib import Path

MACD_TRADER_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/macd_trader")
SWING_TRADER_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/swing_trader")
sys.path.insert(0, str(MACD_TRADER_DIR))
sys.path.insert(0, str(SWING_TRADER_DIR))

from config_loader import MacdConfig, EntryConfig, ExitConfig, OrderConfig, RiskConfig, OpendConfig  # noqa: E402
from backtest import _load_data  # noqa: E402
from swing_backtest import swing_replay  # noqa: E402
from swing_symbol_store import SwingSymbolStore  # noqa: E402


def main():
    symbols = sys.argv[1].split(",")
    start_date, end_date = sys.argv[2], sys.argv[3]

    store = SwingSymbolStore(str(SWING_TRADER_DIR / "data" / "symbols.json"))

    combined_monthly: dict[str, float] = {}
    for symbol_id in symbols:
        cfg_dict = store.get(symbol_id)
        if not cfg_dict:
            print(f"  {symbol_id}: 登録されていません — スキップ")
            continue
        macd_cfg = MacdConfig(**cfg_dict["macd"])
        entry_cfg = EntryConfig(**cfg_dict["entry"])
        exit_cfg = ExitConfig(**cfg_dict["exit"])
        order_cfg = OrderConfig(**cfg_dict["order"])
        risk_cfg = RiskConfig(**cfg_dict["risk"])
        opend_cfg = OpendConfig(**cfg_dict["opend"])

        try:
            df = _load_data(symbol_id, start_date, end_date, macd_cfg, opend_cfg)
        except SystemExit as e:
            print(f"  {symbol_id}: エラー — {e}")
            continue

        monthly: dict[str, float] = {}

        def on_exit(price, qty, entry, hold, reason, daily_trades, bar_time, pnl, gc_dur_at_exit):
            key = bar_time.strftime("%Y-%m")
            monthly[key] = monthly.get(key, 0.0) + pnl

        closed_trades, total_pnl = swing_replay(
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
    if total_months:
        print(f"  {total_months}ヶ月中{profitable_months}ヶ月が黒字（{profitable_months / total_months * 100:.0f}%）")


if __name__ == "__main__":
    main()
