"""
screen.py
新規銘柄を監視対象に追加する前の、事前スクリーニングレポートを生成する。

やること（機械的な下ごしらえの自動化）:
  1. デフォルト設定（または既存登録済みならその設定）で終日バックテスト
  2. エントリー時刻を5つの時間帯に分類し、傾向を集計
  3. 5時間帯の境界から作れる全ての「連続する時間帯の組み合わせ」(15通り)を
     実際にバックテストで再生し、結果を一覧化する
     （単純な合算ではなく、逐次依存性を考慮して毎回実際に再生する）
  4. HTMLレポート + 「AI評価用にコピー」ボタンを出力する

最終判断（追加するか、どの時間帯にするか）はレポートを見てAIに評価を依頼すること。
このスクリプト自体は「追加すべきか」を自動判定しない。

使い方:
    python3 screen.py US.XXXX 2026-04-25 2026-07-24
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import time as dt_time
from pathlib import Path

BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

from config_loader import MacdConfig, EntryConfig, ExitConfig, OrderConfig, RiskConfig, OpendConfig
from backtest import _load_data, _replay, KLINE_WINDOW
from symbol_store import SymbolStore, DEFAULT_CONFIG

# 標準の5時間帯の境界（マニュアルの「取引時間帯の戦略」に合わせる）
SLOT_BOUNDARIES = [
    dt_time(9, 30), dt_time(10, 0), dt_time(11, 30),
    dt_time(14, 0), dt_time(15, 30), dt_time(16, 0),
]
SLOT_LABELS = ["①9:30-10:00 オープニングラッシュ", "②10:00-11:30 最良時間帯★",
               "③11:30-14:00 ランチ閑散", "④14:00-15:30 アフタヌーン", "⑤15:30-16:00 クロージング"]


def load_candidate_config(symbol_id: str, market: str, timeframe: str, target_position_value: float) -> dict:
    """
    既に data/symbols.json に登録済みならその設定を使う。
    未登録の候補銘柄なら、DEFAULT_CONFIG に target_position_value を適用して使う。
    """
    store = SymbolStore(str(BASE_DIR / "data" / "symbols.json"))
    cfg = store.get(symbol_id)
    if cfg:
        return cfg

    import copy as _copy
    cfg = _copy.deepcopy(DEFAULT_CONFIG)
    cfg["symbol"] = symbol_id
    cfg["market"] = market
    cfg["macd"]["timeframe"] = timeframe
    cfg["risk"]["max_position_value"] = target_position_value
    return cfg


def classify_slot(t: dt_time) -> int:
    for i in range(len(SLOT_BOUNDARIES) - 1):
        if SLOT_BOUNDARIES[i] <= t < SLOT_BOUNDARIES[i + 1]:
            return i
    return -1


def run_screen(symbol_id: str, start_date: str, end_date: str,
                market: str = "US", timeframe: str = "K_1M",
                target_position_value: float = 800.0, on_progress=None):
    def progress(msg):
        print(msg)
        if on_progress:
            on_progress(msg)

    cfg_dict = load_candidate_config(symbol_id, market, timeframe, target_position_value)
    macd_cfg = MacdConfig(**cfg_dict["macd"])
    entry_cfg = EntryConfig(**{**cfg_dict["entry"], "trading_hours_start": "", "trading_hours_end": ""})
    exit_cfg = ExitConfig(**cfg_dict["exit"])
    order_cfg = OrderConfig(**cfg_dict["order"])
    risk_cfg = RiskConfig(**cfg_dict["risk"])
    opend_cfg = OpendConfig(**cfg_dict["opend"])

    progress(f"過去データ取得中: {symbol_id} {timeframe} {start_date}〜{end_date} ...")
    df = _load_data(symbol_id, start_date, end_date, macd_cfg, opend_cfg)

    # ── ① 終日ベースラインを再生し、個別取引を記録する ──
    trades = []
    pending_entry_time = [None]

    def on_entry(price, qty, gc_dur, bar_time):
        pending_entry_time[0] = bar_time

    def on_exit(price, qty, entry, hold, reason, daily_trades, bar_time, pnl):
        trades.append({
            "entry_time": pending_entry_time[0],
            "exit_time": bar_time,
            "pnl": pnl,
            "reason": reason,
        })
        pending_entry_time[0] = None

    baseline_trades, baseline_pnl = _replay(
        df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
        on_entry=on_entry, on_exit=on_exit,
    )
    progress(f"終日ベースライン: {baseline_trades}件 / {baseline_pnl:+.2f} USD")

    # ── ② エントリー時刻を5時間帯に分類 ──
    slot_stats = [{"count": 0, "pnl": 0.0} for _ in SLOT_LABELS]
    for t in trades:
        if t["entry_time"] is None:
            continue
        idx = classify_slot(t["entry_time"].time())
        if idx >= 0:
            slot_stats[idx]["count"] += 1
            slot_stats[idx]["pnl"] += t["pnl"]

    # ── ③ 全ての連続時間帯の組み合わせ(15通り)を実際にバックテスト ──
    progress("時間帯の組み合わせを検証中（15パターン）...")
    window_results = []
    n = len(SLOT_BOUNDARIES) - 1
    for i in range(n):
        for j in range(i, n):
            start_t, end_t = SLOT_BOUNDARIES[i], SLOT_BOUNDARIES[j + 1]
            is_full_day = (i == 0 and j == n - 1)
            wc, wp = _replay(
                df, macd_cfg, entry_cfg, exit_cfg, order_cfg, risk_cfg,
                hours_filter=None if is_full_day else (start_t, end_t),
            )
            label = "終日" if is_full_day else f"{start_t.strftime('%H:%M')}-{end_t.strftime('%H:%M')}"
            window_results.append({
                "label": label, "start": start_t.strftime("%H:%M"), "end": end_t.strftime("%H:%M"),
                "trades": wc, "pnl": wp, "is_full_day": is_full_day,
            })
    window_results.sort(key=lambda r: -r["pnl"])

    # ── ④ 全体の勝率・損益比 ──
    wins = [t["pnl"] for t in trades if t["pnl"] > 0]
    losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0

    # ── ⑤ 月別内訳（ベースライン） ──
    monthly = defaultdict(lambda: {"count": 0, "pnl": 0.0})
    for t in trades:
        m = t["exit_time"].strftime("%Y-%m")
        monthly[m]["count"] += 1
        monthly[m]["pnl"] += t["pnl"]

    html = render_html(symbol_id, start_date, end_date, cfg_dict, baseline_trades, baseline_pnl,
                        win_rate, avg_win, avg_loss, slot_stats, window_results, monthly, target_position_value)

    out_path = BASE_DIR / "logs" / f"screen_{symbol_id.replace('.', '_')}_{start_date}_{end_date}.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"\nスクリーニングレポートを生成しました: {out_path}")
    print(f"ブラウザで開く: file://{out_path.resolve()}")
    return out_path


def render_html(symbol_id, start_date, end_date, cfg_dict, baseline_trades, baseline_pnl,
                 win_rate, avg_win, avg_loss, slot_stats, window_results, monthly, target_position_value):
    profit_ratio = (avg_win / abs(avg_loss)) if avg_loss else 0

    slot_rows = "".join(
        f'<tr><td>{label}</td><td class="num">{s["count"]}</td><td class="num">{s["pnl"]:+.2f}</td></tr>'
        for label, s in zip(SLOT_LABELS, slot_stats)
    )

    def _window_row(idx, r):
        row_class = ' class="best"' if idx < 3 else ""
        return (f'<tr{row_class}><td>{r["label"]}</td><td class="num">{r["trades"]}</td>'
                f'<td class="num">{r["pnl"]:+.2f}</td></tr>')

    window_rows = "".join(_window_row(idx, r) for idx, r in enumerate(window_results))

    monthly_rows = "".join(
        f'<tr><td>{m}</td><td class="num">{d["count"]}</td><td class="num">{d["pnl"]:+.2f}</td></tr>'
        for m, d in sorted(monthly.items())
    )

    top3 = window_results[:3]
    top3_text = "\n".join(
        f"- {r['label']}: {r['trades']}件 / {r['pnl']:+.2f} USD" for r in top3
    )
    monthly_text = "\n".join(
        f"- {m}: {d['count']}件 / {d['pnl']:+.2f} USD" for m, d in sorted(monthly.items())
    )

    prompt = f"""# 新規監視銘柄のスクリーニング評価を依頼します

MACD Traderに「{symbol_id}」を新規登録すべきか判断してください。
期間: {start_date} 〜 {end_date}（デフォルト設定、想定元本 ${target_position_value:.0f}）

## 終日ベースライン
- 取引数: {baseline_trades}件 / 合計損益: {baseline_pnl:+.2f} USD
- 勝率: {win_rate:.1f}% / 平均利益: {avg_win:+.2f} / 平均損失: {avg_loss:+.2f} / 損益比: {profit_ratio:.2f}

## 月別内訳（ベースライン、偏りの確認用）
{monthly_text}

## 時間帯の組み合わせ検証（上位3件）
{top3_text}

## 評価してほしいポイント
1. この銘柄は監視対象に追加する価値があるか
2. 追加する場合、上位の時間帯候補は特定の月に偏った「まぐれ」ではないか（月別内訳で確認）
3. サンプル数は十分か（少なすぎる場合は判断を保留すべきと指摘してほしい）
4. 追加するならどの時間帯設定（trading_hours_start/end）を推奨するか、あるいは終日のままが良いか
5. 追加を見送るべきという結論も歓迎します
"""

    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>銘柄スクリーニング: {symbol_id}</title>
<style>
  body {{ background:#0d1117; color:#e6edf3; font-family:-apple-system,'Hiragino Sans',sans-serif; margin:0; padding:32px; }}
  h1 {{ font-size:20px; margin-bottom:4px; }}
  h2 {{ font-size:15px; color:#7d8590; margin:28px 0 10px; text-transform:uppercase; letter-spacing:.03em; }}
  .summary {{ color:#7d8590; font-size:14px; margin-bottom:20px; }}
  .card {{ background:#161b22; border:1px solid #30363d; border-radius:10px; padding:20px 24px; margin-bottom:16px; }}
  .stat-row {{ display:flex; flex-wrap:wrap; gap:24px; }}
  .stat .label {{ font-size:11px; color:#7d8590; }}
  .stat .val {{ font-size:16px; font-weight:600; font-variant-numeric:tabular-nums; }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; }}
  th, td {{ padding:6px 10px; border-bottom:1px solid #21262d; text-align:left; }}
  th {{ color:#7d8590; font-weight:500; font-size:12px; }}
  td.num, th {{ font-variant-numeric:tabular-nums; text-align:right; }}
  tr.best {{ background:#0e2a1a; }}
  .copy-btn {{ background:#1f6feb; border:1px solid #1f6feb; color:#fff; padding:8px 16px; border-radius:6px; font-size:13px; cursor:pointer; }}
  .copy-btn:hover {{ background:#388bfd; }}
  .copy-btn.copied {{ background:#238636; border-color:#2ea043; }}
  #toast {{ position:fixed; bottom:24px; left:50%; transform:translateX(-50%); background:#238636; color:#fff; padding:10px 20px; border-radius:8px; font-size:13px; opacity:0; transition:opacity .2s; pointer-events:none; }}
</style></head>
<body>
  <h1>銘柄スクリーニング: {symbol_id}</h1>
  <div class="summary">{start_date} 〜 {end_date} ｜ 想定元本 ${target_position_value:.0f} ｜ デフォルト設定・終日</div>

  <button class="copy-btn" onclick="copyPrompt(this)">🤖 AI評価用にコピー</button>

  <div class="card" style="margin-top:20px">
    <div class="stat-row">
      <div class="stat"><span class="label">取引数</span><br><span class="val">{baseline_trades}</span></div>
      <div class="stat"><span class="label">合計損益</span><br><span class="val" style="color:{'#3fb950' if baseline_pnl>=0 else '#f85149'}">{baseline_pnl:+.2f} USD</span></div>
      <div class="stat"><span class="label">勝率</span><br><span class="val">{win_rate:.1f}%</span></div>
      <div class="stat"><span class="label">損益比</span><br><span class="val">{profit_ratio:.2f}</span></div>
    </div>
  </div>

  <h2>時間帯別の傾向（エントリー時刻ベース）</h2>
  <div class="card">
    <table>
      <thead><tr><th>時間帯</th><th>件数</th><th>合計損益</th></tr></thead>
      <tbody>{slot_rows}</tbody>
    </table>
  </div>

  <h2>時間帯の組み合わせ検証（全15パターン、実際に再生・損益順）</h2>
  <div class="card">
    <table>
      <thead><tr><th>時間帯</th><th>件数</th><th>合計損益</th></tr></thead>
      <tbody>{window_rows}</tbody>
    </table>
  </div>

  <h2>月別内訳（ベースライン、偏りの確認用）</h2>
  <div class="card">
    <table>
      <thead><tr><th>月</th><th>件数</th><th>合計損益</th></tr></thead>
      <tbody>{monthly_rows}</tbody>
    </table>
  </div>

  <div id="toast">クリップボードにコピーしました</div>

<script>
const PROMPT_TEXT = {json.dumps(prompt, ensure_ascii=False)};

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
  try {{ ok = document.execCommand('copy'); }} catch (e) {{ ok = false; }}
  document.body.removeChild(ta);
  if (ok) {{ showCopied(btn); }}
  else {{ window.prompt('自動コピーに失敗しました。以下を選択して Cmd+C でコピーしてください:', text); }}
}}

function copyPrompt(btn) {{
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(PROMPT_TEXT).then(() => showCopied(btn)).catch(() => fallbackCopy(PROMPT_TEXT, btn));
  }} else {{
    fallbackCopy(PROMPT_TEXT, btn);
  }}
}}
</script>
</body></html>"""


def main():
    if len(sys.argv) < 4:
        print("使い方: python3 screen.py <SYMBOL> <開始日> <終了日> [市場(US/HK)] [足種] [想定元本]")
        print("例:     python3 screen.py US.AAPL 2026-04-25 2026-07-24")
        sys.exit(1)
    symbol_id = sys.argv[1].upper()
    start_date, end_date = sys.argv[2], sys.argv[3]
    market = sys.argv[4] if len(sys.argv) > 4 else "US"
    timeframe = sys.argv[5] if len(sys.argv) > 5 else "K_1M"
    target_position_value = float(sys.argv[6]) if len(sys.argv) > 6 else 800.0
    run_screen(symbol_id, start_date, end_date, market, timeframe, target_position_value)


if __name__ == "__main__":
    main()
