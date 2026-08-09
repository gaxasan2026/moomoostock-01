"""
swing_backtest.py
スイングトレード用パラメータ（swing_config.SWING_DEFAULT_CONFIG）を、
過去データで検証するためのCLIツール。macd_trader/backtest.py の _replay() と
同じ「直近200本ウィンドウ・ルックアヘッド無し」の再生ループを使うが、
トラッカーだけ SwingSignalTracker に差し替える。

K_DAY/K_60Mの本数はK_1Mの月次ウォークフォワードよりずっと少なく、
fast_replay.py のような高速化は不要と判断し、macd_trader/backtest.py の
_replay() 相当のシンプルな実装のみ用意する（macd_trader側のコードは一切変更しない）。

使い方:
    python3 swing_backtest.py US.MU 2023-01-01 2026-07-01
    python3 swing_backtest.py US.MU 2023-01-01 2026-07-01 --timeframe K_60M
    python3 swing_backtest.py US.MU 2023-01-01 2026-07-01 --sweep gc_duration_minutes 1440,2880,4320
        # entryの1パラメータを複数の値で試し、結果を一覧表示する（SWING_DEFAULT_CONFIGは変更しない）
    python3 swing_backtest.py US.MU 2023-01-01 2026-07-01 --grid gc_duration_minutes=1440,2880 kdj_max_d=0,80
        # entryの複数パラメータを総当たりで組み合わせ、合計損益の降順で一覧表示する
        # （--sweep/--gridとも対象はエントリー設定[EntryConfig]のみ。損切り/利確等のエグジット設定は対象外）

出力:
    logs/swing_backtest_<SYMBOL>_<開始日>_<終了日>_<timeframe>.csv
    --sweep / --grid 使用時はCSV出力なし（コンソールに一覧のみ表示、macd_trader/backtest.pyと同じ仕様）
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import itertools
import sys
from pathlib import Path

MACD_TRADER_DIR = Path(__file__).parent.parent / "macd_trader"
sys.path.insert(0, str(MACD_TRADER_DIR))
sys.path.insert(0, str(Path(__file__).parent))

from config_loader import (  # noqa: E402
    MacdConfig, EntryConfig, ExitConfig, OrderConfig, RiskConfig, OpendConfig, OscillatorConfig,
)
from engine_factory import build_trend_engine, build_oscillator_engine  # noqa: E402
from bar_signals import compute_bar_signals, decide_trade  # noqa: E402
from trade_engine import TradeEngine  # noqa: E402
from risk_manager import RiskManager  # noqa: E402
from trade_logger import TradeLogger  # noqa: E402
from backtest import _load_data  # noqa: E402  (read-only再利用。macd_trader側は変更しない)

from swing_config import SWING_DEFAULT_CONFIG  # noqa: E402
from swing_signal_tracker import SwingSignalTracker  # noqa: E402

BASE_DIR = Path(__file__).parent
KLINE_WINDOW = 200  # macd_trader/backtest.pyと同じロールング窓サイズ


def _build_configs(timeframe_override: str | None):
    cfg_dict = copy.deepcopy(SWING_DEFAULT_CONFIG)
    if timeframe_override:
        cfg_dict["macd"]["timeframe"] = timeframe_override
    macd_cfg = MacdConfig(**cfg_dict["macd"])
    entry_cfg = EntryConfig(**cfg_dict["entry"])
    exit_cfg = ExitConfig(**cfg_dict["exit"])
    order_cfg = OrderConfig(**cfg_dict["order"])
    risk_cfg = RiskConfig(**cfg_dict["risk"])
    opend_cfg = OpendConfig(**cfg_dict["opend"])
    osc_cfg = OscillatorConfig(**cfg_dict.get("oscillator", {}))
    return macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg, opend_cfg, osc_cfg


def swing_replay(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
                  osc_cfg: OscillatorConfig | None = None,
                  on_entry=None, on_exit=None) -> tuple[int, float]:
    """
    コア再生ループ（macd_trader/backtest.py の _replay() と同型、
    トラッカーのみ SwingSignalTracker）。engine_factory経由でトレンド/オシレータ
    エンジンを選ぶため、MA_RSI等の組み合わせもそのまま再生できる。
    Returns: (確定取引数, 合計損益)
    """
    macd_engine = build_trend_engine(macd_cfg)
    kdj_engine = build_oscillator_engine(osc_cfg or OscillatorConfig())
    tracker = SwingSignalTracker(peak_confirmation_bars=exit_cfg.peak_confirmation_bars)
    risk_mgr = RiskManager(risk_cfg)
    trade_engine = TradeEngine(entry_cfg, exit_cfg, risk_mgr)

    closed_trades = 0
    total_pnl = 0.0

    for i in range(KLINE_WINDOW, len(df) + 1):
        window = df.iloc[i - KLINE_WINDOW:i]
        signals = compute_bar_signals(window, macd_engine, kdj_engine, tracker, entry_cfg.kdj_max_d)
        action, reason = decide_trade(tracker, trade_engine, signals)

        if action == "sell":
            qty = tracker.position.quantity
            entry = tracker.position.entry_price
            hold = round(tracker.hold_minutes, 1)
            pnl = (signals.current_price - entry) * qty
            total_pnl += pnl
            if on_exit:
                on_exit(signals.current_price, qty, entry, hold, reason,
                        tracker.daily_trades + 1, signals.bar_time, pnl,
                        tracker.gc_duration_minutes)
            tracker.close_position(signals.current_price, reason)
            closed_trades += 1
        elif action == "buy":
            qty = risk_mgr.compute_quantity(signals.current_price, order_cfg.quantity)
            if on_entry:
                on_entry(signals.current_price, qty, tracker.gc_duration_minutes, signals.bar_time)
            tracker.open_position(signals.current_price, qty, signals.bar_time)

    return closed_trades, total_pnl


def run_swing_backtest(symbol_id: str, start_date: str, end_date: str, timeframe: str | None):
    macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg, opend_cfg, osc_cfg = _build_configs(timeframe)

    print(f"設定: timeframe={macd_cfg.timeframe} "
          f"gc_duration_minutes={entry_cfg.gc_duration_minutes} "
          f"take_profit={exit_cfg.take_profit_pct}% stop_loss={exit_cfg.stop_loss_pct}% "
          f"max_hold={exit_cfg.max_hold_minutes}min")

    df = _load_data(symbol_id, start_date, end_date, macd_cfg, opend_cfg)
    print(f"データ: {len(df)}本 ({df['time_key'].iloc[0]} 〜 {df['time_key'].iloc[-1]})")

    safe = symbol_id.replace(".", "_")
    out_path = BASE_DIR / "logs" / f"swing_backtest_{safe}_{start_date}_{end_date}_{macd_cfg.timeframe}.csv"
    out_path.unlink(missing_ok=True)
    trade_log = TradeLogger(str(out_path), enabled=True)

    gc_durations_at_entry = []

    def on_entry(price, qty, gc_dur, bar_time):
        gc_durations_at_entry.append(gc_dur)
        trade_log.log_entry(symbol_id, price, qty, gc_dur, bar_time)

    def on_exit(price, qty, entry, hold, reason, daily_trades, bar_time, pnl, gc_dur_at_exit):
        trade_log.log_exit(symbol_id, price, qty, entry, hold, reason, daily_trades, bar_time)

    closed_trades, total_pnl = swing_replay(
        df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg, osc_cfg=osc_cfg,
        on_entry=on_entry, on_exit=on_exit,
    )

    print(f"\nスイングバックテスト完了: {symbol_id} {start_date} 〜 {end_date}")
    print(f"確定取引数（SELL）: {closed_trades}件")
    print(f"合計損益: {total_pnl:+.2f} USD")
    if gc_durations_at_entry:
        one_day = 24 * 60
        crossing_day = sum(1 for d in gc_durations_at_entry if d >= one_day)
        print(f"エントリー時のGC継続時間: 平均{sum(gc_durations_at_entry)/len(gc_durations_at_entry):.0f}分 "
              f"（1日={one_day}分以上のもの: {crossing_day}/{len(gc_durations_at_entry)}件）")
    print(f"出力ファイル: {out_path}")


def run_sweep(symbol_id: str, start_date: str, end_date: str,
              param_name: str, values: list, timeframe: str | None):
    """
    entryの1パラメータを複数の値で試し、結果を一覧表示する。
    SWING_DEFAULT_CONFIGは一切変更しない（メモリ上でEntryConfigを複製して差し替えるだけ）。
    macd_trader/backtest.py の run_sweep() と同じ実装パターン。
    """
    macd_cfg, base_entry_cfg, exit_cfg, order_cfg, risk_cfg, opend_cfg, osc_cfg = _build_configs(timeframe)

    if not hasattr(base_entry_cfg, param_name):
        raise SystemExit(f"エントリー設定に存在しないパラメータです: {param_name}")

    df = _load_data(symbol_id, start_date, end_date, macd_cfg, opend_cfg)

    # 型はdataclassの型注釈から取得する（CLI引数は常に文字列のため）
    field_type = next(f.type for f in dataclasses.fields(base_entry_cfg) if f.name == param_name)

    print(f"\n=== パラメータスイープ: {symbol_id} / {param_name} (timeframe={macd_cfg.timeframe}) ===")
    print(f"期間: {start_date} 〜 {end_date}")
    print(f"{'値':>10} {'取引数':>8} {'合計損益(USD)':>14}")
    for raw_value in values:
        value = raw_value if field_type is bool else field_type(raw_value)
        entry_cfg = dataclasses.replace(base_entry_cfg, **{param_name: value})
        closed_trades, total_pnl = swing_replay(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg, osc_cfg=osc_cfg)
        print(f"{value!s:>10} {closed_trades:>8} {total_pnl:>+14.2f}")


def parse_grid_args(tokens: list[str]) -> dict[str, list[str]]:
    """['gc_duration_minutes=1440,2880', 'stop_loss_pct=3,5,7'] のような
    'パラメータ名=値1,値2,...' のリストを {パラメータ名: [値, ...]} に変換する"""
    grid_spec: dict[str, list[str]] = {}
    for token in tokens:
        if "=" not in token:
            raise SystemExit(f"--grid の引数は パラメータ名=値1,値2,... の形式で指定してください: {token}")
        name, raw_values = token.split("=", 1)
        grid_spec[name] = raw_values.split(",")
    return grid_spec


def run_grid(symbol_id: str, start_date: str, end_date: str,
             grid_spec: dict[str, list[str]], timeframe: str | None):
    """
    entryの複数パラメータを総当たりで組み合わせて試し、合計損益の降順で一覧表示する。
    SWING_DEFAULT_CONFIGは一切変更しない。macd_trader/backtest.py の run_grid() と同じ実装パターン。
    """
    macd_cfg, base_entry_cfg, exit_cfg, order_cfg, risk_cfg, opend_cfg, osc_cfg = _build_configs(timeframe)

    for name in grid_spec:
        if not hasattr(base_entry_cfg, name):
            raise SystemExit(f"エントリー設定に存在しないパラメータです: {name}")

    df = _load_data(symbol_id, start_date, end_date, macd_cfg, opend_cfg)

    field_types = {f.name: f.type for f in dataclasses.fields(base_entry_cfg)}
    param_names = list(grid_spec.keys())
    value_lists = [
        [raw if field_types[name] is bool else field_types[name](raw) for raw in grid_spec[name]]
        for name in param_names
    ]
    combos = list(itertools.product(*value_lists))

    print(f"\n=== グリッドサーチ: {symbol_id} / {', '.join(param_names)} (timeframe={macd_cfg.timeframe}) ===")
    print(f"期間: {start_date} 〜 {end_date}")
    print(f"組み合わせ数: {len(combos)}件\n")

    results = []
    for combo in combos:
        overrides = dict(zip(param_names, combo))
        entry_cfg = dataclasses.replace(base_entry_cfg, **overrides)
        closed_trades, total_pnl = swing_replay(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg, osc_cfg=osc_cfg)
        results.append((overrides, closed_trades, total_pnl))

    results.sort(key=lambda r: r[2], reverse=True)  # 合計損益の降順（最良の組み合わせが上位に来る）

    col_widths = [max(len(name), 10) + 2 for name in param_names]
    header = "".join(f"{name:>{w}}" for name, w in zip(param_names, col_widths)) + f"{'取引数':>8} {'合計損益(USD)':>14}"
    print(header)
    for overrides, closed_trades, total_pnl in results:
        row = "".join(f"{overrides[name]!s:>{w}}" for name, w in zip(param_names, col_widths))
        print(f"{row}{closed_trades:>8} {total_pnl:>+14.2f}")


def main():
    parser = argparse.ArgumentParser(description="スイングトレード用バックテスト")
    parser.add_argument("symbol")
    parser.add_argument("start_date")
    parser.add_argument("end_date")
    parser.add_argument("--timeframe", default=None, choices=["K_60M", "K_DAY"])
    parser.add_argument("--sweep", nargs=2, metavar=("PARAM", "VALUES"),
                         help="例: --sweep gc_duration_minutes 1440,2880,4320")
    parser.add_argument("--grid", nargs="+", metavar="PARAM=V1,V2,...",
                         help="例: --grid gc_duration_minutes=1440,2880 kdj_max_d=0,80")
    args = parser.parse_args()

    try:
        if args.sweep:
            param_name, raw_values = args.sweep
            run_sweep(args.symbol, args.start_date, args.end_date,
                      param_name, raw_values.split(","), args.timeframe)
        elif args.grid:
            grid_spec = parse_grid_args(args.grid)
            run_grid(args.symbol, args.start_date, args.end_date, grid_spec, args.timeframe)
        else:
            run_swing_backtest(args.symbol, args.start_date, args.end_date, args.timeframe)
    except (Exception, SystemExit) as e:
        print(f"!!! 失敗: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
