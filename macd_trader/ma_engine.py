"""
ma_engine.py
MA_RSI戦略（MACD_KDJ戦略の代替候補）の「トレンド系」担当エンジン。
短期・長期の移動平均線クロスオーバーを、macd_trader/macd_engine.py の
MacdValues と全く同じ形（macd/signal/histogram/is_golden/cross）で返すアダプター。

米国トレーダーの実務慣習に合わせ、用途によって使う移動平均の種類を分ける:
- デイトレード: EMA(9,21) が定番（直近値動きへの反応速度を重視）
- スイング: SMA(20,50) が定番（「ゴールデンクロス/デッドクロス」は慣習的にSMA基準）

signal_tracker.py の update() は "macd > signal" の大小関係しか見ておらず、
trade_engine.py の should_buy/should_sell もMACDの実体（EMA差分であること）には
依存していない（histogramフィルターはオプション、should_sellはmacd_valsを
判定に使っていない）。そのため、この出力をそのままcompute_bar_signals()へ
渡すだけで、コアの売買判定ロジックを一切変更せずにMA戦略を検証できる
（engine_factory.py が trend_indicator="ma" の場合にこのクラスを選択する）。

histogramは、trade_engine.should_buy()の既存フィルター（entry.macd_histogram_min）
をそのまま「短期線と長期線の乖離幅フィルター」として再利用できるよう、
価格に対する乖離率（%）で返す（MACDの絶対値ヒストグラムとは単位が異なる。
銘柄ごとに株価水準が大きく違うため、絶対額では閾値をフェアに揃えられないため）。
"""
from __future__ import annotations

import pandas as pd

from macd_engine import CrossSignal, MacdValues


class MaEngine:
    def __init__(self, fast: int = 9, slow: int = 21, method: str = "ema"):
        if method not in ("ema", "sma"):
            raise ValueError(f"method must be 'ema' or 'sma': {method}")
        self.fast = fast
        self.slow = slow
        self.method = method

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """必須カラム: close"""
        df = df.copy()
        if self.method == "ema":
            ma_fast = df["close"].ewm(span=self.fast, adjust=False).mean()
            ma_slow = df["close"].ewm(span=self.slow, adjust=False).mean()
        else:
            ma_fast = df["close"].rolling(window=self.fast, min_periods=1).mean()
            ma_slow = df["close"].rolling(window=self.slow, min_periods=1).mean()
        df["macd"] = ma_fast            # MacdValues.macd に相当（短期線）
        df["signal"] = ma_slow          # MacdValues.signal に相当（長期線）
        # 乖離率（%）。entry.macd_histogram_min を「乖離幅フィルター」として使うための単位。
        df["histogram"] = (df["macd"] - df["signal"]) / df["close"] * 100
        df["is_golden"] = (df["macd"] > df["signal"]).astype(bool)

        df["cross"] = CrossSignal.NONE.value
        prev_golden = df["is_golden"].shift(1, fill_value=False).astype(bool)
        curr_golden = df["is_golden"]
        df.loc[curr_golden & (~prev_golden), "cross"] = CrossSignal.GOLDEN_CROSS.value
        df.loc[(~curr_golden) & prev_golden, "cross"] = CrossSignal.DEAD_CROSS.value

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
