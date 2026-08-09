"""
verify_position_reconciliation.py
ポジション照合機能（position_store.py / position_reconciler.py / SignalTracker・
SwingSignalTrackerのrestore_position・force_adjust_quantity）の恒久的な回帰テスト。

過去に見つかった2つの見落としを踏まえ、以下を必ず両方満たす形でテストする:
1. 共有インターフェース（ダックタイピングで差し替えられるクラス）は、
   全ての実装クラスに対して同じテストを回す（SignalTracker と SwingSignalTracker の両方）。
   -- SwingSignalTrackerだけにrestore_position()が無く、本番のbot再起動で
      AttributeErrorとして発覚した実例があるため。
2. 個々の機能だけでなく、機能同士の組み合わせも明示的にテストする
   （restore_position() → ウォームアップ再生、というシーケンス）。
   -- 単体ではそれぞれ動作していたが、組み合わせると「エントリーより前の
      過去バーがピーク価格を汚染する」バグが本番の誤った売却を引き起こした実例があるため。

実行: python3 verify_position_reconciliation.py
"""
import sys
import os
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).parent.parent / "macd_trader"))
sys.path.insert(0, str(Path(__file__).parent))

import logging
from signal_tracker import SignalTracker
from swing_signal_tracker import SwingSignalTracker
from position_store import PositionStore
from position_reconciler import reconcile, apply_reconcile, restore_from_store

logging.basicConfig(level=logging.ERROR)  # テスト出力を汚さないためERROR以上のみ
logger = logging.getLogger("verify")

results = []


def check(name, cond):
    results.append((name, cond))
    print(("OK  " if cond else "FAIL"), name)


class FakeCfg:
    def __init__(self, mock_data=False):
        self.mock_data = mock_data


class FakeOrderManager:
    def __init__(self, real_qty, mock_data=False):
        self._real_qty = real_qty
        self.cfg = FakeCfg(mock_data=mock_data)

    def get_real_position_qty(self):
        return self._real_qty


class FakeState:
    def __init__(self):
        self.excluded_qty = 0
        self.position_check_ok = True


# ─── 1. 共有インターフェースの回帰テスト: 全実装クラスに同じテストを回す ───

def run_shared_tracker_matrix(tracker_cls, label):
    """SignalTracker / SwingSignalTracker 両方に対して同一のテストを回す"""
    print(f"\n=== {label}: restore_position / force_adjust_quantity ===")

    t = tracker_cls()
    entry_time = datetime(2026, 1, 1, 10, 0)
    peak_time = datetime(2026, 1, 1, 15, 0)
    t.restore_position(entry_price=40.0, entry_time=entry_time, quantity=80,
                        peak_price=45.0, peak_time=peak_time, bars_since_peak=2)
    check(f"{label}: restore_position -> has_position", t.has_position is True)
    check(f"{label}: restore_position -> entry_price", t.position.entry_price == 40.0)
    check(f"{label}: restore_position -> quantity", t.position.quantity == 80)
    check(f"{label}: restore_position -> peak_price", t.position.peak_price == 45.0)
    check(f"{label}: restore_position -> bars_since_peak", t.position.bars_since_peak == 2)

    old = t.force_adjust_quantity(50)
    check(f"{label}: force_adjust_quantity -> returns old qty", old == 80)
    check(f"{label}: force_adjust_quantity -> new qty applied", t.position.quantity == 50)

    old2 = t.force_adjust_quantity(0)
    check(f"{label}: force_adjust_quantity(0) -> closes position", t.position is None)
    check(f"{label}: force_adjust_quantity(0) -> returns old qty", old2 == 50)

    none_result = t.force_adjust_quantity(10)
    check(f"{label}: force_adjust_quantity on no position -> None", none_result is None)

    # ─ reconcile() / apply_reconcile() も両クラスで確認 ─
    store = PositionStore(f"/tmp/_verify_recon_{label}.json")

    t2 = tracker_cls()
    t2.current_time = datetime(2026, 1, 1, 10, 0)
    t2.open_position(price=50.0, quantity=100, timestamp=t2.current_time)

    r = reconcile(f"US.TEST_{label}", t2, FakeOrderManager(real_qty=150), store, logger)
    check(f"{label}: reconcile real>managed -> excluded=50", r.excluded_qty == 50)
    check(f"{label}: reconcile real>managed -> managed qty unchanged", t2.position.quantity == 100)

    t3 = tracker_cls()
    t3.current_time = datetime(2026, 1, 1, 10, 0)
    t3.open_position(price=50.0, quantity=100, timestamp=t3.current_time)
    r2 = reconcile(f"US.TEST_{label}2", t3, FakeOrderManager(real_qty=0), store, logger)
    check(f"{label}: reconcile real=0 -> force closed", t3.has_position is False)
    check(f"{label}: reconcile real=0 -> changed=True", r2.changed is True)

    state = FakeState()
    ok = apply_reconcile(f"US.TEST_{label}3", t2, FakeOrderManager(real_qty=None, mock_data=False),
                          store, logger, state)
    check(f"{label}: apply_reconcile API失敗 -> ok=False", ok is False)
    check(f"{label}: apply_reconcile API失敗 -> position_check_ok反映", state.position_check_ok is False)

    if os.path.exists(f"/tmp/_verify_recon_{label}.json"):
        os.remove(f"/tmp/_verify_recon_{label}.json")


run_shared_tracker_matrix(SignalTracker, "SignalTracker")
run_shared_tracker_matrix(SwingSignalTracker, "SwingSignalTracker")


# ─── 2. 組み合わせテスト: restore_position() → ウォームアップ再生の相互作用 ───
# 過去に発生した実際のバグ: entry_timeより前の過去バーの高値がピークとして
# 誤って採用され、実際にはエントリー直後で含み損益ほぼ0%のポジションが
# 「ピークから47.97%下落」という誤判定でSELLされた（US.SNDK, 2026-08-01）。

def run_warmup_interaction_test(tracker_cls, label):
    print(f"\n=== {label}: restore_position + ウォームアップ再生の組み合わせ ===")

    t = tracker_cls()
    entry_time = datetime(2026, 7, 31, 16, 0)
    t.restore_position(entry_price=1214.83, entry_time=entry_time, quantity=5,
                        peak_price=1214.83, peak_time=entry_time, bars_since_peak=0)
    t.current_time = entry_time

    # ウォームアップ相当: entry_timeより前の過去バー（実際に高値2335を記録していた
    # SNDKの状況を再現）を再生する。この時点でpeak_priceが変化してはならない。
    for hours_before in [200, 150, 100, 50, 10, 1]:
        bar_time = entry_time - timedelta(hours=hours_before)
        t.update(macd=1.0, signal=0.5, current_price=2335.0, timestamp=bar_time)

    check(f"{label}: entry前の過去バー(高値2335)がpeakを汚染しない",
          t.position.peak_price == 1214.83)
    check(f"{label}: entry前の過去バーでbars_since_peakが増えない",
          t.position.bars_since_peak == 0)

    # entry_time以降のバー（本当にライブで価格が上がった場合）は正しく反映されるべき
    t.update(macd=1.0, signal=0.5, current_price=1250.0, timestamp=entry_time + timedelta(hours=1))
    check(f"{label}: entry後の値上がりは正しくpeakに反映される",
          t.position.peak_price == 1250.0)

    # entry直後、値上がりなしで下落した場合の下落率が「正しい基準」で計算されること
    t2 = tracker_cls()
    t2.restore_position(entry_price=1214.83, entry_time=entry_time, quantity=5,
                         peak_price=1214.83, peak_time=entry_time, bars_since_peak=0)
    t2.current_time = entry_time
    for hours_before in [200, 100, 10]:
        bar_time = entry_time - timedelta(hours=hours_before)
        t2.update(macd=1.0, signal=0.5, current_price=2335.0, timestamp=bar_time)
    # entry直後、価格がエントリー価格とほぼ同じ（正常な状態）
    t2.update(macd=1.0, signal=0.5, current_price=1214.83, timestamp=entry_time)
    drop_pct = t2.drop_from_peak_pct
    check(f"{label}: 誤ったピーク汚染がなければdrop_from_peak_pctはほぼ0% (実測={drop_pct:.2f}%)",
          abs(drop_pct) < 1.0)


run_warmup_interaction_test(SignalTracker, "SignalTracker")
run_warmup_interaction_test(SwingSignalTracker, "SwingSignalTracker")


# ─── 3. restore_from_store: 永続化データからの復元がウォームアップ前に必要な形か ───

def run_restore_from_store_test(tracker_cls, label):
    print(f"\n=== {label}: restore_from_store ===")
    store = PositionStore(f"/tmp/_verify_restore_{label}.json")
    store.save("US.RESTORE", entry_price=33.0, entry_time="2026-01-01T09:00:00",
               quantity=40, peak_price=38.0, peak_time="2026-01-01T14:00:00",
               bars_since_peak=1)
    t = tracker_cls()
    t.current_time = datetime(2026, 1, 2, 9, 0)
    restore_from_store("US.RESTORE", t, store, logger)
    check(f"{label}: restore_from_store -> has_position", t.has_position is True)
    check(f"{label}: restore_from_store -> quantity", t.position.quantity == 40)
    check(f"{label}: restore_from_store -> entry_price", t.position.entry_price == 33.0)
    os.remove(f"/tmp/_verify_restore_{label}.json")


run_restore_from_store_test(SignalTracker, "SignalTracker")
run_restore_from_store_test(SwingSignalTracker, "SwingSignalTracker")


# ─── 結果 ───
print()
n_fail = sum(1 for _, c in results if not c)
print(f"合計 {len(results)}件中 {n_fail}件失敗")
sys.exit(1 if n_fail else 0)
