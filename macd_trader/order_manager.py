"""
order_manager.py
moomoo OpenAPI を使った発注・ポジション照会のラッパー
paper_trading=True の場合はモック動作でAPIを呼ばない
"""
import logging
from datetime import datetime
from typing import Optional
from config_loader import OrderConfig, OpendConfig

class OrderManager:
    def __init__(self, order_cfg: OrderConfig, opend_cfg: OpendConfig,
                 symbol: str, market: str, timeframe: str = "K_1M",
                 logger=None):
        self.cfg = order_cfg
        self.opend = opend_cfg
        self.symbol = symbol
        self.market = market
        self.timeframe = timeframe
        self._logger = logger or logging.getLogger(__name__)

        self._quote_ctx = None
        self._trade_ctx = None
        self._connected = False
        self._mock_df = None
        self._mock_cursor = 200

    # ─── 接続管理 ────────────────────────────────────────────────

    def connect(self):
        """moomoo OpenDへ接続する"""
        if self.cfg.mock_data:
            self._logger.info("📋 モックデータモード: moomoo接続をスキップ（生成データ使用）")
            self._connected = True
            self._init_mock_data()
            return

        try:
            import futu as ft
            trd_env = ft.TrdEnv.SIMULATE if self.cfg.paper_trading else ft.TrdEnv.REAL

            self._quote_ctx = ft.OpenQuoteContext(
                host=self.opend.host, port=self.opend.port
            )
            if self.market == "US":
                self._trade_ctx = ft.OpenSecTradeContext(
                    filter_trdmarket=ft.TrdMarket.US,
                    host=self.opend.host,
                    port=self.opend.port,
                    security_firm=ft.SecurityFirm.FUTUINC,
                )
            else:
                self._trade_ctx = ft.OpenSecTradeContext(
                    filter_trdmarket=ft.TrdMarket.HK,
                    host=self.opend.host,
                    port=self.opend.port,
                    security_firm=ft.SecurityFirm.FUTUSECURITIES,
                )
            self._connected = True
            self._logger.info(f"✅ moomoo OpenD接続成功 ({self.opend.host}:{self.opend.port})")

            # K線データ取得前にサブスクライブが必要
            from futu import SubType
            subtype_map = {
                "K_1M": SubType.K_1M, "K_5M": SubType.K_5M,
                "K_15M": SubType.K_15M, "K_30M": SubType.K_30M,
                "K_60M": SubType.K_60M, "K_DAY": SubType.K_DAY,
            }
            sub_type = subtype_map.get(self.timeframe, SubType.K_1M)
            ret, data = self._quote_ctx.subscribe([self.symbol], [sub_type])
            if ret == ft.RET_OK:
                self._logger.info(f"✅ サブスクライブ成功: {self.symbol} ({self.timeframe})")
            else:
                self._logger.warning(f"サブスクライブ失敗: {data}")

        except ImportError:
            self._logger.error("futu-api がインストールされていません: pip install futu-api")
            raise
        except Exception as e:
            self._logger.error(f"moomoo接続失敗: {e}")
            raise

    def _init_mock_data(self, total_bars: int = 2000):
        """起動時に一度だけトレンドのある価格系列を生成してキャッシュする"""
        import pandas as pd
        import numpy as np
        np.random.seed(42)
        now = datetime.now().replace(second=0, microsecond=0)
        idx = pd.date_range(end=now, periods=total_bars, freq="1min")
        t = np.arange(total_bars)
        price = (
            100.0
            + np.sin(t / 100) * 3.0        # 長周期トレンド（MACDの方向性）
            + np.sin(t / 25) * 1.5         # 中周期波（GC/DCが発生しやすい周期）
            + np.cumsum(np.random.randn(total_bars) * 0.03)  # 微小ノイズ
        )
        self._mock_df = pd.DataFrame({
            "time_key": idx,
            "open":   price + np.random.randn(total_bars) * 0.02,
            "high":   price + abs(np.random.randn(total_bars) * 0.05),
            "low":    price - abs(np.random.randn(total_bars) * 0.05),
            "close":  price,
            "volume": np.random.randint(1000, 50000, total_bars).astype(float),
        })
        # GC発生位置の10本前から開始（すぐにエントリーを確認できるようにする）
        self._mock_cursor = self._find_first_gc_cursor(kline_num=200, search_from=200)
        self._logger.info(f"モックデータ生成完了: {total_bars}本 / 開始位置: bar {self._mock_cursor} (GC約10本前)")

    def _find_first_gc_cursor(self, kline_num: int, search_from: int) -> int:
        """MACDのGCが最初に発生するバーを探し、10本前のカーソル位置を返す"""
        import pandas as pd
        price = self._mock_df["close"]
        ema12 = price.ewm(span=12, adjust=False).mean()
        ema26 = price.ewm(span=26, adjust=False).mean()
        macd = ema12 - ema26
        signal = macd.ewm(span=9, adjust=False).mean()
        is_golden = macd > signal
        prev_golden = is_golden.shift(1, fill_value=False)
        gc_bars = (is_golden & ~prev_golden)
        candidates = [i for i in gc_bars[gc_bars].index if i > search_from]
        if candidates:
            gc_bar = candidates[0]
            return max(kline_num, gc_bar - 10)
        return kline_num

    def disconnect(self):
        """接続を切断する"""
        if self._quote_ctx:
            try:
                self._quote_ctx.close()
            except Exception:
                pass
        if self._trade_ctx:
            try:
                self._trade_ctx.close()
            except Exception:
                pass
        self._quote_ctx = None
        self._trade_ctx = None
        self._connected = False
        self._logger.info("🔌 moomoo接続を切断しました")

    def reconnect(self):
        """切断後に再接続する"""
        self._logger.info(f"🔄 再接続中... ({self.symbol})")
        self.disconnect()
        import time as _time
        _time.sleep(3)
        self.connect()

    # ─── リアルタイム価格取得 ────────────────────────────────────

    def get_current_price(self) -> Optional[float]:
        """現在価格を取得する"""
        if self.cfg.mock_data:
            return None

        try:
            import futu as ft
            ret, data = self._quote_ctx.get_market_snapshot([self.symbol])
            if ret == ft.RET_OK and not data.empty:
                return float(data["last_price"].iloc[0])
        except Exception as e:
            self._logger.error(f"価格取得エラー: {e}")
        return None

    def get_kline_data(self, kline_num: int = 200):
        """K線データを取得してDataFrameとして返す"""
        if self.cfg.mock_data:
            if self._mock_df is None or self._mock_cursor >= len(self._mock_df):
                self._logger.warning("モックデータが終端に達しました")
                return None
            df = self._mock_df.iloc[self._mock_cursor - kline_num:self._mock_cursor].copy()
            self._mock_cursor += 1
            return df

        try:
            import futu as ft
            from futu import KLType
            timeframe_map = {
                "K_1M": KLType.K_1M, "K_5M": KLType.K_5M,
                "K_15M": KLType.K_15M, "K_30M": KLType.K_30M,
                "K_60M": KLType.K_60M, "K_DAY": KLType.K_DAY,
            }
            kl_type = timeframe_map.get(self.timeframe, KLType.K_1M)
            ret, data = self._quote_ctx.get_cur_kline(
                self.symbol, kline_num, ktype=kl_type
            )
            if ret == ft.RET_OK:
                return data
            else:
                self._logger.warning(f"K線取得失敗 (ret={ret}): {data}")
        except Exception as e:
            self._logger.warning(f"K線取得エラー: {e}")
        return None

    # ─── 発注 ────────────────────────────────────────────────────

    def place_buy_order(self, price: float) -> tuple[bool, str]:
        """
        買い注文を発注する
        Returns: (成功したか, 注文IDまたはエラーメッセージ)
        """
        qty = self.cfg.quantity

        if self.cfg.mock_data:
            msg = (f"[MOCK] 買い注文: {self.symbol} {qty}株 "
                   f"@ {'成行' if self.cfg.order_type == 'market' else f'{price:.4f}'}")
            self._logger.info(msg)
            return True, "MOCK_ORDER_BUY"

        try:
            import futu as ft
            trd_env = ft.TrdEnv.SIMULATE if self.cfg.paper_trading else ft.TrdEnv.REAL

            if self.cfg.order_type == "market":
                order_price = 0.0
                order_type = ft.OrderType.MARKET
            else:
                offset = price * self.cfg.limit_offset_pct / 100
                order_price = price + offset
                order_type = ft.OrderType.NORMAL

            ret, data = self._trade_ctx.place_order(
                price=order_price,
                qty=qty,
                code=self.symbol,
                trd_side=ft.TrdSide.BUY,
                order_type=order_type,
                trd_env=trd_env,
            )
            if ret == ft.RET_OK:
                order_id = str(data["order_id"].iloc[0])
                self._logger.info(f"✅ 買い注文成功: {order_id}")
                return True, order_id
            else:
                self._logger.error(f"買い注文失敗: {data}")
                return False, str(data)

        except Exception as e:
            self._logger.error(f"買い注文エラー: {e}")
            return False, str(e)

    def place_sell_order(self, price: float, reason: str) -> tuple[bool, str]:
        """
        売り注文を発注する
        Returns: (成功したか, 注文IDまたはエラーメッセージ)
        """
        qty = self.cfg.quantity

        if self.cfg.mock_data:
            msg = (f"[MOCK] 売り注文: {self.symbol} {qty}株 "
                   f"@ {'成行' if self.cfg.order_type == 'market' else f'{price:.4f}'} "
                   f"理由={reason}")
            self._logger.info(msg)
            return True, "MOCK_ORDER_SELL"

        try:
            import futu as ft
            trd_env = ft.TrdEnv.SIMULATE if self.cfg.paper_trading else ft.TrdEnv.REAL

            if self.cfg.order_type == "market":
                order_price = 0.0
                order_type = ft.OrderType.MARKET
            else:
                offset = price * self.cfg.limit_offset_pct / 100
                order_price = price - offset
                order_type = ft.OrderType.NORMAL

            ret, data = self._trade_ctx.place_order(
                price=order_price,
                qty=qty,
                code=self.symbol,
                trd_side=ft.TrdSide.SELL,
                order_type=order_type,
                trd_env=trd_env,
            )
            if ret == ft.RET_OK:
                order_id = str(data["order_id"].iloc[0])
                self._logger.info(f"✅ 売り注文成功: {order_id}")
                return True, order_id
            else:
                self._logger.error(f"売り注文失敗: {data}")
                return False, str(data)

        except Exception as e:
            self._logger.error(f"売り注文エラー: {e}")
            return False, str(e)
