"""
backtest_ma_rsi.py
MA_RSI戦略（MACD_KDJ戦略の代替候補）の一括バックテスト。

米国トレーダーの実務慣習に合わせた「群衆心理に乗る」設定で固定し、パラメータ探索は
行わない（多くの市場参加者が実際に見ている水準だからこそ意味がある、という方針のため）:
- デイトレード（macd_trader相当）: EMA(9,21) + RSI(14)、閾値70
- スイング（swing_trader相当）: SMA(20,50) + RSI(14)、閾値70

エントリー継続時間・Exit設定（利確/損切り/最大保有時間/ピーク下落等）は、比較対象の
MACD_KDJ戦略と同条件にするため、各アプリの登録銘柄の現行設定をそのまま流用する。
kdj_max_dだけ、KDJ用にキャリブレーションされた値からRSI用の閾値(70)に置き換える。

signal_tracker.py・trade_engine.py・bar_signals.py は一切変更していない
（MaEngine/RsiEngineがMacdValues/KdjValuesと同じ形の値を返すダックタイピングだけで
成立している）。

使い方:
    python3 backtest_ma_rsi.py day     # macd_trader全銘柄・直近3年
    python3 backtest_ma_rsi.py swing   # swing_trader全銘柄・直近3年
    python3 backtest_ma_rsi.py day --years 1
"""
from __future__ import annotations

import dataclasses
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

MACD_TRADER_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/macd_trader")
SWING_TRADER_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/swing_trader")
RESEARCH_DIR = Path(__file__).parent
sys.path.insert(0, str(MACD_TRADER_DIR))
sys.path.insert(0, str(SWING_TRADER_DIR))
sys.path.insert(0, str(RESEARCH_DIR))

from config_loader import MacdConfig, EntryConfig, ExitConfig, OrderConfig, RiskConfig, OpendConfig  # noqa: E402
from bar_signals import compute_bar_signals, decide_trade  # noqa: E402
from signal_tracker import SignalTracker  # noqa: E402
from trade_engine import TradeEngine  # noqa: E402
from risk_manager import RiskManager  # noqa: E402
from history_loader import load_or_fetch_history  # noqa: E402
from symbol_store import SymbolStore  # noqa: E402
from swing_symbol_store import SwingSymbolStore  # noqa: E402

from ma_engine import MaEngine  # noqa: E402
from rsi_engine import RsiEngine  # noqa: E402

KLINE_WINDOW = 200
RSI_MAX_D = 70.0  # RSIの「買われすぎ」閾値（業界標準の70/30の70側）

SCOPES = {
    "day": {
        "label": "デイトレード（macd_trader相当）",
        "symbols_path": MACD_TRADER_DIR / "data" / "symbols.json",
        "store_cls": SymbolStore,
        "ma_fast": 9, "ma_slow": 21, "ma_method": "ema",
        "rsi_period": 14,
    },
    "swing": {
        "label": "スイング（swing_trader相当）",
        "symbols_path": SWING_TRADER_DIR / "data" / "symbols.json",
        "store_cls": SwingSymbolStore,
        "ma_fast": 20, "ma_slow": 50, "ma_method": "sma",
        "rsi_period": 14,
    },
}


def _replay_ma_rsi(df: pd.DataFrame, ma_engine: MaEngine, rsi_engine: RsiEngine,
                    entry_cfg: EntryConfig, exit_cfg: ExitConfig,
                    order_cfg: OrderConfig, risk_cfg: RiskConfig) -> tuple[int, float, int]:
    """backtest.py の _replay() と同じコア再生ループ。エンジンだけMA/RSIに差し替える。
    Returns: (確定取引数, 合計損益, 勝ちトレード数, 月別損益{"YYYY-MM": pnl})"""
    tracker = SignalTracker(peak_confirmation_bars=exit_cfg.peak_confirmation_bars)
    risk_mgr = RiskManager(risk_cfg)
    trade_engine = TradeEngine(entry_cfg, exit_cfg, risk_mgr)

    closed_trades = 0
    total_pnl = 0.0
    wins = 0
    monthly_pnl: dict[str, float] = {}

    for i in range(KLINE_WINDOW, len(df) + 1):
        window = df.iloc[i - KLINE_WINDOW:i]
        signals = compute_bar_signals(window, ma_engine, rsi_engine, tracker, entry_cfg.kdj_max_d)
        action, reason = decide_trade(tracker, trade_engine, signals)

        if action == "sell":
            qty = tracker.position.quantity
            entry = tracker.position.entry_price
            pnl = (signals.current_price - entry) * qty
            total_pnl += pnl
            if pnl > 0:
                wins += 1
            month_key = signals.bar_time.strftime("%Y-%m")
            monthly_pnl[month_key] = monthly_pnl.get(month_key, 0.0) + pnl
            tracker.close_position(signals.current_price, reason)
            closed_trades += 1
        elif action == "buy":
            qty = risk_mgr.compute_quantity(signals.current_price, order_cfg.quantity)
            tracker.open_position(signals.current_price, qty, signals.bar_time)

    return closed_trades, total_pnl, wins, monthly_pnl


def run_scope(scope_key: str, years: int = 3, gc_duration_override: float | None = None,
              histogram_min_override: float | None = None,
              only_symbols: list[str] | None = None) -> list[tuple]:
    scope = SCOPES[scope_key]
    store = scope["store_cls"](str(scope["symbols_path"]))
    symbols = store.list()
    if only_symbols:
        symbols = [c for c in symbols if c["symbol"] in only_symbols]

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=365 * years)).strftime("%Y-%m-%d")

    print(f"\n{'=' * 70}")
    print(f"  MA_RSI戦略バックテスト: {scope['label']}")
    print(f"  MA: {scope['ma_method'].upper()}({scope['ma_fast']},{scope['ma_slow']}) / "
          f"RSI({scope['rsi_period']}) 閾値{RSI_MAX_D}")
    if gc_duration_override is not None:
        print(f"  gc_duration_minutes 上書き: {gc_duration_override}分")
    if histogram_min_override is not None:
        print(f"  乖離幅フィルター（macd_histogram_min）上書き: {histogram_min_override}%")
    print(f"  期間: {start_date} 〜 {end_date}（{years}年）・対象{len(symbols)}銘柄")
    print(f"{'=' * 70}")

    results = []
    combined_monthly: dict[str, float] = {}
    for cfg in symbols:
        symbol_id = cfg["symbol"]
        macd_cfg = MacdConfig(**cfg["macd"])
        entry_cfg = EntryConfig(**cfg["entry"])
        entry_overrides = {"kdj_max_d": RSI_MAX_D}
        if gc_duration_override is not None:
            entry_overrides["gc_duration_minutes"] = gc_duration_override
        if histogram_min_override is not None:
            entry_overrides["macd_histogram_min"] = histogram_min_override
        entry_cfg = dataclasses.replace(entry_cfg, **entry_overrides)
        exit_cfg = ExitConfig(**cfg["exit"])
        order_cfg = OrderConfig(**cfg["order"])
        risk_cfg = RiskConfig(**cfg["risk"])
        opend_cfg = OpendConfig(**cfg["opend"])

        try:
            df = load_or_fetch_history(
                symbol_id, start_date, end_date,
                timeframe=macd_cfg.timeframe, host=opend_cfg.host, port=opend_cfg.port,
            )
            df["time_key"] = pd.to_datetime(df["time_key"])
            df = df.sort_values("time_key").reset_index(drop=True)
            if len(df) < KLINE_WINDOW:
                print(f"  {symbol_id}: データ不足（{len(df)}本）— スキップ")
                continue

            ma_engine = MaEngine(fast=scope["ma_fast"], slow=scope["ma_slow"], method=scope["ma_method"])
            rsi_engine = RsiEngine(period=scope["rsi_period"])

            trades, pnl, wins, monthly = _replay_ma_rsi(
                df, ma_engine, rsi_engine, entry_cfg, exit_cfg, order_cfg, risk_cfg)
            win_rate = (wins / trades * 100) if trades else 0.0
            results.append((symbol_id, trades, pnl, win_rate))
            for month_key, month_pnl in monthly.items():
                combined_monthly[month_key] = combined_monthly.get(month_key, 0.0) + month_pnl
            print(f"  {symbol_id}: {trades}取引 | 損益 {pnl:+.2f} | 勝率 {win_rate:.1f}%")
        except (Exception, SystemExit) as e:
            print(f"  {symbol_id}: エラー — {e}")

    print(f"\n{'-' * 70}")
    total_trades = sum(r[1] for r in results)
    total_pnl = sum(r[2] for r in results)
    profitable = sum(1 for r in results if r[2] > 0)
    print(f"  合計: {len(results)}銘柄中{profitable}銘柄が黒字・{total_trades}取引・合計損益 {total_pnl:+.2f}")

    if combined_monthly:
        print(f"\n  月次内訳（全{len(symbols)}銘柄合算）:")
        profitable_months = 0
        for month_key in sorted(combined_monthly.keys()):
            m_pnl = combined_monthly[month_key]
            mark = "✅" if m_pnl > 0 else ("➖" if m_pnl == 0 else "⚠️")
            if m_pnl > 0:
                profitable_months += 1
            print(f"    {month_key}: {mark} {m_pnl:+.2f}")
        total_months = len(combined_monthly)
        print(f"  {total_months}ヶ月中{profitable_months}ヶ月が黒字"
              f"（{profitable_months / total_months * 100:.0f}%）")
    return results


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in SCOPES:
        raise SystemExit(
            f"使い方: python3 backtest_ma_rsi.py <{'|'.join(SCOPES)}> "
            f"[--years N] [--gc-duration MIN] [--histogram-min PCT] [--symbols US.A,US.B]")
    years_arg = 3
    if "--years" in sys.argv:
        years_arg = int(sys.argv[sys.argv.index("--years") + 1])
    gc_arg = None
    if "--gc-duration" in sys.argv:
        gc_arg = float(sys.argv[sys.argv.index("--gc-duration") + 1])
    hist_arg = None
    if "--histogram-min" in sys.argv:
        hist_arg = float(sys.argv[sys.argv.index("--histogram-min") + 1])
    symbols_arg = None
    if "--symbols" in sys.argv:
        symbols_arg = sys.argv[sys.argv.index("--symbols") + 1].split(",")
    run_scope(sys.argv[1], years=years_arg, gc_duration_override=gc_arg,
              histogram_min_override=hist_arg, only_symbols=symbols_arg)
