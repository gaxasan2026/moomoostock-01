"""
config_loader.py
設定値のデータクラス定義（data/symbols.json から読み込まれる各設定の型）
"""
from dataclasses import dataclass, field


@dataclass
class MacdConfig:
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9
    timeframe: str = "K_1M"
    trend_indicator: str = "macd"  # "macd" | "ma"。engine_factory.build_trend_engine()が参照する。
                                    # "ma"の場合、fast_period/slow_periodをそのままMA期間として使う
                                    # （signal_periodは未使用）
    ma_method: str = "ema"  # "ema" | "sma"。trend_indicator="ma"の場合のみ使用


@dataclass
class EntryConfig:
    gc_duration_minutes: float = 3.0
    macd_histogram_min: float = 0.0
    volume_surge_ratio: float = 1.0
    max_spread_pct: float = 0.1
    trading_hours_start: str = ""  # "HH:MM"形式。空文字なら時間帯制限なし
    trading_hours_end: str = ""    # "HH:MM"形式。空文字なら時間帯制限なし
    kdj_max_d: float = 0.0  # オシレータ追加確認フィルター（KDJ/RSI共通）。0で無効。
                            # >0の場合、エントリー時にオシレータの値がこの値未満
                            # （KDJなら%D、RSIならRSI値。売られすぎ圏からの回復）
                            # かつ %K > %D（上向き転換。RSIでは常に満たされる）であることも要求する


@dataclass
class ExitConfig:
    peak_drop_pct: float = 1.5
    peak_drop_duration_minutes: float = 5.0
    peak_confirmation_bars: int = 3
    dead_cross: bool = True
    dc_duration_minutes: float = 0.0
    take_profit_pct: float = 3.0
    stop_loss_pct: float = 1.5
    max_hold_minutes: float = 60.0


@dataclass
class OrderConfig:
    quantity: int = 5
    order_type: str = "market"
    limit_offset_pct: float = 0.02
    paper_trading: bool = True
    mock_data: bool = False


@dataclass
class RiskConfig:
    max_daily_trades: int = 10
    max_daily_loss_pct: float = 3.0
    cooldown_minutes: float = 5.0
    max_position_value: float = 0.0


@dataclass
class LoggingConfig:
    level: str = "INFO"
    save_trade_log: bool = True
    trade_log_path: str = "logs/trades.csv"
    show_realtime_price: bool = True


@dataclass
class OpendConfig:
    host: str = "127.0.0.1"
    port: int = 11111


@dataclass
class OscillatorConfig:
    indicator: str = "kdj"  # "kdj" | "rsi"。engine_factory.build_oscillator_engine()が参照する
    rsi_period: int = 14    # indicator="rsi"の場合のみ使用


@dataclass
class TradingConfig:
    symbol: str = "US.SPCX"
    market: str = "US"
    macd: MacdConfig = field(default_factory=MacdConfig)
    entry: EntryConfig = field(default_factory=EntryConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    order: OrderConfig = field(default_factory=OrderConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    opend: OpendConfig = field(default_factory=OpendConfig)
    oscillator: OscillatorConfig = field(default_factory=OscillatorConfig)


