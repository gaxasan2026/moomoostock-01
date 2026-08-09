"""
report.py
取引終了後に、銘柄ごとの設定値の有用性を評価するレポートを生成する
（macd_trader/report.py のスイング版。ロジックはほぼ同じだが、時間軸が
分単位ではなく日/時間単位になるため表示を調整し、AI評価プロンプトも
スイング特有の観点（ギャップリスク・足種）を含める）。

使い方:
    python3 report.py                              # 本日分のレポート
    python3 report.py 2026-07-25                    # 指定日のレポート
    python3 report.py 2026-07-01_2026-07-30         # 期間指定のレポート（開始日_終了日、両端含む）
"""
import csv
import io
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOG_DIR = BASE_DIR / "logs"
SYMBOLS_PATH = BASE_DIR / "data" / "symbols.json"

CSV_FIELDS = [
    "timestamp", "action", "symbol", "price", "quantity",
    "entry_price", "pnl_usd", "pnl_pct",
    "hold_minutes", "exit_reason", "gc_duration", "daily_trades",
]


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


def fmt_minutes_as_days(minutes) -> str:
    """分単位の値を「Xdays（Y分）」形式にする。スイングでは値が大きく分単位のままだと読みにくいため"""
    if minutes is None:
        return "—"
    m = float(minutes)
    days = m / (24 * 60)
    return f"{days:.1f}日（{m:.0f}分）"


def load_symbols() -> dict:
    if not SYMBOLS_PATH.exists():
        return {}
    with open(SYMBOLS_PATH, encoding="utf-8") as f:
        return json.load(f).get("symbols", {})


def parse_date_arg(arg: str) -> tuple[str, str]:
    """'YYYY-MM-DD' または 'YYYY-MM-DD_YYYY-MM-DD'（期間指定）を (開始日, 終了日) に変換する"""
    if "_" in arg:
        start, end = arg.split("_", 1)
        if start > end:
            start, end = end, start
        return start, end
    return arg, arg


def load_trades(symbol_id: str, start_date: str, end_date: str) -> list:
    safe = symbol_id.replace(".", "_")
    path = LOG_DIR / f"trades_{safe}.csv"
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            d = row["timestamp"][:10]
            if start_date <= d <= end_date:
                rows.append(row)
    return rows


def rows_to_csv_text(rows: list) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


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

    macd_cfg = cfg.get("macd", {})
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
            "timeframe": macd_cfg.get("timeframe"),
            "gc_duration_minutes": entry_cfg.get("gc_duration_minutes"),
            "macd_histogram_min": entry_cfg.get("macd_histogram_min"),
            "volume_surge_ratio": entry_cfg.get("volume_surge_ratio"),
            "take_profit_pct": exit_cfg.get("take_profit_pct"),
            "stop_loss_pct": exit_cfg.get("stop_loss_pct"),
            "peak_drop_pct": exit_cfg.get("peak_drop_pct"),
            "peak_drop_duration_minutes": exit_cfg.get("peak_drop_duration_minutes"),
            "max_hold_minutes": exit_cfg.get("max_hold_minutes"),
        },
    }


def build_reason_table_text(by_reason: dict) -> str:
    lines = ["理由 | 回数 | 合計損益(USD) | 平均損益率"]
    for cat, v in sorted(by_reason.items(), key=lambda kv: -kv[1]["count"]):
        avg_pct = v["pnl_pct_sum"] / v["count"]
        lines.append(f"{cat} | {v['count']} | {v['pnl']:+.2f} | {avg_pct:+.2f}%")
    return "\n".join(lines)


def build_symbol_prompt(date_str: str, r: dict, raw_csv_text: str) -> str:
    cfg = r["config"]
    return f"""# スイングトレードボットの設定値評価を依頼します

moomoo OpenAPI連携のMACDゴールデンクロス/デッドクロス スイングトレードボット（Paper Trading、
数日〜数週間の保有を想定）について、{date_str} の銘柄「{r['symbol']}」の取引結果から、
設定値（エントリー/エグジット条件）が実際の値動きに対して適切かどうか評価してください。

## 現在の設定値
- 足種 (timeframe): {cfg['timeframe']}
- GC継続時間 (gc_duration_minutes): {fmt_minutes_as_days(cfg['gc_duration_minutes'])}
- MACDヒストグラム下限 (macd_histogram_min): {cfg['macd_histogram_min']}
- 出来高倍率 (volume_surge_ratio): {cfg['volume_surge_ratio']}倍
- 利確ライン (take_profit_pct): {cfg['take_profit_pct']}%
- 損切りライン (stop_loss_pct): {cfg['stop_loss_pct']}%
- ピーク下落率 (peak_drop_pct): {cfg['peak_drop_pct']}%
- ピーク下落確認時間 (peak_drop_duration_minutes): {fmt_minutes_as_days(cfg['peak_drop_duration_minutes'])}
- 最大保有時間 (max_hold_minutes): {fmt_minutes_as_days(cfg['max_hold_minutes'])}

## 集計結果
- 取引数: {r['trades']}件　勝率: {r['win_rate']:.1f}%
- 合計損益: {r['total_pnl']:+.2f} USD　平均損益率: {r['avg_pnl_pct']:+.2f}%
- 平均保有時間: {fmt_minutes_as_days(r['avg_hold'])}　平均GC継続(エントリー時): {fmt_minutes_as_days(r['avg_gc_duration'])}

### エグジット理由の内訳
{build_reason_table_text(r['by_reason'])}

## 個別取引の生データ (CSV)
```csv
{raw_csv_text.strip()}
```

## 評価してほしいポイント
1. 利確ライン・損切りラインは実際に機能しているか（一度も到達していない場合、閾値が広すぎる可能性は？）
2. 損切りでの実際の損益率が、設定値（stop_loss_pct）を大きく超えていないか
   （日足/60分足はバー間の窓開けにより、想定を超える損失になり得ます）
3. デッドクロス/ピーク下落による決済が多い場合、その決済タイミングの損益幅は妥当か
4. エントリー条件（GC継続時間・出来高条件）は厳しすぎる/緩すぎるか。足種（timeframe）に対して妥当な長さか
5. 上記を踏まえ、具体的にどのパラメータをどの値に変更すべきか（数値を提案してください）
6. 現在のサンプル数で判断を下すにはデータが不足していないか
   （スイングトレードは取引頻度が低いため、月0〜1件程度でも珍しくありません）
"""


def build_portfolio_prompt(date_str: str, results: list, raw_by_symbol: dict) -> str:
    total_trades = sum(r["trades"] for r in results)
    total_pnl = sum(r["total_pnl"] for r in results)

    per_symbol_summary = "\n".join(
        f"- {r['symbol']} ({r['config']['timeframe']}): {r['trades']}件 / 勝率{r['win_rate']:.1f}% / "
        f"損益{r['total_pnl']:+.2f}USD / 主なエグジット理由: "
        f"{max(r['by_reason'].items(), key=lambda kv: kv[1]['count'])[0] if r['by_reason'] else '—'}"
        for r in sorted(results, key=lambda x: x["total_pnl"])
    )

    all_csv_blocks = "\n\n".join(
        f"### {sid}\n```csv\n{rows_to_csv_text(rows).strip()}\n```"
        for sid, rows in raw_by_symbol.items()
    )

    return f"""# スイングトレードボット 全銘柄の設定値評価を依頼します

moomoo OpenAPI連携のMACDゴールデンクロス/デッドクロス スイングトレードボット（Paper Trading）について、
{date_str} の全{len(results)}銘柄の取引結果から、各銘柄の設定値の有用性と、
銘柄間の傾向の違いを評価してください。1銘柄あたりの取引頻度が低いため、
単一銘柄・単一期間の結果だけで判断を確定させず、傾向として捉えてください。

## 全体サマリー
全銘柄合計 {total_trades}取引 ｜ 合計損益 {total_pnl:+.2f} USD

## 銘柄別サマリー
{per_symbol_summary}

## 個別取引の生データ (CSV, 銘柄別)
{all_csv_blocks}

## 評価してほしいポイント
1. 銘柄ごとに利確・損切り・デッドクロス・ピーク下落のどれで決済されることが多いか、傾向の違い
2. 損切りの実際の損益率が設定値を大きく超えている銘柄はないか（窓開けの影響が大きい銘柄の特定）
3. 損益が悪い銘柄と良い銘柄で、設定値または値動きの特性にどんな違いがありそうか
4. K_60MとK_DAYで足種が混在している場合、その違いが結果に影響していそうか
5. 全銘柄共通で調整した方が良さそうなパラメータはあるか。逆に銘柄ごとに個別調整すべきパラメータはあるか
6. このまま継続監視すべき銘柄、設定変更すべき銘柄、監視停止を検討すべき銘柄の仕分け
"""


def render_html(date_str: str, results: list, raw_by_symbol: dict) -> str:
    total_trades = sum(r["trades"] for r in results)
    total_pnl = sum(r["total_pnl"] for r in results)

    prompts = {r["symbol"]: build_symbol_prompt(date_str, r, rows_to_csv_text(raw_by_symbol[r["symbol"]]))
               for r in results}
    if results:
        prompts["__portfolio__"] = build_portfolio_prompt(date_str, results, raw_by_symbol)

    rows_html = []
    for r in sorted(results, key=lambda x: x["total_pnl"]):
        pnl_color = "var(--good)" if r["total_pnl"] >= 0 else "var(--bad)"
        reason_rows = "".join(
            f'<tr><td class="sub">{cat}</td><td class="num">{v["count"]}</td>'
            f'<td class="num">{v["pnl"]:+.2f}</td>'
            f'<td class="num">{(v["pnl_pct_sum"]/v["count"]):+.2f}%</td></tr>'
            for cat, v in sorted(r["by_reason"].items(), key=lambda kv: -kv[1]["count"])
        )
        cfg = r["config"]
        sym = r["symbol"]
        rows_html.append(f"""
        <section class="symbol-block">
          <div class="block-head">
            <h2>{sym}<span class="tf-badge">{cfg['timeframe']}</span></h2>
            <button class="copy-btn" onclick="copyPrompt('{sym}', this)">🤖 AI評価用にコピー</button>
          </div>
          <div class="stat-row">
            <div class="stat"><span class="label">取引数</span><span class="val">{r['trades']}</span></div>
            <div class="stat"><span class="label">勝率</span><span class="val">{r['win_rate']:.1f}%</span></div>
            <div class="stat"><span class="label">合計損益</span><span class="val" style="color:{pnl_color}">{r['total_pnl']:+.2f} USD</span></div>
            <div class="stat"><span class="label">平均損益率</span><span class="val">{r['avg_pnl_pct']:+.2f}%</span></div>
            <div class="stat"><span class="label">平均保有時間</span><span class="val">{fmt_minutes_as_days(r['avg_hold'])}</span></div>
            <div class="stat"><span class="label">平均GC継続(エントリー時)</span><span class="val">{fmt_minutes_as_days(r['avg_gc_duration'])}</span></div>
          </div>

          <h3>エグジット理由の内訳</h3>
          <table>
            <thead><tr><th>理由</th><th>回数</th><th>合計損益(USD)</th><th>平均損益率</th></tr></thead>
            <tbody>{reason_rows}</tbody>
          </table>

          <h3>設定値（参考）</h3>
          <table class="config-table">
            <thead><tr><th>GC継続時間</th><th>利確</th><th>損切り</th><th>ピーク下落率</th><th>最大保有時間</th></tr></thead>
            <tbody><tr>
              <td class="num">{fmt_minutes_as_days(cfg['gc_duration_minutes'])}</td>
              <td class="num">{cfg['take_profit_pct']}%</td>
              <td class="num">{cfg['stop_loss_pct']}%</td>
              <td class="num">{cfg['peak_drop_pct']}%</td>
              <td class="num">{fmt_minutes_as_days(cfg['max_hold_minutes'])}</td>
            </tr></tbody>
          </table>
        </section>
        """)

    portfolio_button = (
        '<button class="copy-btn portfolio" onclick="copyPrompt(\'__portfolio__\', this)">'
        '🤖 全銘柄まとめてAI評価用にコピー</button>'
        if results else ""
    )

    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>Swing Trader レポート {date_str}</title>
<style>
  :root{{
    --bg:#f6f5f1; --panel:#ffffff; --panel2:#eeecea; --border:#dedad4;
    --text:#1a2038; --text2:#5a637a; --text3:#8a93aa;
    --accent:#1a5fc0; --accent-bg:#e8f0fd;
    --good:#147a4e; --bad:#c0301a; --header-bg:#0e2140;
  }}
  body {{ background:var(--bg); color:var(--text); font-family:-apple-system,'Hiragino Sans',sans-serif; margin:0; padding:32px; }}
  h1 {{ font-size:20px; margin-bottom:4px; }}
  .summary-row {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:28px; flex-wrap:wrap; gap:12px; }}
  .summary {{ color:var(--text2); font-size:14px; }}
  .symbol-block {{ background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:20px 24px; margin-bottom:20px; }}
  .block-head {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:12px; }}
  .symbol-block h2 {{ font-size:16px; margin:0; display:flex; align-items:center; gap:8px; }}
  .tf-badge {{ font-size:11px; font-weight:600; padding:2px 8px; border-radius:999px; background:var(--accent-bg); color:var(--accent); }}
  .symbol-block h3 {{ font-size:13px; color:var(--text2); margin:18px 0 8px; text-transform:uppercase; letter-spacing:.03em; }}
  .stat-row {{ display:flex; flex-wrap:wrap; gap:20px; }}
  .stat {{ display:flex; flex-direction:column; gap:2px; }}
  .stat .label {{ font-size:11px; color:var(--text2); }}
  .stat .val {{ font-size:16px; font-weight:600; font-variant-numeric:tabular-nums; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th, td {{ padding:6px 10px; border-bottom:1px solid var(--border); text-align:left; }}
  th {{ color:var(--text2); font-weight:500; font-size:12px; }}
  td.num, th {{ font-variant-numeric:tabular-nums; }}
  td.sub {{ color:var(--text); }}
  .config-table td {{ color:var(--accent); }}
  .copy-btn {{ background:var(--panel2); border:1px solid var(--border); color:var(--text); padding:6px 12px; border-radius:6px; font-size:12px; cursor:pointer; white-space:nowrap; }}
  .copy-btn:hover {{ background:var(--border); }}
  .copy-btn.portfolio {{ background:var(--accent); border-color:var(--accent); color:#fff; font-size:13px; padding:8px 16px; }}
  .copy-btn.portfolio:hover {{ filter:brightness(1.1); }}
  .copy-btn.copied {{ background:var(--good); border-color:var(--good); color:#fff; }}
  #toast {{ position:fixed; bottom:24px; left:50%; transform:translateX(-50%); background:var(--good); color:#fff; padding:10px 20px; border-radius:8px; font-size:13px; opacity:0; transition:opacity .2s; pointer-events:none; }}
</style></head>
<body>
  <h1>🕯 Swing Trader 取引レポート</h1>
  <div class="summary-row">
    <div class="summary">{date_str} ｜ 全銘柄合計 {total_trades}取引 ｜ 合計損益 {total_pnl:+.2f} USD</div>
    {portfolio_button}
  </div>
  {''.join(rows_html) if rows_html else '<p style="color:var(--text2)">この期間の取引データがありません。</p>'}

  <div id="toast">クリップボードにコピーしました</div>

<script>
const PROMPTS = {json.dumps(prompts, ensure_ascii=False)};

function showCopied(btn) {{
  const toast = document.getElementById('toast');
  toast.style.opacity = 1;
  setTimeout(() => toast.style.opacity = 0, 2000);
  if (btn) {{
    const original = btn.textContent;
    btn.textContent = '✅ コピー済み';
    btn.classList.add('copied');
    setTimeout(() => {{ btn.textContent = original; btn.classList.remove('copied'); }}, 2000);
  }}
}}

function fallbackCopy(text, btn) {{
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  let ok = false;
  try {{
    ok = document.execCommand('copy');
  }} catch (e) {{
    ok = false;
  }}
  document.body.removeChild(ta);
  if (ok) {{
    showCopied(btn);
  }} else {{
    window.prompt('自動コピーに失敗しました。以下を選択して Cmd+C でコピーしてください:', text);
  }}
}}

function copyPrompt(key, btn) {{
  const text = PROMPTS[key];
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(() => showCopied(btn)).catch(() => fallbackCopy(text, btn));
  }} else {{
    fallbackCopy(text, btn);
  }}
}}
</script>
</body></html>"""


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%Y-%m-%d")
    start_date, end_date = parse_date_arg(arg)
    is_range = start_date != end_date
    date_label = f"{start_date} 〜 {end_date}" if is_range else start_date
    file_label = f"{start_date}_{end_date}" if is_range else start_date

    symbols = load_symbols()
    results = []
    raw_by_symbol = {}
    for sid, cfg in symbols.items():
        rows = load_trades(sid, start_date, end_date)
        if rows:
            results.append(analyze(sid, rows, cfg))
            raw_by_symbol[sid] = rows

    html = render_html(date_label, results, raw_by_symbol)
    out_path = LOG_DIR / f"report_{file_label}.html"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"レポートを生成しました: {out_path}")
    print(f"ブラウザで開く: file://{out_path.resolve()}")


if __name__ == "__main__":
    main()
