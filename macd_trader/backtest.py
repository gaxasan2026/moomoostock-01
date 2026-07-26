"""
backtest.py
過去データを使い、実運用と同じロジック（macd_engine / trade_engine / signal_tracker）で
シミュレーション取引を再生する。パラメータの有用性を、実際に運用する前に検証するためのツール。

使い方:
    python3 backtest.py US.MU 2026-07-01 2026-07-24
    python3 backtest.py US.MU 2026-07-01 2026-07-24 09:30-10:00   # 指定時間帯のみエントリー対象にする
    python3 backtest.py US.MU 2026-07-01 2026-07-24 --sweep gc_duration_minutes 3,5,7,10,15
        # entryの1パラメータを複数の値で試し、結果を一覧表示する（symbols.jsonは変更しない）
    python3 backtest.py US.MU 2026-07-01 2026-07-24 --grid gc_duration_minutes=3,5,7 macd_histogram_min=0,0.002,0.005
        # entryの複数パラメータを総当たりで組み合わせ、合計損益の降順で一覧表示する（symbols.jsonは変更しない）

出力:
    logs/backtest_<SYMBOL>_<開始日>_<終了日>[_<時刻>].csv （trade_logger.py と同じCSV形式）
    --sweep / --grid 使用時はCSV出力なし（コンソールに一覧のみ表示）

注意（--grid）:
    パラメータ間には逐次依存性（クールダウン・ピーク追跡等）があるため、組み合わせた結果は
    各パラメータを個別に--sweepした結果の単純な足し算にはならない（非線形効果が生じうる）。
    組み合わせ数はパラメータ数・値の数に応じて指数的に増えるため、値の候補は絞り込んでから使うこと。

注意:
    ライブ運用では OpenD から5秒ごとにポーリングし、確定していない当該バーの
    リアルタイム価格でもピーク追跡・エグジット判定を行っている。
    過去データ（確定済みのOHLC足）にはバー内の値動きが残っていないため、
    このバックテストは「バー確定時点」でのみ判定を行う近似になる
    （ピーク下落系のエグジットタイミングがライブと数分ずれる可能性がある）。
"""
from __future__ import annotations

import dataclasses
import itertools
import sys
from datetime import time as dt_time
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from config_loader import MacdConfig, EntryConfig, ExitConfig, OrderConfig, RiskConfig, OpendConfig
from macd_engine import MacdEngine
from kdj_engine import KdjEngine
from bar_signals import compute_bar_signals, decide_trade
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
        signals = compute_bar_signals(window, macd_engine, kdj_engine, tracker, entry_cfg.kdj_max_d)

        in_window = (
            hours_filter is None
            or hours_filter[0] <= signals.bar_time.time() < hours_filter[1]
        )
        # 時間帯外でも保有中ポジションの決済判定は常に行う（新規エントリーのみ制限する）
        if tracker.has_position or in_window:
            action, reason = decide_trade(tracker, trade_engine, signals)
        else:
            action, reason = None, "時間帯外"

        if action == "sell":
            qty = tracker.position.quantity  # BUY時の株数をそのまま使う
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


def parse_grid_args(tokens: list[str]) -> dict[str, list[str]]:
    """['gc_duration_minutes=3,5,7', 'macd_histogram_min=0,0.002'] のような
    'パラメータ名=値1,値2,...' のリストを {パラメータ名: [値, ...]} に変換する"""
    grid_spec: dict[str, list[str]] = {}
    for token in tokens:
        if "=" not in token:
            raise SystemExit(f"--grid の引数は パラメータ名=値1,値2,... の形式で指定してください: {token}")
        name, raw_values = token.split("=", 1)
        grid_spec[name] = raw_values.split(",")
    return grid_spec


def run_grid(symbol_id: str, start_date: str, end_date: str,
             grid_spec: dict[str, list[str]],
             hours_filter: tuple[dt_time, dt_time] | None = None):
    """
    entryの複数パラメータを総当たりで組み合わせて試し、合計損益の降順で一覧表示する。
    symbols.jsonは一切変更しない（メモリ上でEntryConfigを複製して差し替えるだけ）。
    """
    cfg_dict = load_symbol_config(symbol_id)
    macd_cfg = MacdConfig(**cfg_dict["macd"])
    base_entry_cfg = EntryConfig(**cfg_dict["entry"])
    exit_cfg = ExitConfig(**cfg_dict["exit"])
    order_cfg = OrderConfig(**cfg_dict["order"])
    risk_cfg = RiskConfig(**cfg_dict["risk"])
    opend_cfg = OpendConfig(**cfg_dict["opend"])

    for name in grid_spec:
        if not hasattr(base_entry_cfg, name):
            raise SystemExit(f"エントリー設定に存在しないパラメータです: {name}")

    df = _load_data(symbol_id, start_date, end_date, macd_cfg, opend_cfg)

    # 型はdataclassの型注釈から取得する（--sweepと同じ理由。JSON上0.0が整数0として読まれる場合があるため）
    field_types = {f.name: f.type for f in dataclasses.fields(base_entry_cfg)}
    param_names = list(grid_spec.keys())
    value_lists = [
        [raw if field_types[name] is bool else field_types[name](raw) for raw in grid_spec[name]]
        for name in param_names
    ]
    combos = list(itertools.product(*value_lists))

    print(f"\n=== グリッドサーチ: {symbol_id} / {', '.join(param_names)} ===")
    print(f"期間: {start_date} 〜 {end_date}" + (f" / 時間帯: {hours_filter[0]}-{hours_filter[1]}" if hours_filter else ""))
    print(f"組み合わせ数: {len(combos)}件\n")

    results = []
    for combo in combos:
        overrides = dict(zip(param_names, combo))
        entry_cfg = dataclasses.replace(base_entry_cfg, **overrides)
        closed_trades, total_pnl = _replay(
            df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg, hours_filter=hours_filter,
        )
        results.append((overrides, closed_trades, total_pnl))

    results.sort(key=lambda r: r[2], reverse=True)  # 合計損益の降順（最良の組み合わせが上位に来る）

    col_widths = [max(len(name), 10) + 2 for name in param_names]
    header = "".join(f"{name:>{w}}" for name, w in zip(param_names, col_widths)) + f"{'取引数':>8} {'合計損益(USD)':>14}"
    print(header)
    for overrides, closed_trades, total_pnl in results:
        row = "".join(f"{overrides[name]!s:>{w}}" for name, w in zip(param_names, col_widths))
        print(f"{row}{closed_trades:>8} {total_pnl:>+14.2f}")


def main():
    if len(sys.argv) < 4:
        print("使い方: python3 backtest.py <SYMBOL> <開始日> <終了日> [開始時刻-終了時刻]")
        print("        python3 backtest.py <SYMBOL> <開始日> <終了日> --sweep <パラメータ名> <値1,値2,...> [開始時刻-終了時刻]")
        print("        python3 backtest.py <SYMBOL> <開始日> <終了日> --grid <パラメータ名=値1,値2,...> [パラメータ名=... ...] [開始時刻-終了時刻]")
        print("例:     python3 backtest.py US.MU 2026-07-01 2026-07-24")
        print("例:     python3 backtest.py US.MU 2026-07-01 2026-07-24 09:30-10:00")
        print("例:     python3 backtest.py US.MU 2026-07-01 2026-07-24 --sweep gc_duration_minutes 3,5,7,10,15")
        print("例:     python3 backtest.py US.MU 2026-07-01 2026-07-24 --grid gc_duration_minutes=3,5,7 macd_histogram_min=0,0.002,0.005")
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
    elif len(sys.argv) > 4 and sys.argv[4] == "--grid":
        rest = sys.argv[5:]
        if not rest:
            print("使い方: python3 backtest.py <SYMBOL> <開始日> <終了日> --grid <パラメータ名=値1,値2,...> [パラメータ名=... ...] [開始時刻-終了時刻]")
            sys.exit(1)
        hours_filter = None
        if "=" not in rest[-1]:
            hours_filter = parse_hours_arg(rest.pop())
        if not rest:
            print("使い方: python3 backtest.py <SYMBOL> <開始日> <終了日> --grid <パラメータ名=値1,値2,...> [パラメータ名=... ...] [開始時刻-終了時刻]")
            sys.exit(1)
        grid_spec = parse_grid_args(rest)
        run_grid(symbol_id, start_date, end_date, grid_spec, hours_filter)
    else:
        hours_filter = parse_hours_arg(sys.argv[4]) if len(sys.argv) > 4 else None
        run_backtest(symbol_id, start_date, end_date, hours_filter)


if __name__ == "__main__":
    main()
