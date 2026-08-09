"""
backtest_macd_pct_filter.py
MACD_KDJ戦略（現行の実運用設定）に、MA_RSI側で使ったのと同じ「価格に対する
乖離率（%）」ベースのヒストグラムフィルターを適用して検証する。

目的: デイトレード比較（backtest_ma_rsi.py day）でMA_RSI側にのみ乖離幅
フィルターを追加していたため、フェアな比較になっていなかった。MACD側にも
同じ発想のフィルター（macd_engine_pct.MacdEnginePct）をかけて、両者を
同条件で比較し直す。

使い方:
    python3 backtest_macd_pct_filter.py --histogram-min 0.1
    python3 backtest_macd_pct_filter.py --histogram-min 0.3 --years 1
    python3 backtest_macd_pct_filter.py --histogram-min 0.3 --symbols US.MU,US.TSLA
"""
from __future__ import annotations

import dataclasses
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

MACD_TRADER_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/macd_trader")
RESEARCH_DIR = Path(__file__).parent
sys.path.insert(0, str(MACD_TRADER_DIR))
sys.path.insert(0, str(RESEARCH_DIR))

from config_loader import MacdConfig, EntryConfig, ExitConfig, OrderConfig, RiskConfig, OpendConfig  # noqa: E402
from bar_signals import compute_bar_signals, decide_trade  # noqa: E402
from signal_tracker import SignalTracker  # noqa: E402
from trade_engine import TradeEngine  # noqa: E402
from risk_manager import RiskManager  # noqa: E402
from kdj_engine import KdjEngine  # noqa: E402
from history_loader import load_or_fetch_history  # noqa: E402
from symbol_store import SymbolStore  # noqa: E402

from macd_engine_pct import MacdEnginePct  # noqa: E402

KLINE_WINDOW = 200
SYMBOLS_PATH = MACD_TRADER_DIR / "data" / "symbols.json"


def _replay(df, macd_engine, kdj_engine, entry_cfg, exit_cfg, order_cfg, risk_cfg):
    tracker = SignalTracker(peak_confirmation_bars=exit_cfg.peak_confirmation_bars)
    risk_mgr = RiskManager(risk_cfg)
    trade_engine = TradeEngine(entry_cfg, exit_cfg, risk_mgr)

    closed_trades = 0
    total_pnl = 0.0
    wins = 0
    monthly_pnl: dict[str, float] = {}

    for i in range(KLINE_WINDOW, len(df) + 1):
        window = df.iloc[i - KLINE_WINDOW:i]
        signals = compute_bar_signals(window, macd_engine, kdj_engine, tracker, entry_cfg.kdj_max_d)
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


def run(histogram_min: float, years: int = 1, only_symbols: list[str] | None = None):
    store = SymbolStore(str(SYMBOLS_PATH))
    symbols = store.list()
    if only_symbols:
        symbols = [c for c in symbols if c["symbol"] in only_symbols]

    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=365 * years)).strftime("%Y-%m-%d")

    print(f"\n{'=' * 70}")
    print("  MACD_KDJ戦略バックテスト（乖離率%ヒストグラムフィルター適用）")
    print(f"  乖離幅フィルター（macd_histogram_min）: {histogram_min}%")
    print(f"  期間: {start_date} 〜 {end_date}（{years}年）・対象{len(symbols)}銘柄")
    print(f"{'=' * 70}")

    results = []
    combined_monthly: dict[str, float] = {}
    for cfg in symbols:
        symbol_id = cfg["symbol"]
        macd_cfg = MacdConfig(**cfg["macd"])
        entry_cfg = EntryConfig(**cfg["entry"])
        entry_cfg = dataclasses.replace(entry_cfg, macd_histogram_min=histogram_min)
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

            macd_engine = MacdEnginePct(macd_cfg.fast_period, macd_cfg.slow_period, macd_cfg.signal_period)
            kdj_engine = KdjEngine()

            trades, pnl, wins, monthly = _replay(
                df, macd_engine, kdj_engine, entry_cfg, exit_cfg, order_cfg, risk_cfg)
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
        print("\n  月次内訳（全銘柄合算）:")
        profitable_months = 0
        for month_key in sorted(combined_monthly.keys()):
            m_pnl = combined_monthly[month_key]
            mark = "✅" if m_pnl > 0 else ("➖" if m_pnl == 0 else "⚠️")
            if m_pnl > 0:
                profitable_months += 1
            print(f"    {month_key}: {mark} {m_pnl:+.2f}")
        total_months = len(combined_monthly)
        print(f"  {total_months}ヶ月中{profitable_months}ヶ月が黒字（{profitable_months / total_months * 100:.0f}%）")

    return results


if __name__ == "__main__":
    if "--histogram-min" not in sys.argv:
        raise SystemExit(
            "使い方: python3 backtest_macd_pct_filter.py --histogram-min PCT "
            "[--years N] [--symbols US.A,US.B]")
    hist_arg = float(sys.argv[sys.argv.index("--histogram-min") + 1])
    years_arg = 1
    if "--years" in sys.argv:
        years_arg = int(sys.argv[sys.argv.index("--years") + 1])
    symbols_arg = None
    if "--symbols" in sys.argv:
        symbols_arg = sys.argv[sys.argv.index("--symbols") + 1].split(",")
    run(hist_arg, years=years_arg, only_symbols=symbols_arg)
