"""
kdj_engine.py
KDJ（ストキャスティクス系）指標を計算する。
MACDエントリー時の追加確認フィルタとして使うための補助モジュール
（billpwchan/futu_algo の KDJ_Cross.py と同じ計算式を採用）。
"""
import pandas as pd
from dataclasses import dataclass


@dataclass
class KdjValues:
    k: float
    d: float
    j: float


class KdjEngine:
    def __init__(self, fast_k: int = 9, slow_k: int = 3, slow_d: int = 3):
        self.fast_k = fast_k
        self.slow_k = slow_k
        self.slow_d = slow_d

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        OHLCV DataFrameからKDJ関連の列を追加して返す
        必須カラム: high, low, close
        """
        df = df.copy()
        low_min = df["low"].rolling(window=self.fast_k, min_periods=1).min()
        high_max = df["high"].rolling(window=self.fast_k, min_periods=1).max()
        denom = (high_max - low_min).astype(float)
        rsv = pd.Series(50.0, index=df.index)  # 高値=安値（値動きなし）の場合は中立値
        valid = denom > 0
        rsv[valid] = (df["close"][valid] - low_min[valid]) / denom[valid] * 100

        df["kdj_k"] = rsv.ewm(com=self.slow_k - 1, adjust=False).mean()
        df["kdj_d"] = df["kdj_k"].ewm(com=self.slow_d - 1, adjust=False).mean()
        df["kdj_j"] = 3 * df["kdj_k"] - 2 * df["kdj_d"]
        return df

    def get_latest(self, df: pd.DataFrame) -> KdjValues:
        row = df.iloc[-1]
        return KdjValues(k=float(row["kdj_k"]), d=float(row["kdj_d"]), j=float(row["kdj_j"]))
