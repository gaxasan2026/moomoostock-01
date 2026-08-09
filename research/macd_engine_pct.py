"""
macd_engine_pct.py
macd_trader/macd_engine.py の薄いラッパー。histogramだけを価格に対する
乖離率（%）に正規化して返す（銘柄ごとの株価水準差を吸収するため）。

MA_RSI側のヒストグラムフィルター（ma_engine.pyのhistogram、乖離率%）と
同じ単位に揃え、entry.macd_histogram_min によるノイズ除去フィルターを
MACD_KDJ側にもフェアに適用できるようにするための研究用アダプター。
macd/signal/is_golden/cross は本物のMacdEngineの計算をそのまま使う
（macd_trader/macd_engine.py 自体は一切変更しない）。
"""
from __future__ import annotations

import pandas as pd

from macd_engine import MacdEngine, MacdValues, CrossSignal


class MacdEnginePct:
    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9):
        self._inner = MacdEngine(fast, slow, signal)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self._inner.calculate(df)
        df = df.copy()
        df["histogram"] = df["histogram"] / df["close"] * 100
        return df

    def get_latest(self, df: pd.DataFrame) -> MacdValues:
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
