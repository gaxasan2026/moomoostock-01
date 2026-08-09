"""
verify_daily_pnl.py
日次損益集計（macd_trader/daily_pnl.py の compute_daily_pnl）の恒久的な回帰テスト。

このロジックは、bot（macd_trader/swing_trader）がこのマシンのローカル時刻（JST）で
記録したタイムスタンプを、米国東部時間（ET）の取引日に変換してから集計する。
JSTとETの時差は約13時間（サマータイム時）あり、JSTの1日は米国の2営業日に
またがるため、変換を誤ると「その日の損益」が実際とズレる。特に以下を明示的にテストする:
1. JST/ETの日付境界: JST午前中の取引が前日のET取引日に属することがある
   （例: JST 8時の取引はET前日19時に相当する）。
2. 取引がない日を0円として連続的に埋める（グラフの日付軸を連続させるため）。
3. 累積損益は、表示ウィンドウより前の全履歴を含めて正しく計算される
   （ウィンドウの先頭日が不自然にゼロ近辺から始まらない）。
4. BUY行・pnl_usd空欄の行は集計対象から除外する。
5. 複数銘柄（複数CSVファイル）が正しく合算される。
6. logsディレクトリが存在しない場合でもクラッシュせず、0円埋めの結果を返す。

実行: python3 verify_daily_pnl.py
"""
import sys
import shutil
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "macd_trader"))

from daily_pnl import compute_daily_pnl  # noqa: E402

results = []


def check(name, cond):
    results.append((name, cond))
    print(("OK  " if cond else "FAIL"), name)


TMP_DIR = Path("/tmp/_verify_daily_pnl_logs")


def _write_csv(name: str, rows: list[str]):
    header = "timestamp,action,symbol,price,quantity,entry_price,pnl_usd,pnl_pct,hold_minutes,exit_reason,gc_duration,daily_trades"
    (TMP_DIR / name).write_text(header + "\n" + "\n".join(rows) + "\n", encoding="utf-8")


def setup():
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR)
    TMP_DIR.mkdir(parents=True)


def teardown():
    if TMP_DIR.exists():
        shutil.rmtree(TMP_DIR)


# ─── 1. JST/ET日付境界: JST午前の取引はET前日に属する ───

def test_jst_et_boundary():
    setup()
    # JST 2026-08-05 08:00 の決済 -> ET 2026-08-04 19:00 のはず（前日扱い）
    _write_csv("trades_US_TEST1.csv", [
        "2026-08-05T08:00:00,SELL,US.TEST1,100.0,5,90.0,50.00,11.1,60.0,利確,,1",
    ])
    result = compute_daily_pnl(TMP_DIR, days=5)
    by_date = {e["date"]: e for e in result}
    check("JST8時の取引はET前日(08-04)に集計される",
          by_date.get("2026-08-04", {}).get("pnl_usd") == 50.00)
    check("JST8時の取引はET当日(08-05)には計上されない",
          by_date.get("2026-08-05", {}).get("pnl_usd") == 0.0)
    teardown()


def test_jst_et_boundary_late_hour():
    setup()
    # JST 2026-08-05 23:00 の決済 -> ET 2026-08-05 10:00 のはず（当日扱い）
    _write_csv("trades_US_TEST2.csv", [
        "2026-08-05T23:00:00,SELL,US.TEST2,100.0,5,90.0,30.00,11.1,60.0,利確,,1",
    ])
    result = compute_daily_pnl(TMP_DIR, days=5)
    by_date = {e["date"]: e for e in result}
    check("JST23時の取引はET当日(08-05)に集計される",
          by_date.get("2026-08-05", {}).get("pnl_usd") == 30.00)
    teardown()


# ─── 2. 取引がない日は0円で連続的に埋まる ───

def test_zero_fill_continuity():
    setup()
    _write_csv("trades_US_TEST3.csv", [
        "2026-08-01T22:00:00,SELL,US.TEST3,100.0,5,90.0,10.00,11.1,60.0,利確,,1",
    ])
    result = compute_daily_pnl(TMP_DIR, days=10)
    check("指定日数分、日付が連続して埋まる", len(result) == 10)
    dates = [e["date"] for e in result]
    check("日付は昇順で連続している", dates == sorted(dates) and len(set(dates)) == 10)
    no_trade_days = [e for e in result if e["date"] != "2026-08-01"]
    check("取引がない日はすべてpnl_usd=0/trades=0",
          all(e["pnl_usd"] == 0.0 and e["trades"] == 0 for e in no_trade_days))
    teardown()


# ─── 3. 累積損益はウィンドウ外の履歴も含めて正しく計算される ───

def test_cumulative_includes_pre_window_history():
    setup()
    _write_csv("trades_US_TEST4.csv", [
        # ウィンドウ(直近5日)より前の取引 — 累積には反映されるが表示行には出ない
        "2026-07-01T10:00:00,SELL,US.TEST4,100.0,5,50.0,200.00,100.0,60.0,利確,,1",
        "2026-08-05T10:00:00,SELL,US.TEST4,100.0,5,90.0,10.00,11.1,60.0,利確,,1",
    ])
    result = compute_daily_pnl(TMP_DIR, days=5)
    check("ウィンドウ外の取引は日別内訳には現れない",
          not any(e["date"] == "2026-07-01" for e in result))
    last = result[-1]
    check("累積損益はウィンドウ外の履歴(+200)を含めて計算される（200+10=210）",
          last["cumulative_pnl_usd"] == 210.00)
    first = result[0]
    check("ウィンドウ先頭日の累積損益もウィンドウ外の履歴を含む（ゼロから始まらない）",
          first["cumulative_pnl_usd"] == 200.00)
    teardown()


# ─── 4. BUY行・pnl_usd空欄行は除外する ───

def test_excludes_buy_and_empty_pnl():
    setup()
    _write_csv("trades_US_TEST5.csv", [
        "2026-08-05T22:00:00,BUY,US.TEST5,100.0,5,100.0,,,0.0,,30.0,",
        "2026-08-05T23:00:00,SELL,US.TEST5,105.0,5,100.0,25.00,5.0,60.0,利確,,1",
    ])
    result = compute_daily_pnl(TMP_DIR, days=3)
    today = result[-1]
    check("BUY行は無視され、SELL行のpnlのみ集計される（合計25.00・取引1件）",
          today["pnl_usd"] == 25.00 and today["trades"] == 1)
    teardown()


# ─── 5. 複数銘柄（複数CSV）が合算される ───

def test_aggregates_multiple_symbols():
    setup()
    _write_csv("trades_US_A.csv", [
        "2026-08-05T22:00:00,SELL,US.A,100.0,5,90.0,10.00,11.1,60.0,利確,,1",
    ])
    _write_csv("trades_US_B.csv", [
        "2026-08-05T22:30:00,SELL,US.B,50.0,5,55.0,-5.00,-9.1,60.0,損切り,,1",
    ])
    result = compute_daily_pnl(TMP_DIR, days=3)
    today = result[-1]
    check("複数銘柄のCSVが同じET日付に合算される（10.00-5.00=5.00・取引2件）",
          today["pnl_usd"] == 5.00 and today["trades"] == 2)
    check("勝率は正しく計算される（2件中1件が勝ち=50%）", today["win_rate"] == 50.0)
    teardown()


# ─── 6. logsディレクトリが存在しなくてもクラッシュしない ───

def test_missing_log_dir():
    missing_dir = Path("/tmp/_verify_daily_pnl_missing_dir_xyz")
    if missing_dir.exists():
        shutil.rmtree(missing_dir)
    result = compute_daily_pnl(missing_dir, days=5)
    check("logsディレクトリが無くてもクラッシュせず0円埋めの結果を返す",
          len(result) == 5 and all(e["pnl_usd"] == 0.0 for e in result))


test_jst_et_boundary()
test_jst_et_boundary_late_hour()
test_zero_fill_continuity()
test_cumulative_includes_pre_window_history()
test_excludes_buy_and_empty_pnl()
test_aggregates_multiple_symbols()
test_missing_log_dir()


# ─── 結果 ───
print()
n_fail = sum(1 for _, c in results if not c)
print(f"合計 {len(results)}件中 {n_fail}件失敗")
sys.exit(1 if n_fail else 0)
