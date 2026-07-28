"""
regime_analysis.py
「レンジ相場ではMACD Traderが構造的に機能しないのではないか」という仮説と、
「時間帯評価も月次で確認すべき」という指摘を検証する。

やっていること:
1. 銘柄・暦月ごとに「トレンド相場」か「レンジ相場」かを、Kaufman Efficiency Ratio
   （日次終値の正味変化 / 値動きの絶対値合計。1に近いほどトレンド、0に近いほどレンジ）
   で分類する。
2. walk_forward.py が既に出したベースライン結果（学習窓なし・固定設定）を、この
   レジーム分類でグループ分けし、「トレンド月 vs レンジ月」で成績に差が出るかを見る。
3. 時間帯（マニュアル記載の5区分）ごとの成績を、集計値ではなく銘柄×月単位で計算し、
   レジームと掛け合わせて確認する（「有望な時間帯」が特定の月に依存していないか）。

前提: fetch_sample_data.py で1年分のデータ取得済み、walk_forward.py の
ベースラインCSVが既に存在すること。
"""
from __future__ import annotations

import csv
import sys
from datetime import date, datetime, time as dt_time
from pathlib import Path

import numpy as np
import pandas as pd

MACD_TRADER_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/macd_trader")
STUDIO_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/backtest_studio")
RESEARCH_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/research")
sys.path.insert(0, str(MACD_TRADER_DIR))
sys.path.insert(0, str(STUDIO_DIR))
sys.path.insert(0, str(RESEARCH_DIR))

from backtest import _load_data  # noqa: E402
from config_loader import MacdConfig, EntryConfig, ExitConfig, OrderConfig, RiskConfig, OpendConfig  # noqa: E402
import macd_client  # noqa: E402
from fast_replay import fast_replay  # noqa: E402
import walk_forward as wf  # noqa: E402

RESULTS_DIR = RESEARCH_DIR / "results"
BASELINE_CSV = RESULTS_DIR / "walk_forward_full_20260728_092147.csv"

# マニュアル記載の5区分（ET）
TIME_SLOTS = [
    ("opening_rush", dt_time(9, 30), dt_time(10, 0)),
    ("best_window", dt_time(10, 0), dt_time(11, 30)),
    ("lunch", dt_time(11, 30), dt_time(14, 0)),
    ("afternoon", dt_time(14, 0), dt_time(15, 30)),
    ("closing", dt_time(15, 30), dt_time(16, 0)),
]

TREND_ER_THRESHOLD_PERCENTILE = 50  # 中央値で「トレンド月」「レンジ月」を分ける


def efficiency_ratio(daily_closes: pd.Series) -> float:
    """Kaufman Efficiency Ratio: 1に近いほどトレンド、0に近いほどレンジ相場。"""
    if len(daily_closes) < 3:
        return float("nan")
    diffs = daily_closes.diff().dropna()
    total_movement = diffs.abs().sum()
    net_change = abs(daily_closes.iloc[-1] - daily_closes.iloc[0])
    return net_change / total_movement if total_movement > 0 else 0.0


def classify_regimes(today: date) -> dict:
    """{(symbol, months_ago): {"er":..., "regime": "trend"/"range"}} を返す。"""
    records = []
    for symbol_id, sector in wf.SYMBOLS_FULL:
        base_cfg = macd_client.get_defaults()
        macd_cfg = MacdConfig(**base_cfg["macd"])
        opend_cfg = OpendConfig(**base_cfg["opend"])
        full_df = _load_data(symbol_id, wf.FETCH_START, wf.FETCH_END, macd_cfg, opend_cfg)

        for months_ago in wf.TEST_MONTHS_AGO:
            start, end = wf.month_bounds(months_ago, today)
            sub = full_df[(full_df["time_key"].dt.date >= start) & (full_df["time_key"].dt.date <= end)]
            if len(sub) < 200:
                continue
            daily_closes = sub.groupby(sub["time_key"].dt.date)["close"].last()
            er = efficiency_ratio(daily_closes)
            records.append({"symbol": symbol_id, "sector": sector, "months_ago": months_ago,
                             "start": start, "end": end, "er": er})

    df = pd.DataFrame(records)
    threshold = np.nanpercentile(df["er"], TREND_ER_THRESHOLD_PERCENTILE)
    df["regime"] = np.where(df["er"] >= threshold, "trend", "range")
    print(f"ER中央値（トレンド/レンジの分割点）: {threshold:.3f}")
    return df


def analyze_baseline_by_regime(regime_df: pd.DataFrame):
    """既存のwalk_forwardベースライン結果を、レジーム分類でグループ分けして比較する。"""
    baseline = pd.read_csv(BASELINE_CSV)
    baseline = baseline[baseline["variant"] == "baseline"].copy()
    baseline["test_month_start"] = pd.to_datetime(baseline["test_month_start"]).dt.date

    regime_df2 = regime_df.copy()
    regime_df2["start"] = pd.to_datetime(regime_df2["start"]).dt.date

    merged = baseline.merge(
        regime_df2[["symbol", "start", "er", "regime"]],
        left_on=["symbol", "test_month_start"], right_on=["symbol", "start"], how="inner",
    )

    print("\n=== ベースライン成績: トレンド月 vs レンジ月（月次データ、9ヶ月×10銘柄）===")
    for regime in ["trend", "range"]:
        sub = merged[merged["regime"] == regime]
        n = len(sub)
        mean_pnl = sub["test_total_pnl"].mean()
        median_pnl = sub["test_total_pnl"].median()
        positive_rate = (sub["test_total_pnl"] > 0).mean() * 100
        print(f"{regime:>6}: {n}件 / 平均損益 {mean_pnl:+.2f}USD / 中央値 {median_pnl:+.2f}USD / 黒字月の割合 {positive_rate:.1f}%")

    out_path = RESULTS_DIR / "regime_baseline_analysis.csv"
    merged.to_csv(out_path, index=False)
    print(f"詳細: {out_path}")
    return merged


def analyze_time_slots_by_regime(regime_df: pd.DataFrame, today: date):
    """銘柄×月×時間帯ごとにバックテストし、レジームと掛け合わせて集計する。"""
    rows = []
    for symbol_id, sector in wf.SYMBOLS_FULL:
        base_cfg = macd_client.get_defaults()
        macd_cfg = MacdConfig(**base_cfg["macd"])
        entry_cfg = EntryConfig(**base_cfg["entry"])
        exit_cfg = ExitConfig(**base_cfg["exit"])
        order_cfg = OrderConfig(**base_cfg["order"])
        risk_cfg = RiskConfig(**base_cfg["risk"])
        opend_cfg = OpendConfig(**base_cfg["opend"])
        full_df = _load_data(symbol_id, wf.FETCH_START, wf.FETCH_END, macd_cfg, opend_cfg)
        print(f"--- {symbol_id} 時間帯別バックテスト中 ---", flush=True)

        for months_ago in wf.TEST_MONTHS_AGO:
            start, end = wf.month_bounds(months_ago, today)
            from datetime import timedelta
            buf_start = start - timedelta(days=wf.TEST_BUFFER_DAYS)
            month_df = full_df[(full_df["time_key"].dt.date >= buf_start) & (full_df["time_key"].dt.date <= end)]
            if len(month_df) < 200:
                continue

            for slot_name, slot_start, slot_end in TIME_SLOTS:
                stats = {"trades": 0, "pnl": 0.0, "wins": 0}

                def on_exit(price, qty, entry, hold, reason, daily_trades, bar_time, pnl,
                            _stats=stats, _start=start):
                    if bar_time.date() < _start:
                        return
                    _stats["trades"] += 1
                    _stats["pnl"] += pnl
                    if pnl > 0:
                        _stats["wins"] += 1

                try:
                    fast_replay(month_df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
                                hours_filter=(slot_start, slot_end), on_exit=on_exit)
                except (Exception, SystemExit) as e:
                    print(f"  !!! {symbol_id} B{months_ago:02d} {slot_name} 失敗: {e}", flush=True)
                    continue

                rows.append({
                    "symbol": symbol_id, "sector": sector, "months_ago": months_ago,
                    "test_month_start": start, "slot": slot_name,
                    "trades": stats["trades"], "total_pnl": round(stats["pnl"], 2),
                    "win_rate": round(stats["wins"] / stats["trades"] * 100, 1) if stats["trades"] else 0.0,
                })

    slots_df = pd.DataFrame(rows)
    out_path = RESULTS_DIR / "regime_timeslot_analysis.csv"
    slots_df.to_csv(out_path, index=False)
    print(f"\n詳細: {out_path}", flush=True)

    regime_df2 = regime_df.copy()
    regime_df2["test_month_start"] = pd.to_datetime(regime_df2["start"]).dt.date
    slots_df["test_month_start"] = pd.to_datetime(slots_df["test_month_start"]).dt.date
    merged = slots_df.merge(regime_df2[["symbol", "test_month_start", "regime"]],
                             on=["symbol", "test_month_start"], how="inner")

    print("\n=== 時間帯別成績: レジーム別（月次データの平均、集計値ではない）===")
    print(f"{'時間帯':<14}{'トレンド月平均':>16}{'レンジ月平均':>16}{'トレンド月黒字率':>18}{'レンジ月黒字率':>16}")
    for slot_name, _, _ in TIME_SLOTS:
        trend_sub = merged[(merged["slot"] == slot_name) & (merged["regime"] == "trend")]
        range_sub = merged[(merged["slot"] == slot_name) & (merged["regime"] == "range")]
        trend_mean = trend_sub["total_pnl"].mean()
        range_mean = range_sub["total_pnl"].mean()
        trend_pos = (trend_sub["total_pnl"] > 0).mean() * 100
        range_pos = (range_sub["total_pnl"] > 0).mean() * 100
        print(f"{slot_name:<14}{trend_mean:>+16.2f}{range_mean:>+16.2f}{trend_pos:>17.1f}%{range_pos:>15.1f}%")

    return merged


def main():
    today = datetime.now().date()
    print(f"=== レジーム分析開始 {datetime.now():%Y-%m-%d %H:%M:%S} ===", flush=True)

    regime_df = classify_regimes(today)
    regime_df.to_csv(RESULTS_DIR / "regime_classification.csv", index=False)
    print(f"レジーム分類を保存: {RESULTS_DIR / 'regime_classification.csv'}", flush=True)

    analyze_baseline_by_regime(regime_df)
    analyze_time_slots_by_regime(regime_df, today)

    print("\n完了", flush=True)


if __name__ == "__main__":
    main()
