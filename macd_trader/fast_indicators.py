"""
fast_indicators.py
backtest.py / Backtest Studio 専用の高速な指標計算。ライブ取引（bot_manager.py）は
一切これをimportしない。

重要な設計方針:
- macd_engine.py / kdj_engine.py / bar_signals.py（ライブ取引と共有しているコード）は
  一切変更しない。ここは全く別のモジュールであり、既存のライブ取引の
  挙動には何の影響も与えない。
- 売買判定ロジック（SignalTracker / TradeEngine / decide_trade）は複製しない。
  ここで高速化するのは「200本ウィンドウからMACD/KDJの最終値を計算する」という
  純粋な数値計算の部分だけ。遅さの原因はpandasのSeries/DataFrame操作の
  呼び出しごとのオーバーヘッドであり、計算内容そのものではない
  （実測: 200本の単純な切り出し自体は0.008ms、MACD計算は0.715ms、KDJ計算は0.737ms）。
- macd_engine.calculate()と同じ再帰式（EMA, adjust=False）、
  kdj_engine.calculate()と同じ再帰式（RSV→EMA、com指定）をnumpyで再実装し、
  「最終行の値」だけを返す（中間の全行分の列は作らない）。
- 数値・取引単位での既存実装との一致は verify_fast_indicators.py / verify_fast_replay.py
  （research/）で検証済み（誤差1e-13〜1e-14、取引結果は完全一致）。
"""
from __future__ import annotations

import numpy as np

from macd_engine import CrossSignal, MacdValues
from kdj_engine import KdjValues


def _ewm(values: np.ndarray, alpha: float) -> np.ndarray:
    """pandas の .ewm(..., adjust=False).mean() と同じ再帰式。
    EMA[0] = values[0]; EMA[t] = alpha*values[t] + (1-alpha)*EMA[t-1]"""
    n = len(values)
    out = np.empty(n, dtype=np.float64)
    out[0] = values[0]
    prev = values[0]
    for i in range(1, n):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def fast_macd_latest(closes: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9) -> MacdValues:
    """macd_engine.MacdEngine.calculate()+get_latest() の最終行と同じ値を返す（高速版）。"""
    alpha_fast = 2.0 / (fast + 1)
    alpha_slow = 2.0 / (slow + 1)
    alpha_signal = 2.0 / (signal + 1)

    ema_fast = _ewm(closes, alpha_fast)
    ema_slow = _ewm(closes, alpha_slow)
    macd = ema_fast - ema_slow
    sig = _ewm(macd, alpha_signal)
    hist = macd - sig
    is_golden = macd > sig

    curr_golden = bool(is_golden[-1])
    prev_golden = bool(is_golden[-2]) if len(is_golden) > 1 else False
    if curr_golden and not prev_golden:
        cross = CrossSignal.GOLDEN_CROSS
    elif (not curr_golden) and prev_golden:
        cross = CrossSignal.DEAD_CROSS
    else:
        cross = CrossSignal.NONE

    return MacdValues(
        macd=float(macd[-1]), signal=float(sig[-1]), histogram=float(hist[-1]),
        cross=cross, is_golden=curr_golden,
    )


def fast_kdj_latest(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                     fast_k: int = 9, slow_k: int = 3, slow_d: int = 3) -> KdjValues:
    """kdj_engine.KdjEngine.calculate()+get_latest() の最終行と同じ値を返す（高速版）。"""
    n = len(closes)
    rsv = np.empty(n, dtype=np.float64)
    for i in range(n):
        start = max(0, i - fast_k + 1)
        low_min = lows[start:i + 1].min()
        high_max = highs[start:i + 1].max()
        denom = high_max - low_min
        rsv[i] = (closes[i] - low_min) / denom * 100 if denom > 0 else 50.0

    alpha_k = 1.0 / (1 + (slow_k - 1))   # pandas .ewm(com=slow_k-1, adjust=False)
    alpha_d = 1.0 / (1 + (slow_d - 1))
    k = _ewm(rsv, alpha_k)
    d = _ewm(k, alpha_d)
    j = 3 * k - 2 * d

    return KdjValues(k=float(k[-1]), d=float(d[-1]), j=float(j[-1]))
