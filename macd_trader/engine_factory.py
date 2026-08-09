"""
engine_factory.py
設定（MacdConfig.trend_indicator / OscillatorConfig.indicator）に応じて、
トレンド系・オシレータ系のエンジンを選択して構築する。

bar_signals.compute_bar_signals() は macd_engine/kdj_engine を型ではなく
「calculate()/get_latest()を持つオブジェクト」としてダックタイピングで受け取っており、
signal_tracker.py・trade_engine.py もMACD/KDJの実体には依存していない
（research/ma_engine.py・research/rsi_engine.py での検証で確認済み。両モジュールは
2026-08時点でmacd_trader/ma_engine.py・macd_trader/rsi_engine.pyへ本番移動した）。
そのため、この工場関数を呼び出し側（bot_manager.py・swing_bot_manager.py・
backtest.py・swing_backtest.py）で使うだけで、コアの売買判定ロジックを一切
変更せずに指標の組み合わせを切り替えられる。

現時点で選べる組み合わせ:
- トレンド系: "macd"（MacdEngine、既定） | "ma"（MaEngine、EMA/SMAクロスオーバー）
- オシレータ系: "kdj"（KdjEngine、既定） | "rsi"（RsiEngine）
"""
from __future__ import annotations

from config_loader import MacdConfig, OscillatorConfig
from macd_engine import MacdEngine
from kdj_engine import KdjEngine
from ma_engine import MaEngine
from rsi_engine import RsiEngine


def build_trend_engine(macd_cfg: MacdConfig):
    if macd_cfg.trend_indicator == "ma":
        return MaEngine(macd_cfg.fast_period, macd_cfg.slow_period, macd_cfg.ma_method)
    return MacdEngine(macd_cfg.fast_period, macd_cfg.slow_period, macd_cfg.signal_period)


def build_oscillator_engine(osc_cfg: OscillatorConfig):
    if osc_cfg.indicator == "rsi":
        return RsiEngine(osc_cfg.rsi_period)
    return KdjEngine()
