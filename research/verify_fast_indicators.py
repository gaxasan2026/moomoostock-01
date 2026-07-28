"""
verify_fast_indicators.py
fast_indicators.py の高速版が、既存の macd_engine.py / kdj_engine.py と
数値的に一致することを検証する。この検証に通らない限り、fast_indicators.py を
walk_forward.py 等の実際の検証に使ってはならない。
"""
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

MACD_TRADER_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/macd_trader")
sys.path.insert(0, str(MACD_TRADER_DIR))

from macd_engine import MacdEngine  # noqa: E402
from kdj_engine import KdjEngine  # noqa: E402
from history_loader import load_or_fetch_history  # noqa: E402
from fast_indicators import fast_macd_latest, fast_kdj_latest  # noqa: E402

KLINE_WINDOW = 200
N_SAMPLES = 5000  # 検証に使うウィンドウ数

df = load_or_fetch_history("US.NVDA", "2025-07-27", "2026-07-26", timeframe="K_1M")
df["time_key"] = pd.to_datetime(df["time_key"])
df = df.sort_values("time_key").reset_index(drop=True)

macd_engine = MacdEngine()
kdj_engine = KdjEngine()

rng = np.random.default_rng(42)
indices = rng.choice(range(KLINE_WINDOW, len(df)), size=N_SAMPLES, replace=False)

max_diff_macd = 0.0
max_diff_signal = 0.0
max_diff_hist = 0.0
max_diff_k = 0.0
max_diff_d = 0.0
max_diff_j = 0.0
golden_mismatches = 0
cross_mismatches = 0

t_official = 0.0
t_fast = 0.0

for i in indices:
    window = df.iloc[i - KLINE_WINDOW:i]
    closes = window["close"].to_numpy(dtype=np.float64)
    highs = window["high"].to_numpy(dtype=np.float64)
    lows = window["low"].to_numpy(dtype=np.float64)

    t0 = time.perf_counter()
    official_macd_df = macd_engine.calculate(window)
    official_macd = macd_engine.get_latest(official_macd_df)
    official_kdj_df = kdj_engine.calculate(window)
    official_kdj = kdj_engine.get_latest(official_kdj_df)
    t_official += time.perf_counter() - t0

    t0 = time.perf_counter()
    fast_macd = fast_macd_latest(closes)
    fast_kdj = fast_kdj_latest(highs, lows, closes)
    t_fast += time.perf_counter() - t0

    max_diff_macd = max(max_diff_macd, abs(official_macd.macd - fast_macd.macd))
    max_diff_signal = max(max_diff_signal, abs(official_macd.signal - fast_macd.signal))
    max_diff_hist = max(max_diff_hist, abs(official_macd.histogram - fast_macd.histogram))
    max_diff_k = max(max_diff_k, abs(official_kdj.k - fast_kdj.k))
    max_diff_d = max(max_diff_d, abs(official_kdj.d - fast_kdj.d))
    max_diff_j = max(max_diff_j, abs(official_kdj.j - fast_kdj.j))
    if official_macd.is_golden != fast_macd.is_golden:
        golden_mismatches += 1
    if official_macd.cross != fast_macd.cross:
        cross_mismatches += 1

print(f"検証サンプル数: {N_SAMPLES}")
print(f"MACD 最大誤差: {max_diff_macd:.2e}")
print(f"Signal 最大誤差: {max_diff_signal:.2e}")
print(f"Histogram 最大誤差: {max_diff_hist:.2e}")
print(f"KDJ %K 最大誤差: {max_diff_k:.2e}")
print(f"KDJ %D 最大誤差: {max_diff_d:.2e}")
print(f"KDJ %J 最大誤差: {max_diff_j:.2e}")
print(f"is_golden 不一致件数: {golden_mismatches}")
print(f"cross 不一致件数: {cross_mismatches}")
print(f"\n公式実装の所要時間: {t_official:.3f}秒 ({t_official/N_SAMPLES*1000:.4f}ms/件)")
print(f"高速版の所要時間  : {t_fast:.3f}秒 ({t_fast/N_SAMPLES*1000:.4f}ms/件)")
print(f"高速化倍率: {t_official/t_fast:.1f}倍")

TOLERANCE = 1e-6
ok = (max_diff_macd < TOLERANCE and max_diff_signal < TOLERANCE and max_diff_hist < TOLERANCE
      and max_diff_k < TOLERANCE and max_diff_d < TOLERANCE and max_diff_j < TOLERANCE
      and golden_mismatches == 0 and cross_mismatches == 0)
print(f"\n{'✅ 検証OK — 数値的に一致' if ok else '❌ 検証NG — 不一致あり、使用不可'}")
sys.exit(0 if ok else 1)
