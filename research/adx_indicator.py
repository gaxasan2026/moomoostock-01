"""
adx_indicator.py
ADX（Average Directional Index、平均方向性指数）の実装。

MACD/KDJとは別系統の「トレンド強度専用」指標。標準的なWilderの計算式に従う:
  1. True Range (TR)、+DM、-DMを算出
  2. Wilderの平滑化（= EMA with alpha=1/period, 数学的に同一）でTR/+DM/-DMを平滑化
  3. +DI = 100 * smoothed(+DM) / smoothed(TR)、-DIも同様
  4. DX = 100 * |+DI - -DI| / (+DI + -DI)
  5. ADXはDXをさらにWilder平滑化したもの

慣習的な解釈: ADX<20は「トレンドなし」、20〜25は「トレンド形成中」、
25超は「トレンドあり」、40超は「強いトレンド」とされる（日足を前提にした目安。
1分足でも同じ目安が使えるかは実測して確認する）。

既存の macd_engine.py/kdj_engine.py と同様、この指標も macd_trader本体
（bar_signals.py等）には一切組み込んでいない。研究専用。
"""
from __future__ import annotations

import numpy as np


def _wilder_smooth(values: np.ndarray, period: int) -> np.ndarray:
    """Wilderの平滑化。EMA(alpha=1/period)と数学的に同一
    （Smoothed[t] = ((period-1)*Smoothed[t-1] + values[t]) / period ）。"""
    alpha = 1.0 / period
    n = len(values)
    out = np.empty(n, dtype=np.float64)
    out[0] = values[0]
    prev = values[0]
    for i in range(1, n):
        prev = alpha * values[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def compute_adx_series(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> np.ndarray:
    """ADXの全系列を返す（先頭 period*2 本程度はウォームアップで信頼性が低い）。"""
    n = len(closes)
    if n < 2:
        return np.zeros(n)

    up_move = highs[1:] - highs[:-1]
    down_move = lows[:-1] - lows[1:]

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    prev_close = closes[:-1]
    tr1 = highs[1:] - lows[1:]
    tr2 = np.abs(highs[1:] - prev_close)
    tr3 = np.abs(lows[1:] - prev_close)
    tr = np.maximum(tr1, np.maximum(tr2, tr3))

    # 先頭1本分ズレる（差分を取ったため）。先頭にTR=high-lowの初期値を1つ足して長さを揃える
    tr = np.concatenate([[highs[0] - lows[0]], tr])
    plus_dm = np.concatenate([[0.0], plus_dm])
    minus_dm = np.concatenate([[0.0], minus_dm])

    smoothed_tr = _wilder_smooth(tr, period)
    smoothed_plus_dm = _wilder_smooth(plus_dm, period)
    smoothed_minus_dm = _wilder_smooth(minus_dm, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = np.where(smoothed_tr > 0, 100 * smoothed_plus_dm / smoothed_tr, 0.0)
        minus_di = np.where(smoothed_tr > 0, 100 * smoothed_minus_dm / smoothed_tr, 0.0)
        di_sum = plus_di + minus_di
        dx = np.where(di_sum > 0, 100 * np.abs(plus_di - minus_di) / di_sum, 0.0)

    adx = _wilder_smooth(dx, period)
    return adx


def fast_adx_latest(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    """ウィンドウの最終バーのADX値を返す。"""
    adx = compute_adx_series(highs, lows, closes, period)
    return float(adx[-1])
