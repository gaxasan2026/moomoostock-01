"""
regime_gate.py
レンジ相場判定ロジックの候補を検証するためのプロトタイプ集。

候補①: ダマシ（whipsaw）頻度カウンタ（gated_replay）
  直近N日間のGC/DC発生回数が閾値を超えたら、レンジ相場とみなして新規エントリーを止める。
候補②: MACDヒストグラム平均振幅（gated_replay_histogram）
  直近N日間の|ヒストグラム|の平均が閾値未満なら、トレンド不在（レンジ相場）とみなして
  新規エントリーを止める。既存の macd_histogram_min は単発のクロス時点の大きさを見る
  フィルタだが、これは「ここ数日ずっと弱い」という時系列的な弱さを見る点が異なる。
候補③: ADX（gated_replay_adx）
  直近200本ウィンドウから算出したADX（adx_indicator.py）が閾値未満なら、
  トレンド不在（レンジ相場）とみなして新規エントリーを止める。MACD/KDJとは
  別系統の、トレンド強度専用の指標を使う点が候補①②と異なる。
候補④: KDJ J値の振動頻度（gated_replay_kdj_oscillation）
  直近N日間で、J値が極端域（<10 または >90）に突入した回数が閾値を超えたら、
  レンジ相場とみなして新規エントリーを止める。「突入」は連続して極端域にいる間は
  1回と数え、極端域を出てから再度入った時だけ次の1回として数える
  （同じ振動の中で二重カウントしないため）。

あくまで研究用のプロトタイプであり、macd_trader本体（fast_replay.py含む）には
一切組み込んでいない。

重要: 過去のデータだけを使うローリング（因果的）判定にしている。判定時点で
未来のデータは使わない — 判定対象のバーより前の情報だけを見る。

いずれもエントリーのみを止め、既存ポジションのエグジット判定は通常通り行う
（②の検討時に決めたスコープ）。
"""
from __future__ import annotations

import sys
from collections import deque
from datetime import datetime, time as dt_time
from pathlib import Path

import numpy as np
import pandas as pd

MACD_TRADER_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/macd_trader")
sys.path.insert(0, str(MACD_TRADER_DIR))

from macd_engine import CrossSignal  # noqa: E402
from fast_indicators import fast_macd_latest, fast_kdj_latest  # noqa: E402
from adx_indicator import fast_adx_latest  # noqa: E402
from bar_signals import BarSignals, decide_trade  # noqa: E402
from signal_tracker import SignalTracker  # noqa: E402
from trade_engine import TradeEngine  # noqa: E402
from risk_manager import RiskManager  # noqa: E402

KLINE_WINDOW = 200


def gated_replay(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
                  whipsaw_window_days: float | None = None, whipsaw_threshold: int | None = None,
                  hours_filter: tuple[dt_time, dt_time] | None = None,
                  on_entry=None, on_exit=None, on_gate=None) -> tuple[int, float]:
    """fast_replay.py と同じ判定ロジックに、ダマシ頻度によるエントリー抑制ゲートを
    追加したもの。whipsaw_window_days か whipsaw_threshold が None ならゲート無効
    （= fast_replay と同一結果になるはずのフォールバック挙動）。
    on_gate(bar_time, whipsaw_count) はエントリーがブロックされた瞬間に呼ばれる。"""
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
    gate_active = whipsaw_window_days is not None and whipsaw_threshold is not None
    cross_history = deque()  # (bar_time, cross_type) — GOLDEN_CROSS/DEAD_CROSSのみ保持

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

        # ── ダマシ頻度の記録（過去のクロスだけを見る、未来は使わない） ──
        if macd_vals.cross in (CrossSignal.GOLDEN_CROSS, CrossSignal.DEAD_CROSS):
            cross_history.append(bar_time)
        whipsaw_count = 0
        if gate_active:
            cutoff = bar_time - pd.Timedelta(days=whipsaw_window_days)
            while cross_history and cross_history[0] < cutoff:
                cross_history.popleft()
            whipsaw_count = len(cross_history)

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

        # ── ゲート: エントリーのみ抑制。エグジットには影響しない ──
        if action == "buy" and gate_active and whipsaw_count > whipsaw_threshold:
            if on_gate:
                on_gate(bar_time, whipsaw_count)
            action, reason = None, f"レンジ判定でエントリー抑制（直近{whipsaw_window_days}日で{whipsaw_count}回クロス）"

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


def gated_replay_histogram(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
                            hist_window_days: float | None = None, hist_min_avg_pct: float | None = None,
                            hours_filter: tuple[dt_time, dt_time] | None = None,
                            on_entry=None, on_exit=None, on_gate=None) -> tuple[int, float]:
    """候補②: MACDヒストグラム平均振幅ゲート。直近hist_window_days日間の
    |histogram|を価格に対する比率(%)で平均し、hist_min_avg_pct未満なら
    「トレンド不在」とみなして新規エントリーを止める。
    hist_window_days か hist_min_avg_pct が None ならゲート無効（fast_replayと同一結果）。"""
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
    gate_active = hist_window_days is not None and hist_min_avg_pct is not None
    hist_history = deque()  # (bar_time, |histogram|/price*100)
    hist_sum = 0.0

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

        # ── ヒストグラム平均振幅の記録（過去のバーだけを見る、未来は使わない） ──
        hist_pct = abs(macd_vals.histogram) / current_price * 100 if current_price > 0 else 0.0
        hist_history.append((bar_time, hist_pct))
        hist_sum += hist_pct
        avg_hist_pct = 0.0
        if gate_active:
            cutoff = bar_time - pd.Timedelta(days=hist_window_days)
            while hist_history and hist_history[0][0] < cutoff:
                hist_sum -= hist_history.popleft()[1]
            avg_hist_pct = hist_sum / len(hist_history) if hist_history else 0.0

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

        # ── ゲート: エントリーのみ抑制。エグジットには影響しない ──
        if action == "buy" and gate_active and avg_hist_pct < hist_min_avg_pct:
            if on_gate:
                on_gate(bar_time, avg_hist_pct)
            action, reason = None, f"レンジ判定でエントリー抑制（直近{hist_window_days}日の平均|hist|={avg_hist_pct:.4f}%）"

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


def gated_replay_adx(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
                      adx_min: float | None = None, adx_period: int = 14,
                      hours_filter: tuple[dt_time, dt_time] | None = None,
                      on_entry=None, on_exit=None, on_gate=None) -> tuple[int, float]:
    """候補③: ADXゲート。直近200本ウィンドウから算出したADXがadx_min未満なら
    「トレンド不在」とみなして新規エントリーを止める。
    adx_min が None ならゲート無効（fast_replayと同一結果）。"""
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
    gate_active = adx_min is not None

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

        adx_val = 0.0
        if gate_active:
            adx_val = fast_adx_latest(window_highs, window_lows, window_closes, period=adx_period)

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

        # ── ゲート: エントリーのみ抑制。エグジットには影響しない ──
        if action == "buy" and gate_active and adx_val < adx_min:
            if on_gate:
                on_gate(bar_time, adx_val)
            action, reason = None, f"レンジ判定でエントリー抑制（ADX={adx_val:.1f} < {adx_min}）"

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


def gated_replay_kdj_oscillation(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
                                  osc_window_days: float | None = None, osc_threshold: int | None = None,
                                  j_low: float = 10.0, j_high: float = 90.0,
                                  hours_filter: tuple[dt_time, dt_time] | None = None,
                                  on_entry=None, on_exit=None, on_gate=None) -> tuple[int, float]:
    """候補④: KDJ J値振動頻度ゲート。直近osc_window_days日間で、J値が極端域
    （j_low未満 または j_high超）に「新規突入」した回数がosc_thresholdを超えたら、
    レンジ相場とみなして新規エントリーを止める。連続して極端域にいる間は1回と数え、
    一度極端域を出てから再度入った時だけ次の1回として数える。
    osc_window_days か osc_threshold が None ならゲート無効（fast_replayと同一結果）。

    注: このゲートはJ値の振動そのものを見るため、entry_cfg.kdj_max_dの設定に
    関わらずKDJを常に計算する（既存のkdj_max_dフィルタとは別の仕組みのため）。"""
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
    gate_active = osc_window_days is not None and osc_threshold is not None
    entry_history = deque()  # (bar_time,) — 極端域への「新規突入」タイミングのみ
    was_extreme = False

    n = len(df)
    for i in range(KLINE_WINDOW, n + 1):
        lo = i - KLINE_WINDOW
        window_closes = closes[lo:i]
        window_highs = highs[lo:i]
        window_lows = lows[lo:i]

        macd_vals = fast_macd_latest(window_closes, macd_cfg.fast_period, macd_cfg.slow_period, macd_cfg.signal_period)
        kdj_vals_for_entry = fast_kdj_latest(window_highs, window_lows, window_closes) if entry_cfg.kdj_max_d > 0 else None
        kdj_vals_for_gate = kdj_vals_for_entry if kdj_vals_for_entry is not None else fast_kdj_latest(window_highs, window_lows, window_closes)

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

        # ── J値の極端域への新規突入を記録（過去のバーだけを見る、未来は使わない） ──
        is_extreme = kdj_vals_for_gate.j < j_low or kdj_vals_for_gate.j > j_high
        if is_extreme and not was_extreme:
            entry_history.append(bar_time)
        was_extreme = is_extreme

        osc_count = 0
        if gate_active:
            cutoff = bar_time - pd.Timedelta(days=osc_window_days)
            while entry_history and entry_history[0] < cutoff:
                entry_history.popleft()
            osc_count = len(entry_history)

        signals = BarSignals(
            macd_vals=macd_vals, kdj_vals=kdj_vals_for_entry, volume_ratio=volume_ratio,
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

        # ── ゲート: エントリーのみ抑制。エグジットには影響しない ──
        if action == "buy" and gate_active and osc_count > osc_threshold:
            if on_gate:
                on_gate(bar_time, osc_count)
            action, reason = None, f"レンジ判定でエントリー抑制（直近{osc_window_days}日でJ値極端域突入{osc_count}回）"

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
