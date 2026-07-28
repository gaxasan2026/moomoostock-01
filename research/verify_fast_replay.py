"""
verify_fast_replay.py
fast_replay.py が backtest.py の _replay() と、取引単位で完全に同じ結果を
出すことを検証する。1つでも不一致があれば使用してはならない。
"""
import sys
import time
from pathlib import Path

import pandas as pd

MACD_TRADER_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/macd_trader")
STUDIO_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/backtest_studio")
sys.path.insert(0, str(MACD_TRADER_DIR))
sys.path.insert(0, str(STUDIO_DIR))

from backtest import _load_data, _replay  # noqa: E402
from config_loader import MacdConfig, EntryConfig, ExitConfig, OrderConfig, RiskConfig, OpendConfig  # noqa: E402
from fast_replay import fast_replay  # noqa: E402
import macd_client  # noqa: E402


def collect_trades(replay_fn, df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg):
    trades = []

    def on_exit(price, qty, entry, hold, reason, daily_trades, bar_time, pnl):
        trades.append((round(price, 6), qty, round(entry, 6), round(hold, 3), reason, str(bar_time), round(pnl, 6)))

    closed_trades, total_pnl = replay_fn(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg, on_exit=on_exit)
    return closed_trades, round(total_pnl, 6), trades


SCENARIOS = [
    # (symbol, start, end, entry_overrides)
    ("US.NVDA", "2026-04-01", "2026-06-30", {}),
    ("US.NVDA", "2026-04-01", "2026-06-30", {"kdj_max_d": 50.0}),
    ("US.NVDA", "2026-04-01", "2026-06-30", {"gc_duration_minutes": 7.0, "kdj_max_d": 80.0}),
    ("US.JPM", "2026-01-01", "2026-03-31", {"kdj_max_d": 50.0}),
    ("US.XOM", "2025-10-01", "2025-12-31", {}),
]

all_ok = True
for symbol_id, start, end, overrides in SCENARIOS:
    base_cfg = macd_client.get_defaults()
    macd_cfg = MacdConfig(**base_cfg["macd"])
    entry_cfg = EntryConfig(**{**base_cfg["entry"], **overrides})
    exit_cfg = ExitConfig(**base_cfg["exit"])
    order_cfg = OrderConfig(**base_cfg["order"])
    risk_cfg = RiskConfig(**base_cfg["risk"])
    opend_cfg = OpendConfig(**base_cfg["opend"])

    df = _load_data(symbol_id, start, end, macd_cfg, opend_cfg)

    t0 = time.time()
    official_n, official_pnl, official_trades = collect_trades(_replay, df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg)
    t_official = time.time() - t0

    t0 = time.time()
    fast_n, fast_pnl, fast_trades = collect_trades(fast_replay, df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg)
    t_fast = time.time() - t0

    match = (official_n == fast_n and official_pnl == fast_pnl and official_trades == fast_trades)
    status = "✅ 一致" if match else "❌ 不一致"
    print(f"{status} {symbol_id} {start}〜{end} {overrides}")
    print(f"   公式: {official_n}件 {official_pnl:+.2f}USD ({t_official:.1f}秒)")
    print(f"   高速: {fast_n}件 {fast_pnl:+.2f}USD ({t_fast:.1f}秒)  高速化 {t_official/t_fast:.1f}倍")
    if not match:
        all_ok = False
        for a, b in zip(official_trades, fast_trades):
            if a != b:
                print(f"   差分: 公式={a}\n         高速={b}")
        if len(official_trades) != len(fast_trades):
            print(f"   取引数が違う: 公式{len(official_trades)}件 vs 高速{len(fast_trades)}件")

print(f"\n{'✅ 全シナリオ一致 — 使用可' if all_ok else '❌ 不一致あり — 使用不可、原因調査が必要'}")
sys.exit(0 if all_ok else 1)
