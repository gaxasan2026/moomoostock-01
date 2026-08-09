"""
analyze_gc_runs.py
「エントリー件数が少ないのはなぜか」を検証する分析スクリプト。

2つの仮説を定量的に確認する:
  ① GC（ゴールデンクロス）自体の発生回数・継続長（本数=日数）の分布。
     gc_duration_minutes の閾値（1〜4日）が、そのうち何%のGCを
     「短すぎる」として見送っているか。
  ② ポジション保有中は新しいGCが発生してもエントリーできない
     （has_positionでブロックされる）ため、保有期間の長さが
     機会損失にどれだけ寄与しているか。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

MACD_TRADER_DIR = Path(__file__).parent.parent / "macd_trader"
sys.path.insert(0, str(MACD_TRADER_DIR))
sys.path.insert(0, str(Path(__file__).parent))

from config_loader import OpendConfig  # noqa: E402
from macd_engine import MacdEngine  # noqa: E402
from backtest import _load_data  # noqa: E402

from swing_backtest import _build_configs, swing_replay  # noqa: E402

SYMBOLS = ["US.COHR", "US.TSLA", "US.QQQ", "US.NVDA", "US.MU", "US.MSFT"]
START_DATE = "2023-01-01"
END_DATE = "2026-07-30"
GC_THRESHOLDS_DAYS = [1, 2, 3, 4]  # 1440/2880/4320/5760分に対応


def find_gc_runs(df) -> list[int]:
    """is_golden=Trueが連続している区間の長さ（本数=日数）のリストを返す"""
    runs = []
    current_len = 0
    for is_golden in df["is_golden"]:
        if is_golden:
            current_len += 1
        else:
            if current_len > 0:
                runs.append(current_len)
            current_len = 0
    if current_len > 0:
        runs.append(current_len)
    return runs


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeframe", default=None, choices=["K_60M", "K_DAY"])
    args = parser.parse_args()

    macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg, opend_cfg = _build_configs(args.timeframe)
    macd_engine = MacdEngine(macd_cfg.fast_period, macd_cfg.slow_period, macd_cfg.signal_period)

    # K_60Mは1本=1時間（米国市場は1日約6.5時間）なので、日数換算の閾値は
    # バー数ではなく実時間（分）で判定する。GC_THRESHOLDS_DAYSの「日」相当を
    # バー数に変換する際は、K_DAYなら1本=1日、K_60Mなら1本=1時間として扱う。
    bars_per_day = 1 if macd_cfg.timeframe == "K_DAY" else 6.5
    unit_label = "日" if macd_cfg.timeframe == "K_DAY" else "時間(≈本数/6.5)"

    print(f"=== GC発生・継続長の分析 {START_DATE}〜{END_DATE} ({macd_cfg.timeframe}) ===\n")
    print(f"{'銘柄':<10}{'総本数':>8}{'GC発生回数':>12}"
          + "".join(f"{d}日未満(%)".rjust(14) for d in GC_THRESHOLDS_DAYS)
          + f"{'平均継続('+unit_label+')':>20}{'最長(本)':>10}")

    all_runs_summary = []

    for symbol in SYMBOLS:
        try:
            df = _load_data(symbol, START_DATE, END_DATE, macd_cfg, opend_cfg)
        except (Exception, SystemExit) as e:
            print(f"  !!! {symbol} 失敗: {e}")
            continue
        macd_df = macd_engine.calculate(df)
        runs = find_gc_runs(macd_df)
        total_bars = len(macd_df)
        n_runs = len(runs)
        avg_len_bars = sum(runs) / n_runs if n_runs else 0.0
        max_len_bars = max(runs) if runs else 0

        pct_below = []
        for d in GC_THRESHOLDS_DAYS:
            threshold_bars = d * bars_per_day
            below = sum(1 for r in runs if r < threshold_bars)
            pct_below.append(below / n_runs * 100 if n_runs else 0.0)

        avg_len_display = avg_len_bars if macd_cfg.timeframe == "K_DAY" else avg_len_bars  # 時間表記は本数=時間で一致
        print(f"{symbol:<10}{total_bars:>8}{n_runs:>12}"
              + "".join(f"{p:>13.0f}%" for p in pct_below)
              + f"{avg_len_display:>20.1f}{max_len_bars:>10}")
        all_runs_summary.append((symbol, runs))

    print("\n=== ポジション保有中に発生したGCイベント（機会損失の推定） ===")
    print("（保有中に新たなGCが発生した回数。has_positionでブロックされ、エントリーできない）")
    print(f"{'銘柄':<10}{'総取引数':>10}{'総保有日数':>12}{'保有中に発生したGC回数':>24}")

    for symbol in SYMBOLS:
        try:
            df = _load_data(symbol, START_DATE, END_DATE, macd_cfg, opend_cfg)
        except (Exception, SystemExit):
            continue

        hold_days_total = [0]
        trade_count = [0]
        blocked_gc_count = [0]

        def on_exit(price, qty, entry, hold, reason, daily_trades, bar_time, pnl, gc_dur, _hd=hold_days_total, _tc=trade_count):
            _hd[0] += hold / (24 * 60)
            _tc[0] += 1

        # 保有中に発生したGC回数を数えるため、簡易的に別途MACDのGC発生タイミングと
        # 実際のトレード区間（エントリー〜エグジット）を突き合わせる
        macd_df = macd_engine.calculate(df)
        cross_up_dates = set(macd_df.loc[macd_df["cross"] == "golden_cross", "time_key"])

        entry_exit_windows = []

        def on_entry(price, qty, gc_dur, bar_time, _w=entry_exit_windows):
            _w.append([bar_time, None])

        def on_exit2(price, qty, entry, hold, reason, daily_trades, bar_time, pnl, gc_dur, _w=entry_exit_windows):
            if _w and _w[-1][1] is None:
                _w[-1][1] = bar_time

        swing_replay(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
                     on_entry=on_entry, on_exit=lambda *a: (on_exit(*a), on_exit2(*a)))

        blocked = 0
        for gc_date in cross_up_dates:
            for start, end in entry_exit_windows:
                if end is None:
                    continue
                if start < gc_date < end:
                    blocked += 1
                    break

        print(f"{symbol:<10}{trade_count[0]:>10}{hold_days_total[0]:>12.0f}{blocked:>24}")


if __name__ == "__main__":
    main()
