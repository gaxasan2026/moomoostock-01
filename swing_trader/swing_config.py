"""
swing_config.py
スイングトレードbot向けの初期設定値。

macd_trader/symbol_store.py の DEFAULT_CONFIG と同じキー構造を踏襲。

2026-07-30、実データによる較正の結果、K_60M・短期サイクル設定に更新した
（swing_trader/calibrate_gc_duration.py, analyze_gc_runs.py, test_short_cycle.py,
calibrate_short_cycle_yearly.py での検証結果に基づく）。根拠:
- 日足(K_DAY)はゴールデンクロス自体の発生が年10回程度と少なく、銘柄・年によって
  最適なgc_duration_minutesが割れ、一貫した較正結果が得られなかった
- K_60M・同一セッション内エントリー(gc_duration_minutes=120分)・
  2営業日で強制決済(max_hold_minutes=2880分)という短期サイクル設定は、
  6銘柄(COHR/TSLA/QQQ/NVDA/MU/MSFT)・2023〜2026年の全年度で合計プラス、
  かつ2023〜2025のフル3年間はUS.MU（この検証で繰り返し支配的だった1銘柄）を
  除いても一貫してプラスという、このセッションで唯一頑健性が確認できた設定
- 2026年（部分年）分だけは依然MU依存が残る点は既知の限界として認識しておくこと

この変更はSWING_DEFAULT_CONFIGの変更であり、既に登録済みの銘柄には影響しない
（swing_symbol_store.py の add() が新規登録時にのみ適用するため）。
"""

SWING_DEFAULT_CONFIG: dict = {
    "market": "US",
    "macd": {"fast_period": 12, "slow_period": 26, "signal_period": 9, "timeframe": "K_60M",
             "trend_indicator": "macd", "ma_method": "ema"},
    "entry": {
        "gc_duration_minutes": 120.0,  # 同一セッション内（2時間）でのエントリーを許容
        "macd_histogram_min": 0.0,
        "volume_surge_ratio": 1.0,
        "max_spread_pct": 0.1,
        "trading_hours_start": "",
        "trading_hours_end": "",
        "kdj_max_d": 0.0,
    },
    "oscillator": {"indicator": "kdj", "rsi_period": 14},
    "exit": {
        "peak_drop_pct": 5.0,
        "peak_drop_duration_minutes": 120.0,  # 同一セッション内（2時間）で確定
        "peak_confirmation_bars": 2,
        "dead_cross": True,
        "dc_duration_minutes": 0.0,
        "take_profit_pct": 10.0,
        "stop_loss_pct": 5.0,
        "max_hold_minutes": 2880.0,  # 2営業日で強制決済
    },
    "order": {"quantity": 5, "order_type": "market", "limit_offset_pct": 0.02,
              "paper_trading": True, "mock_data": False},
    "risk": {"max_daily_trades": 3, "max_daily_loss_pct": 5.0,
             "cooldown_minutes": 5.0, "max_position_value": 0.0},
    "logging": {"level": "INFO", "save_trade_log": True, "show_realtime_price": True},
    "opend": {"host": "127.0.0.1", "port": 11111},
}
