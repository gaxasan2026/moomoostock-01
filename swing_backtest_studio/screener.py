"""
screener.py
OpenD市場全体スクリーニング（Stage 1: 有望候補の絞り込み）。

「有望性」の根拠は個々の銘柄が構造的にスイング戦略（2営業日以内に
take_profit/stop_loss幅=5〜10%動く）に適しているかどうかであり、
中核条件は流動性（時価総額・売買代金）とボラティリティ（値幅）に置く。
MACDゴールデンクロス（直近で発生中）は「今のトレンドに乗る」ための
補助条件として扱い、必須にはしない（require_gold_cross=Falseが既定）。

ここで絞り込んだ候補は、あくまでStage 2（Swing Backtest Studioの
バックテストバッチ）で実際にPnLを検証して初めて「有望」と判断できる。
"""
from __future__ import annotations

import futu as ft
from futu.quote.stock_screen_const import (
    BasicProperty,
    CumulativeProperty,
    Pattern,
    Period,
    ScrMarket,
    ScrSortDir,
    SimpleField,
    SimpleProperty,
)

DEFAULT_OPEND_HOST = "127.0.0.1"
DEFAULT_OPEND_PORT = 11111

_MARKET_MAP = {"US": ScrMarket.US}
_GOLD_CROSS_PERIOD_MAP = {"K_DAY": Period.DAY, "K_60M": Period.HOUR_1}


def discover_candidates(
    market="US",
    market_cap_min=10_000_000_000.0,
    avg_turnover_min=20_000_000.0,
    amplitude_min=0.05,
    amplitude_days=5,
    require_gold_cross=False,
    gold_cross_timeframe="K_DAY",
    max_results=50,
    host=DEFAULT_OPEND_HOST,
    port=DEFAULT_OPEND_PORT,
):
    scr_market = _MARKET_MAP.get(market)
    if scr_market is None:
        raise ValueError(f"未対応の市場です: {market}")
    gold_cross_period = _GOLD_CROSS_PERIOD_MAP.get(gold_cross_timeframe, Period.DAY)

    req = ft.StockScreenRequest()
    req.page_from = 0
    req.page_count = max(1, min(int(max_results), 200))
    req.add_simple_field(field=int(SimpleField.MARKET), values=[int(scr_market)])
    if market_cap_min:
        req.add_simple_property(name=int(SimpleProperty.MARKET_CAP), lower=float(market_cap_min))
    if avg_turnover_min:
        req.add_cumulative_property(
            name=int(CumulativeProperty.AVG_TURNOVER), days=int(amplitude_days), lower=float(avg_turnover_min)
        )
    if amplitude_min:
        req.add_cumulative_property(
            name=int(CumulativeProperty.AMPLITUDE), days=int(amplitude_days), lower=float(amplitude_min)
        )
    if require_gold_cross:
        req.add_indicator_pattern(name=int(Pattern.MACD_GOLD_CROSS), period_type=int(gold_cross_period))

    req.add_retrieve_basic(name=int(BasicProperty.CODE))
    req.add_retrieve_simple(name=int(SimpleProperty.MARKET_CAP))
    req.add_retrieve_simple(name=int(SimpleProperty.PRICE))
    req.add_retrieve_cumulative(name=int(CumulativeProperty.AMPLITUDE), days=int(amplitude_days))
    req.add_retrieve_cumulative(name=int(CumulativeProperty.AVG_TURNOVER), days=int(amplitude_days))
    req.set_sort(
        direction=int(ScrSortDir.DESC),
        property_type="cumulative",
        property_params={"name": int(CumulativeProperty.AMPLITUDE), "days": int(amplitude_days)},
    )

    quote_ctx = ft.OpenQuoteContext(host=host, port=port)
    try:
        ret, data = quote_ctx.get_stock_screen(req)
    finally:
        quote_ctx.close()

    if ret != ft.RET_OK:
        raise RuntimeError(f"OpenDスクリーニングに失敗しました: {data}")

    _last_page, all_count, items = data
    results = []
    for item in items or []:
        code = market_cap = price = amplitude = avg_turnover = None
        for r in item.get("results", []):
            name = r.get("property", {}).get("name")
            rtype = r.get("type")
            if rtype == "basic" and name == int(BasicProperty.CODE):
                code = r.get("sval")
            elif rtype == "simple" and name == int(SimpleProperty.MARKET_CAP):
                market_cap = r.get("dval")
            elif rtype == "simple" and name == int(SimpleProperty.PRICE):
                price = r.get("dval")
            elif rtype == "cumulative" and name == int(CumulativeProperty.AMPLITUDE):
                amplitude = r.get("dval")
            elif rtype == "cumulative" and name == int(CumulativeProperty.AVG_TURNOVER):
                avg_turnover = r.get("dval")
        if not code:
            continue
        symbol_id = code if "." in code else f"{market}.{code}"
        results.append(
            {
                "symbol": symbol_id,
                "code": code,
                "market_cap": market_cap,
                "price": price,
                "amplitude_pct": round(amplitude * 100, 2) if amplitude is not None else None,
                "avg_turnover": avg_turnover,
            }
        )

    return {"all_count": all_count, "results": results}
