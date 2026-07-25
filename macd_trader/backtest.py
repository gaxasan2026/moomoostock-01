"""
backtest.py
過去データを使い、実運用と同じロジック（macd_engine / trade_engine / signal_tracker）で
シミュレーション取引を再生する。パラメータの有用性を、実際に運用する前に検証するためのツール。

使い方:
    python3 backtest.py US.MU 2026-07-01 2026-07-24
    python3 backtest.py US.MU 2026-07-01 2026-07-24 09:30-10:00   # 指定時間帯のみエントリー対象にする

出力:
    logs/backtest_<SYMBOL>_<開始日>_<終了日>[_<時刻>].csv （trade_logger.py と同じCSV形式）

注意:
    ライブ運用では OpenD から5秒ごとにポーリングし、確定していない当該バーの
    リアルタイム価格でもピーク追跡・エグジット判定を行っている。
    過去データ（確定済みのOHLC足）にはバー内の値動きが残っていないため、
    このバックテストは「バー確定時点」でのみ判定を行う近似になる
    （ピーク下落系のエグジットタイミングがライブと数分ずれる可能性がある）。
"""
from __future__ import annotations

import sys
from datetime import time as dt_time
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from config_loader import MacdConfig, EntryConfig, ExitConfig, OrderConfig, RiskConfig, OpendConfig
from macd_engine import MacdEngine
from signal_tracker import SignalTracker
from trade_engine import TradeEngine
from risk_manager import RiskManager
from trade_logger import TradeLogger
from history_loader import load_or_fetch_history
from symbol_store import SymbolStore

KLINE_WINDOW = 200  # ライブ運用と同じロールング窓サイズ（get_kline_data(kline_num=200)に合わせる）


def load_symbol_config(symbol_id: str) -> dict:
    store = SymbolStore(str(BASE_DIR / "data" / "symbols.json"))
    cfg = store.get(symbol_id)
    if not cfg:
        raise SystemExit(f"銘柄が見つかりません: {symbol_id}（Web GUIまたはdata/symbols.jsonに登録してください）")
    return cfg


def parse_hours_arg(arg: str) -> tuple[dt_time, dt_time]:
    """'HH:MM-HH:MM' を (開始時刻, 終了時刻) に変換する"""
    start_str, end_str = arg.split("-", 1)
    start_t = dt_time.fromisoformat(start_str)
    end_t = dt_time.fromisoformat(end_str)
    return start_t, end_t


def run_backtest(symbol_id: str, start_date: str, end_date: str,
                  hours_filter: tuple[dt_time, dt_time] | None = None):
    cfg_dict = load_symbol_config(symbol_id)
    macd_cfg = MacdConfig(**cfg_dict["macd"])
    entry_cfg = EntryConfig(**cfg_dict["entry"])
    exit_cfg = ExitConfig(**cfg_dict["exit"])
    order_cfg = OrderConfig(**cfg_dict["order"])
    risk_cfg = RiskConfig(**cfg_dict["risk"])
    opend_cfg = OpendConfig(**cfg_dict["opend"])

    df = load_or_fetch_history(
        symbol_id, start_date, end_date,
        timeframe=macd_cfg.timeframe, host=opend_cfg.host, port=opend_cfg.port,
    )
    df["time_key"] = pd.to_datetime(df["time_key"])
    df = df.sort_values("time_key").reset_index(drop=True)

    if len(df) < KLINE_WINDOW:
        raise SystemExit(f"データが不足しています（{len(df)}本、最低{KLINE_WINDOW}本必要）。期間を広げてください。")

    macd_engine = MacdEngine(macd_cfg.fast_period, macd_cfg.slow_period, macd_cfg.signal_period)
    tracker = SignalTracker(peak_confirmation_bars=exit_cfg.peak_confirmation_bars)
    risk_mgr = RiskManager(risk_cfg)
    trade_engine = TradeEngine(entry_cfg, exit_cfg, risk_mgr)

    safe = symbol_id.replace(".", "_")
    hours_suffix = ""
    if hours_filter is not None:
        hours_suffix = f"_{hours_filter[0].strftime('%H%M')}-{hours_filter[1].strftime('%H%M')}"
    out_path = BASE_DIR / "logs" / f"backtest_{safe}_{start_date}_{end_date}{hours_suffix}.csv"
    out_path.unlink(missing_ok=True)  # バックテストは決定論的な再生のため、毎回上書きする（追記しない）
    trade_log = TradeLogger(str(out_path), enabled=True)

    closed_trades = 0
    total_pnl = 0.0  # tracker.daily_realized_pnl はセッション境界でリセットされるため、
                     # 期間全体の合計はここで別途積算する

    for i in range(KLINE_WINDOW, len(df) + 1):
        window = df.iloc[i - KLINE_WINDOW:i]
        window = macd_engine.calculate(window)
        macd_vals = macd_engine.get_latest(window)

        current_price = float(window["close"].iloc[-1])
        bar_time = window["time_key"].iloc[-1]
        if hasattr(bar_time, "to_pydatetime"):
            bar_time = bar_time.to_pydatetime()

        volume_ratio = 1.0
        if len(window) >= 20 and "volume" in window.columns:
            avg = window["volume"].iloc[-21:-1].mean()
            curr = float(window["volume"].iloc[-1])
            if avg > 0:
                volume_ratio = curr / avg

        tracker.update(macd=macd_vals.macd, signal=macd_vals.signal,
                        current_price=current_price, timestamp=bar_time)

        if tracker.has_position:
            sell, reason = trade_engine.should_sell(tracker, macd_vals)
            if sell:
                entry = tracker.position.entry_price
                hold = round(tracker.hold_minutes, 1)
                total_pnl += (current_price - entry) * order_cfg.quantity
                trade_log.log_exit(symbol_id, current_price, order_cfg.quantity,
                                    entry, hold, reason, tracker.daily_trades + 1, bar_time)
                tracker.close_position(current_price, reason)
                closed_trades += 1
        else:
            in_window = (
                hours_filter is None
                or hours_filter[0] <= bar_time.time() < hours_filter[1]
            )
            if in_window:
                buy, reason = trade_engine.should_buy(tracker, macd_vals, volume_ratio)
                if buy:
                    trade_log.log_entry(symbol_id, current_price, order_cfg.quantity,
                                         tracker.gc_duration_minutes, bar_time)
                    tracker.open_position(current_price, order_cfg.quantity, bar_time)

    print(f"\nバックテスト完了: {symbol_id} {start_date} 〜 {end_date}")
    print(f"確定取引数（SELL）: {closed_trades}件")
    print(f"合計損益: {total_pnl:+.2f} USD")
    print(f"出力ファイル: {out_path}")


def main():
    if len(sys.argv) < 4:
        print("使い方: python3 backtest.py <SYMBOL> <開始日> <終了日> [開始時刻-終了時刻]")
        print("例:     python3 backtest.py US.MU 2026-07-01 2026-07-24")
        print("例:     python3 backtest.py US.MU 2026-07-01 2026-07-24 09:30-10:00")
        sys.exit(1)
    symbol_id = sys.argv[1].upper()
    start_date, end_date = sys.argv[2], sys.argv[3]
    hours_filter = parse_hours_arg(sys.argv[4]) if len(sys.argv) > 4 else None
    run_backtest(symbol_id, start_date, end_date, hours_filter)


if __name__ == "__main__":
    main()
