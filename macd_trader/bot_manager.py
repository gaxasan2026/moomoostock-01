"""
bot_manager.py
複数銘柄のトレードBotをスレッドで管理する
"""
import collections
import logging
import threading
import time
from datetime import datetime
from typing import Optional

from config_loader import (
    TradingConfig, MacdConfig, EntryConfig, ExitConfig,
    OrderConfig, RiskConfig, LoggingConfig, OpendConfig,
)
from macd_engine import MacdEngine
from signal_tracker import SignalTracker
from trade_engine import TradeEngine
from order_manager import OrderManager
from risk_manager import RiskManager
from trade_logger import TradeLogger

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
        self.gain_pct: float = 0.0
        self.daily_trades: int = 0
        self.daily_pnl: float = 0.0
        self.gc_duration: float = 0.0
        self.macd_hist: float = 0.0
        self.trades: list = []

    def to_dict(self) -> dict:
        return {
            "symbol_id": self.symbol_id,
            "running": self.running,
            "status": self.status,
            "current_price": self.current_price,
            "has_position": self.has_position,
            "entry_price": self.entry_price,
            "gain_pct": self.gain_pct,
            "daily_trades": self.daily_trades,
            "daily_pnl": self.daily_pnl,
            "gc_duration": self.gc_duration,
            "macd_hist": self.macd_hist,
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
            name=f"bot-{symbol_id}",
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


# ─── ボットループ（スレッドで実行） ──────────────────────────────

def _bot_loop(symbol_id: str, cfg_dict: dict, state: BotState):
    logger = logging.getLogger(f"bot.{symbol_id}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = _QueueLogHandler(symbol_id)
    logger.addHandler(handler)

    order_mgr = None
    try:
        cfg = _to_config(cfg_dict)
        state.status = "起動中"
        logger.info(f"[{symbol_id}] 起動 paper={cfg.order.paper_trading}")

        macd_engine = MacdEngine(cfg.macd.fast_period, cfg.macd.slow_period, cfg.macd.signal_period)
        tracker = SignalTracker(peak_confirmation_bars=cfg.exit.peak_confirmation_bars)
        risk_mgr = RiskManager(cfg.risk)
        trade_engine = TradeEngine(cfg.entry, cfg.exit, risk_mgr)
        order_mgr = OrderManager(cfg.order, cfg.opend, cfg.symbol, cfg.market, cfg.macd.timeframe, logger=logger)
        trade_log = TradeLogger(cfg.logging.trade_log_path, cfg.logging.save_trade_log)

        order_mgr.connect()
        logger.info(f"[{symbol_id}] 監視開始")
        state.status = "監視中"
        last_bar_time = None

        while not state.stop_event.is_set():
            df = order_mgr.get_kline_data(kline_num=200)
            if df is None or len(df) < 40:
                bar_count = 0 if df is None else len(df)
                logger.warning(f"[{symbol_id}] K線不足 ({bar_count}本) — 5秒後リトライ")
                time.sleep(5)
                continue

            df = macd_engine.calculate(df)
            macd_vals = macd_engine.get_latest(df)

            current_bar_time = df["time_key"].iloc[-1]
            if hasattr(current_bar_time, "to_pydatetime"):
                current_bar_time = current_bar_time.to_pydatetime()
            is_new_bar = (last_bar_time is None or current_bar_time != last_bar_time)

            current_price = float(df["close"].iloc[-1])
            now = datetime.now()
            bar_time = current_bar_time if isinstance(current_bar_time, datetime) else now

            volume_ratio = 1.0
            if len(df) >= 20 and "volume" in df.columns:
                avg = df["volume"].iloc[-21:-1].mean()
                curr = float(df["volume"].iloc[-1])
                if avg > 0:
                    volume_ratio = curr / avg

            tracker.update(macd=macd_vals.macd, signal=macd_vals.signal,
                           current_price=current_price, timestamp=bar_time)

            state.current_price = current_price
            state.has_position = tracker.has_position
            state.entry_price = tracker.position.entry_price if tracker.has_position else 0.0
            state.gain_pct = tracker.gain_pct
            state.daily_trades = tracker.daily_trades
            state.daily_pnl = tracker.daily_realized_pnl
            state.gc_duration = tracker.gc_duration_minutes
            state.macd_hist = macd_vals.histogram

            if tracker.has_position:
                state.status = f"保有中 ({tracker.gain_pct:+.2f}%)"
            elif tracker.is_golden_cross:
                state.status = f"GC待機 ({tracker.gc_duration_minutes:.1f}分)"
            else:
                state.status = "監視中"

            if is_new_bar:
                last_bar_time = current_bar_time

                if tracker.has_position:
                    sell, reason = trade_engine.should_sell(tracker, macd_vals)
                    if sell:
                        ok, _ = order_mgr.place_sell_order(current_price, reason)
                        if ok:
                            entry = tracker.position.entry_price
                            pnl = (current_price - entry) * cfg.order.quantity
                            pnl_pct = (current_price - entry) / entry * 100
                            hold = round(tracker.hold_minutes, 1)
                            state.trades.insert(0, {
                                "action": "SELL", "symbol": symbol_id,
                                "price": round(current_price, 4),
                                "quantity": cfg.order.quantity,
                                "entry_price": round(entry, 4),
                                "pnl_usd": round(pnl, 2),
                                "pnl_pct": round(pnl_pct, 2),
                                "hold_minutes": hold,
                                "exit_reason": reason,
                                "timestamp": now.strftime("%H:%M:%S"),
                            })
                            trade_log.log_exit(symbol_id, current_price, cfg.order.quantity,
                                               entry, hold, reason, tracker.daily_trades + 1, now)
                            tracker.close_position(current_price, reason)
                            logger.info(f"[{symbol_id}] SELL @ {current_price:.4f} | {reason} | PnL {pnl:+.2f}USD")
                else:
                    buy, reason = trade_engine.should_buy(tracker, macd_vals, volume_ratio)
                    if buy:
                        ok, _ = order_mgr.place_buy_order(current_price)
                        if ok:
                            state.trades.insert(0, {
                                "action": "BUY", "symbol": symbol_id,
                                "price": round(current_price, 4),
                                "quantity": cfg.order.quantity,
                                "entry_price": round(current_price, 4),
                                "pnl_usd": None, "pnl_pct": None,
                                "hold_minutes": 0.0, "exit_reason": "",
                                "timestamp": now.strftime("%H:%M:%S"),
                            })
                            trade_log.log_entry(symbol_id, current_price, cfg.order.quantity,
                                                tracker.gc_duration_minutes, now)
                            tracker.open_position(current_price, cfg.order.quantity, bar_time)
                            logger.info(f"[{symbol_id}] BUY @ {current_price:.4f} | GC {tracker.gc_duration_minutes:.1f}分")

            time.sleep(5)

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
    )
