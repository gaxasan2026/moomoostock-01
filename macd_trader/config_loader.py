"""
config_loader.py
trading_config.yaml を読み込み、バリデーションして返す
"""
import yaml
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MacdConfig:
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9
    timeframe: str = "K_1M"


@dataclass
class EntryConfig:
    gc_duration_minutes: float = 3.0
    macd_histogram_min: float = 0.0
    volume_surge_ratio: float = 1.0
    max_spread_pct: float = 0.1


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


def load_config(path: str = "trading_config.yaml") -> TradingConfig:
    """YAMLファイルを読み込んでTradingConfigを返す"""
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"設定ファイルが見つかりません: {path}")

    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    cfg = TradingConfig(
        symbol=raw.get("symbol", "US.SPCX"),
        market=raw.get("market", "US"),
        macd=MacdConfig(**raw.get("macd", {})),
        entry=EntryConfig(**raw.get("entry", {})),
        exit=ExitConfig(**raw.get("exit", {})),
        order=OrderConfig(**raw.get("order", {})),
        risk=RiskConfig(**raw.get("risk", {})),
        logging=LoggingConfig(**raw.get("logging", {})),
        opend=OpendConfig(**raw.get("opend", {})),
    )

    _validate(cfg)
    return cfg


def _validate(cfg: TradingConfig):
    """基本的なバリデーション"""
    assert cfg.macd.fast_period < cfg.macd.slow_period, \
        "fast_period は slow_period より小さくしてください"
    assert cfg.order.quantity > 0, "quantity は1以上にしてください"
    assert cfg.order.order_type in ("market", "limit"), \
        "order_type は 'market' または 'limit' を指定してください"
    assert cfg.macd.timeframe in (
        "K_1M", "K_3M", "K_5M", "K_15M", "K_30M", "K_60M",
        "K_DAY", "K_WEEK", "K_MON"
    ), f"timeframe が不正です: {cfg.macd.timeframe}"
    if cfg.order.paper_trading is False:
        print("⚠️  警告: paper_trading=false です。本番取引が有効です！")
