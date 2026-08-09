"""
daily_pnl.py
取引ログ(logs/trades_*.csv)から、米国東部時間(ET)の取引日単位で損益を集計する。

bot（macd_trader/swing_trader）はこのマシンのローカル時刻（JST）でタイムスタンプを
記録しているため、そのままJST日付で区切ると米国の取引日とズレる
（JSTの1日は、時差の関係で米国の2営業日にまたがる）。そこでタイムスタンプを
JSTとみなしてET（America/New_York、サマータイムも自動考慮）へ変換してから
日付を決定する。

macd_trader/swing_traderの両方から読み取り専用でimportして使う共通ロジック
（backtest_studio等が既にorder_manager等をread-only importしているのと同じ流儀）。
"""
import csv
from collections import defaultdict
from datetime import date as date_cls
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
ET = ZoneInfo("America/New_York")


def _to_et_date(ts: datetime) -> date_cls:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=JST)
    return ts.astimezone(ET).date()


def _load_realized_pnl_by_et_date(log_dir: Path) -> dict:
    """{ET日付のISO文字列: {"pnl_usd": float, "trades": int, "wins": int}}"""
    by_date = defaultdict(lambda: {"pnl_usd": 0.0, "trades": 0, "wins": 0})
    if not log_dir.exists():
        return by_date
    for csv_path in sorted(log_dir.glob("trades_*.csv")):
        try:
            with open(csv_path, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get("action") != "SELL":
                        continue
                    pnl_raw = row.get("pnl_usd")
                    if pnl_raw in (None, ""):
                        continue
                    try:
                        ts = datetime.fromisoformat(row["timestamp"])
                        pnl = float(pnl_raw)
                    except (ValueError, KeyError):
                        continue
                    key = _to_et_date(ts).isoformat()
                    entry = by_date[key]
                    entry["pnl_usd"] += pnl
                    entry["trades"] += 1
                    if pnl > 0:
                        entry["wins"] += 1
        except OSError:
            continue
    return by_date


def compute_daily_pnl(log_dir: Path, days: int = 30) -> list[dict]:
    """
    直近days日分（米国取引日ベース、今日を含む連続した日付）の損益を返す。
    取引がない日も0円として含める（グラフの日付軸を連続させ、土日・祝日の
    抜けが「データ欠損」と誤読されないようにするため）。
    cumulative_pnl_usd（累積損益）は、表示ウィンドウより前の全履歴を含めて
    正しく計算する（ウィンドウ内だけで計算すると、直近30日の最初の日の
    累積値が不自然にゼロ付近から始まってしまうため）。
    """
    by_date = _load_realized_pnl_by_et_date(log_dir)

    today_et = datetime.now(JST).astimezone(ET).date()
    window_start = today_et - timedelta(days=days - 1)

    running = 0.0
    for key in sorted(by_date.keys()):
        if date_cls.fromisoformat(key) < window_start:
            running += by_date[key]["pnl_usd"]

    result = []
    cursor = window_start
    while cursor <= today_et:
        key = cursor.isoformat()
        e = by_date.get(key, {"pnl_usd": 0.0, "trades": 0, "wins": 0})
        running += e["pnl_usd"]
        result.append({
            "date": key,
            "pnl_usd": round(e["pnl_usd"], 2),
            "trades": e["trades"],
            "win_rate": round(e["wins"] / e["trades"] * 100, 1) if e["trades"] else 0.0,
            "cumulative_pnl_usd": round(running, 2),
        })
        cursor += timedelta(days=1)
    return result
