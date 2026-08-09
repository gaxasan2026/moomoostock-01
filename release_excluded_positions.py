"""
release_excluded_positions.py
「対象外」株（bot管理外だが実口座に存在する保有株）を成行で解放（売却）する
一回限りの手動スクリプト。

2026-08-04、以下5銘柄・計25株がbot管理外の「対象外」として検出された:
  US.CBRS 5株 / US.SPCX 5株 / US.TSLA 5株 （macd_trader管理下の銘柄設定を流用）
  US.COP  5株 / US.SNDK 5株 （swing_trader管理下の銘柄設定を流用）
ユーザーの明示的な指示により、2026-08-04 22:30 JST（市場開場後）に実行する。
botの自動売買ロジック（reconcile/pending_order等）とは無関係の一回限りの操作。

安全策:
- 実行直前に実残高を再照会し、想定株数(TARGETSのexpected_qty)を下回っていれば
  その銘柄はスキップする（想定と食い違う状態で機械的に売らない）。
- 実残高が想定以上でも、売るのは想定株数分だけに留める（それ以外の増分には触れない）。
- 発注前に市場状態を確認し、取引時間外なら発注せず理由を報告する
  （時間外に発注しても約定しないだけでなく、報告のタイミングもずれるため）。
- 発注後は実残高照会で約定を確認してから結果を報告する（「発注した」で終わらせない）。

実行: python3 release_excluded_positions.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "macd_trader"))

from config_loader import OrderConfig, OpendConfig  # noqa: E402
from order_manager import OrderManager, get_market_state_for_symbol  # noqa: E402
import futu as ft  # noqa: E402

HOST, PORT = "127.0.0.1", 11111

TARGETS = [
    # (symbol, expected_qty, market)
    ("US.CBRS", 5, "US"),
    ("US.SPCX", 5, "US"),
    ("US.TSLA", 5, "US"),
    ("US.COP", 5, "US"),
    ("US.SNDK", 5, "US"),
]

FILL_CHECK_RETRIES = 6
FILL_CHECK_INTERVAL_SEC = 15


def _query_real_qty(trd, symbol) -> int:
    ret, data = trd.position_list_query(trd_env=ft.TrdEnv.SIMULATE, code=symbol, refresh_cache=True)
    if ret != ft.RET_OK or data is None or data.empty:
        return 0
    return int(data["qty"].iloc[0])


def main():
    trd = ft.OpenSecTradeContext(
        host=HOST, port=PORT,
        security_firm=ft.SecurityFirm.FUTUSECURITIES,
        filter_trdmarket=ft.TrdMarket.US,
    )

    placed = []  # (symbol, expected_qty, real_qty_before)
    results = []  # (symbol, status, detail)

    try:
        for symbol, expected_qty, market in TARGETS:
            market_info = get_market_state_for_symbol(symbol, HOST, PORT)
            if not market_info or not market_info["is_regular_hours"]:
                label = market_info["label"] if market_info else "不明"
                results.append((symbol, "スキップ", f"取引時間外のため発注せず（市場状態: {label}）"))
                continue

            real_qty = _query_real_qty(trd, symbol)
            if real_qty < expected_qty:
                results.append((symbol, "スキップ", f"実残高{real_qty}株が想定{expected_qty}株未満のため安全側でスキップ"))
                continue

            cfg = OrderConfig(order_type="market", paper_trading=True, mock_data=False)
            opend = OpendConfig(host=HOST, port=PORT)
            om = OrderManager(cfg, opend, symbol, market, "K_1M")
            try:
                om.connect()
                price = om.get_current_price() or 0.0
                ok, msg = om.place_sell_order(price, "対象外株解放（手動）", expected_qty, force_market=True)
            finally:
                om.disconnect()

            if ok:
                placed.append((symbol, expected_qty, real_qty))
                results.append((symbol, "発注済み", f"成行 {expected_qty}株 @約${price:.2f} (注文ID {msg})"))
            else:
                results.append((symbol, "発注失敗", str(msg)))

        # ─ 約定確認: 実残高が発注前より expected_qty 分減っているか、リトライしながら確認する ─
        for attempt in range(FILL_CHECK_RETRIES):
            if not placed:
                break
            time.sleep(FILL_CHECK_INTERVAL_SEC)
            still_pending = []
            for symbol, expected_qty, real_qty_before in placed:
                real_qty_now = _query_real_qty(trd, symbol)
                if real_qty_now <= real_qty_before - expected_qty:
                    for i, (s, status, detail) in enumerate(results):
                        if s == symbol and status == "発注済み":
                            results[i] = (symbol, "約定確認", f"{detail} → 約定済み（残高 {real_qty_before}株 → {real_qty_now}株）")
                else:
                    still_pending.append((symbol, expected_qty, real_qty_before))
            placed = still_pending

        for symbol, expected_qty, real_qty_before in placed:
            for i, (s, status, detail) in enumerate(results):
                if s == symbol and status == "発注済み":
                    results[i] = (symbol, "約定未確認", f"{detail} → {FILL_CHECK_RETRIES * FILL_CHECK_INTERVAL_SEC}秒待っても約定を確認できず（moomooアプリで直接確認してください）")

    finally:
        trd.close()

    print("\n=== 対象外株 解放結果 ===")
    for symbol, status, detail in results:
        print(f"[{status}] {symbol}: {detail}")

    return results


if __name__ == "__main__":
    main()
