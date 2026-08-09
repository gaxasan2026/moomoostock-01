"""
verify_force_sell_and_market_state.py
即時売却機能（BotManager.force_sell/force_sell_all）・市場開閉状態判定
（order_manager.get_market_state_for_symbol）・成行強制発注（place_sell_order
のforce_market引数）・発注確定の分離（pending_order）の恒久的な回帰テスト。

過去の見落としパターン（~/.claude/CLAUDE.md 見落とし防止チェックリスト）を踏まえ、
以下を明示的にテストする:
1. 共有インターフェース: macd_trader/bot_manager.py と swing_trader/swing_bot_manager.py
   は独立した2つのBotManager実装（ダックタイピングで差し替え可能）なので、
   force_sell/force_sell_all・pending_order関連のガード条件は両方に対して同じテストを回す。
2. 機能の組み合わせ: 「市場が閉場中」と「force_market=True」を組み合わせたとき、
   注文自体はforce_market優先で成行になる一方、実際の約定は市場が開くまで
   持ち越される可能性がある、という2つの独立した事実を混同しない
   （2026-08-03、US.MRVLの実運用中に「内部的には売却済みだが実口座には
   まだ残っている」不整合として実際に発生したケースに対応）。この不整合の
   再発防止として「注文が通った」と「ポジションを開閉した」を分離した
   pending_order機構（_check_pending_order/_is_regular_hours/_finalize_buy/
   _finalize_sell）についても、確定前後でtracker/pos_store/state.tradesが
   正しく更新される（される/されない）ことをテストする。

実行: python3 verify_force_sell_and_market_state.py
"""
import sys
import time
import logging
from pathlib import Path
from datetime import datetime
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).parent.parent / "macd_trader"))
sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd
import futu as ft

from bot_manager import (
    BotManager as MacdBotManager, BotState as MacdBotState,
    _check_pending_order as macd_check_pending_order,
    _is_regular_hours as macd_is_regular_hours,
)
from swing_bot_manager import (
    BotManager as SwingBotManager, BotState as SwingBotState,
    _check_pending_order as swing_check_pending_order,
    _is_regular_hours as swing_is_regular_hours,
)
from signal_tracker import SignalTracker
from swing_signal_tracker import SwingSignalTracker
from order_manager import (
    OrderManager, get_market_state_for_symbol,
    _MARKET_STATE_LABELS, _REGULAR_HOURS_STATES,
)
from config_loader import OrderConfig

results = []


def check(name, cond):
    results.append((name, cond))
    print(("OK  " if cond else "FAIL"), name)


# ─── 1. 共有インターフェース: force_sell/force_sell_all のガード条件 ───
# macd_trader/bot_manager.py と swing_trader/swing_bot_manager.py は
# 別々のBotManagerクラス（フォーク）なので、両方に同じテストを回す。

def run_force_sell_guard_matrix(manager_cls, state_cls, label):
    print(f"\n=== {label}: force_sell guard ===")

    mgr = manager_cls()

    # (a) 銘柄が登録されていない
    ok, msg = mgr.force_sell("US.NOTFOUND")
    check(f"{label}: 未登録銘柄はエラー", ok is False)

    # (b) 停止中（running=False）
    st_stopped = state_cls("US.STOPPED")
    st_stopped.running = False
    st_stopped.has_position = True
    mgr._bots["US.STOPPED"] = st_stopped
    ok, msg = mgr.force_sell("US.STOPPED")
    check(f"{label}: 停止中はエラー", ok is False)
    check(f"{label}: 停止中はforce_sell_requestedがセットされない",
          st_stopped.force_sell_requested is False)

    # (c) 稼働中だがポジションなし
    st_nopos = state_cls("US.NOPOS")
    st_nopos.running = True
    st_nopos.has_position = False
    mgr._bots["US.NOPOS"] = st_nopos
    ok, msg = mgr.force_sell("US.NOPOS")
    check(f"{label}: ポジションなしはエラー", ok is False)
    check(f"{label}: ポジションなしはforce_sell_requestedがセットされない",
          st_nopos.force_sell_requested is False)

    # (d) 稼働中かつポジションあり -> 成功しフラグが立つ
    st_ok = state_cls("US.HASPOS")
    st_ok.running = True
    st_ok.has_position = True
    mgr._bots["US.HASPOS"] = st_ok
    ok, msg = mgr.force_sell("US.HASPOS")
    check(f"{label}: 稼働中+ポジションありは成功", ok is True)
    check(f"{label}: 稼働中+ポジションありはforce_sell_requestedがセットされる",
          st_ok.force_sell_requested is True)

    # (d2) pending_order（約定確認待ち）中は二重発注を防ぐため拒否する
    st_pending = state_cls("US.PENDING")
    st_pending.running = True
    st_pending.has_position = True
    st_pending.pending_order = {"side": "SELL", "qty": 5}
    mgr._bots["US.PENDING"] = st_pending
    ok, msg = mgr.force_sell("US.PENDING")
    check(f"{label}: pending_order中は二重発注防止のためエラー", ok is False)
    check(f"{label}: pending_order中はforce_sell_requestedがセットされない",
          st_pending.force_sell_requested is False)

    # (e) force_sell_all は 稼働中+ポジションあり+pending_orderなしの銘柄だけを対象にする
    mgr2 = manager_cls()
    targets_expected = set()
    for sid, running, has_pos, pending in [
        ("US.A", True, True, None), ("US.B", True, False, None),
        ("US.C", False, True, None), ("US.D", False, False, None),
        ("US.E", True, True, {"side": "BUY", "qty": 3}),
    ]:
        st = state_cls(sid)
        st.running = running
        st.has_position = has_pos
        st.pending_order = pending
        mgr2._bots[sid] = st
        if running and has_pos and not pending:
            targets_expected.add(sid)
    targets = mgr2.force_sell_all()
    check(f"{label}: force_sell_allは稼働中+ポジションあり+約定待ちなしだけを対象にする",
          set(targets) == targets_expected)
    check(f"{label}: force_sell_all対象外銘柄はフラグが立たない",
          mgr2._bots["US.B"].force_sell_requested is False
          and mgr2._bots["US.C"].force_sell_requested is False
          and mgr2._bots["US.E"].force_sell_requested is False
          and mgr2._bots["US.D"].force_sell_requested is False)


run_force_sell_guard_matrix(MacdBotManager, MacdBotState, "macd_trader.BotManager")
run_force_sell_guard_matrix(SwingBotManager, SwingBotState, "swing_trader.BotManager")


# ─── 2. get_market_state_for_symbol: 生の状態名 -> 分類ロジック ───
# _REGULAR_HOURS_STATES / _MARKET_STATE_LABELS は「市場が開いているかどうか」を
# 呼び出し側（確認ダイアログ）に伝える唯一の情報源なので、分類を誤ると
# 「閉場中なのに開場中と表示される」という安全上の見落としに直結する。

def _mock_market_state_ctx(raw_state):
    """get_market_state()がraw_stateを返すOpenQuoteContextのモックを作る"""
    ctx = MagicMock()
    df = pd.DataFrame({"market_state": [raw_state]})
    ctx.get_market_state.return_value = (ft.RET_OK, df)
    return ctx


print("\n=== get_market_state_for_symbol: 状態分類 ===")

for raw in ["MORNING", "AFTERNOON"]:
    with patch("futu.OpenQuoteContext", return_value=_mock_market_state_ctx(raw)):
        info = get_market_state_for_symbol("US.TEST", "127.0.0.1", 11111)
    check(f"get_market_state_for_symbol: {raw} は取引時間中と判定される",
          info is not None and info["is_regular_hours"] is True)

for raw in ["PRE_MARKET_BEGIN", "PRE_MARKET_END", "AFTER_HOURS_BEGIN",
            "AFTER_HOURS_END", "CLOSED", "NONE", "REST", "WAITING_OPEN", "AUCTION"]:
    with patch("futu.OpenQuoteContext", return_value=_mock_market_state_ctx(raw)):
        info = get_market_state_for_symbol("US.TEST", "127.0.0.1", 11111)
    check(f"get_market_state_for_symbol: {raw} は取引時間外と判定される（誤って開場中扱いしない）",
          info is not None and info["is_regular_hours"] is False)

# 未知の状態名（APIの将来的な仕様変更等）が来ても、開場中と誤判定せず
# 安全側（取引時間外）に倒れることを確認する
with patch("futu.OpenQuoteContext", return_value=_mock_market_state_ctx("SOME_FUTURE_STATE")):
    info = get_market_state_for_symbol("US.TEST", "127.0.0.1", 11111)
check("get_market_state_for_symbol: 未知の状態名は安全側（取引時間外）に倒れる",
      info is not None and info["is_regular_hours"] is False)

# 照会失敗時はNoneを返す（呼び出し側でエラー表示にできるようにする）
ctx_fail = MagicMock()
ctx_fail.get_market_state.return_value = (ft.RET_ERROR, "error")
with patch("futu.OpenQuoteContext", return_value=ctx_fail):
    info = get_market_state_for_symbol("US.TEST", "127.0.0.1", 11111)
check("get_market_state_for_symbol: 照会失敗時はNoneを返す", info is None)


# ─── 3. place_sell_order: force_market=True は設定のorder_typeに関わらず成行にする ───
# 「ユーザーがGUIで即時売却を押した」という明示的な意図を、銘柄ごとの
# 通常設定（指値/成行）より必ず優先させる、という契約のテスト。

def _make_order_manager(order_type):
    cfg = OrderConfig(order_type=order_type, mock_data=False, paper_trading=True)

    class FakeOpend:
        host = "127.0.0.1"
        port = 11111

    om = OrderManager(cfg, FakeOpend(), "US.TEST", "US", "K_1M")
    fake_trade_ctx = MagicMock()
    fake_trade_ctx.place_order.return_value = (
        ft.RET_OK, pd.DataFrame({"order_id": ["123"]})
    )
    om._trade_ctx = fake_trade_ctx
    return om, fake_trade_ctx


print("\n=== place_sell_order: force_market の優先度 ===")

for configured_order_type in ["limit", "market"]:
    om, fake_ctx = _make_order_manager(configured_order_type)
    ok, _ = om.place_sell_order(price=100.0, reason="手動決済", quantity=5, force_market=True)
    used_order_type = fake_ctx.place_order.call_args.kwargs["order_type"]
    check(f"place_sell_order: order_type={configured_order_type}設定でもforce_market=Trueなら成行注文",
          ok is True and used_order_type == ft.OrderType.MARKET)

# force_market=False（通常のbot自動売却経路）では設定通りになることの回帰確認
om_limit, fake_ctx_limit = _make_order_manager("limit")
om_limit.place_sell_order(price=100.0, reason="通常決済", quantity=5, force_market=False)
check("place_sell_order: force_market=Falseかつorder_type=limitなら指値注文のまま",
      fake_ctx_limit.place_order.call_args.kwargs["order_type"] == ft.OrderType.NORMAL)

om_market, fake_ctx_market = _make_order_manager("market")
om_market.place_sell_order(price=100.0, reason="通常決済", quantity=5, force_market=False)
check("place_sell_order: force_market=Falseかつorder_type=marketなら成行注文のまま",
      fake_ctx_market.place_order.call_args.kwargs["order_type"] == ft.OrderType.MARKET)


# ─── 4. _is_regular_hours: 市場状態が取得できない場合は安全側(False)に倒れる ───

class _FakeOpend:
    host = "127.0.0.1"
    port = 11111


class _FakeCfgForHours:
    symbol = "US.TEST"
    opend = _FakeOpend()


print("\n=== _is_regular_hours: フェイルセーフ ===")
for fn, label in [(macd_is_regular_hours, "macd_trader"), (swing_is_regular_hours, "swing_trader")]:
    with patch("futu.OpenQuoteContext", return_value=_mock_market_state_ctx("MORNING")):
        check(f"{label}._is_regular_hours: MORNINGはTrue", fn(_FakeCfgForHours()) is True)
    with patch("futu.OpenQuoteContext", return_value=_mock_market_state_ctx("CLOSED")):
        check(f"{label}._is_regular_hours: CLOSEDはFalse", fn(_FakeCfgForHours()) is False)
    ctx_fail = MagicMock()
    ctx_fail.get_market_state.return_value = (ft.RET_ERROR, "error")
    with patch("futu.OpenQuoteContext", return_value=ctx_fail):
        check(f"{label}._is_regular_hours: 照会失敗時はFalse（安全側）", fn(_FakeCfgForHours()) is False)


# ─── 5. _check_pending_order: 「注文が通った」と「ポジションを開閉した」の分離 ───
# 2026-08-03のUS.MRVL不整合（市場時間外の決済で内部状態だけ即座にクローズし、
# 実口座には約定していない5株が残った）の再発防止機構そのもののテスト。

class _FakeState:
    def __init__(self):
        self.trades = []


class _FakeOrderMgrQty:
    def __init__(self, qty):
        self._qty = qty

    def get_real_position_qty(self):
        return self._qty


_pending_logger = logging.getLogger("verify_pending_order")


def run_pending_order_matrix(check_fn, tracker_cls, label):
    print(f"\n=== {label}: _check_pending_order ===")
    pos_store = MagicMock()
    trade_log = MagicMock()

    # --- SELL: 実残高が確認できない間は確定させない ---
    tracker = tracker_cls()
    tracker.open_position(100.0, 5, datetime(2026, 1, 1, 9, 0))
    state = _FakeState()
    state.pending_order = {
        "side": "SELL", "price": 101.0, "qty": 5, "reason": "テスト決済",
        "entry_price": 100.0, "hold_minutes": 30.0, "pre_qty": 5,
        "requested_at": time.monotonic(),
    }
    ok = check_fn("US.TEST", state, tracker, _FakeOrderMgrQty(None),
                  pos_store, trade_log, _pending_logger, datetime.now())
    check(f"{label}: SELL 実残高不明ならFalseを返しpending_order/トラッカーを維持",
          ok is False and state.pending_order is not None and tracker.has_position is True)

    # --- SELL: まだ未約定（実残高が減っていない） ---
    ok = check_fn("US.TEST", state, tracker, _FakeOrderMgrQty(5),
                  pos_store, trade_log, _pending_logger, datetime.now())
    check(f"{label}: SELL 未約定ならTrueを返すがpending_order/トラッカーは維持（早期クローズしない）",
          ok is True and state.pending_order is not None and tracker.has_position is True)

    # --- SELL: 約定確認（実残高が0に） ---
    ok = check_fn("US.TEST", state, tracker, _FakeOrderMgrQty(0),
                  pos_store, trade_log, _pending_logger, datetime.now())
    check(f"{label}: SELL 実残高0で確定しpending_orderが解消・トラッカーも閉じる",
          ok is True and state.pending_order is None and tracker.has_position is False)
    check(f"{label}: SELL 確定後にstate.tradesへ元の理由でSELL行が追加される",
          len(state.trades) == 1 and state.trades[0]["action"] == "SELL"
          and state.trades[0]["exit_reason"] == "テスト決済")

    # --- BUY: 約定確認前後 ---
    tracker2 = tracker_cls()
    state2 = _FakeState()
    state2.pending_order = {
        "side": "BUY", "price": 50.0, "qty": 10, "reason": "テストエントリー",
        "gc_duration": 30.0, "bar_time": datetime(2026, 1, 2, 9, 0).isoformat(),
        "pre_qty": 0, "requested_at": time.monotonic(),
    }
    ok = check_fn("US.TEST", state2, tracker2, _FakeOrderMgrQty(0),
                  pos_store, trade_log, _pending_logger, datetime.now())
    check(f"{label}: BUY 未約定の間はトラッカーがポジションを持たない",
          ok is True and state2.pending_order is not None and tracker2.has_position is False)

    ok = check_fn("US.TEST", state2, tracker2, _FakeOrderMgrQty(10),
                  pos_store, trade_log, _pending_logger, datetime.now())
    check(f"{label}: BUY 実残高が注文数量分増えたら確定しポジションが開く",
          ok is True and state2.pending_order is None and tracker2.has_position is True
          and tracker2.position.quantity == 10)
    check(f"{label}: BUY 確定後にstate.tradesへBUY行が追加される",
          len(state2.trades) == 1 and state2.trades[0]["action"] == "BUY")

    # --- 長時間未約定: 警告フラグが一度だけ立つ（多重警告防止） ---
    tracker3 = tracker_cls()
    tracker3.open_position(100.0, 3, datetime(2026, 1, 1, 9, 0))
    state3 = _FakeState()
    state3.pending_order = {
        "side": "SELL", "price": 100.0, "qty": 3, "reason": "テスト決済",
        "entry_price": 100.0, "hold_minutes": 10.0, "pre_qty": 3,
        "requested_at": time.monotonic() - 100000,  # 6時間の警告閾値を大きく超過させる
    }
    check_fn("US.TEST", state3, tracker3, _FakeOrderMgrQty(3),
             pos_store, trade_log, _pending_logger, datetime.now())
    check(f"{label}: 長時間未約定で警告フラグが立つ", state3.pending_order.get("_warned") is True)
    check_fn("US.TEST", state3, tracker3, _FakeOrderMgrQty(3),
             pos_store, trade_log, _pending_logger, datetime.now())
    check(f"{label}: 警告フラグが立った後もpending_orderは維持される（誤って確定しない）",
          state3.pending_order is not None and tracker3.has_position is True)


run_pending_order_matrix(macd_check_pending_order, SignalTracker, "macd_trader")
run_pending_order_matrix(swing_check_pending_order, SwingSignalTracker, "swing_trader")


# ─── 結果 ───
print()
n_fail = sum(1 for _, c in results if not c)
print(f"合計 {len(results)}件中 {n_fail}件失敗")
sys.exit(1 if n_fail else 0)
