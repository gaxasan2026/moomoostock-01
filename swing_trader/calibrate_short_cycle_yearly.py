"""
calibrate_short_cycle_yearly.py
test_short_cycle.py で最良だった候補（K_60M, gc_duration_minutes=120,
peak_drop_duration_minutes=120, max_hold_minutes=2880）について、
年別の内訳で頑健性を確認する。合計損益だけでは特定の年（銘柄）に
偏った結果でないか分からないため、calibrate_gc_duration.pyと同じ流儀で検証する。
"""
from __future__ import annotations

import dataclasses
import sys
from collections import defaultdict
from pathlib import Path

MACD_TRADER_DIR = Path(__file__).parent.parent / "macd_trader"
sys.path.insert(0, str(MACD_TRADER_DIR))
sys.path.insert(0, str(Path(__file__).parent))

from backtest import _load_data  # noqa: E402

from swing_backtest import _build_configs, swing_replay  # noqa: E402

SYMBOLS = ["US.COHR", "US.TSLA", "US.QQQ", "US.NVDA", "US.MU", "US.MSFT"]
START_DATE = "2023-01-01"
END_DATE = "2026-07-30"

GC_DURATION = 120.0
PEAK_DROP_DURATION = 120.0
MAX_HOLD = 2880.0


def main():
    print(f"=== 短期サイクル候補（gc=120分/peak=120分/max_hold=2880分, K_60M）年別頑健性確認 ===\n")

    results: dict[str, dict[int, dict]] = defaultdict(lambda: defaultdict(lambda: {"trades": 0, "pnl": 0.0}))

    for symbol in SYMBOLS:
        macd_cfg, base_entry_cfg, base_exit_cfg, order_cfg, risk_cfg, opend_cfg = _build_configs("K_60M")
        entry_cfg = dataclasses.replace(base_entry_cfg, gc_duration_minutes=GC_DURATION)
        exit_cfg = dataclasses.replace(base_exit_cfg, peak_drop_duration_minutes=PEAK_DROP_DURATION,
                                        max_hold_minutes=MAX_HOLD)
        try:
            df = _load_data(symbol, START_DATE, END_DATE, macd_cfg, opend_cfg)
        except (Exception, SystemExit) as e:
            print(f"  !!! {symbol} データ取得失敗: {e}")
            continue
        print(f"--- {symbol} ({len(df)}本) ---", flush=True)

        def on_exit(price, qty, entry, hold, reason, daily_trades, bar_time, pnl, gc_dur, _sym=symbol):
            year = bar_time.year
            b = results[_sym][year]
            b["trades"] += 1
            b["pnl"] += pnl

        swing_replay(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg, on_exit=on_exit)

    years = [2023, 2024, 2025, 2026]
    print("\n" + "=" * 90)
    header = f"{'銘柄':<10}" + "".join(f"{y:>16}" for y in years) + f"{'合計':>16}"
    print(header)
    year_totals = defaultdict(float)
    grand_total = 0.0
    for symbol in SYMBOLS:
        if symbol not in results:
            continue
        row = f"{symbol:<10}"
        row_total = 0.0
        for y in years:
            b = results[symbol].get(y, {"trades": 0, "pnl": 0.0})
            row_total += b["pnl"]
            year_totals[y] += b["pnl"]
            cell = f"{b['pnl']:+.0f}({b['trades']})" if b["trades"] else "—"
            row += f"{cell:>16}"
        grand_total += row_total
        row += f"{row_total:>+16.2f}"
        print(row)

    print("-" * 90)
    total_row = f"{'年別合計':<10}" + "".join(f"{year_totals[y]:>+16.2f}" for y in years) + f"{grand_total:>+16.2f}"
    print(total_row)

    print("\n=== MUを除いた場合（一銘柄依存でないか確認） ===")
    for y in years:
        ex_mu = sum(results[s].get(y, {"pnl": 0.0})["pnl"] for s in SYMBOLS if s != "US.MU" and s in results)
        print(f"  {y}: {ex_mu:+.2f} （MU含む: {year_totals[y]:+.2f}）")


if __name__ == "__main__":
    main()
