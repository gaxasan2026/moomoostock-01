"""
history_loader.py
過去K線データをOpenD経由で取得し、ローカルにキャッシュする

過去データは一度取得すれば変わらないため、同じ銘柄・足種・期間の
再取得はOpenDに問い合わせずローカルキャッシュ（data/history/）から読む。
これにより、パラメータ調整のために何度もバックテストを実行しても
moomooの過去K線クォータを消費しない。
"""
import logging
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).parent
HISTORY_DIR = BASE_DIR / "data" / "history"

logger = logging.getLogger(__name__)


def _cache_path(symbol: str, timeframe: str, start_date: str, end_date: str) -> Path:
    safe = symbol.replace(".", "_")
    return HISTORY_DIR / f"{safe}_{timeframe}_{start_date}_{end_date}.csv"


def _fetch_from_opend(symbol: str, start_date: str, end_date: str,
                       ktype, host: str, port: int) -> pd.DataFrame:
    import futu as ft

    quote_ctx = ft.OpenQuoteContext(host=host, port=port)
    try:
        ret, quota = quote_ctx.get_history_kl_quota(get_detail=False)
        if ret == ft.RET_OK:
            used_quota, remain_quota, _detail_list = quota
            logger.info(f"過去K線クォータ: 使用済み={used_quota} 残り={remain_quota}")
        else:
            logger.warning(f"クォータ状況の取得に失敗しました: {quota}")

        frames = []
        page_req_key = None
        while True:
            ret, data, page_req_key = quote_ctx.request_history_kline(
                symbol, start=start_date, end=end_date, ktype=ktype,
                autype=ft.AuType.QFQ, max_count=1000, page_req_key=page_req_key,
            )
            if ret != ft.RET_OK:
                raise RuntimeError(f"過去K線取得失敗: {data}")
            frames.append(data)
            if page_req_key is None:
                break
        return pd.concat(frames, ignore_index=True)
    finally:
        quote_ctx.close()


def load_or_fetch_history(symbol: str, start_date: str, end_date: str,
                           timeframe: str = "K_1M",
                           host: str = "127.0.0.1", port: int = 11111) -> pd.DataFrame:
    """
    ローカルキャッシュ（data/history/）があればそれを読み込み、
    なければOpenDから取得してキャッシュに保存したうえで返す。
    """
    import futu as ft
    ktype_map = {
        "K_1M": ft.KLType.K_1M, "K_5M": ft.KLType.K_5M,
        "K_15M": ft.KLType.K_15M, "K_30M": ft.KLType.K_30M,
        "K_60M": ft.KLType.K_60M, "K_DAY": ft.KLType.K_DAY,
    }
    ktype = ktype_map.get(timeframe, ft.KLType.K_1M)

    cache_path = _cache_path(symbol, timeframe, start_date, end_date)
    if cache_path.exists():
        logger.info(f"キャッシュから読み込み: {cache_path.name}")
        return pd.read_csv(cache_path, parse_dates=["time_key"])

    logger.info(f"OpenDから過去データ取得中: {symbol} {timeframe} {start_date}〜{end_date}")
    df = _fetch_from_opend(symbol, start_date, end_date, ktype, host, port)

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(cache_path, index=False)
    logger.info(f"キャッシュに保存: {cache_path.name} ({len(df)}本)")
    return df
