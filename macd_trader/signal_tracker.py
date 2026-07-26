"""
signal_tracker.py
GC継続時間の計測、ポジション保有中のピーク価格追跡、
各種状態管理を担当する
"""
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
import logging

from macd_engine import CrossSignal

logger = logging.getLogger(__name__)


@dataclass
class PositionInfo:
    """現在のポジション情報"""
    entry_price: float
    entry_time: datetime
    quantity: int
    peak_price: float           # 保有中の最高値
    peak_time: datetime         # 最高値到達時刻
    bars_since_peak: int = 0   # ピーク更新なしのバー数


class SignalTracker:
    """
    GC/DC継続時間の計測とピーク価格追跡を行うクラス
    main_loop から毎バー update() を呼ぶことで状態を更新する
    """

    def __init__(self, peak_confirmation_bars: int = 3):
        self.peak_confirmation_bars = peak_confirmation_bars

        # GC/DC の開始時刻
        self._gc_start_time: Optional[datetime] = None
        self._dc_start_time: Optional[datetime] = None

        # 現在のMACD/Signal値（前回比較用）
        self._prev_macd: Optional[float] = None
        self._prev_signal: Optional[float] = None
        self._current_is_golden: bool = False

        # ポジション
        self.position: Optional[PositionInfo] = None

        # クールダウン
        self._last_trade_time: Optional[datetime] = None

        # 当日の統計
        self._daily_trades: int = 0
        self._daily_realized_pnl: float = 0.0
        self._daily_start_balance: float = 0.0

        # 最新価格（外部から参照用）
        self.current_price: float = 0.0
        self.current_time: datetime = datetime.now()

    # ─── 毎バー呼ぶメインの更新関数 ────────────────────────────

    def update(self, macd: float, signal: float,
               current_price: float, timestamp: datetime):
        """
        毎バー（or ティック）呼び出す。
        GC/DC継続時間とピーク価格を更新する。
        """
        is_new_session = (
            self.current_time is not None
            and timestamp.date() != self.current_time.date()
        )

        self.current_price = current_price
        self.current_time = timestamp

        is_golden = macd > signal

        # ─ GC/DC 状態の更新 ─
        if is_new_session:
            # 取引セッションの区切り（日付変更）。夜間の市場休止をまたいで
            # GC/DC継続時間を数え続けると無意味な値になるため、起点を当バーにリセットする。
            self._gc_start_time = timestamp if is_golden else None
            self._dc_start_time = None if is_golden else timestamp
            # 日次取引回数・日次損益カウンターもリセットする
            # （max_daily_trades/max_daily_loss_pctが複数日にまたがって
            # 累積し続けると、2日目以降の取引が意図せず抑制されるため）
            self.reset_daily()
        elif is_golden:
            if not self._current_is_golden:
                # DCからGCに転換
                self._gc_start_time = timestamp
                self._dc_start_time = None
                logger.info(f"🟢 ゴールデンクロス発生 @ {current_price:.4f}")
        else:
            if self._current_is_golden:
                # GCからDCに転換
                self._dc_start_time = timestamp
                self._gc_start_time = None
                logger.info(f"🔴 デッドクロス発生 @ {current_price:.4f}")

        self._current_is_golden = is_golden

        # ─ ポジション保有中のピーク更新 ─
        if self.position is not None:
            if current_price > self.position.peak_price:
                self.position.peak_price = current_price
                self.position.peak_time = timestamp
                self.position.bars_since_peak = 0
                logger.debug(f"📈 ピーク更新: {current_price:.4f}")
            else:
                self.position.bars_since_peak += 1

        self._prev_macd = macd
        self._prev_signal = signal

    # ─── GC/DC 状態の参照 ───────────────────────────────────────

    @property
    def is_golden_cross(self) -> bool:
        return self._current_is_golden

    @property
    def gc_duration_minutes(self) -> float:
        """GCが継続している時間（分）"""
        if self._gc_start_time is None:
            return 0.0
        return (self.current_time - self._gc_start_time).total_seconds() / 60.0

    @property
    def dc_duration_minutes(self) -> float:
        """DCが継続している時間（分）"""
        if self._dc_start_time is None:
            return 0.0
        return (self.current_time - self._dc_start_time).total_seconds() / 60.0

    # ─── ポジション情報の参照 ────────────────────────────────────

    @property
    def has_position(self) -> bool:
        return self.position is not None

    @property
    def drop_from_peak_pct(self) -> float:
        """ピークからの下落率（%）"""
        if self.position is None or self.position.peak_price <= 0:
            return 0.0
        return (self.position.peak_price - self.current_price) / self.position.peak_price * 100.0

    @property
    def peak_drop_duration_minutes(self) -> float:
        """ピーク到達から経過した時間（分）"""
        if self.position is None:
            return 0.0
        return (self.current_time - self.position.peak_time).total_seconds() / 60.0

    @property
    def peak_confirmed(self) -> bool:
        """ピーク確定判定（N本連続で高値更新なし）"""
        if self.position is None:
            return False
        return self.position.bars_since_peak >= self.peak_confirmation_bars

    @property
    def gain_pct(self) -> float:
        """現在の含み益率（%）"""
        if self.position is None:
            return 0.0
        return (self.current_price - self.position.entry_price) / self.position.entry_price * 100.0

    @property
    def hold_minutes(self) -> float:
        """ポジション保有時間（分）"""
        if self.position is None:
            return 0.0
        return (self.current_time - self.position.entry_time).total_seconds() / 60.0

    # ─── ポジション操作 ──────────────────────────────────────────

    def open_position(self, price: float, quantity: int, timestamp: datetime):
        self.position = PositionInfo(
            entry_price=price,
            entry_time=timestamp,
            quantity=quantity,
            peak_price=price,
            peak_time=timestamp,
        )
        logger.info(f"📥 ポジションオープン: {quantity}株 @ {price:.4f}")

    def close_position(self, price: float, reason: str):
        if self.position is None:
            return
        pnl = (price - self.position.entry_price) * self.position.quantity
        self._daily_realized_pnl += pnl
        self._daily_trades += 1
        self._last_trade_time = self.current_time
        logger.info(
            f"📤 ポジションクローズ: @ {price:.4f} "
            f"理由={reason} PnL={pnl:+.2f}USD "
            f"(本日: {self._daily_trades}回, {self._daily_realized_pnl:+.2f}USD)"
        )
        self.position = None

    # ─── リスク管理用 ────────────────────────────────────────────

    @property
    def daily_trades(self) -> int:
        return self._daily_trades

    @property
    def daily_realized_pnl(self) -> float:
        return self._daily_realized_pnl

    def cooldown_remaining_minutes(self) -> float:
        """クールダウン残り時間（分）、0以下なら取引可能"""
        if self._last_trade_time is None:
            return 0.0
        # cooldownはRiskManagerで判定するため、ここでは経過時間だけ返す
        elapsed = (self.current_time - self._last_trade_time).total_seconds() / 60.0
        return elapsed

    def reset_daily(self):
        """日次リセット（毎朝0時に呼ぶ）"""
        self._daily_trades = 0
        self._daily_realized_pnl = 0.0
        logger.info("📅 日次リセット完了")
