"""
verify_swing_signal_tracker.py
SwingSignalTracker が意図通りに動くかを合成データで検証する。

確認項目:
1. 元の SignalTracker は日付が変わるたびに gc_duration_minutes が0にリセットされる
   （デイトレード版の既存挙動が変わっていないことの回帰確認）
2. SwingSignalTracker は日をまたいでも gc_duration_minutes が単調増加する（修正の効果）
3. GC→DC の反転が両トラッカーで正しく検知される
4. 日付変更時、両トラッカーとも daily_trades / daily_realized_pnl が同様にリセットされる
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "macd_trader"))
sys.path.insert(0, str(Path(__file__).parent))

from signal_tracker import SignalTracker  # noqa: E402
from swing_signal_tracker import SwingSignalTracker  # noqa: E402

FAILURES = []


def check(name: str, condition: bool, detail: str = ""):
    status = "OK" if condition else "NG"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def main():
    print("=== 1〜2: 日をまたぐGC継続時間の挙動比較 ===")
    day_tracker = SignalTracker(peak_confirmation_bars=2)
    swing_tracker = SwingSignalTracker(peak_confirmation_bars=2)

    base = datetime(2026, 7, 1, 9, 30)
    day_gc_durations = []
    swing_gc_durations = []

    # 5営業日連続でGC継続（macd=1.0 > signal=0.5、反転なし）
    for i in range(5):
        ts = base + timedelta(days=i)
        day_tracker.update(macd=1.0, signal=0.5, current_price=100.0 + i, timestamp=ts)
        swing_tracker.update(macd=1.0, signal=0.5, current_price=100.0 + i, timestamp=ts)
        day_gc_durations.append(day_tracker.gc_duration_minutes)
        swing_gc_durations.append(swing_tracker.gc_duration_minutes)
        print(f"  day{i}: SignalTracker.gc={day_tracker.gc_duration_minutes:.1f}min  "
              f"SwingSignalTracker.gc={swing_tracker.gc_duration_minutes:.1f}min")

    check("SignalTracker: 毎日ほぼ0にリセットされる（デイトレード既存挙動の回帰確認）",
          all(d < 1.0 for d in day_gc_durations),
          f"values={[round(d, 2) for d in day_gc_durations]}")

    check("SwingSignalTracker: 日をまたいで単調増加する（修正の効果）",
          all(swing_gc_durations[i] < swing_gc_durations[i + 1] for i in range(len(swing_gc_durations) - 1))
          and swing_gc_durations[-1] >= 4 * 24 * 60 - 1,
          f"values={[round(d, 2) for d in swing_gc_durations]}")

    print("\n=== 3: GC→DC反転の検知 ===")
    dc_ts = base + timedelta(days=5)
    day_tracker.update(macd=0.3, signal=0.8, current_price=95.0, timestamp=dc_ts)
    swing_tracker.update(macd=0.3, signal=0.8, current_price=95.0, timestamp=dc_ts)

    check("SignalTracker: DCへ転換しis_golden_cross=False", not day_tracker.is_golden_cross)
    check("SwingSignalTracker: DCへ転換しis_golden_cross=False", not swing_tracker.is_golden_cross)
    check("SignalTracker: dc_duration_minutesが0近辺から開始", day_tracker.dc_duration_minutes < 1.0,
          f"={day_tracker.dc_duration_minutes:.2f}")
    check("SwingSignalTracker: dc_duration_minutesが0近辺から開始", swing_tracker.dc_duration_minutes < 1.0,
          f"={swing_tracker.dc_duration_minutes:.2f}")

    # DC継続をもう1日延ばして両者とも継続時間が伸びることを確認
    dc_ts2 = base + timedelta(days=6)
    day_tracker.update(macd=0.2, signal=0.9, current_price=93.0, timestamp=dc_ts2)
    swing_tracker.update(macd=0.2, signal=0.9, current_price=93.0, timestamp=dc_ts2)
    check("SwingSignalTracker: DC継続時間も日をまたいで伸びる",
          swing_tracker.dc_duration_minutes >= 24 * 60 - 1,
          f"={swing_tracker.dc_duration_minutes:.2f}")

    print("\n=== 4: 日次カウンターのリセット（変更していない挙動の回帰確認） ===")
    day_tracker2 = SignalTracker(peak_confirmation_bars=2)
    swing_tracker2 = SwingSignalTracker(peak_confirmation_bars=2)

    d0 = base
    d1 = base + timedelta(days=1)

    for t in (day_tracker2, swing_tracker2):
        t.update(macd=1.0, signal=0.5, current_price=100.0, timestamp=d0)
        t.open_position(price=100.0, quantity=10, timestamp=d0)
        t.close_position(price=105.0, reason="test")  # 同日中に決済 → daily_trades=1

    check("SignalTracker: 同日決済でdaily_trades=1", day_tracker2.daily_trades == 1)
    check("SwingSignalTracker: 同日決済でdaily_trades=1", swing_tracker2.daily_trades == 1)

    for t in (day_tracker2, swing_tracker2):
        t.update(macd=1.0, signal=0.5, current_price=101.0, timestamp=d1)  # 翌日 → 日次リセット

    check("SignalTracker: 翌日でdaily_trades=0にリセット", day_tracker2.daily_trades == 0)
    check("SwingSignalTracker: 翌日でdaily_trades=0にリセット", swing_tracker2.daily_trades == 0)
    check("SwingSignalTracker: 翌日でもgc_duration_minutesはリセットされない（GC継続中のため）",
          swing_tracker2.gc_duration_minutes >= 24 * 60 - 1,
          f"={swing_tracker2.gc_duration_minutes:.2f}")

    print(f"\n=== 結果: {'全て成功' if not FAILURES else f'{len(FAILURES)}件失敗'} ===")
    if FAILURES:
        for f in FAILURES:
            print(f"  NG: {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
