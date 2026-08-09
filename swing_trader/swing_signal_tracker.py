"""
swing_signal_tracker.py
macd_trader/signal_tracker.py の SignalTracker を最小限フォークしたもの。

唯一の差分: update() が日付境界（is_new_session）でGC/DC継続時間を
強制リセットしない。デイトレード版は「夜間の市場休止をまたいでカウントし
続けると無意味」という前提でリセットしていたが、スイングトレードでは
複数日にまたがるトレンドの継続性そのものが判定材料なので、通常の
GC⇄DC遷移検知ロジックだけに任せる（日をまたいでも遷移がなければ
継続時間は伸び続ける）。日次取引回数・日次損益カウンター（reset_daily）は
デイトレード版と同様に日付変更時にリセットする（スイングでは発生頻度が
低く実害がないため、あえて別概念を作らずそのまま踏襲する）。

それ以外の public インターフェース（プロパティ・メソッド）は
SignalTracker と完全に同一。trade_engine.py / risk_manager.py / bar_signals.py は
いずれも SignalTracker を型ヒントとしてのみ参照するダックタイピングのため、
このクラスをそのまま代わりに渡して動作する。
"""
from datetime import datetime
from typing import Optional
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "macd_trader"))
from signal_tracker import PositionInfo  # noqa: E402  (日付ロジックを含まないため再利用)

logger = logging.getLogger(__name__)


class SwingSignalTracker:
    """
    GC/DC継続時間の計測とピーク価格追跡を行うクラス（スイングトレード版）。
    signal_tracker.SignalTracker と同じ契約で動作する。
    """

    def __init__(self, peak_confirmation_bars: int = 2):
        self.peak_confirmation_bars = peak_confirmation_bars

        self._gc_start_time: Optional[datetime] = None
        self._dc_start_time: Optional[datetime] = None

        self._prev_macd: Optional[float] = None
        self._prev_signal: Optional[float] = None
        self._current_is_golden: bool = False

        self.position: Optional[PositionInfo] = None

        self._last_trade_time: Optional[datetime] = None

        self._daily_trades: int = 0
        self._daily_realized_pnl: float = 0.0
        self._daily_start_balance: float = 0.0

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

        # ─ 日次カウンターのみリセット（GC/DC継続時間は日をまたいでも維持する） ─
        if is_new_session:
            self.reset_daily()

        # ─ GC/DC 状態の更新（通常の遷移検知のみ。日付境界による特別扱いはしない） ─
        if is_golden:
            if not self._current_is_golden:
                self._gc_start_time = timestamp
                self._dc_start_time = None
                logger.info(f"🟢 ゴールデンクロス発生 @ {current_price:.4f}")
        else:
            if self._current_is_golden:
                self._dc_start_time = timestamp
                self._gc_start_time = None
                logger.info(f"🔴 デッドクロス発生 @ {current_price:.4f}")

        self._current_is_golden = is_golden

        # ─ ポジション保有中のピーク更新 ─
        # entry_timeより前のバーは対象外にする。restore_position()後にウォームアップで
        # 過去バーを再生するケース（起動時のポジション復元）で、エントリーより前の
        # 過去の高値がピークとして誤って採用されるのを防ぐため
        # （ライブのティック単位更新では timestamp は常に entry_time 以降なので影響なし）。
        if self.position is not None and timestamp >= self.position.entry_time:
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
        """GCが継続している時間（分）。日をまたいでも蓄積され続ける"""
        if self._gc_start_time is None:
            return 0.0
        return (self.current_time - self._gc_start_time).total_seconds() / 60.0

    @property
    def dc_duration_minutes(self) -> float:
        """DCが継続している時間（分）。日をまたいでも蓄積され続ける"""
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

    def restore_position(self, entry_price: float, entry_time: datetime,
                          quantity: int, peak_price: float, peak_time: datetime,
                          bars_since_peak: int = 0):
        """
        永続化された状態からポジションを復元する（bot起動時のみ使用）。
        open_position()と異なり「今エントリーした」わけではないため、
        日次取引回数・クールダウン等には一切影響しない。
        """
        self.position = PositionInfo(
            entry_price=entry_price,
            entry_time=entry_time,
            quantity=quantity,
            peak_price=peak_price,
            peak_time=peak_time,
            bars_since_peak=bars_since_peak,
        )
        logger.info(f"📥 ポジション復元: {quantity}株 @ {entry_price:.4f}")

    def force_adjust_quantity(self, real_qty: int) -> Optional[int]:
        """
        実口座の保有数量に合わせて強制的に補正する。
        外部（アプリ等）での売却・全売却を検知した場合にのみ呼ぶこと
        （買い増しの検知では呼ばない — 増加分はTrader管理対象に含めない設計のため）。

        real_qty <= 0 の場合はポジションを強制クローズする。この際、実際の売却価格が
        分からないため損益計算は行わない（bot自身の決済ではなく、外部要因による調整のため）。

        Returns: 補正前の数量（呼び出し側がログ・永続化更新に使う）。ポジションが無ければNone。
        """
        if self.position is None:
            return None
        old_qty = self.position.quantity
        if real_qty == old_qty:
            return None
        if real_qty <= 0:
            self.position = None
        else:
            self.position.quantity = real_qty
        return old_qty

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
        elapsed = (self.current_time - self._last_trade_time).total_seconds() / 60.0
        return elapsed

    def reset_daily(self):
        """日次リセット（日付変更時に呼ぶ）"""
        self._daily_trades = 0
        self._daily_realized_pnl = 0.0
        logger.info("📅 日次リセット完了")
