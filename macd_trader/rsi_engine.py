"""
rsi_engine.py
MA_RSI戦略（MACD_KDJ戦略の代替候補）の「オシレータ系」担当エンジン。
RSIを、macd_trader/kdj_engine.py の KdjValues と同じ形（k/d/j）で返すアダプター。

trade_engine.should_buy() のKDJ追加確認フィルターは以下の2条件をORでなくANDで見ている:
  1. kdj_vals.d < entry.kdj_max_d （オシレータが「高すぎない」＝過熱していない）
  2. kdj_vals.k > kdj_vals.d （直近で下から上に転換した＝底打ちして戻り始めた）
KDJのK/Dのような2本線の交差概念はRSI（1本線）には無いため、d=RSI値・
k=RSI値+微小値 として2番目の条件を実質的に無効化（常に満たす）し、
1番目の条件（閾値）だけを実効フィルターとして使う。j値は未使用のため0を返す。
（engine_factory.py が oscillator.indicator="rsi" の場合にこのクラスを選択する）
"""
from __future__ import annotations

import pandas as pd

from kdj_engine import KdjValues

_K_EPSILON = 0.01  # kdj_vals.k > kdj_vals.d を常に満たすための微小オフセット


class RsiEngine:
    def __init__(self, period: int = 14):
        self.period = period

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """必須カラム: close"""
        df = df.copy()
        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / self.period, adjust=False, min_periods=self.period).mean()
        avg_loss = loss.ewm(alpha=1 / self.period, adjust=False, min_periods=self.period).mean()
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        rsi = 100 - (100 / (1 + rs))
        # 平均下落幅が0（一方的な上昇）の場合はRSI=100とする
        rsi = rsi.where(avg_loss > 0, 100.0)
        df["rsi"] = rsi.fillna(50.0)  # ウォームアップ不足期間は中立値
        return df

    def get_latest(self, df: pd.DataFrame) -> KdjValues:
        rsi = float(df.iloc[-1]["rsi"])
        return KdjValues(k=rsi + _K_EPSILON, d=rsi, j=0.0)
