"""
test_short_cycle.py
「継続期間が短い(K_60MのGC平均12〜14時間)ことを踏まえ、エントリーからエグジットまでを
2営業日以内で完結させる設定値があるか」を検証するスクリプト。

--sweep/--gridはEntryConfigのフィールドしか変更できないため、ExitConfig（max_hold_minutes,
peak_drop_duration_minutes）も同時に変える必要があるこの検証は、専用スクリプトとして書く。

max_hold_minutes=2880分（2営業日）を上限として強制し、gc_duration_minutesを
短い候補で振って、実際の平均保有時間・エグジット理由の内訳・損益を確認する。
K_60Mで検証する（K_DAYは1本=1日のため、2日以内という制約自体があまり意味を持たない）。
"""
from __future__ import annotations

import copy
import dataclasses
import sys
from collections import Counter
from pathlib import Path

MACD_TRADER_DIR = Path(__file__).parent.parent / "macd_trader"
sys.path.insert(0, str(MACD_TRADER_DIR))
sys.path.insert(0, str(Path(__file__).parent))

from backtest import _load_data  # noqa: E402

from swing_backtest import _build_configs, swing_replay  # noqa: E402
from bar_signals import compute_bar_signals, decide_trade  # noqa: E402
from macd_engine import MacdEngine  # noqa: E402
from kdj_engine import KdjEngine  # noqa: E402
from trade_engine import TradeEngine  # noqa: E402
from risk_manager import RiskManager  # noqa: E402
from swing_signal_tracker import SwingSignalTracker  # noqa: E402

SYMBOLS = ["US.COHR", "US.TSLA", "US.QQQ", "US.NVDA", "US.MU", "US.MSFT"]
START_DATE = "2023-01-01"
END_DATE = "2026-07-30"

# (gc_duration_minutes, peak_drop_duration_minutes) の候補。max_hold_minutesは2880固定。
# 米国市場のセッションは約6.5時間(390分)しかなく、夜間は一気に約17.5時間(1050分)
# 経過時間が飛ぶ。そのため「セッション終了(≈390分)〜翌セッション開始(≈1410分)」の
# 間の閾値はどれも実質同じ意味になる（その間に評価されるバーが存在しないため）。
# この「死角」をまたいで意味のある比較になるよう、候補は意図的に離す。
CANDIDATES = [
    (120.0, 120.0),    # 同一セッション内・2時間
    (300.0, 180.0),    # 同一セッション内・セッション終盤(5時間)
    (1440.0, 720.0),   # 翌営業日にまたぐ(1日)
    (2880.0, 1440.0),  # 2営業日にまたぐ(2日＝仮説の上限そのもの)
]
MAX_HOLD_MINUTES = 2880.0  # 2営業日相当の上限
KLINE_WINDOW = 200


def replay_with_stats(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg):
    macd_engine = MacdEngine(macd_cfg.fast_period, macd_cfg.slow_period, macd_cfg.signal_period)
    kdj_engine = KdjEngine()
    tracker = SwingSignalTracker(peak_confirmation_bars=exit_cfg.peak_confirmation_bars)
    risk_mgr = RiskManager(risk_cfg)
    trade_engine = TradeEngine(entry_cfg, exit_cfg, risk_mgr)

    closed_trades = 0
    total_pnl = 0.0
    hold_minutes_list = []
    reason_counter = Counter()

    for i in range(KLINE_WINDOW, len(df) + 1):
        window = df.iloc[i - KLINE_WINDOW:i]
        signals = compute_bar_signals(window, macd_engine, kdj_engine, tracker, entry_cfg.kdj_max_d)
        action, reason = decide_trade(tracker, trade_engine, signals)

        if action == "sell":
            qty = tracker.position.quantity
            entry = tracker.position.entry_price
            hold = tracker.hold_minutes
            pnl = (signals.current_price - entry) * qty
            total_pnl += pnl
            hold_minutes_list.append(hold)
            # 理由を大分類する（利確/損切り/ピーク系/デッドクロス/時間切れ）
            if "利確" in reason:
                reason_counter["利確"] += 1
            elif "損切り" in reason:
                reason_counter["損切り"] += 1
            elif "ピーク" in reason:
                reason_counter["ピーク下落"] += 1
            elif "デッドクロス" in reason or "DC" in reason:
                reason_counter["デッドクロス"] += 1
            elif "時間切れ" in reason:
                reason_counter["保有時間切れ(強制)"] += 1
            else:
                reason_counter["その他"] += 1
            tracker.close_position(signals.current_price, reason)
            closed_trades += 1
        elif action == "buy":
            qty = risk_mgr.compute_quantity(signals.current_price, order_cfg.quantity)
            tracker.open_position(signals.current_price, qty, signals.bar_time)

    return closed_trades, total_pnl, hold_minutes_list, reason_counter


def main():
    print(f"=== 短期サイクル検証（K_60M、max_hold_minutes={MAX_HOLD_MINUTES:.0f}分=2営業日固定） ===\n")

    for gc_dur, peak_dur in CANDIDATES:
        print(f"\n{'='*90}")
        print(f"候補: gc_duration_minutes={gc_dur:.0f}分 / peak_drop_duration_minutes={peak_dur:.0f}分 / max_hold_minutes={MAX_HOLD_MINUTES:.0f}分")
        print(f"{'銘柄':<10}{'取引数':>8}{'合計損益':>12}{'平均保有(分)':>14}{'2日以内(%)':>12}  エグジット理由内訳")

        for symbol in SYMBOLS:
            macd_cfg, base_entry_cfg, base_exit_cfg, order_cfg, risk_cfg, opend_cfg = _build_configs("K_60M")
            entry_cfg = dataclasses.replace(base_entry_cfg, gc_duration_minutes=gc_dur)
            exit_cfg = dataclasses.replace(base_exit_cfg,
                                            peak_drop_duration_minutes=peak_dur,
                                            max_hold_minutes=MAX_HOLD_MINUTES)
            try:
                df = _load_data(symbol, START_DATE, END_DATE, macd_cfg, opend_cfg)
            except (Exception, SystemExit) as e:
                print(f"  !!! {symbol} データ取得失敗: {e}")
                continue

            trades, pnl, holds, reasons = replay_with_stats(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg)
            avg_hold = sum(holds) / len(holds) if holds else 0.0
            within_2days = sum(1 for h in holds if h <= MAX_HOLD_MINUTES) / len(holds) * 100 if holds else 0.0
            reason_str = ", ".join(f"{k}:{v}" for k, v in reasons.most_common())
            print(f"{symbol:<10}{trades:>8}{pnl:>+12.2f}{avg_hold:>14.1f}{within_2days:>11.0f}%  {reason_str}")


if __name__ == "__main__":
    main()
