"""
batch_manager.py
複数銘柄・複数パラメータ組み合わせのスイングバックテストを、非同期ジョブとして実行する。

backtest_studio/batch_manager.py と同じ「バックグラウンドスレッド＋ポーリング」パターンを
踏襲するが、以下の点はswing_trader向けに変更している:
- fast_replay ではなく swing_backtest.swing_replay を使う（K_60M/K_DAYの本数は
  K_1Mよりずっと少なく、高速化なしの実装で十分という判断は swing_backtest.py と同じ）
- 月次ではなく「年次」の内訳で頑健性を確認する。実測（このセッションの較正作業）で
  スイングトレードは1銘柄あたり月0〜1件程度しか取引が発生せず、月次では
  ほとんどのセルが空になり頑健性の判断材料にならないことを確認済みのため
- 足種（timeframe）をバッチ全体で1つ選べるようにする（K_60M/K_DAY）。
  日足・60分足で最適なパラメータが大きく異なることが分かっているため
"""
from __future__ import annotations

import dataclasses
import itertools
import sys
import threading
import time
import uuid
from pathlib import Path

MACD_TRADER_DIR = Path(__file__).parent.parent / "macd_trader"
SWING_TRADER_DIR = Path(__file__).parent.parent / "swing_trader"
sys.path.insert(0, str(MACD_TRADER_DIR))
sys.path.insert(0, str(SWING_TRADER_DIR))

from backtest import _load_data  # noqa: E402
from config_loader import (  # noqa: E402
    MacdConfig, EntryConfig, ExitConfig, OrderConfig, RiskConfig, OpendConfig, OscillatorConfig,
)

from swing_backtest import swing_replay  # noqa: E402

import swing_client

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
              grid_spec: dict, timeframe: str | None = None) -> str:
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
            target=self._run, args=(job, symbols, start_date, end_date, grid_spec, timeframe),
            daemon=True, name=f"swing-batch-{job_id}",
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
              grid_spec: dict, timeframe: str | None):
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

                base_cfg = swing_client.get_defaults() if is_new else swing_client.get_symbol(symbol_id)
                if base_cfg is None:
                    job.progress["symbols"][i]["state"] = "error"
                    job.progress["symbols"][i]["detail"] = "銘柄が見つかりません"
                    continue

                macd_cfg = MacdConfig(**base_cfg["macd"])
                if timeframe:
                    macd_cfg = dataclasses.replace(macd_cfg, timeframe=timeframe)
                base_entry_cfg = EntryConfig(**base_cfg["entry"])
                exit_cfg = ExitConfig(**base_cfg["exit"])
                order_cfg = OrderConfig(**base_cfg["order"])
                risk_cfg = RiskConfig(**base_cfg["risk"])
                opend_cfg = OpendConfig(**base_cfg["opend"])
                osc_cfg = OscillatorConfig(**base_cfg.get("oscillator", {}))

                job.progress["symbols"][i]["detail"] = "データ取得中...（初回のみ）"
                try:
                    df = _load_data(symbol_id, start_date, end_date, macd_cfg, opend_cfg)
                except (Exception, SystemExit) as e:
                    job.progress["symbols"][i]["state"] = "error"
                    job.progress["symbols"][i]["detail"] = str(e)
                    continue

                for ci, raw_overrides in enumerate(combos):
                    job.progress["symbols"][i]["detail"] = f"再生中... ({ci}/{len(combos)})"
                    overrides = _typed_overrides(base_entry_cfg, raw_overrides)
                    entry_cfg = dataclasses.replace(base_entry_cfg, **overrides)
                    yearly: dict[str, float] = {}
                    win_count = 0
                    hold_total = 0.0

                    def on_exit(price, qty, entry, hold, reason, daily_trades, bar_time, pnl, gc_dur):
                        nonlocal win_count, hold_total
                        key = str(bar_time.year)
                        yearly[key] = yearly.get(key, 0.0) + pnl
                        hold_total += hold
                        if pnl > 0:
                            win_count += 1

                    try:
                        closed_trades, total_pnl = swing_replay(
                            df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
                            osc_cfg=osc_cfg, on_exit=on_exit,
                        )
                    except (Exception, SystemExit) as e:
                        job.progress["symbols"][i]["detail"] = f"失敗: {e}"
                        continue

                    years_sorted = sorted(yearly.items())
                    positive_years = sum(1 for _, v in years_sorted if v > 0)
                    all_results.append({
                        "symbol_id": symbol_id,
                        "is_new": is_new,
                        "timeframe": macd_cfg.timeframe,
                        "overrides": overrides,  # 型変換済み（数値/bool）。symbols.jsonへ文字列のまま書かれるのを防ぐ
                        "trades": closed_trades,
                        "total_pnl": round(total_pnl, 2),
                        "win_rate": round(win_count / closed_trades * 100, 1) if closed_trades else 0.0,
                        "avg_hold_minutes": round(hold_total / closed_trades, 1) if closed_trades else 0.0,
                        "yearly": [{"year": y, "pnl": round(v, 2)} for y, v in years_sorted],
                        "robust_total": len(years_sorted),
                        "robust_positive": positive_years,
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
    """{'gc_duration_minutes': ['120','300'], 'kdj_max_d': ['0','50']} のような仕様から
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
