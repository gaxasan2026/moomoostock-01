"""
batch_manager.py
複数銘柄・複数パラメータ組み合わせのバックテストを、非同期ジョブとして実行する。

macd_trader/screen_manager.py と同じ「バックグラウンドスレッド＋ポーリング」パターンを
踏襲するが、以下の点は意図的に変更している:
- 銘柄ごとにシリアル実行し、進捗を銘柄単位の構造化リストとして持つ
  （OpenDへの同時接続を避けるため。文字列1本の進捗ではなく銘柄ごとの状態を持つ）
- backtest.py の _load_data/load_symbol_config が送出する SystemExit を確実に捕捉する
  （screen_manager.py の except Exception だけでは捕まらない既知の穴を引き継がない）
- ジョブは軽いTTLで間引く（screen_manager.py は無期限に貯まる設計）

パラメータ組み合わせ（grid_spec）は {パラメータ名: [値, ...]} の形式。
空辞書なら単発実行、1キーならSweep相当、複数キーならGrid相当になる
（呼び出し側でモードを区別する必要はない）。
"""
from __future__ import annotations

import dataclasses
import itertools
import sys
import threading
import time
import uuid
from datetime import time as dt_time
from pathlib import Path

MACD_TRADER_DIR = Path(__file__).parent.parent / "macd_trader"
sys.path.insert(0, str(MACD_TRADER_DIR))

from backtest import _load_data  # noqa: E402
from fast_replay import fast_replay  # noqa: E402
# fast_replayはbacktest.py._replay()と数値的・取引単位で完全一致することを検証済み
# （macd_trader/fast_indicators.py・fast_replay.py参照）。高速化倍率は約4〜10倍。
from config_loader import MacdConfig, EntryConfig, ExitConfig, OrderConfig, RiskConfig, OpendConfig  # noqa: E402

import macd_client

MAX_COMBINATIONS = 500   # 銘柄数 × グリッド組み合わせ数の上限（暴走防止）
JOB_TTL_SECONDS = 3600   # 完了/エラー後1時間で間引く


class BatchJob:
    def __init__(self, job_id: str):
        self.job_id = job_id
        self.status = "running"  # running / done / error
        self.progress: dict = {"symbols": []}
        self.results: list = []
        self.error: str | None = None
        self.created_at = time.time()


class BatchBacktestManager:
    def __init__(self):
        self._jobs: dict[str, BatchJob] = {}
        self._lock = threading.Lock()

    def start(self, symbols: list[dict], start_date: str, end_date: str,
              grid_spec: dict, hours_filter: tuple[dt_time, dt_time] | None = None) -> str:
        self._evict_old()
        job_id = uuid.uuid4().hex[:12]
        job = BatchJob(job_id)
        job.progress["symbols"] = [
            {"symbol_id": s["symbol_id"], "state": "pending", "detail": "待機中"}
            for s in symbols
        ]
        with self._lock:
            self._jobs[job_id] = job
        thread = threading.Thread(
            target=self._run, args=(job, symbols, start_date, end_date, grid_spec, hours_filter),
            daemon=True, name=f"batch-{job_id}",
        )
        thread.start()
        return job_id

    def get(self, job_id: str):
        return self._jobs.get(job_id)

    def _evict_old(self):
        now = time.time()
        with self._lock:
            stale = [jid for jid, j in self._jobs.items()
                     if j.status != "running" and now - j.created_at > JOB_TTL_SECONDS]
            for jid in stale:
                del self._jobs[jid]

    def _run(self, job: BatchJob, symbols: list[dict], start_date: str, end_date: str,
              grid_spec: dict, hours_filter):
        try:
            combos = _build_combos(grid_spec)
            total_combos = len(symbols) * len(combos)
            if total_combos > MAX_COMBINATIONS:
                raise ValueError(
                    f"組み合わせ数が多すぎます（{total_combos}件 > 上限{MAX_COMBINATIONS}件）。"
                    f"銘柄数かパラメータの値の数を減らしてください。"
                )

            all_results = []
            for i, sym in enumerate(symbols):
                symbol_id = sym["symbol_id"]
                is_new = bool(sym.get("is_new", False))
                job.progress["symbols"][i]["state"] = "active"
                job.progress["symbols"][i]["detail"] = "設定取得中..."

                base_cfg = macd_client.get_defaults() if is_new else macd_client.get_symbol(symbol_id)
                if base_cfg is None:
                    job.progress["symbols"][i]["state"] = "error"
                    job.progress["symbols"][i]["detail"] = "銘柄が見つかりません"
                    continue

                macd_cfg = MacdConfig(**base_cfg["macd"])
                base_entry_cfg = EntryConfig(**base_cfg["entry"])
                exit_cfg = ExitConfig(**base_cfg["exit"])
                order_cfg = OrderConfig(**base_cfg["order"])
                risk_cfg = RiskConfig(**base_cfg["risk"])
                opend_cfg = OpendConfig(**base_cfg["opend"])

                job.progress["symbols"][i]["detail"] = "データ取得中...（初回のみ）"
                df = _load_data(symbol_id, start_date, end_date, macd_cfg, opend_cfg)

                for ci, raw_overrides in enumerate(combos):
                    job.progress["symbols"][i]["detail"] = f"再生中... ({ci}/{len(combos)})"
                    overrides = _typed_overrides(base_entry_cfg, raw_overrides)
                    entry_cfg = dataclasses.replace(base_entry_cfg, **overrides)
                    monthly: dict[str, float] = {}
                    win_count = 0
                    hold_total = 0.0

                    def on_exit(price, qty, entry, hold, reason, daily_trades, bar_time, pnl):
                        nonlocal win_count, hold_total
                        key = bar_time.strftime("%Y-%m")
                        monthly[key] = monthly.get(key, 0.0) + pnl
                        hold_total += hold
                        if pnl > 0:
                            win_count += 1

                    closed_trades, total_pnl = fast_replay(
                        df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
                        hours_filter=hours_filter, on_exit=on_exit,
                    )
                    months_sorted = sorted(monthly.items())
                    positive_months = sum(1 for _, v in months_sorted if v > 0)
                    all_results.append({
                        "symbol_id": symbol_id,
                        "is_new": is_new,
                        "overrides": overrides,  # 型変換済み（数値/bool）。symbols.jsonへ文字列のまま書かれるのを防ぐ
                        "trades": closed_trades,
                        "total_pnl": round(total_pnl, 2),
                        "win_rate": round(win_count / closed_trades * 100, 1) if closed_trades else 0.0,
                        "avg_hold_minutes": round(hold_total / closed_trades, 1) if closed_trades else 0.0,
                        "monthly": [{"month": m, "pnl": round(v, 2)} for m, v in months_sorted],
                        "robust_total": len(months_sorted),
                        "robust_positive": positive_months,
                    })

                job.progress["symbols"][i]["state"] = "done"
                job.progress["symbols"][i]["detail"] = f"{len(combos)}/{len(combos)}件 完了"

            all_results.sort(key=lambda r: r["total_pnl"], reverse=True)
            job.results = all_results
            job.status = "done"
        except (Exception, SystemExit) as e:
            job.status = "error"
            job.error = str(e)


def _build_combos(grid_spec: dict) -> list[dict]:
    """{'gc_duration_minutes': ['3','5'], 'kdj_max_d': ['0','50']} のような仕様から
    全組み合わせのリスト（生の文字列値のまま）を作る。型変換は_typed_overridesで行う。
    空辞書なら単発実行として [{}] を返す。"""
    if not grid_spec:
        return [{}]
    names = list(grid_spec.keys())
    value_lists = [grid_spec[name] for name in names]
    return [dict(zip(names, combo)) for combo in itertools.product(*value_lists)]


def _typed_overrides(base_entry_cfg: EntryConfig, raw_overrides: dict) -> dict:
    field_types = {f.name: f.type for f in dataclasses.fields(base_entry_cfg)}
    typed = {}
    for name, raw in raw_overrides.items():
        if name not in field_types:
            raise ValueError(f"エントリー設定に存在しないパラメータです: {name}")
        ftype = field_types[name]
        typed[name] = raw if ftype is bool else ftype(raw)
    return typed
