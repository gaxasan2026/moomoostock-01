"""
macd_engine.py
pandas を使ってMACDを計算し、ゴールデンクロス/デッドクロスを検出する
"""
import pandas as pd
import numpy as np
from dataclasses import dataclass
from enum import Enum


class CrossSignal(Enum):
    NONE = "none"
    GOLDEN_CROSS = "golden_cross"   # GC: MACDがシグナルを上抜け
    DEAD_CROSS = "dead_cross"       # DC: MACDがシグナルを下抜け


@dataclass
class MacdValues:
    macd: float        # MACDライン
    signal: float      # シグナルライン
    histogram: float   # ヒストグラム（MACD - Signal）
    cross: CrossSignal # 直前との比較でのクロス判定
    is_golden: bool    # True=MACDがシグナルより上
    timestamp: pd.Timestamp = None


class MacdEngine:
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self.fast = fast
        self.slow = slow
        self.signal_period = signal
        self._prev_is_golden: bool | None = None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        OHLCV DataFrameからMACD関連の列を追加して返す
        必須カラム: close
        """
        df = df.copy()
        ema_fast = df["close"].ewm(span=self.fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=self.slow, adjust=False).mean()
        df["macd"] = ema_fast - ema_slow
        df["signal"] = df["macd"].ewm(span=self.signal_period, adjust=False).mean()
        df["histogram"] = df["macd"] - df["signal"]
        df["is_golden"] = (df["macd"] > df["signal"]).astype(bool)

        # クロス検出
        df["cross"] = CrossSignal.NONE.value
        prev_golden = df["is_golden"].shift(1, fill_value=False).astype(bool)
        curr_golden = df["is_golden"]
        df.loc[curr_golden & (~prev_golden), "cross"] = CrossSignal.GOLDEN_CROSS.value
        df.loc[(~curr_golden) & prev_golden, "cross"] = CrossSignal.DEAD_CROSS.value

        return df

    def get_latest(self, df: pd.DataFrame) -> MacdValues:
        """DataFrameの最新行のMACDValueを返す"""
        row = df.iloc[-1]
        cross_str = row.get("cross", CrossSignal.NONE.value)
        cross = CrossSignal(cross_str) if cross_str else CrossSignal.NONE

        return MacdValues(
            macd=float(row["macd"]),
            signal=float(row["signal"]),
            histogram=float(row["histogram"]),
            cross=cross,
            is_golden=bool(row["is_golden"]),
            timestamp=row.get("time_key", None),
        )

    def detect_cross(self, prev_macd: float, prev_signal: float,
                     curr_macd: float, curr_signal: float) -> CrossSignal:
        """2点のMACD/Signal値からクロスを検出する（差分更新用）"""
        prev_golden = prev_macd > prev_signal
        curr_golden = curr_macd > curr_signal
        if not prev_golden and curr_golden:
            return CrossSignal.GOLDEN_CROSS
        if prev_golden and not curr_golden:
            return CrossSignal.DEAD_CROSS
        return CrossSignal.NONE
