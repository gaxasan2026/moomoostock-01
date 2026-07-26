"""
bar_signals.py
「1バー分のウィンドウから指標を計算し、トラッカーを更新し、売買判定を行う」という
ライブ運用（bot_manager.py）とバックテスト（backtest.py）で共通の処理をまとめる。

ライブとバックテストの違い（ポーリング間隔とバー確定タイミングのズレ）はこのモジュールでは
吸収しない。compute_bar_signals() はトラッカー更新（ピーク追跡等）を毎回行い、
decide_trade() は呼び出し側が「今回売買判定してよいか」を判断した上で呼ぶこと。
- ライブ: 5秒ごとに compute_bar_signals() を呼ぶが、decide_trade() は新しいバー確定時のみ呼ぶ
- バックテスト: 1バー=1回の呼び出しなので、両方を毎回呼ぶ
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from macd_engine import MacdEngine, MacdValues
from kdj_engine import KdjEngine, KdjValues
from signal_tracker import SignalTracker
from trade_engine import TradeEngine


@dataclass
class BarSignals:
    macd_vals: MacdValues
    kdj_vals: KdjValues | None
    volume_ratio: float
    current_price: float
    bar_time: datetime


def compute_bar_signals(window, macd_engine: MacdEngine, kdj_engine: KdjEngine,
                         tracker: SignalTracker, kdj_max_d: float) -> BarSignals:
    """
    直近Nバーのウィンドウから MACD・（有効なら）KDJ・出来高比率を計算し、
    tracker.update() でピーク追跡等の状態を更新する。
    """
    df = macd_engine.calculate(window)
    macd_vals = macd_engine.get_latest(df)

    kdj_vals = None
    if kdj_max_d > 0:
        kdj_df = kdj_engine.calculate(window)
        kdj_vals = kdj_engine.get_latest(kdj_df)

    current_price = float(df["close"].iloc[-1])
    bar_time = df["time_key"].iloc[-1]
    if hasattr(bar_time, "to_pydatetime"):
        bar_time = bar_time.to_pydatetime()
    if not isinstance(bar_time, datetime):
        bar_time = datetime.now()  # time_keyが想定外の型だった場合の安全策

    volume_ratio = 1.0
    if len(df) >= 20 and "volume" in df.columns:
        avg = df["volume"].iloc[-21:-1].mean()
        curr = float(df["volume"].iloc[-1])
        if avg > 0:
            volume_ratio = curr / avg

    tracker.update(macd=macd_vals.macd, signal=macd_vals.signal,
                   current_price=current_price, timestamp=bar_time)

    return BarSignals(
        macd_vals=macd_vals, kdj_vals=kdj_vals, volume_ratio=volume_ratio,
        current_price=current_price, bar_time=bar_time,
    )


def decide_trade(tracker: SignalTracker, trade_engine: TradeEngine,
                  signals: BarSignals) -> tuple[str | None, str]:
    """
    ポジション有無に応じて売買判定を1回行う。
    Returns: (action, reason) — action は 'sell' / 'buy' / None
    """
    if tracker.has_position:
        should, reason = trade_engine.should_sell(tracker, signals.macd_vals)
        return ("sell" if should else None), reason
    else:
        should, reason = trade_engine.should_buy(
            tracker, signals.macd_vals, signals.volume_ratio, signals.kdj_vals
        )
        return ("buy" if should else None), reason
