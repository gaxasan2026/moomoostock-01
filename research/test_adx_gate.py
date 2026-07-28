"""
test_adx_gate.py
レンジ相場判定ロジック候補③（ADX）の検証。

「ADX（直近200本ウィンドウ、period=14）が閾値未満ならエントリーを止める」という
設定を何通りか試し、何もしない場合（baseline）と比較する。ウォークフォワード検証・
候補①②と同じ10銘柄・同じ9検証月（B09〜B01）を使い、月次の内訳つきで集計する。

閾値の根拠:
- 合成データでの妥当性確認済み（明確なトレンド→ADX94.21、横ばい→ADX3.98）
- 実データ実測（3銘柄・1ヶ月分、1分足）: 平均24・中央値21.8、
  10%ile=13.1・25%ile=16.4・75%ile=29.7・90%ile=38.7
- 慣習的な閾値（20=トレンドなし、25=トレンド形成中）と実測分布を踏まえ、
  15（厳格・実測25%ile未満相当）・20（慣習的閾値）・25（慣習的閾値、やや積極的）を試す
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

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
from regime_gate import gated_replay_adx  # noqa: E402
import walk_forward as wf  # noqa: E402

RESULTS_DIR = RESEARCH_DIR / "results"

# ADX閾値の候補。Noneはゲート無効（baseline）。
ADX_CONFIGS = [None, 15, 20, 25]


def run_one(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg, adx_min, test_start):
    stats = {"trades": 0, "pnl": 0.0, "wins": 0}
    gate_hits = [0]

    def on_exit(price, qty, entry, hold, reason, daily_trades, bar_time, pnl):
        if bar_time.date() < test_start:
            return
        stats["trades"] += 1
        stats["pnl"] += pnl
        if pnl > 0:
            stats["wins"] += 1

    def on_gate(bar_time, adx_val):
        if bar_time.date() >= test_start:
            gate_hits[0] += 1

    gated_replay_adx(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
                      adx_min=adx_min, on_exit=on_exit, on_gate=on_gate)
    return stats, gate_hits[0]


def main():
    today = datetime.now().date()
    print(f"=== ADXゲート検証開始 {datetime.now():%Y-%m-%d %H:%M:%S} ===", flush=True)

    rows = []
    for symbol_id, sector in wf.SYMBOLS_FULL:
        base_cfg = macd_client.get_defaults()
        macd_cfg = MacdConfig(**base_cfg["macd"])
        entry_cfg = EntryConfig(**base_cfg["entry"])
        exit_cfg = ExitConfig(**base_cfg["exit"])
        order_cfg = OrderConfig(**base_cfg["order"])
        risk_cfg = RiskConfig(**base_cfg["risk"])
        opend_cfg = OpendConfig(**base_cfg["opend"])

        print(f"--- {symbol_id} ({sector}) ---", flush=True)
        t0 = time.time()
        full_df = _load_data(symbol_id, wf.FETCH_START, wf.FETCH_END, macd_cfg, opend_cfg)
        print(f"  {len(full_df)}本 ({time.time()-t0:.1f}秒)", flush=True)

        for months_ago in wf.TEST_MONTHS_AGO:
            test_start, test_end = wf.month_bounds(months_ago, today)
            buf_start = test_start - timedelta(days=max(wf.TEST_BUFFER_DAYS, 5))
            month_df = full_df[(full_df["time_key"].dt.date >= buf_start) & (full_df["time_key"].dt.date <= test_end)]
            if len(month_df) < 200:
                continue

            for adx_min in ADX_CONFIGS:
                try:
                    stats, gate_hits = run_one(month_df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
                                                adx_min, test_start)
                except (Exception, SystemExit) as e:
                    print(f"  !!! {symbol_id} B{months_ago:02d} adx_min={adx_min} 失敗: {e}", flush=True)
                    continue

                label = "baseline" if adx_min is None else f"adx{adx_min}"
                rows.append({
                    "symbol": symbol_id, "sector": sector, "test_month_start": test_start,
                    "config": label, "trades": stats["trades"], "total_pnl": round(stats["pnl"], 2),
                    "win_rate": round(stats["wins"] / stats["trades"] * 100, 1) if stats["trades"] else 0.0,
                    "gate_hits": gate_hits,
                })

    df_out = pd.DataFrame(rows)
    out_path = RESULTS_DIR / "adx_gate_analysis.csv"
    df_out.to_csv(out_path, index=False)
    print(f"\n詳細: {out_path}", flush=True)

    print("\n=== サマリー（設定ごとの9ヶ月合計損益・全10銘柄合計） ===")
    configs_order = ["baseline"] + [f"adx{a}" for a in ADX_CONFIGS if a is not None]
    for cfg in configs_order:
        sub = df_out[df_out["config"] == cfg]
        if sub.empty:
            continue
        total = sub["total_pnl"].sum()
        mean = sub["total_pnl"].mean()
        pos_rate = (sub["total_pnl"] > 0).mean() * 100
        print(f"{cfg:<12} 合計={total:>+9.2f}  平均={mean:>+7.2f}  黒字月割合={pos_rate:>5.1f}%  件数={len(sub)}")

    print("\n完了", flush=True)


if __name__ == "__main__":
    main()
