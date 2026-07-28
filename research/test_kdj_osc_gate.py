"""
test_kdj_osc_gate.py
レンジ相場判定ロジック候補④（KDJ J値の振動頻度）の検証。

「直近N日間でJ値が極端域（<10 または >90）に新規突入した回数が閾値を超えたら
エントリーを止める」という設定を何通りか試し、何もしない場合（baseline）と比較する。
ウォークフォワード検証・候補①②③と同じ10銘柄・同じ9検証月（B09〜B01）を使い、
月次の内訳つきで集計する。

閾値は実測値から較正（3銘柄・1年分、日次の「極端域への新規突入」回数）:
  平均43回・中央値43回・10%ile=38・25%ile=41・75%ile=46・90%ile=49（銘柄間でほぼ一定）。
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
from regime_gate import gated_replay_kdj_oscillation  # noqa: E402
import walk_forward as wf  # noqa: E402

RESULTS_DIR = RESEARCH_DIR / "results"

# (osc_window_days, osc_threshold) の候補。Noneはゲート無効（baseline）。
GATE_CONFIGS = [
    (None, None),
    (3, 110),
    (3, 140),
    (5, 190),
    (5, 230),
]


def run_one(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
            window_days, threshold, test_start):
    stats = {"trades": 0, "pnl": 0.0, "wins": 0}
    gate_hits = [0]

    def on_exit(price, qty, entry, hold, reason, daily_trades, bar_time, pnl):
        if bar_time.date() < test_start:
            return
        stats["trades"] += 1
        stats["pnl"] += pnl
        if pnl > 0:
            stats["wins"] += 1

    def on_gate(bar_time, count):
        if bar_time.date() >= test_start:
            gate_hits[0] += 1

    gated_replay_kdj_oscillation(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
                                  osc_window_days=window_days, osc_threshold=threshold,
                                  on_exit=on_exit, on_gate=on_gate)
    return stats, gate_hits[0]


def main():
    today = datetime.now().date()
    print(f"=== KDJ振動頻度ゲート検証開始 {datetime.now():%Y-%m-%d %H:%M:%S} ===", flush=True)

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

            for window_days, threshold in GATE_CONFIGS:
                try:
                    stats, gate_hits = run_one(month_df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
                                                window_days, threshold, test_start)
                except (Exception, SystemExit) as e:
                    print(f"  !!! {symbol_id} B{months_ago:02d} window={window_days} threshold={threshold} 失敗: {e}", flush=True)
                    continue

                label = "baseline" if window_days is None else f"w{window_days}_t{threshold}"
                rows.append({
                    "symbol": symbol_id, "sector": sector, "test_month_start": test_start,
                    "config": label, "trades": stats["trades"], "total_pnl": round(stats["pnl"], 2),
                    "win_rate": round(stats["wins"] / stats["trades"] * 100, 1) if stats["trades"] else 0.0,
                    "gate_hits": gate_hits,
                })

    df_out = pd.DataFrame(rows)
    out_path = RESULTS_DIR / "kdj_osc_gate_analysis.csv"
    df_out.to_csv(out_path, index=False)
    print(f"\n詳細: {out_path}", flush=True)

    print("\n=== サマリー（設定ごとの9ヶ月合計損益・全10銘柄合計） ===")
    configs_order = ["baseline"] + [f"w{w}_t{t}" for w, t in GATE_CONFIGS if w is not None]
    for cfg in configs_order:
        sub = df_out[df_out["config"] == cfg]
        if sub.empty:
            continue
        total = sub["total_pnl"].sum()
        mean = sub["total_pnl"].mean()
        pos_rate = (sub["total_pnl"] > 0).mean() * 100
        print(f"{cfg:<14} 合計={total:>+9.2f}  平均={mean:>+7.2f}  黒字月割合={pos_rate:>5.1f}%  件数={len(sub)}")

    print("\n完了", flush=True)


if __name__ == "__main__":
    main()
