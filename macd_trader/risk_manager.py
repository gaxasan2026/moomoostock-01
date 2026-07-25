"""
risk_manager.py
取引前のリスクチェックを一元管理する
"""
import logging
from config_loader import RiskConfig
from signal_tracker import SignalTracker

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, cfg: RiskConfig, initial_balance: float = 0.0):
        self.cfg = cfg
        self.initial_balance = initial_balance

    def can_trade(self, tracker: SignalTracker) -> tuple[bool, str]:
        """
        取引可能かどうかをチェックする
        Returns: (可能か, 不可の場合の理由)
        """
        # 1. 日次取引回数上限
        if self.cfg.max_daily_trades > 0:
            if tracker.daily_trades >= self.cfg.max_daily_trades:
                return False, f"日次取引上限({self.cfg.max_daily_trades}回)に達しました"

        # 2. 日次損失上限
        if self.cfg.max_daily_loss_pct > 0 and self.initial_balance > 0:
            loss_pct = abs(tracker.daily_realized_pnl) / self.initial_balance * 100
            if tracker.daily_realized_pnl < 0 and loss_pct >= self.cfg.max_daily_loss_pct:
                return False, f"日次損失上限({self.cfg.max_daily_loss_pct}%)に達しました"

        # 3. クールダウン
        if self.cfg.cooldown_minutes > 0:
            elapsed = tracker.cooldown_remaining_minutes()
            if 0 < elapsed < self.cfg.cooldown_minutes:
                remaining = self.cfg.cooldown_minutes - elapsed
                return False, f"クールダウン中 (あと{remaining:.1f}分)"

        return True, ""

    def compute_quantity(self, price: float, default_quantity: int) -> int:
        """
        max_position_valueが設定されていれば、現在価格から株数を自動計算する
        （銘柄間で株価水準に関わらず想定元本を揃えるため）。
        0（未設定）の場合は default_quantity（固定株数）をそのまま使う。
        """
        if self.cfg.max_position_value > 0 and price > 0:
            return max(1, int(self.cfg.max_position_value // price))
        return default_quantity

    def check_position_value(self, price: float, quantity: int) -> tuple[bool, str]:
        """ポジション金額の上限チェック"""
        if self.cfg.max_position_value > 0:
            value = price * quantity
            if value > self.cfg.max_position_value:
                return False, f"ポジション上限超過: {value:.0f}USD > {self.cfg.max_position_value:.0f}USD"
        return True, ""
