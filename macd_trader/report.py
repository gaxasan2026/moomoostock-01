"""
report.py
取引終了後に、銘柄ごとの設定値の有用性を評価するレポートを生成する

使い方:
    python3 report.py            # 本日分のレポート
    python3 report.py 2026-07-25 # 指定日のレポート
"""
import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
SYMBOLS_PATH = BASE_DIR / "data" / "symbols.json"


def categorize_exit(reason: str) -> str:
    if reason.startswith("利確"):
        return "利確 (take_profit_pct)"
    if reason.startswith("損切り"):
        return "損切り (stop_loss_pct)"
    if reason.startswith("デッドクロス") or reason.startswith("DC継続"):
        return "デッドクロス (dead_cross)"
    if reason.startswith("ピーク下落"):
        return "ピーク下落 (peak_drop_pct)"
    if reason.startswith("保有時間切れ"):
        return "保有時間切れ (max_hold_minutes)"
    return f"その他: {reason}" if reason else "不明"


def load_symbols() -> dict:
    if not SYMBOLS_PATH.exists():
        return {}
    with open(SYMBOLS_PATH, encoding="utf-8") as f:
        return json.load(f).get("symbols", {})


def load_trades(symbol_id: str, target_date: str) -> list:
    safe = symbol_id.replace(".", "_")
    path = LOG_DIR / f"trades_{safe}.csv"
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["timestamp"][:10] == target_date:
                rows.append(row)
    return rows


def analyze(symbol_id: str, rows: list, cfg: dict) -> dict:
    sells = [r for r in rows if r["action"] == "SELL"]
    buys = [r for r in rows if r["action"] == "BUY"]

    n = len(sells)
    wins = [r for r in sells if float(r["pnl_usd"]) > 0]
    total_pnl = sum(float(r["pnl_usd"]) for r in sells)
    avg_pnl_pct = sum(float(r["pnl_pct"]) for r in sells) / n if n else 0.0
    avg_hold = sum(float(r["hold_minutes"]) for r in sells) / n if n else 0.0
    win_rate = len(wins) / n * 100 if n else 0.0

    by_reason = defaultdict(lambda: {"count": 0, "pnl": 0.0, "pnl_pct_sum": 0.0})
    for r in sells:
        cat = categorize_exit(r["exit_reason"])
        by_reason[cat]["count"] += 1
        by_reason[cat]["pnl"] += float(r["pnl_usd"])
        by_reason[cat]["pnl_pct_sum"] += float(r["pnl_pct"])

    gc_durations = [float(r["gc_duration"]) for r in buys if r["gc_duration"]]
    avg_gc = sum(gc_durations) / len(gc_durations) if gc_durations else 0.0

    exit_cfg = cfg.get("exit", {})
    entry_cfg = cfg.get("entry", {})

    return {
        "symbol": symbol_id,
        "trades": n,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "avg_pnl_pct": avg_pnl_pct,
        "avg_hold": avg_hold,
        "avg_gc_duration": avg_gc,
        "by_reason": dict(by_reason),
        "config": {
            "gc_duration_minutes": entry_cfg.get("gc_duration_minutes"),
            "take_profit_pct": exit_cfg.get("take_profit_pct"),
            "stop_loss_pct": exit_cfg.get("stop_loss_pct"),
            "peak_drop_pct": exit_cfg.get("peak_drop_pct"),
            "max_hold_minutes": exit_cfg.get("max_hold_minutes"),
        },
    }


def render_html(date_str: str, results: list) -> str:
    total_trades = sum(r["trades"] for r in results)
    total_pnl = sum(r["total_pnl"] for r in results)

    rows_html = []
    for r in sorted(results, key=lambda x: x["total_pnl"]):
        pnl_color = "#3fb950" if r["total_pnl"] >= 0 else "#f85149"
        reason_rows = "".join(
            f'<tr><td class="sub">{cat}</td><td class="num">{v["count"]}</td>'
            f'<td class="num">{v["pnl"]:+.2f}</td>'
            f'<td class="num">{(v["pnl_pct_sum"]/v["count"]):+.2f}%</td></tr>'
            for cat, v in sorted(r["by_reason"].items(), key=lambda kv: -kv[1]["count"])
        )
        cfg = r["config"]
        rows_html.append(f"""
        <section class="symbol-block">
          <h2>{r['symbol']}</h2>
          <div class="stat-row">
            <div class="stat"><span class="label">取引数</span><span class="val">{r['trades']}</span></div>
            <div class="stat"><span class="label">勝率</span><span class="val">{r['win_rate']:.1f}%</span></div>
            <div class="stat"><span class="label">合計損益</span><span class="val" style="color:{pnl_color}">{r['total_pnl']:+.2f} USD</span></div>
            <div class="stat"><span class="label">平均損益率</span><span class="val">{r['avg_pnl_pct']:+.2f}%</span></div>
            <div class="stat"><span class="label">平均保有時間</span><span class="val">{r['avg_hold']:.1f}分</span></div>
            <div class="stat"><span class="label">平均GC継続(エントリー時)</span><span class="val">{r['avg_gc_duration']:.1f}分</span></div>
          </div>

          <h3>エグジット理由の内訳</h3>
          <table>
            <thead><tr><th>理由</th><th>回数</th><th>合計損益(USD)</th><th>平均損益率</th></tr></thead>
            <tbody>{reason_rows}</tbody>
          </table>

          <h3>設定値（参考）</h3>
          <table class="config-table">
            <thead><tr><th>GC継続時間<br>(gc_duration_minutes)</th><th>利確ライン<br>(take_profit_pct)</th><th>損切りライン<br>(stop_loss_pct)</th><th>ピーク下落率<br>(peak_drop_pct)</th><th>最大保有時間<br>(max_hold_minutes)</th></tr></thead>
            <tbody><tr>
              <td class="num">{cfg['gc_duration_minutes']}分</td>
              <td class="num">{cfg['take_profit_pct']}%</td>
              <td class="num">{cfg['stop_loss_pct']}%</td>
              <td class="num">{cfg['peak_drop_pct']}%</td>
              <td class="num">{cfg['max_hold_minutes']}分</td>
            </tr></tbody>
          </table>
        </section>
        """)

    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>MACD Trader レポート {date_str}</title>
<style>
  body {{ background:#0d1117; color:#e6edf3; font-family:-apple-system,'Hiragino Sans',sans-serif; margin:0; padding:32px; }}
  h1 {{ font-size:20px; margin-bottom:4px; }}
  .summary {{ color:#7d8590; font-size:14px; margin-bottom:28px; }}
  .symbol-block {{ background:#161b22; border:1px solid #30363d; border-radius:10px; padding:20px 24px; margin-bottom:20px; }}
  .symbol-block h2 {{ font-size:16px; margin:0 0 12px; }}
  .symbol-block h3 {{ font-size:13px; color:#7d8590; margin:18px 0 8px; text-transform:uppercase; letter-spacing:.03em; }}
  .stat-row {{ display:flex; flex-wrap:wrap; gap:20px; }}
  .stat {{ display:flex; flex-direction:column; gap:2px; }}
  .stat .label {{ font-size:11px; color:#7d8590; }}
  .stat .val {{ font-size:16px; font-weight:600; font-variant-numeric:tabular-nums; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th, td {{ padding:6px 10px; border-bottom:1px solid #21262d; text-align:left; }}
  th {{ color:#7d8590; font-weight:500; font-size:12px; }}
  td.num, th {{ font-variant-numeric:tabular-nums; }}
  td.sub {{ color:#e6edf3; }}
  .config-table td {{ color:#79c0ff; }}
</style></head>
<body>
  <h1>MACD Trader 日次レポート</h1>
  <div class="summary">{date_str} ｜ 全銘柄合計 {total_trades}取引 ｜ 合計損益 {total_pnl:+.2f} USD</div>
  {''.join(rows_html) if rows_html else '<p style="color:#7d8590">この日の取引データがありません。</p>'}
</body></html>"""


def main():
    target_date = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    symbols = load_symbols()
    results = []
    for sid, cfg in symbols.items():
        rows = load_trades(sid, target_date)
        if rows:
            results.append(analyze(sid, rows, cfg))

    html = render_html(target_date, results)
    out_path = LOG_DIR / f"report_{target_date}.html"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"レポートを生成しました: {out_path}")
    print(f"ブラウザで開く: file://{out_path.resolve()}")


if __name__ == "__main__":
    main()
