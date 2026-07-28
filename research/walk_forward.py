"""
walk_forward.py
ローリング・ウォークフォワード検証。

方針（ユーザーとの合意事項）:
- 学習窓の長さ（1・2・3ヶ月）を比較する。検証対象の月（B09〜B01の9ヶ月）は
  学習窓の長さによらず固定する（案B）— 学習窓の長さ以外の条件を揃えるため。
- 学習期間内で「最良の設定値」を選ぶ基準は、取引あたり平均損益（total_pnl / trades）。
  ただし取引数が極端に少ない組み合わせが偶然勝つのを避けるため、最低取引数
  （MIN_TRAIN_TRADES）に満たない組み合わせは候補から除外する。
- 比較先（ベースライン）は「再最適化せず固定設定を9ヶ月そのまま適用した場合」。
  登録済み銘柄（NVDA・TSLA）は現在のproduction設定、未登録銘柄はMACD Trader
  標準のデフォルト設定を固定適用する。
- これはあくまで一つの試行であり、他の指標・他のベースライン・他の学習窓パターン
  という次善の選択肢が存在することを前提とする（1回の結果で結論を急がない）。

前提: 対象10銘柄の1年分のK_1Mデータが data/history/ に取得済みであること
（fetch_sample_data.py 参照）。
"""
from __future__ import annotations

import csv
import dataclasses
import itertools
import sys
import time
from datetime import date, datetime
from pathlib import Path

MACD_TRADER_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/macd_trader")
STUDIO_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/backtest_studio")
sys.path.insert(0, str(MACD_TRADER_DIR))
sys.path.insert(0, str(STUDIO_DIR))

from backtest import _load_data  # noqa: E402
from config_loader import MacdConfig, EntryConfig, ExitConfig, OrderConfig, RiskConfig, OpendConfig  # noqa: E402
import macd_client  # noqa: E402
from fast_replay import fast_replay  # noqa: E402
# fast_replayはbacktest.py._replay()と数値的・取引単位で完全一致することを
# verify_fast_indicators.py / verify_fast_replay.py で検証済み（2026-07-28）。
# 高速化 約4〜10倍（KDJ有効時は約4倍、MACDのみの場合は約10倍）。

RESULTS_DIR = Path("/Users/onomasayuki/Claude/moomoostock-01/research/results")

SYMBOLS_FULL = [
    ("US.NVDA", "半導体"),
    ("US.CRM", "ソフトウェア/クラウド"),
    ("US.TSLA", "自動車/EV"),
    ("US.JPM", "金融"),
    ("US.MRNA", "ヘルスケア/バイオ"),
    ("US.XOM", "エネルギー"),
    ("US.BA", "資本財/防衛"),
    ("US.NFLX", "通信/メディア"),
    ("US.AMZN", "小売/eコマース"),
    ("US.FCX", "素材/資源"),
]

# パイロット実行用（--pilot）: 業種を分散させた4銘柄・約3時間の想定
SYMBOLS_PILOT = [
    ("US.NVDA", "半導体"),
    ("US.JPM", "金融"),
    ("US.MRNA", "ヘルスケア/バイオ"),
    ("US.XOM", "エネルギー"),
]

GRID_SPEC_FULL = {
    "gc_duration_minutes": [2.0, 3.0, 5.0, 7.0],
    "kdj_max_d": [0.0, 50.0, 80.0],
}

# パイロット実行用（--pilot）: 2値×2値=4組み合わせに縮小
GRID_SPEC_PILOT = {
    "gc_duration_minutes": [3.0, 7.0],
    "kdj_max_d": [0.0, 50.0],
}

PILOT_MODE = "--pilot" in sys.argv
SYMBOLS = SYMBOLS_PILOT if PILOT_MODE else SYMBOLS_FULL
GRID_SPEC = GRID_SPEC_PILOT if PILOT_MODE else GRID_SPEC_FULL

WINDOW_LENGTHS = [1, 2, 3]     # 学習窓の長さ（ヶ月）
TEST_MONTHS_AGO = list(range(9, 0, -1))   # B09〜B01（固定・案B）
MIN_TRAIN_TRADES = 3          # この件数未満の組み合わせは候補から除外
TEST_BUFFER_DAYS = 5          # 検証月の直前に足すウォームアップ用バッファ（暦日）

# fetch_sample_data.py で実際に取得した範囲と完全一致させる（history_loaderの
# キャッシュは日付範囲の完全一致キーのため、月境界の計算値をそのまま渡すと
# キャッシュミスして毎回OpenDへ再取得しに行ってしまう）
FETCH_START = "2025-07-27"
FETCH_END = "2026-07-26"


def month_bounds(months_ago: int, today: date) -> tuple[date, date]:
    """today の月を0として、months_ago ヶ月前の暦月の (開始日, 終了日) を返す。"""
    y, m = today.year, today.month
    total = (y * 12 + (m - 1)) - months_ago
    ny, nm = divmod(total, 12)
    start = date(ny, nm + 1, 1)
    if nm + 1 == 12:
        end = date(ny + 1, 1, 1)
    else:
        end = date(ny, nm + 2, 1)
    from datetime import timedelta
    return start, end - timedelta(days=1)


def build_combos(grid_spec: dict) -> list[dict]:
    names = list(grid_spec.keys())
    return [dict(zip(names, combo)) for combo in itertools.product(*(grid_spec[n] for n in names))]


def run_replay(df, macd_cfg, entry_overrides, exit_cfg, order_cfg, risk_cfg,
               base_entry_cfg, date_filter_from=None):
    entry_cfg = dataclasses.replace(base_entry_cfg, **entry_overrides)
    stats = {"trades": 0, "pnl": 0.0, "wins": 0}

    def on_exit(price, qty, entry, hold, reason, daily_trades, bar_time, pnl):
        bt_date = bar_time.date() if hasattr(bar_time, "date") else bar_time
        if date_filter_from is not None and bt_date < date_filter_from:
            return  # ウォームアップ用バッファ期間中の取引はカウントしない
        stats["trades"] += 1
        stats["pnl"] += pnl
        if pnl > 0:
            stats["wins"] += 1

    fast_replay(df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg, on_exit=on_exit)
    return stats


def get_base_config(symbol_id: str, registered: bool) -> dict:
    if registered:
        cfg = macd_client.get_symbol(symbol_id)
        if cfg:
            return cfg
    return macd_client.get_defaults()


def main():
    today = datetime.now().date()
    mode_label = "PILOT" if PILOT_MODE else "FULL"
    print(f"=== ウォークフォワード検証開始 [{mode_label}] {datetime.now():%Y-%m-%d %H:%M:%S} ===")
    print(f"対象銘柄: {[s for s, _ in SYMBOLS]}")
    print(f"検証月（固定・B09〜B01）: {[month_bounds(m, today) for m in TEST_MONTHS_AGO]}")

    combos = build_combos(GRID_SPEC)
    print(f"グリッド組み合わせ数: {len(combos)}")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"walk_forward_{mode_label.lower()}_{datetime.now():%Y%m%d_%H%M%S}.csv"
    fieldnames = [
        "symbol", "sector", "variant", "test_month_start", "test_month_end",
        "train_start", "train_end", "chosen_gc_duration_minutes", "chosen_kdj_max_d",
        "train_trades", "train_avg_pnl_per_trade",
        "test_trades", "test_total_pnl", "test_win_rate",
    ]
    rows = []
    errors = []

    def save_csv():
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    registered_ids = {"US.NVDA", "US.TSLA"}

    for symbol_id, sector in SYMBOLS:
        try:
            registered = symbol_id in registered_ids
            base_cfg = get_base_config(symbol_id, registered)
            macd_cfg = MacdConfig(**base_cfg["macd"])
            base_entry_cfg = EntryConfig(**base_cfg["entry"])
            exit_cfg = ExitConfig(**base_cfg["exit"])
            order_cfg = OrderConfig(**base_cfg["order"])
            risk_cfg = RiskConfig(**base_cfg["risk"])
            opend_cfg = OpendConfig(**base_cfg["opend"])

            print(f"\n--- {symbol_id} ({sector}) 全期間 {FETCH_START}〜{FETCH_END} 読み込み中 ---", flush=True)
            t0 = time.time()
            full_df = _load_data(symbol_id, FETCH_START, FETCH_END, macd_cfg, opend_cfg)
            print(f"  {len(full_df)}本 ({time.time()-t0:.1f}秒)", flush=True)
        except (Exception, SystemExit) as e:
            print(f"  !!! {symbol_id} の設定/データ読み込みに失敗、この銘柄をスキップします: {e}", flush=True)
            errors.append(f"{symbol_id}: 読み込み失敗 — {e}")
            continue

        # ── ベースライン（固定設定を各検証月にそのまま適用） ──
        for months_ago in TEST_MONTHS_AGO:
            try:
                test_start, test_end = month_bounds(months_ago, today)
                buf_start = test_start - __import__("datetime").timedelta(days=TEST_BUFFER_DAYS)
                test_df = full_df[(full_df["time_key"].dt.date >= buf_start) & (full_df["time_key"].dt.date <= test_end)]
                if len(test_df) < 200:
                    continue
                stats = run_replay(test_df, macd_cfg, {}, exit_cfg, order_cfg, risk_cfg,
                                    base_entry_cfg, date_filter_from=test_start)
                rows.append({
                    "symbol": symbol_id, "sector": sector, "variant": "baseline",
                    "test_month_start": test_start, "test_month_end": test_end,
                    "train_start": "", "train_end": "",
                    "chosen_gc_duration_minutes": base_entry_cfg.gc_duration_minutes,
                    "chosen_kdj_max_d": base_entry_cfg.kdj_max_d,
                    "train_trades": "", "train_avg_pnl_per_trade": "",
                    "test_trades": stats["trades"], "test_total_pnl": round(stats["pnl"], 2),
                    "test_win_rate": round(stats["wins"] / stats["trades"] * 100, 1) if stats["trades"] else 0.0,
                })
            except (Exception, SystemExit) as e:
                print(f"  !!! {symbol_id} baseline B{months_ago:02d} 失敗、スキップ: {e}", flush=True)
                errors.append(f"{symbol_id} baseline B{months_ago:02d}: {e}")

        # ── 学習窓ごとのウォークフォワード ──
        for window_len in WINDOW_LENGTHS:
            print(f"  window={window_len}ヶ月 ...", flush=True)
            for months_ago in TEST_MONTHS_AGO:
                try:
                    test_start, test_end = month_bounds(months_ago, today)
                    train_end_month_ago = months_ago + 1
                    train_start_month_ago = months_ago + window_len
                    train_start, _ = month_bounds(train_start_month_ago, today)
                    _, train_end = month_bounds(train_end_month_ago, today)

                    train_df = full_df[(full_df["time_key"].dt.date >= train_start) & (full_df["time_key"].dt.date <= train_end)]
                    if len(train_df) < 200:
                        continue

                    best = None
                    for combo in combos:
                        stats = run_replay(train_df, macd_cfg, combo, exit_cfg, order_cfg, risk_cfg, base_entry_cfg)
                        if stats["trades"] < MIN_TRAIN_TRADES:
                            continue
                        avg = stats["pnl"] / stats["trades"]
                        if best is None or avg > best[1]:
                            best = (combo, avg, stats["trades"])

                    if best is None:
                        # 最低取引数を満たす組み合わせが無い場合は、取引数最多のものを採用する
                        fallback = max(
                            ((combo, run_replay(train_df, macd_cfg, combo, exit_cfg, order_cfg, risk_cfg, base_entry_cfg))
                             for combo in combos),
                            key=lambda x: x[1]["trades"],
                        )
                        combo, stats = fallback
                        avg = stats["pnl"] / stats["trades"] if stats["trades"] else 0.0
                        best = (combo, avg, stats["trades"])

                    chosen_combo, train_avg, train_trades = best

                    buf_start = test_start - __import__("datetime").timedelta(days=TEST_BUFFER_DAYS)
                    test_df = full_df[(full_df["time_key"].dt.date >= buf_start) & (full_df["time_key"].dt.date <= test_end)]
                    if len(test_df) < 200:
                        continue
                    test_stats = run_replay(test_df, macd_cfg, chosen_combo, exit_cfg, order_cfg, risk_cfg,
                                             base_entry_cfg, date_filter_from=test_start)

                    rows.append({
                        "symbol": symbol_id, "sector": sector, "variant": f"{window_len}mo",
                        "test_month_start": test_start, "test_month_end": test_end,
                        "train_start": train_start, "train_end": train_end,
                        "chosen_gc_duration_minutes": chosen_combo["gc_duration_minutes"],
                        "chosen_kdj_max_d": chosen_combo["kdj_max_d"],
                        "train_trades": train_trades, "train_avg_pnl_per_trade": round(train_avg, 3),
                        "test_trades": test_stats["trades"], "test_total_pnl": round(test_stats["pnl"], 2),
                        "test_win_rate": round(test_stats["wins"] / test_stats["trades"] * 100, 1) if test_stats["trades"] else 0.0,
                    })
                except (Exception, SystemExit) as e:
                    print(f"  !!! {symbol_id} {window_len}mo B{months_ago:02d} 失敗、スキップ: {e}", flush=True)
                    errors.append(f"{symbol_id} {window_len}mo B{months_ago:02d}: {e}")

        # 銘柄が1つ終わるたびに書き出す（途中終了しても部分的な結果が残るように）
        save_csv()
        print(f"  （ここまでの結果を保存: {out_path}）", flush=True)

    save_csv()
    print(f"\n詳細結果を書き出しました: {out_path}", flush=True)
    if errors:
        print(f"\n⚠️ {len(errors)}件のフォールドでエラーが発生し、スキップされました:")
        for e in errors:
            print(f"  - {e}")

    # ── サマリー（銘柄 × 学習窓 の9ヶ月合計、ベースラインとの比較） ──
    print("\n=== サマリー（検証9ヶ月の合計損益） ===")
    print(f"{'銘柄':<10}{'ベースライン':>14}{'1ヶ月学習':>14}{'2ヶ月学習':>14}{'3ヶ月学習':>14}")
    from collections import defaultdict
    totals = defaultdict(lambda: defaultdict(float))
    counts = defaultdict(lambda: defaultdict(int))
    for r in rows:
        totals[r["symbol"]][r["variant"]] += r["test_total_pnl"]
        counts[r["symbol"]][r["variant"]] += 1
    for symbol_id, _ in SYMBOLS:
        t = totals[symbol_id]
        print(f"{symbol_id:<10}{t.get('baseline', 0):>14.2f}{t.get('1mo', 0):>14.2f}{t.get('2mo', 0):>14.2f}{t.get('3mo', 0):>14.2f}")

    grand = defaultdict(float)
    for symbol_id, _ in SYMBOLS:
        for v in ("baseline", "1mo", "2mo", "3mo"):
            grand[v] += totals[symbol_id].get(v, 0)
    print("-" * 66)
    print(f"{'合計':<10}{grand['baseline']:>14.2f}{grand['1mo']:>14.2f}{grand['2mo']:>14.2f}{grand['3mo']:>14.2f}")

    print("\n完了")


if __name__ == "__main__":
    main()
