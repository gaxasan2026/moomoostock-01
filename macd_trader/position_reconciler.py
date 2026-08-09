"""
position_reconciler.py
実口座の保有数量とTrader自身が管理する数量（tracker.position）を照合する。
起動時（ウォームアップ直後）とポーリング毎の両方から、同じreconcile()を呼ぶことで
挙動を一致させる（起動時だけ特別扱いのロジックを持たない）。

方針（要求仕様どおり）:
- 実残高 == Trader管理数量: 何もしない
- 実残高 > Trader管理数量（手動買い増し等）: Trader管理数量は変更しない。
  差分は「対象外」として表示のみに使い、売買判断の対象にはしない
  （手動操作には理由があるかもしれないため）
- 実残高 < Trader管理数量（手動売却・全売却等）: Trader管理数量を即座に
  実残高まで引き下げる（誤ったショート発注を防止する）。実残高が0なら
  ポジションを強制クローズする。実際の売却価格が分からないため損益計算は行わない
- 実残高が確認できない（API障害等）: 呼び出し側に「確認できない」ことを伝え、
  その回の売買判断を見送らせる（安全側に倒す）
"""
from dataclasses import dataclass
from datetime import datetime

from signal_tracker import SignalTracker
from order_manager import OrderManager
from position_store import PositionStore
from discord_notifier import notify_position_mismatch


@dataclass
class ReconcileResult:
    ok: bool                     # False = 実残高を確認できなかった（売買判断を見送ること）
    excluded_qty: int = 0        # 「対象外」株数（手動買い増し分）
    changed: bool = False        # 今回の照合で新たに検知/補正を行ったか（アラーム判定用）
    detail: str = ""


def reconcile(symbol_id: str, tracker: SignalTracker, order_mgr: OrderManager,
              pos_store: PositionStore, logger) -> ReconcileResult:
    real_qty = order_mgr.get_real_position_qty()
    if real_qty is None:
        if order_mgr.cfg.mock_data:
            return ReconcileResult(ok=True, excluded_qty=0)
        return ReconcileResult(ok=False, detail="実口座の残高を確認できません")

    managed_qty = tracker.position.quantity if tracker.has_position else 0

    if real_qty == managed_qty:
        return ReconcileResult(ok=True, excluded_qty=0)

    if real_qty > managed_qty:
        # 手動買い増し等。Trader管理数量は変更せず、差分は表示専用の「対象外」として返す
        excluded = real_qty - managed_qty
        detail = f"自動取引: {managed_qty}株 / 対象外(手動): {excluded}株"
        return ReconcileResult(ok=True, excluded_qty=excluded, changed=True, detail=detail)

    # real_qty < managed_qty: 手動売却等 → 誤ったショートを防ぐため即座に引き下げる
    old_qty = tracker.force_adjust_quantity(real_qty)
    if real_qty <= 0:
        pos_store.clear(symbol_id)
        detail = f"実口座の保有がなくなったため強制クローズしました（旧: {old_qty}株 → 0株）"
    else:
        pos = tracker.position
        pos_store.save(symbol_id, pos.entry_price, pos.entry_time.isoformat(),
                        real_qty, pos.peak_price, pos.peak_time.isoformat(),
                        pos.bars_since_peak)
        detail = f"Trader管理数量を引き下げました（{old_qty}株 → {real_qty}株）"
    return ReconcileResult(ok=True, excluded_qty=0, changed=True, detail=detail)


def apply_reconcile(symbol_id: str, tracker: SignalTracker, order_mgr: OrderManager,
                     pos_store: PositionStore, logger, state, app_name: str = "Day Trader") -> bool:
    """
    reconcile()を呼び、結果をstateへ反映し、新規の不一致のみDiscord通知する。
    macd_trader/swing_trader共通で使う（stateはexcluded_qty: int /
    position_check_ok: bool の2属性を持つBotStateであればダックタイピングで動く）。
    app_name: Discord通知の送信元表示名（呼び出し側のアプリ名を渡す）

    Returns: 実残高を確認できたか（Falseならこのサイクルの売買判断を見送ること）
    """
    result = reconcile(symbol_id, tracker, order_mgr, pos_store, logger)
    state.position_check_ok = result.ok
    if not result.ok:
        logger.warning(f"[{symbol_id}] {result.detail}")
        return False

    if result.excluded_qty > 0:
        if result.excluded_qty != state.excluded_qty:
            logger.warning(f"[{symbol_id}] {result.detail}")
            notify_position_mismatch(symbol_id, result.detail, app_name=app_name)
        state.excluded_qty = result.excluded_qty
    else:
        state.excluded_qty = 0
        if result.changed:
            # 強制引き下げ（手動売却検知）等の一過性イベント
            logger.warning(f"[{symbol_id}] {result.detail}")
            notify_position_mismatch(symbol_id, result.detail, app_name=app_name)

    return True


def restore_from_store(symbol_id: str, tracker: SignalTracker,
                        pos_store: PositionStore, logger):
    """
    起動時、永続化されたポジションがあればtrackerに復元する。
    ウォームアップ（過去バーの再生）より前に呼ぶこと — 復元後にウォームアップを
    行うことで、GC/DC継続時間だけでなくピーク価格の追跡も過去バーから正しく
    継続され、停止直前のピークが正しく引き継がれる。
    """
    persisted = pos_store.get(symbol_id)
    if not persisted:
        return
    tracker.restore_position(
        entry_price=persisted["entry_price"],
        entry_time=datetime.fromisoformat(persisted["entry_time"]),
        quantity=persisted["quantity"],
        peak_price=persisted["peak_price"],
        peak_time=datetime.fromisoformat(persisted["peak_time"]),
        bars_since_peak=persisted.get("bars_since_peak", 0),
    )
    logger.info(f"[{symbol_id}] 永続化されたポジションを復元: "
                f"{persisted['quantity']}株 @ {persisted['entry_price']:.4f}")
