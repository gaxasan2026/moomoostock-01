"""
order_manager.py
moomoo OpenAPI を使った発注・ポジション照会のラッパー
paper_trading=True の場合はモック動作でAPIを呼ばない
"""
import fcntl
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from config_loader import OrderConfig, OpendConfig

# moomoo/futuのポジション照会APIは「10回/30秒/口座」のレート制限がある。
# macd_trader・swing_traderは別プロセスとして動作し、かつそれぞれ複数銘柄
# （=複数OrderManagerインスタンス）を持つため、銘柄ごとに個別照会すると
# プロセス内・プロセス間の両方でこの上限を容易に超える
# （実際に7銘柄+8銘柄が同時稼働した際に発生を確認済み）。
#
# 対策として、①銘柄を絞らず口座全体のポジション一覧を1回で取得し、
# ②その結果をプロセスをまたいだファイル共有キャッシュ（TTL付き）に置くことで、
# 銘柄数・プロセス数が増えても実際のAPI呼び出し回数はTTLの間隔に収束させる。
_position_query_lock = threading.Lock()
_last_position_query_at = 0.0
_MIN_POSITION_QUERY_INTERVAL_SEC = 3.5

# macd_trader/swing_traderの共通の親ディレクトリ（moomoostock-01/）に置き、
# 両プロセスから同じファイルを参照できるようにする。
_POSITION_CACHE_PATH = Path(__file__).resolve().parent.parent / "shared_position_cache.json"
_POSITION_CACHE_TTL_SEC = 8.0


def _read_shared_position_cache(trd_env_key: str) -> Optional[dict]:
    """共有キャッシュが有効期限内であれば {symbol: qty} を返す。無効/未取得ならNone"""
    if not _POSITION_CACHE_PATH.exists():
        return None
    try:
        with open(_POSITION_CACHE_PATH, "r", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                data = json.load(f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        entry = data.get(trd_env_key)
        if not entry or (time.time() - entry.get("ts", 0)) > _POSITION_CACHE_TTL_SEC:
            return None
        return entry.get("positions", {})
    except Exception:
        return None


def _write_shared_position_cache(trd_env_key: str, positions: dict):
    """他のtrd_envのエントリを壊さないよう読み込んでからマージして書き込む
    （プロセス間の書き込み競合は許容する — キャッシュであり正とする情報源ではないため、
    最悪でも次のTTL経過後に自己修復する）"""
    try:
        _POSITION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if _POSITION_CACHE_PATH.exists():
            try:
                with open(_POSITION_CACHE_PATH, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        data[trd_env_key] = {"ts": time.time(), "positions": positions}
        tmp = _POSITION_CACHE_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                json.dump(data, f)
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
        tmp.replace(_POSITION_CACHE_PATH)
    except Exception:
        pass  # キャッシュ書き込み失敗は致命的ではない（次回また直接APIを叩くだけ）


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

    # ─── ポジション照会（実口座との照合用） ────────────────────────

    def get_real_position_qty(self) -> Optional[int]:
        """
        実口座（このsymbol・このtrd_env）の現在の保有数量を取得する。
        銘柄を絞らず口座全体のポジション一覧を取得し、プロセスをまたいだ
        ファイル共有キャッシュ（TTL秒）に載せることで、稼働銘柄数・プロセス数が
        増えてもmoomoo側のレート制限（10回/30秒/口座）を超えないようにする。

        Returns:
            int: 実際の保有数量（保有なしなら0）
            None: 照合対象がない（mock_dataモード）、または照会に失敗した
                  （呼び出し側は「確認できない」扱いとして安全側に倒すこと。
                  0とNoneを混同しないこと — 0は「確認できて、保有なし」の意味）
        """
        if self.cfg.mock_data:
            return None

        trd_env_key = "SIMULATE" if self.cfg.paper_trading else "REAL"

        cached = _read_shared_position_cache(trd_env_key)
        if cached is not None:
            return int(cached.get(self.symbol, 0))

        global _last_position_query_at
        with _position_query_lock:
            # ロック待ちの間に他スレッド（同一プロセス）が更新した可能性があるため再確認
            cached = _read_shared_position_cache(trd_env_key)
            if cached is not None:
                return int(cached.get(self.symbol, 0))

            wait = _MIN_POSITION_QUERY_INTERVAL_SEC - (time.monotonic() - _last_position_query_at)
            if wait > 0:
                time.sleep(wait)
            _last_position_query_at = time.monotonic()

            try:
                import futu as ft
                trd_env = ft.TrdEnv.SIMULATE if self.cfg.paper_trading else ft.TrdEnv.REAL
                # codeを指定せず口座全体を1回で取得する（銘柄ごとの個別照会にしない）
                ret, data = self._trade_ctx.position_list_query(
                    trd_env=trd_env, refresh_cache=True,
                )
                if ret != ft.RET_OK:
                    self._logger.warning(f"ポジション照会失敗: {data}")
                    return None
                positions = {}
                if data is not None and not data.empty:
                    for _, row in data.iterrows():
                        positions[str(row["code"])] = int(row["qty"])
                _write_shared_position_cache(trd_env_key, positions)
                return positions.get(self.symbol, 0)
            except Exception as e:
                self._logger.warning(f"ポジション照会エラー: {e}")
                return None

    # ─── 発注 ────────────────────────────────────────────────────

    def place_buy_order(self, price: float, quantity: int) -> tuple[bool, str]:
        """
        買い注文を発注する
        Returns: (成功したか, 注文IDまたはエラーメッセージ)
        """
        qty = quantity

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

    def place_sell_order(self, price: float, reason: str, quantity: int,
                          force_market: bool = False) -> tuple[bool, str]:
        """
        売り注文を発注する（quantityは対応するBUY時の株数をそのまま渡すこと）
        force_market=True の場合、銘柄設定のorder_typeに関わらず必ず成行注文にする
        （ユーザーによる「即時売却」操作用。今すぐ約定させる意図を優先するため）。
        Returns: (成功したか, 注文IDまたはエラーメッセージ)
        """
        qty = quantity
        use_market = force_market or (self.cfg.order_type == "market")

        if self.cfg.mock_data:
            msg = (f"[MOCK] 売り注文: {self.symbol} {qty}株 "
                   f"@ {'成行' if use_market else f'{price:.4f}'} "
                   f"理由={reason}")
            self._logger.info(msg)
            return True, "MOCK_ORDER_SELL"

        try:
            import futu as ft
            trd_env = ft.TrdEnv.SIMULATE if self.cfg.paper_trading else ft.TrdEnv.REAL

            if use_market:
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


# ─── 市場の開閉状態（即時売却の確認ダイアログ表示用） ──────────────

# MORNING/AFTERNOON以外は「通常取引時間外」として扱う。市場が閉じている間は
# 成行注文を出しても「受理」されるだけで、実際の約定は次の取引時間まで
# 持ち越されるため、この状態を呼び出し側（GUI）に明示する。
_MARKET_STATE_LABELS = {
    "MORNING": "取引時間中", "AFTERNOON": "取引時間中",
    "PRE_MARKET_BEGIN": "プレマーケット", "PRE_MARKET_END": "プレマーケット",
    "AFTER_HOURS_BEGIN": "アフターマーケット", "AFTER_HOURS_END": "アフターマーケット",
    "CLOSED": "休場中", "NONE": "休場中", "REST": "休場中",
    "WAITING_OPEN": "取引開始待ち", "AUCTION": "寄付前オークション",
}
_REGULAR_HOURS_STATES = {"MORNING", "AFTERNOON"}


def get_market_state_for_symbol(symbol: str, host: str, port: int) -> Optional[dict]:
    """
    銘柄の現在の市場状態を取得する。この関数は独立した一時的な接続を
    自前で張って照会する（稼働中ボットの接続とは別。ボットスレッドの
    内部状態にHTTPハンドラのスレッドから直接触れないようにするため）。

    Returns: {"state": 生の状態名, "is_regular_hours": bool, "label": 日本語ラベル}
             取得できなければNone
    """
    try:
        import futu as ft
        ctx = ft.OpenQuoteContext(host=host, port=port)
        try:
            ret, data = ctx.get_market_state([symbol])
        finally:
            ctx.close()
        if ret != ft.RET_OK or data is None or data.empty:
            return None
        raw = str(data["market_state"].iloc[0])
        return {
            "state": raw,
            "is_regular_hours": raw in _REGULAR_HOURS_STATES,
            "label": _MARKET_STATE_LABELS.get(raw, raw),
        }
    except Exception:
        return None
