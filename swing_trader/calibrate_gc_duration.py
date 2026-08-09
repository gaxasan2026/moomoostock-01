"""
calibrate_gc_duration.py
gc_duration_minutesの較正のため、年別の内訳つきで集計する検証スクリプト。
合計損益だけでは特定の年（銘柄）に偏った「まぐれ」かどうか分からないため、
research/配下のスクリプトと同じ流儀（年別・銘柄別で必ず確認する）を踏襲する。

対象: ユーザーが実際に登録している9銘柄のうち、日足200本の最低要件を
満たせる6銘柄（US.CBRS/DRAM/SPCXはデータ不足のため除外）。
"""
from __future__ import annotations

import copy
import sys
from collections import defaultdict
from pathlib import Path

MACD_TRADER_DIR = Path(__file__).parent.parent / "macd_trader"
sys.path.insert(0, str(MACD_TRADER_DIR))
sys.path.insert(0, str(Path(__file__).parent))

import dataclasses  # noqa: E402
from config_loader import EntryConfig  # noqa: E402
from backtest import _load_data  # noqa: E402

from swing_config import SWING_DEFAULT_CONFIG  # noqa: E402
from swing_backtest import _build_configs, swing_replay  # noqa: E402

SYMBOLS = ["US.COHR", "US.TSLA", "US.QQQ", "US.NVDA", "US.MU", "US.MSFT"]
GC_VALUES = [1440.0, 2880.0, 4320.0, 5760.0]
START_DATE = "2023-01-01"
END_DATE = "2026-07-30"


def main():
    print(f"=== gc_duration_minutes 年別較正 {START_DATE}〜{END_DATE} ===", flush=True)

    # symbol -> config(str) -> year -> {"trades": int, "pnl": float}
    results: dict[str, dict[str, dict[int, dict]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(lambda: {"trades": 0, "pnl": 0.0})))

    for symbol in SYMBOLS:
        macd_cfg, base_entry_cfg, exit_cfg, order_cfg, risk_cfg, opend_cfg = _build_configs(None)
        try:
            df = _load_data(symbol, START_DATE, END_DATE, macd_cfg, opend_cfg)
        except (Exception, SystemExit) as e:
            print(f"  !!! {symbol} データ取得失敗: {e}", flush=True)
            continue
        print(f"--- {symbol} ({len(df)}本) ---", flush=True)

        for gc_val in GC_VALUES:
            entry_cfg = dataclasses.replace(base_entry_cfg, gc_duration_minutes=gc_val)

            def on_exit(price, qty, entry, hold, reason, daily_trades, bar_time, pnl, gc_dur_at_exit, _gc=gc_val, _sym=symbol):
                year = bar_time.year
                bucket = results[_sym][str(_gc)][year]
                bucket["trades"] += 1
                bucket["pnl"] += pnl

            try:
                swing_replay(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg, on_exit=on_exit)
            except (Exception, SystemExit) as e:
                print(f"  !!! {symbol} gc={gc_val} 失敗: {e}", flush=True)

    years = [2023, 2024, 2025, 2026]
    print("\n" + "=" * 100)
    for symbol in SYMBOLS:
        if symbol not in results:
            continue
        print(f"\n--- {symbol} ---")
        header = f"{'gc(分)':>10}" + "".join(f"{y:>14}" for y in years) + f"{'合計':>14}"
        print(header)
        for gc_val in GC_VALUES:
            key = str(gc_val)
            row_total = 0.0
            row = f"{gc_val:>10.0f}"
            for y in years:
                b = results[symbol][key].get(y, {"trades": 0, "pnl": 0.0})
                row_total += b["pnl"]
                cell = f"{b['pnl']:+.0f}({b['trades']})" if b["trades"] else "—"
                row += f"{cell:>14}"
            row += f"{row_total:>+14.2f}"
            print(row)

    # 銘柄横断の年別合計（COHR除く/含む両方は行わず、まず単純合計）
    print("\n" + "=" * 100)
    print("=== 6銘柄合計（年別） ===")
    header = f"{'gc(分)':>10}" + "".join(f"{y:>14}" for y in years) + f"{'合計':>14}"
    print(header)
    for gc_val in GC_VALUES:
        key = str(gc_val)
        row_total = 0.0
        row = f"{gc_val:>10.0f}"
        for y in years:
            year_total = sum(results[s][key].get(y, {"pnl": 0.0})["pnl"] for s in SYMBOLS if s in results)
            row_total += year_total
            row += f"{year_total:>+14.2f}"
        row += f"{row_total:>+14.2f}"
        print(row)


if __name__ == "__main__":
    main()
