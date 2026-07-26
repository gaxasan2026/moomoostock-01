"""
main.py
MACDトレーダー メインループ
使い方: python main.py [--config trading_config.yaml]
"""
import argparse
import logging
import signal
import sys
import time
from datetime import datetime

from config_loader import load_config
from macd_engine import MacdEngine
from kdj_engine import KdjEngine
from signal_tracker import SignalTracker
from trade_engine import TradeEngine
from order_manager import OrderManager
from risk_manager import RiskManager
from trade_logger import TradeLogger


# ─── ロガー設定 ──────────────────────────────────────────────

def setup_logging(level: str):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("logs/macd_trader.log", encoding="utf-8"),
        ]
    )


# ─── グレースフルシャットダウン ──────────────────────────────

_running = True

def _handle_signal(sig, frame):
    global _running
    print("\n\n⚠️  停止シグナル受信。現在のループ後に終了します...")
    _running = False

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ─── メイン処理 ──────────────────────────────────────────────

def main(config_path: str):
    # 1. 設定読み込み
    cfg = load_config(config_path)
    setup_logging(cfg.logging.level)
    logger = logging.getLogger(__name__)

    logger.info("=" * 60)
    logger.info(f"  MACD Trader 起動")
    logger.info(f"  銘柄: {cfg.symbol}  足種: {cfg.macd.timeframe}")
    logger.info(f"  エントリー: GC継続 {cfg.entry.gc_duration_minutes}分")
    logger.info(f"  エグジット: ピーク下落 {cfg.exit.peak_drop_pct}% / "
                f"継続 {cfg.exit.peak_drop_duration_minutes}分 / "
                f"DC={'あり' if cfg.exit.dead_cross else 'なし'}")
    logger.info(f"  モード: {'📋 PAPER TRADING' if cfg.order.paper_trading else '🔴 本番取引'}")
    logger.info("=" * 60)

    # 2. 各モジュールの初期化
    macd_engine = MacdEngine(
        fast=cfg.macd.fast_period,
        slow=cfg.macd.slow_period,
        signal=cfg.macd.signal_period,
    )
    kdj_engine = KdjEngine()
    tracker = SignalTracker(
        peak_confirmation_bars=cfg.exit.peak_confirmation_bars
    )
    risk_mgr = RiskManager(cfg.risk)
    trade_engine = TradeEngine(cfg.entry, cfg.exit, risk_mgr)
    order_mgr = OrderManager(cfg.order, cfg.opend, cfg.symbol, cfg.market)
    trade_log = TradeLogger(
        cfg.logging.trade_log_path,
        cfg.logging.save_trade_log
    )

    # 3. OpenD 接続
    try:
        order_mgr.connect()
    except Exception as e:
        logger.error(f"接続エラーで終了: {e}")
        sys.exit(1)

    # 4. メインループ
    logger.info(f"監視開始: {cfg.symbol}")
    last_bar_time = None
    loop_interval_sec = 5  # 5秒ごとにK線データを確認

    try:
        while _running:
            # K線データ取得
            df = order_mgr.get_kline_data(kline_num=200)
            if df is None or len(df) < 40:
                logger.warning("K線データ不足。次のループまで待機...")
                time.sleep(loop_interval_sec)
                continue

            # MACD計算
            df = macd_engine.calculate(df)
            macd_vals = macd_engine.get_latest(df)
            kdj_vals = None
            if cfg.entry.kdj_max_d > 0:
                kdj_df = kdj_engine.calculate(df)
                kdj_vals = kdj_engine.get_latest(kdj_df)

            # 新しいバーが来た場合のみ処理（重複実行防止）
            current_bar_time = df["time_key"].iloc[-1] if "time_key" in df.columns else datetime.now()
            if hasattr(current_bar_time, "to_pydatetime"):
                current_bar_time = current_bar_time.to_pydatetime()

            is_new_bar = (last_bar_time is None or current_bar_time != last_bar_time)

            # 現在価格
            current_price = float(df["close"].iloc[-1])
            now = datetime.now()
            # Paper Tradingではバーのtime_keyを継続時間計測に使う（圧縮テスト用）
            bar_time = current_bar_time if isinstance(current_bar_time, datetime) else now

            # 出来高比率の計算
            volume_ratio = 1.0
            if len(df) >= 20 and "volume" in df.columns:
                avg_vol = df["volume"].iloc[-21:-1].mean()
                curr_vol = float(df["volume"].iloc[-1])
                if avg_vol > 0:
                    volume_ratio = curr_vol / avg_vol

            # 状態更新（毎ループ）
            tracker.update(
                macd=macd_vals.macd,
                signal=macd_vals.signal,
                current_price=current_price,
                timestamp=bar_time,
            )

            if is_new_bar:
                last_bar_time = current_bar_time

                # ── ポジションあり → 売り判定 ──
                if tracker.has_position:
                    sell, reason = trade_engine.should_sell(tracker, macd_vals)
                    if sell:
                        qty = tracker.position.quantity  # BUY時の株数をそのまま使う
                        ok, order_id = order_mgr.place_sell_order(current_price, reason, qty)
                        if ok:
                            trade_log.log_exit(
                                symbol=cfg.symbol,
                                price=current_price,
                                quantity=qty,
                                entry_price=tracker.position.entry_price,
                                hold_minutes=tracker.hold_minutes,
                                exit_reason=reason,
                                daily_trades=tracker.daily_trades + 1,
                                timestamp=now,
                            )
                            tracker.close_position(current_price, reason)
                    else:
                        if cfg.logging.show_realtime_price:
                            trade_engine.log_status(tracker, macd_vals)

                # ── ポジションなし → 買い判定 ──
                else:
                    buy, reason = trade_engine.should_buy(tracker, macd_vals, volume_ratio, kdj_vals)
                    if buy:
                        qty = risk_mgr.compute_quantity(current_price, cfg.order.quantity)
                        ok, order_id = order_mgr.place_buy_order(current_price, qty)
                        if ok:
                            trade_log.log_entry(
                                symbol=cfg.symbol,
                                price=current_price,
                                quantity=qty,
                                gc_duration=tracker.gc_duration_minutes,
                                timestamp=now,
                            )
                            tracker.open_position(current_price, qty, bar_time)
                    else:
                        trade_log.log_skip(reason)
                        if cfg.logging.show_realtime_price:
                            trade_engine.log_status(tracker, macd_vals)

            time.sleep(loop_interval_sec)

    except Exception as e:
        logger.error(f"予期せぬエラー: {e}", exc_info=True)
    finally:
        # ポジションが残っていれば警告
        if tracker.has_position:
            logger.warning(
                f"⚠️  未決済ポジションあり: {cfg.symbol} "
                f"{cfg.order.quantity}株 @ {tracker.position.entry_price:.4f}"
            )
        order_mgr.disconnect()
        logger.info(f"終了。本日取引: {tracker.daily_trades}回 / "
                    f"損益: {tracker.daily_realized_pnl:+.2f}USD")


# ─── エントリーポイント ──────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MACD Trader for moomoo")
    parser.add_argument(
        "--config", default="trading_config.yaml",
        help="設定ファイルのパス (デフォルト: trading_config.yaml)"
    )
    args = parser.parse_args()
    main(args.config)
