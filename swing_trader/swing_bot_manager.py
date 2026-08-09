"""
swing_bot_manager.py
macd_trader/bot_manager.py の BotState/BotManager/_bot_loop を最小限フォークしたもの。

差分:
- SignalTracker → SwingSignalTracker（日をまたぐGC/DC継続を保持する版）
- ポーリング間隔: K_DAY/K_60Mでは新バー確定が稀なため、5秒固定ではなく
  POLL_INTERVAL_SECONDS（既定60秒）に変更

それ以外（スレッドモデル・状態管理・ログストア・注文実行）は
macd_trader/bot_manager.py と同じ構造。macd_engine/kdj_engine/bar_signals/
trade_engine/order_manager/risk_manager/trade_logger/discord_notifierは
日付演算を含まない、または時間足非依存のため、そのままread-only importして使う。
"""
import collections
import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent / "macd_trader"))
sys.path.insert(0, str(Path(__file__).parent))

from config_loader import (  # noqa: E402
    TradingConfig, MacdConfig, EntryConfig, ExitConfig,
    OrderConfig, RiskConfig, LoggingConfig, OpendConfig, OscillatorConfig,
)
from engine_factory import build_trend_engine, build_oscillator_engine  # noqa: E402
from bar_signals import compute_bar_signals, decide_trade  # noqa: E402
from trade_engine import TradeEngine  # noqa: E402
from order_manager import OrderManager, get_market_state_for_symbol  # noqa: E402
from risk_manager import RiskManager  # noqa: E402
from discord_notifier import notify_trade  # noqa: E402
from trade_logger import TradeLogger  # noqa: E402
from position_store import PositionStore  # noqa: E402
from position_reconciler import apply_reconcile, restore_from_store  # noqa: E402

from swing_signal_tracker import SwingSignalTracker  # noqa: E402

POLL_INTERVAL_SECONDS = 60  # K_DAY/K_60Mは新バー確定が稀なため、デイトレード版(5秒)より長く取る

# 実口座の保有数量とTrader管理数量の照合結果を永続化する。macd_trader側とは
# 別ファイル（swing_trader/data/positions.json）で完全に分離する。
_position_store = PositionStore(str(Path(__file__).parent / "data" / "positions.json"))

# 自分の発注直後は実口座への反映（約定）が間に合わない可能性があるため、
# この秒数だけポジション照合をスキップする猶予期間。
RECONCILE_GRACE_SECONDS = 60  # ポーリング間隔(60秒)自体が長いため1サイクル分の猶予を持たせる

# 市場時間外に自分の注文を出した場合、「注文が通った」時点で即座に
# ポジションを開閉したことにしない（実際の約定は次の取引時間まで持ち越されるため）。
# 約定を実残高照会で確認できるまでの間、この秒数以上経過したら一度だけ警告ログを出す
# （自動での再送・取消はしない。約定確認はpending_orderが解消されるまで毎サイクル続ける）。
PENDING_ORDER_WARN_SECONDS = 6 * 3600

# ─── グローバルログストア（ポーリング用） ────────────────────────

_log_store: collections.deque = collections.deque(maxlen=500)
_log_counter: int = 0
_log_lock = threading.Lock()


def _push_log(item: dict):
    global _log_counter
    with _log_lock:
        item["id"] = _log_counter
        _log_store.append(item)
        _log_counter += 1


def get_logs_since(since: int) -> list:
    with _log_lock:
        return [e for e in _log_store if e.get("id", 0) >= since]


class _QueueLogHandler(logging.Handler):
    def __init__(self, symbol_id: str):
        super().__init__()
        self._symbol_id = symbol_id
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record):
        _push_log({
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": record.levelname,
            "symbol": self._symbol_id,
            "message": self.format(record),
        })


# ─── ボット状態クラス ─────────────────────────────────────────────

class BotState:
    def __init__(self, symbol_id: str):
        self.symbol_id = symbol_id
        self.running: bool = False
        self.stop_event: threading.Event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.status: str = "停止中"
        self.current_price: float = 0.0
        self.has_position: bool = False
        self.entry_price: float = 0.0
        self.quantity: int = 0
        self.gain_pct: float = 0.0
        self.daily_trades: int = 0
        self.daily_pnl: float = 0.0
        self.gc_duration: float = 0.0
        self.macd_hist: float = 0.0
        self.trades: list = []
        # ポジション照合（実口座 vs Trader管理数量）関連
        self.excluded_qty: int = 0        # 対象外（手動買い増し等）の株数
        self.position_check_ok: bool = True  # False = 実残高を確認できていない（要注意表示）
        # ユーザーによる即時売却要求
        self.force_sell_requested: bool = False
        # 発注済みだが約定未確認の自分の注文（市場時間外に注文した場合等）。
        # {"side": "BUY"/"SELL", "price", "qty", "reason", "pre_qty", "requested_at", ...}
        self.pending_order: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "symbol_id": self.symbol_id,
            "running": self.running,
            "status": self.status,
            "current_price": self.current_price,
            "has_position": self.has_position,
            "entry_price": self.entry_price,
            "quantity": self.quantity,
            "gain_pct": self.gain_pct,
            "daily_trades": self.daily_trades,
            "daily_pnl": self.daily_pnl,
            "gc_duration": self.gc_duration,
            "macd_hist": self.macd_hist,
            "excluded_qty": self.excluded_qty,
            "position_check_ok": self.position_check_ok,
            "pending_order": (
                {"side": self.pending_order["side"], "qty": self.pending_order["qty"]}
                if self.pending_order else None
            ),
        }


# ─── ボットマネージャー ───────────────────────────────────────────

class BotManager:
    def __init__(self):
        self._bots: dict = {}
        self._lock = threading.Lock()

    def get_state(self, symbol_id: str) -> Optional[BotState]:
        return self._bots.get(symbol_id)

    def all_states(self) -> dict:
        return {sid: s.to_dict() for sid, s in self._bots.items()}

    def all_trades(self) -> list:
        trades = []
        for s in self._bots.values():
            trades.extend(s.trades)
        return sorted(trades, key=lambda x: x.get("timestamp", ""), reverse=True)[:200]

    def start(self, symbol_id: str, cfg_dict: dict):
        with self._lock:
            s = self._bots.get(symbol_id)
            if s and s.running:
                return False, "すでに起動中です"
            state = BotState(symbol_id)
            self._bots[symbol_id] = state

        state.running = True
        state.stop_event.clear()
        state.thread = threading.Thread(
            target=_bot_loop,
            args=(symbol_id, cfg_dict, state),
            daemon=True,
            name=f"swing-bot-{symbol_id}",
        )
        state.thread.start()
        return True, "起動しました"

    def stop(self, symbol_id: str):
        state = self._bots.get(symbol_id)
        if not state:
            return False, "ボットが見つかりません"
        if not state.running:
            return False, "すでに停止中です"
        state.stop_event.set()
        return True, "停止を要求しました"

    def stop_all(self):
        for sid, state in self._bots.items():
            if state.running:
                state.stop_event.set()

    def force_sell(self, symbol_id: str):
        state = self._bots.get(symbol_id)
        if not state:
            return False, "ボットが見つかりません"
        if not state.running:
            return False, "停止中です（moomooアプリから手動で決済してください）"
        if not state.has_position:
            return False, "ポジションを保有していません"
        if state.pending_order:
            return False, "決済処理中です（約定確認待ち）"
        state.force_sell_requested = True
        return True, "即時売却を要求しました"

    def force_sell_all(self) -> list:
        targets = []
        for sid, state in self._bots.items():
            if state.running and state.has_position and not state.pending_order:
                state.force_sell_requested = True
                targets.append(sid)
        return targets


# ─── 発注確定処理（即時確定 / 約定確認後の確定 で共有する） ──────────

def _is_regular_hours(cfg: TradingConfig) -> bool:
    """
    市場状態を確認できない場合はFalse（取引時間外）を返す（安全側のデフォルト）。
    誤って「取引時間中」と判定してしまうと、実際は未約定のまま即座に
    ポジションを開閉したことにしてしまい、実口座との不整合を招くため。
    """
    info = get_market_state_for_symbol(cfg.symbol, cfg.opend.host, cfg.opend.port)
    return bool(info and info["is_regular_hours"])


def _finalize_buy(symbol_id, state, tracker, pos_store, trade_log, logger,
                   price, qty, reason, gc_duration, bar_time, now):
    state.trades.insert(0, {
        "action": "BUY", "symbol": symbol_id,
        "price": round(price, 4),
        "quantity": qty,
        "entry_price": round(price, 4),
        "pnl_usd": None, "pnl_pct": None,
        "hold_minutes": 0.0, "exit_reason": reason,
        "timestamp": now.strftime("%H:%M:%S"),
    })
    trade_log.log_entry(symbol_id, price, qty, gc_duration, now)
    tracker.open_position(price, qty, bar_time)
    pos_store.save(symbol_id, price, bar_time.isoformat(), qty, price, bar_time.isoformat(), 0)
    logger.info(f"[{symbol_id}] BUY @ {price:.4f} | GC {gc_duration:.1f}分 | {qty}株")
    notify_trade(symbol_id, "BUY", price, qty, reason=reason, app_name="Swing Trader")


def _finalize_sell(symbol_id, state, tracker, pos_store, trade_log, logger,
                    price, qty, entry, reason, hold_minutes, now):
    pnl = (price - entry) * qty
    pnl_pct = (price - entry) / entry * 100
    state.trades.insert(0, {
        "action": "SELL", "symbol": symbol_id,
        "price": round(price, 4),
        "quantity": qty,
        "entry_price": round(entry, 4),
        "pnl_usd": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "hold_minutes": hold_minutes,
        "exit_reason": reason,
        "timestamp": now.strftime("%H:%M:%S"),
    })
    trade_log.log_exit(symbol_id, price, qty, entry, hold_minutes, reason,
                        tracker.daily_trades + 1, now)
    tracker.close_position(price, reason)
    pos_store.clear(symbol_id)
    logger.info(f"[{symbol_id}] SELL @ {price:.4f} | {reason} | PnL {pnl:+.2f}USD")
    notify_trade(symbol_id, "SELL", price, qty, reason=reason, pnl=pnl, pnl_pct=pnl_pct, app_name="Swing Trader")


def _check_pending_order(symbol_id, state, tracker, order_mgr, pos_store,
                          trade_log, logger, now) -> bool:
    """
    未確定の自分の注文(state.pending_order)を実残高と照合し、約定が確認できれば
    ポジションの開閉を確定する。まだ未約定なら何もせず待つ。
    Returns: 実残高を確認できたか（Falseならこのサイクルの売買判断を見送ること）
    """
    po = state.pending_order
    real_qty = order_mgr.get_real_position_qty()
    if real_qty is None:
        return False

    filled = (real_qty >= po["pre_qty"] + po["qty"]) if po["side"] == "BUY" \
        else (real_qty <= po["pre_qty"] - po["qty"])

    if not filled:
        waited = time.monotonic() - po["requested_at"]
        if waited > PENDING_ORDER_WARN_SECONDS and not po.get("_warned"):
            logger.warning(
                f"[{symbol_id}] {po['side']}注文が{waited / 3600:.1f}時間経過しても"
                f"未約定です（市場再開待ちの可能性。moomooアプリで直接確認してください）")
            po["_warned"] = True
        return True

    if po["side"] == "BUY":
        _finalize_buy(symbol_id, state, tracker, pos_store, trade_log, logger,
                      po["price"], po["qty"], po["reason"], po["gc_duration"],
                      datetime.fromisoformat(po["bar_time"]), now)
    else:
        _finalize_sell(symbol_id, state, tracker, pos_store, trade_log, logger,
                       po["price"], po["qty"], po["entry_price"], po["reason"],
                       po["hold_minutes"], now)
    state.pending_order = None
    return True


# ─── ボットループ（スレッドで実行） ──────────────────────────────

def _bot_loop(symbol_id: str, cfg_dict: dict, state: BotState):
    logger = logging.getLogger(f"swing_bot.{symbol_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _QueueLogHandler(symbol_id)
    logger.addHandler(handler)

    order_mgr = None
    try:
        cfg = _to_config(cfg_dict)
        state.status = "起動中"
        logger.info(f"[{symbol_id}] 起動 paper={cfg.order.paper_trading} timeframe={cfg.macd.timeframe}")

        macd_engine = build_trend_engine(cfg.macd)
        kdj_engine = build_oscillator_engine(cfg.oscillator)
        tracker = SwingSignalTracker(peak_confirmation_bars=cfg.exit.peak_confirmation_bars)
        risk_mgr = RiskManager(cfg.risk)
        trade_engine = TradeEngine(cfg.entry, cfg.exit, risk_mgr)
        order_mgr = OrderManager(cfg.order, cfg.opend, cfg.symbol, cfg.market, cfg.macd.timeframe, logger=logger)
        trade_log = TradeLogger(cfg.logging.trade_log_path, cfg.logging.save_trade_log)

        order_mgr.connect()

        # ─ 起動時: 永続化されたポジションを復元する ─
        # ウォームアップ（過去バー再生）より前に復元することで、GC/DC継続時間だけでなく
        # ピーク価格の追跡も過去バーから正しく継続され、停止直前のピークが引き継がれる。
        restore_from_store(symbol_id, tracker, _position_store, logger)

        # ─ 起動時ウォームアップ ─
        # SwingSignalTrackerは生成直後、GC/DC継続時間の起点を持たない。何もせず
        # 最新バーだけをupdate()すると「起動した瞬間=GCが始まった瞬間」という誤った
        # 起点になり、既に条件を満たしているトレンドに乗り遅れる（機会損失になる）。
        # そこで直近200本のウィンドウを過去から順にtracker.update()へ流し込み、
        # バックテストと同じ考え方で実際の継続時間を復元してからライブループに入る。
        # このウォームアップ中はdecide_trade()を呼ばないため、売買は一切発生しない。
        warmup_df = order_mgr.get_kline_data(kline_num=200)
        if warmup_df is not None and len(warmup_df) >= 40:
            warmup_macd_df = macd_engine.calculate(warmup_df)
            # get_cur_kline()（ライブAPI）のtime_keyは生の文字列で返る。
            # compute_bar_signals()側はこれを想定し「パース失敗時はdatetime.now()に
            # フォールバック」する設計だが、それはその場でポーリングするライブ運用だから
            # 許容できる代替であり、200本の過去バーを一括処理するここでは使えない
            # （全バーが同じ「今」になり、日またぎの継続時間計算が壊れる）。
            # そのため明示的にパースする。
            warmup_macd_df["time_key"] = pd.to_datetime(warmup_macd_df["time_key"])
            for _, row in warmup_macd_df.iterrows():
                bar_time = row["time_key"].to_pydatetime()
                tracker.update(macd=float(row["macd"]), signal=float(row["signal"]),
                               current_price=float(row["close"]), timestamp=bar_time)
            state_desc = (f"GC継続{tracker.gc_duration_minutes:.0f}分"
                          if tracker.is_golden_cross else f"DC継続{tracker.dc_duration_minutes:.0f}分")
            logger.info(f"[{symbol_id}] ウォームアップ完了: {len(warmup_macd_df)}本再生 | {state_desc}")
        else:
            logger.warning(f"[{symbol_id}] ウォームアップ用データを取得できませんでした（次回ループで再取得します）")

        # ─ 起動時: 実口座と照合する（ウォームアップで復元/継続した状態に対して行う） ─
        apply_reconcile(symbol_id, tracker, order_mgr, _position_store, logger, state, app_name="Swing Trader")

        logger.info(f"[{symbol_id}] 監視開始")
        state.status = "監視中"
        last_bar_time = None  # Noneのままにする＝次のループで直ちに売買判定を行う
        fail_count = 0
        skip_reconcile_until = 0.0  # 自分の発注直後の猶予期間（time.monotonic()と比較）

        while not state.stop_event.is_set():
            df = order_mgr.get_kline_data(kline_num=200)
            if df is None or len(df) < 40:
                bar_count = 0 if df is None else len(df)
                fail_count += 1
                logger.warning(f"[{symbol_id}] K線不足 ({bar_count}本) — {POLL_INTERVAL_SECONDS}秒後リトライ ({fail_count}回目)")
                if fail_count >= 3:
                    logger.warning(f"[{symbol_id}] 接続を再試行します...")
                    try:
                        order_mgr.reconnect()
                        fail_count = 0
                    except Exception as re:
                        logger.error(f"[{symbol_id}] 再接続失敗: {re}")
                time.sleep(POLL_INTERVAL_SECONDS)
                continue
            fail_count = 0

            signals = compute_bar_signals(df, macd_engine, kdj_engine, tracker, cfg.entry.kdj_max_d)
            macd_vals = signals.macd_vals
            current_price = signals.current_price
            bar_time = signals.bar_time
            now = datetime.now()

            current_bar_time = df["time_key"].iloc[-1]
            if hasattr(current_bar_time, "to_pydatetime"):
                current_bar_time = current_bar_time.to_pydatetime()
            is_new_bar = (last_bar_time is None or current_bar_time != last_bar_time)

            # ─ 実口座とのポジション照合。未確定の自分の注文があれば、それを
            #   実残高照会で確認することを優先し、通常の照合・新規売買判断は行わない ─
            skip_trade_decision = False
            if state.pending_order:
                reconcile_ok = _check_pending_order(
                    symbol_id, state, tracker, order_mgr, _position_store, trade_log, logger, now)
                skip_trade_decision = True
            elif time.monotonic() < skip_reconcile_until:
                reconcile_ok = True
            else:
                reconcile_ok = apply_reconcile(symbol_id, tracker, order_mgr, _position_store, logger, state, app_name="Swing Trader")

            state.current_price = current_price
            state.has_position = tracker.has_position
            state.entry_price = tracker.position.entry_price if tracker.has_position else 0.0
            state.quantity = tracker.position.quantity if tracker.has_position else 0
            state.gain_pct = tracker.gain_pct
            state.daily_trades = tracker.daily_trades
            state.daily_pnl = tracker.daily_realized_pnl
            state.gc_duration = tracker.gc_duration_minutes
            state.macd_hist = macd_vals.histogram

            if tracker.has_position:
                state.status = f"保有中 ({tracker.gain_pct:+.2f}%)"
            elif tracker.is_golden_cross:
                days = tracker.gc_duration_minutes / (24 * 60)
                state.status = f"GC待機 ({days:.1f}日)"
            else:
                state.status = "監視中"

            # ─ ユーザーによる即時売却要求（is_new_barに関わらず毎サイクル処理する） ─
            if not skip_trade_decision and state.force_sell_requested:
                state.force_sell_requested = False
                if tracker.has_position:
                    qty = tracker.position.quantity
                    entry = tracker.position.entry_price
                    hold = round(tracker.hold_minutes, 1)
                    ok, _ = order_mgr.place_sell_order(current_price, "手動決済", qty, force_market=True)
                    if ok:
                        if _is_regular_hours(cfg):
                            _finalize_sell(symbol_id, state, tracker, _position_store, trade_log, logger,
                                          current_price, qty, entry, "手動決済", hold, now)
                            skip_reconcile_until = time.monotonic() + RECONCILE_GRACE_SECONDS
                        else:
                            state.pending_order = {
                                "side": "SELL", "price": current_price, "qty": qty,
                                "reason": "手動決済", "entry_price": entry, "hold_minutes": hold,
                                "pre_qty": qty, "requested_at": time.monotonic(),
                            }
                            logger.info(f"[{symbol_id}] 手動決済 注文送信済み（市場時間外のため約定待ち）")

            if skip_trade_decision:
                pass
            elif is_new_bar and not reconcile_ok:
                logger.warning(f"[{symbol_id}] 実残高を確認できないため、今回の売買判断を見送ります")
            elif is_new_bar:
                last_bar_time = current_bar_time
                action, reason = decide_trade(tracker, trade_engine, signals)

                if action == "sell":
                    qty = tracker.position.quantity
                    entry = tracker.position.entry_price
                    hold = round(tracker.hold_minutes, 1)
                    ok, _ = order_mgr.place_sell_order(current_price, reason, qty)
                    if ok:
                        if _is_regular_hours(cfg):
                            _finalize_sell(symbol_id, state, tracker, _position_store, trade_log, logger,
                                          current_price, qty, entry, reason, hold, now)
                            skip_reconcile_until = time.monotonic() + RECONCILE_GRACE_SECONDS
                        else:
                            state.pending_order = {
                                "side": "SELL", "price": current_price, "qty": qty, "reason": reason,
                                "entry_price": entry, "hold_minutes": hold, "pre_qty": qty,
                                "requested_at": time.monotonic(),
                            }
                            logger.info(f"[{symbol_id}] SELL注文送信済み（市場時間外のため約定待ち） | {reason}")
                elif action == "buy":
                    qty = risk_mgr.compute_quantity(current_price, cfg.order.quantity)
                    ok, _ = order_mgr.place_buy_order(current_price, qty)
                    if ok:
                        if _is_regular_hours(cfg):
                            _finalize_buy(symbol_id, state, tracker, _position_store, trade_log, logger,
                                        current_price, qty, reason, tracker.gc_duration_minutes, bar_time, now)
                            skip_reconcile_until = time.monotonic() + RECONCILE_GRACE_SECONDS
                        else:
                            state.pending_order = {
                                "side": "BUY", "price": current_price, "qty": qty, "reason": reason,
                                "gc_duration": tracker.gc_duration_minutes, "bar_time": bar_time.isoformat(),
                                "pre_qty": 0, "requested_at": time.monotonic(),
                            }
                            logger.info(f"[{symbol_id}] BUY注文送信済み（市場時間外のため約定待ち） | {reason}")

            # pending_orderは上の処理で今サイクル中に新規セット/解消され得るため、
            # 表示用ステータスの上書きはここ（サイクルの最後）で確定させる
            if state.pending_order:
                side_label = "買付" if state.pending_order["side"] == "BUY" else "決済"
                state.status = f"{side_label}待ち（市場再開待ち）"

            # 即時売却要求を素早く拾えるよう、短い間隔に分けてスリープする
            # （POLL_INTERVAL_SECONDS=60秒だと、そのまま固定sleepしては
            #   「即時」売却の意図に反して最大60秒待たされてしまうため）
            elapsed = 0.0
            while elapsed < POLL_INTERVAL_SECONDS:
                if state.stop_event.is_set() or state.force_sell_requested:
                    break
                time.sleep(2)
                elapsed += 2

    except Exception as e:
        logger.error(f"[{symbol_id}] エラー: {e}", exc_info=True)
    finally:
        state.running = False
        state.status = "停止中"
        logger.info(f"[{symbol_id}] 停止完了")
        if order_mgr:
            try:
                order_mgr.disconnect()
            except Exception:
                pass
        logger.removeHandler(handler)


def _to_config(d: dict) -> TradingConfig:
    return TradingConfig(
        symbol=d["symbol"],
        market=d.get("market", "US"),
        macd=MacdConfig(**d.get("macd", {})),
        entry=EntryConfig(**d.get("entry", {})),
        exit=ExitConfig(**d.get("exit", {})),
        order=OrderConfig(**d.get("order", {})),
        risk=RiskConfig(**d.get("risk", {})),
        logging=LoggingConfig(**d.get("logging", {})),
        opend=OpendConfig(**d.get("opend", {})),
        oscillator=OscillatorConfig(**d.get("oscillator", {})),
    )
