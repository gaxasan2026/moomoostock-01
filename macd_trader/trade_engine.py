"""
trade_engine.py
エントリー・エグジット条件をYAML設定に基づいて評価する
"""
import logging
from config_loader import EntryConfig, ExitConfig
from signal_tracker import SignalTracker
from risk_manager import RiskManager
from macd_engine import MacdValues

logger = logging.getLogger(__name__)


class TradeEngine:
    def __init__(self, entry_cfg: EntryConfig, exit_cfg: ExitConfig,
                 risk_mgr: RiskManager):
        self.entry = entry_cfg
        self.exit = exit_cfg
        self.risk = risk_mgr

    # ─── エントリー判定 ──────────────────────────────────────────

    def should_buy(self, tracker: SignalTracker, macd_vals: MacdValues,
                   volume_ratio: float = 1.0) -> tuple[bool, str]:
        """
        買いエントリー条件を全てチェックする
        Returns: (買うべきか, 理由/状況メッセージ)
        """
        if tracker.has_position:
            return False, "既にポジションあり"

        # リスクチェック
        ok, reason = self.risk.can_trade(tracker)
        if not ok:
            return False, reason

        # GC状態か
        if not tracker.is_golden_cross:
            return False, f"GC未発生 (DC継続: {tracker.dc_duration_minutes:.1f}分)"

        # GC継続時間
        if self.entry.gc_duration_minutes > 0:
            if tracker.gc_duration_minutes < self.entry.gc_duration_minutes:
                remaining = self.entry.gc_duration_minutes - tracker.gc_duration_minutes
                return False, f"GC継続時間不足 ({tracker.gc_duration_minutes:.1f}/{self.entry.gc_duration_minutes}分, あと{remaining:.1f}分)"

        # MACDヒストグラムの強さフィルター
        if self.entry.macd_histogram_min > 0:
            if macd_vals.histogram < self.entry.macd_histogram_min:
                return False, f"ヒストグラム不足 ({macd_vals.histogram:.4f} < {self.entry.macd_histogram_min})"

        # 出来高フィルター
        if self.entry.volume_surge_ratio > 1.0:
            if volume_ratio < self.entry.volume_surge_ratio:
                return False, f"出来高不足 ({volume_ratio:.2f}x < {self.entry.volume_surge_ratio}x)"

        return True, f"✅ エントリー条件成立 GC継続{tracker.gc_duration_minutes:.1f}分"

    # ─── エグジット判定 ──────────────────────────────────────────

    def should_sell(self, tracker: SignalTracker,
                    macd_vals: MacdValues) -> tuple[bool, str]:
        """
        売りエグジット条件をOR評価する
        Returns: (売るべきか, 理由)
        """
        if not tracker.has_position:
            return False, "ポジションなし"

        # 条件① ピーク確定 + 下落率
        if self.exit.peak_drop_pct > 0 and tracker.peak_confirmed:
            if tracker.drop_from_peak_pct >= self.exit.peak_drop_pct:
                return True, (
                    f"ピーク下落率 {tracker.drop_from_peak_pct:.2f}% "
                    f">= {self.exit.peak_drop_pct}%"
                )

        # 条件① ピーク確定 + 下落継続時間
        if self.exit.peak_drop_duration_minutes > 0 and tracker.peak_confirmed:
            if tracker.peak_drop_duration_minutes >= self.exit.peak_drop_duration_minutes:
                return True, (
                    f"ピーク下落継続 {tracker.peak_drop_duration_minutes:.1f}分 "
                    f">= {self.exit.peak_drop_duration_minutes}分"
                )

        # 条件② デッドクロス
        if self.exit.dead_cross and not tracker.is_golden_cross:
            if self.exit.dc_duration_minutes <= 0:
                return True, "デッドクロス発生"
            if tracker.dc_duration_minutes >= self.exit.dc_duration_minutes:
                return True, f"DC継続 {tracker.dc_duration_minutes:.1f}分"

        # 条件③ 利確
        if self.exit.take_profit_pct > 0:
            if tracker.gain_pct >= self.exit.take_profit_pct:
                return True, f"利確 +{tracker.gain_pct:.2f}% >= +{self.exit.take_profit_pct}%"

        # 条件③ 損切り
        if self.exit.stop_loss_pct > 0:
            if tracker.gain_pct <= -self.exit.stop_loss_pct:
                return True, f"損切り {tracker.gain_pct:.2f}% <= -{self.exit.stop_loss_pct}%"

        # 条件④ 時間切れ
        if self.exit.max_hold_minutes > 0:
            if tracker.hold_minutes >= self.exit.max_hold_minutes:
                return True, f"保有時間切れ {tracker.hold_minutes:.1f}分"

        return False, (
            f"保有中 | 含み益: {tracker.gain_pct:+.2f}% | "
            f"ピーク下落: {tracker.drop_from_peak_pct:.2f}% | "
            f"保有: {tracker.hold_minutes:.1f}分"
        )

    def log_status(self, tracker: SignalTracker, macd_vals: MacdValues):
        """現在の状態をINFOログに出力（定期モニタリング用）"""
        if tracker.has_position:
            _, sell_reason = self.should_sell(tracker, macd_vals)
            logger.info(
                f"[保有中] 価格={tracker.current_price:.4f} | "
                f"MACD={macd_vals.macd:.4f} Signal={macd_vals.signal:.4f} "
                f"Hist={macd_vals.histogram:.4f} | {sell_reason}"
            )
        else:
            gc_status = (
                f"GC継続{tracker.gc_duration_minutes:.1f}分"
                if tracker.is_golden_cross
                else f"DC継続{tracker.dc_duration_minutes:.1f}分"
            )
            logger.info(
                f"[待機中] 価格={tracker.current_price:.4f} | "
                f"MACD={macd_vals.macd:.4f} Signal={macd_vals.signal:.4f} "
                f"Hist={macd_vals.histogram:.4f} | {gc_status}"
            )
