"""
fast_replay.py
backtest.py / Backtest Studio 専用の高速リプレイ。ライブ取引（bot_manager.py）は
一切これをimportしない。

重要: 売買判定ロジックそのもの（SignalTracker / TradeEngine / RiskManager /
decide_trade）は本体（bar_signals.py等）からそのままimportして使う。複製しない。
高速化するのは「1バーごとにMACD/KDJを計算する」部分（fast_indicators.py）だけ。

_replay()（backtest.pyの原実装）とロジック上の違いは無いことを意図している。
唯一の違いは指標計算の実装（pandas→numpy）。数値・取引単位での一致は
research/verify_fast_indicators.py・research/verify_fast_replay.py で検証済み
（誤差1e-13〜1e-14、複数シナリオで取引結果が完全一致）。
"""
from __future__ import annotations

from datetime import datetime, time as dt_time

import numpy as np
import pandas as pd

from bar_signals import BarSignals, decide_trade
from signal_tracker import SignalTracker
from trade_engine import TradeEngine
from risk_manager import RiskManager
from fast_indicators import fast_macd_latest, fast_kdj_latest

KLINE_WINDOW = 200


def fast_replay(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
                 hours_filter: tuple[dt_time, dt_time] | None = None,
                 on_entry=None, on_exit=None) -> tuple[int, float]:
    """backtest.py の _replay() と同じ入出力・同じ判定ロジック。
    指標計算だけ fast_indicators.py の高速版に置き換えている。"""
    tracker = SignalTracker(peak_confirmation_bars=exit_cfg.peak_confirmation_bars)
    risk_mgr = RiskManager(risk_cfg)
    trade_engine = TradeEngine(entry_cfg, exit_cfg, risk_mgr)

    closes = df["close"].to_numpy(dtype=np.float64)
    highs = df["high"].to_numpy(dtype=np.float64)
    lows = df["low"].to_numpy(dtype=np.float64)
    volumes = df["volume"].to_numpy(dtype=np.float64) if "volume" in df.columns else None
    time_keys = df["time_key"].to_numpy()

    closed_trades = 0
    total_pnl = 0.0

    n = len(df)
    for i in range(KLINE_WINDOW, n + 1):
        lo = i - KLINE_WINDOW
        window_closes = closes[lo:i]
        window_highs = highs[lo:i]
        window_lows = lows[lo:i]

        macd_vals = fast_macd_latest(window_closes, macd_cfg.fast_period, macd_cfg.slow_period, macd_cfg.signal_period)

        kdj_vals = None
        if entry_cfg.kdj_max_d > 0:
            kdj_vals = fast_kdj_latest(window_highs, window_lows, window_closes)

        current_price = float(window_closes[-1])
        bar_time = pd.Timestamp(time_keys[i - 1]).to_pydatetime()
        if not isinstance(bar_time, datetime):
            bar_time = datetime.now()

        volume_ratio = 1.0
        if volumes is not None and KLINE_WINDOW >= 20:
            window_vol = volumes[lo:i]
            avg = window_vol[-21:-1].mean()
            curr = float(window_vol[-1])
            if avg > 0:
                volume_ratio = curr / avg

        tracker.update(macd=macd_vals.macd, signal=macd_vals.signal,
                        current_price=current_price, timestamp=bar_time)

        signals = BarSignals(
            macd_vals=macd_vals, kdj_vals=kdj_vals, volume_ratio=volume_ratio,
            current_price=current_price, bar_time=bar_time,
        )

        in_window = (
            hours_filter is None
            or hours_filter[0] <= signals.bar_time.time() < hours_filter[1]
        )
        if tracker.has_position or in_window:
            action, reason = decide_trade(tracker, trade_engine, signals)
        else:
            action, reason = None, "時間帯外"

        if action == "sell":
            qty = tracker.position.quantity
            entry = tracker.position.entry_price
            hold = round(tracker.hold_minutes, 1)
            pnl = (signals.current_price - entry) * qty
            total_pnl += pnl
            if on_exit:
                on_exit(signals.current_price, qty, entry, hold, reason,
                        tracker.daily_trades + 1, signals.bar_time, pnl)
            tracker.close_position(signals.current_price, reason)
            closed_trades += 1
        elif action == "buy":
            qty = risk_mgr.compute_quantity(signals.current_price, order_cfg.quantity)
            if on_entry:
                on_entry(signals.current_price, qty, tracker.gc_duration_minutes, signals.bar_time)
            tracker.open_position(signals.current_price, qty, signals.bar_time)

    return closed_trades, total_pnl
