"""
backtest.py
過去データを使い、実運用と同じロジック（macd_engine / trade_engine / signal_tracker）で
シミュレーション取引を再生する。パラメータの有用性を、実際に運用する前に検証するためのツール。

使い方:
    python3 backtest.py US.MU 2026-07-01 2026-07-24
    python3 backtest.py US.MU 2026-07-01 2026-07-24 09:30-10:00   # 指定時間帯のみエントリー対象にする
    python3 backtest.py US.MU 2026-07-01 2026-07-24 --sweep gc_duration_minutes 3,5,7,10,15
        # entryの1パラメータを複数の値で試し、結果を一覧表示する（symbols.jsonは変更しない）

出力:
    logs/backtest_<SYMBOL>_<開始日>_<終了日>[_<時刻>].csv （trade_logger.py と同じCSV形式）
    --sweep 使用時はCSV出力なし（コンソールに一覧のみ表示）

注意:
    ライブ運用では OpenD から5秒ごとにポーリングし、確定していない当該バーの
    リアルタイム価格でもピーク追跡・エグジット判定を行っている。
    過去データ（確定済みのOHLC足）にはバー内の値動きが残っていないため、
    このバックテストは「バー確定時点」でのみ判定を行う近似になる
    （ピーク下落系のエグジットタイミングがライブと数分ずれる可能性がある）。
"""
from __future__ import annotations

import dataclasses
import sys
from datetime import time as dt_time
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from config_loader import MacdConfig, EntryConfig, ExitConfig, OrderConfig, RiskConfig, OpendConfig
from macd_engine import MacdEngine
from kdj_engine import KdjEngine
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


def _load_data(symbol_id: str, start_date: str, end_date: str,
               macd_cfg: MacdConfig, opend_cfg: OpendConfig) -> pd.DataFrame:
    df = load_or_fetch_history(
        symbol_id, start_date, end_date,
        timeframe=macd_cfg.timeframe, host=opend_cfg.host, port=opend_cfg.port,
    )
    df["time_key"] = pd.to_datetime(df["time_key"])
    df = df.sort_values("time_key").reset_index(drop=True)
    if len(df) < KLINE_WINDOW:
        raise SystemExit(f"データが不足しています（{len(df)}本、最低{KLINE_WINDOW}本必要）。期間を広げてください。")
    return df


def _replay(df: pd.DataFrame, macd_cfg: MacdConfig, entry_cfg: EntryConfig,
            exit_cfg: ExitConfig, order_cfg: OrderConfig, risk_cfg: RiskConfig,
            hours_filter: tuple[dt_time, dt_time] | None = None,
            on_entry=None, on_exit=None) -> tuple[int, float]:
    """
    コア再生ループ。on_entry(price, qty, gc_duration, bar_time) /
    on_exit(price, qty, entry_price, hold_minutes, reason, daily_trades, bar_time, pnl)
    のコールバックで、CSV出力（run_backtest）か集計のみ（run_sweep）かを呼び出し側が選べる。
    Returns: (確定取引数, 合計損益)
    """
    macd_engine = MacdEngine(macd_cfg.fast_period, macd_cfg.slow_period, macd_cfg.signal_period)
    kdj_engine = KdjEngine()
    tracker = SignalTracker(peak_confirmation_bars=exit_cfg.peak_confirmation_bars)
    risk_mgr = RiskManager(risk_cfg)
    trade_engine = TradeEngine(entry_cfg, exit_cfg, risk_mgr)

    closed_trades = 0
    total_pnl = 0.0  # tracker.daily_realized_pnl はセッション境界でリセットされるため、
                     # 期間全体の合計はここで別途積算する

    for i in range(KLINE_WINDOW, len(df) + 1):
        window = df.iloc[i - KLINE_WINDOW:i]
        window = macd_engine.calculate(window)
        macd_vals = macd_engine.get_latest(window)
        kdj_vals = None
        if entry_cfg.kdj_max_d > 0:
            kdj_window = kdj_engine.calculate(window)
            kdj_vals = kdj_engine.get_latest(kdj_window)

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
                qty = tracker.position.quantity  # BUY時の株数をそのまま使う
                entry = tracker.position.entry_price
                hold = round(tracker.hold_minutes, 1)
                pnl = (current_price - entry) * qty
                total_pnl += pnl
                if on_exit:
                    on_exit(current_price, qty, entry, hold, reason,
                            tracker.daily_trades + 1, bar_time, pnl)
                tracker.close_position(current_price, reason)
                closed_trades += 1
        else:
            in_window = (
                hours_filter is None
                or hours_filter[0] <= bar_time.time() < hours_filter[1]
            )
            if in_window:
                buy, reason = trade_engine.should_buy(tracker, macd_vals, volume_ratio, kdj_vals)
                if buy:
                    qty = risk_mgr.compute_quantity(current_price, order_cfg.quantity)
                    if on_entry:
                        on_entry(current_price, qty, tracker.gc_duration_minutes, bar_time)
                    tracker.open_position(current_price, qty, bar_time)

    return closed_trades, total_pnl


def _hours_suffix(hours_filter: tuple[dt_time, dt_time] | None, entry_cfg: EntryConfig) -> str:
    if hours_filter is not None:
        return f"_{hours_filter[0].strftime('%H%M')}-{hours_filter[1].strftime('%H%M')}"
    if entry_cfg.trading_hours_start and entry_cfg.trading_hours_end:
        # symbols.json側のtrading_hours設定が有効な場合も、無制限時のファイルと
        # 混同しないよう出力ファイル名にサフィックスを付ける
        s = entry_cfg.trading_hours_start.replace(":", "")
        e = entry_cfg.trading_hours_end.replace(":", "")
        return f"_cfg{s}-{e}"
    return ""


def run_backtest(symbol_id: str, start_date: str, end_date: str,
                  hours_filter: tuple[dt_time, dt_time] | None = None):
    cfg_dict = load_symbol_config(symbol_id)
    macd_cfg = MacdConfig(**cfg_dict["macd"])
    entry_cfg = EntryConfig(**cfg_dict["entry"])
    exit_cfg = ExitConfig(**cfg_dict["exit"])
    order_cfg = OrderConfig(**cfg_dict["order"])
    risk_cfg = RiskConfig(**cfg_dict["risk"])
    opend_cfg = OpendConfig(**cfg_dict["opend"])

    df = _load_data(symbol_id, start_date, end_date, macd_cfg, opend_cfg)

    safe = symbol_id.replace(".", "_")
    hours_suffix = _hours_suffix(hours_filter, entry_cfg)
    out_path = BASE_DIR / "logs" / f"backtest_{safe}_{start_date}_{end_date}{hours_suffix}.csv"
    out_path.unlink(missing_ok=True)  # バックテストは決定論的な再生のため、毎回上書きする（追記しない）
    trade_log = TradeLogger(str(out_path), enabled=True)

    def on_entry(price, qty, gc_dur, bar_time):
        trade_log.log_entry(symbol_id, price, qty, gc_dur, bar_time)

    def on_exit(price, qty, entry, hold, reason, daily_trades, bar_time, pnl):
        trade_log.log_exit(symbol_id, price, qty, entry, hold, reason, daily_trades, bar_time)

    closed_trades, total_pnl = _replay(
        df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
        hours_filter=hours_filter, on_entry=on_entry, on_exit=on_exit,
    )

    print(f"\nバックテスト完了: {symbol_id} {start_date} 〜 {end_date}")
    print(f"確定取引数（SELL）: {closed_trades}件")
    print(f"合計損益: {total_pnl:+.2f} USD")
    print(f"出力ファイル: {out_path}")


def run_sweep(symbol_id: str, start_date: str, end_date: str,
              param_name: str, values: list,
              hours_filter: tuple[dt_time, dt_time] | None = None):
    """
    entryの1パラメータを複数の値で試し、結果を一覧表示する。
    symbols.jsonは一切変更しない（メモリ上でEntryConfigを複製して差し替えるだけ）。
    """
    cfg_dict = load_symbol_config(symbol_id)
    macd_cfg = MacdConfig(**cfg_dict["macd"])
    base_entry_cfg = EntryConfig(**cfg_dict["entry"])
    exit_cfg = ExitConfig(**cfg_dict["exit"])
    order_cfg = OrderConfig(**cfg_dict["order"])
    risk_cfg = RiskConfig(**cfg_dict["risk"])
    opend_cfg = OpendConfig(**cfg_dict["opend"])

    if not hasattr(base_entry_cfg, param_name):
        raise SystemExit(f"エントリー設定に存在しないパラメータです: {param_name}")

    df = _load_data(symbol_id, start_date, end_date, macd_cfg, opend_cfg)

    # 型はdataclassの型注釈から取得する（JSON上は0.0が整数0として読まれている場合があり、
    # 現在値の型では正しく判定できないため）
    field_type = next(f.type for f in dataclasses.fields(base_entry_cfg) if f.name == param_name)

    print(f"\n=== パラメータスイープ: {symbol_id} / {param_name} ===")
    print(f"期間: {start_date} 〜 {end_date}" + (f" / 時間帯: {hours_filter[0]}-{hours_filter[1]}" if hours_filter else ""))
    print(f"{'値':>10} {'取引数':>8} {'合計損益(USD)':>14}")
    for raw_value in values:
        value = raw_value if field_type is bool else field_type(raw_value)
        entry_cfg = dataclasses.replace(base_entry_cfg, **{param_name: value})
        closed_trades, total_pnl = _replay(
            df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg, hours_filter=hours_filter,
        )
        print(f"{value!s:>10} {closed_trades:>8} {total_pnl:>+14.2f}")


def main():
    if len(sys.argv) < 4:
        print("使い方: python3 backtest.py <SYMBOL> <開始日> <終了日> [開始時刻-終了時刻]")
        print("        python3 backtest.py <SYMBOL> <開始日> <終了日> --sweep <パラメータ名> <値1,値2,...> [開始時刻-終了時刻]")
        print("例:     python3 backtest.py US.MU 2026-07-01 2026-07-24")
        print("例:     python3 backtest.py US.MU 2026-07-01 2026-07-24 09:30-10:00")
        print("例:     python3 backtest.py US.MU 2026-07-01 2026-07-24 --sweep gc_duration_minutes 3,5,7,10,15")
        sys.exit(1)
    symbol_id = sys.argv[1].upper()
    start_date, end_date = sys.argv[2], sys.argv[3]

    if len(sys.argv) > 4 and sys.argv[4] == "--sweep":
        if len(sys.argv) < 7:
            print("使い方: python3 backtest.py <SYMBOL> <開始日> <終了日> --sweep <パラメータ名> <値1,値2,...> [開始時刻-終了時刻]")
            sys.exit(1)
        param_name = sys.argv[5]
        values = sys.argv[6].split(",")
        hours_filter = parse_hours_arg(sys.argv[7]) if len(sys.argv) > 7 else None
        run_sweep(symbol_id, start_date, end_date, param_name, values, hours_filter)
    else:
        hours_filter = parse_hours_arg(sys.argv[4]) if len(sys.argv) > 4 else None
        run_backtest(symbol_id, start_date, end_date, hours_filter)


if __name__ == "__main__":
    main()
