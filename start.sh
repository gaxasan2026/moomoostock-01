#!/bin/bash
# MACD Trader 起動スクリプト
# Mac mini起動後（ログイン項目・手動実行いずれでも）に、OpenDの起動確認・起動から
# MACD Trader Web GUIの起動までを自動化する。
#
# 使い方:
#   bash start.sh                                  # OpenD確認・起動 + Web GUI起動のみ（デフォルト）
#   bash start.sh --start-bots=US.SPCX,US.CBRS,US.MU   # 上記に加え、指定した銘柄のみボットを自動起動する
#
# ⚠️ --start-bots は、停電・再起動等で意図せず取引が止まっていた場合に、
#    人の確認なしに実売買監視が自動的に再開される。それでよい場合のみ使うこと。
# ⚠️ 起動する銘柄は必ず明示的にカンマ区切りで指定すること。data/symbols.jsonには
#    現在停止中・検証保留中の銘柄（NVDA/TSLA/SKHY等）も設定として残っているため、
#    「登録済み全銘柄」を無条件に起動すると、意図せず停止中銘柄の監視が再開されてしまう。

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$SCRIPT_DIR/macd_trader"
OPEND_PORT=11111
WEB_PORT=5001
OPEND_APP="/Applications/moomoo_OpenD.app"
OPEND_WAIT_TIMEOUT=90   # OpenD起動待ちの最大秒数
WEB_WAIT_TIMEOUT=20     # Web GUI起動待ちの最大秒数

START_BOTS_SYMBOLS=""
for arg in "$@"; do
    case "$arg" in
        --start-bots=*)
            START_BOTS_SYMBOLS="${arg#--start-bots=}"
            ;;
    esac
done

log() { echo "[$(date '+%H:%M:%S')] $1"; }

# ── 1. OpenD の起動確認・起動 ──────────────────────────────
if lsof -i :"$OPEND_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    log "OpenD は起動済みです（ポート $OPEND_PORT）"
else
    log "OpenD が起動していません。起動します..."
    open -a "$OPEND_APP"

    log "OpenD の起動を待機中（最大${OPEND_WAIT_TIMEOUT}秒）..."
    elapsed=0
    until lsof -i :"$OPEND_PORT" -sTCP:LISTEN >/dev/null 2>&1; do
        sleep 3
        elapsed=$((elapsed + 3))
        if [ "$elapsed" -ge "$OPEND_WAIT_TIMEOUT" ]; then
            log "❌ OpenD が${OPEND_WAIT_TIMEOUT}秒以内に起動しませんでした。"
            log "   moomoo_OpenD.appを開き、ログイン状態（SMS認証等）を手動で確認してください。"
            exit 1
        fi
    done
    log "✅ OpenD が起動しました"
fi

# ── 2. 既存のWebサーバーを停止 ──────────────────────────────
if lsof -ti :"$WEB_PORT" >/dev/null 2>&1; then
    log "既存のWebサーバーを停止中..."
    kill $(lsof -ti :"$WEB_PORT")
    sleep 1
fi

# ── 3. symbols.json が存在するか確認 ──────────────────────────────
if [ ! -f "$APP_DIR/data/symbols.json" ]; then
    log "⚠️  data/symbols.json が見つかりません"
    log "   symbols.example.json をコピーして編集してください:"
    log "   cp $APP_DIR/data/symbols.example.json $APP_DIR/data/symbols.json"
    exit 1
fi

# ── 4. Web GUI 起動 ──────────────────────────────
log "MACD Trader Web GUI を起動中..."
cd "$APP_DIR"
nohup python3 web_app.py >> "$APP_DIR/logs/start_sh.log" 2>&1 &
disown

log "Web GUI の起動を待機中..."
elapsed=0
until curl -s -o /dev/null "http://localhost:$WEB_PORT/api/status" 2>/dev/null; do
    sleep 1
    elapsed=$((elapsed + 1))
    if [ "$elapsed" -ge "$WEB_WAIT_TIMEOUT" ]; then
        log "❌ Web GUI が${WEB_WAIT_TIMEOUT}秒以内に起動しませんでした。ログを確認してください: $APP_DIR/logs/start_sh.log"
        exit 1
    fi
done
log "✅ Web GUI 起動完了（http://localhost:$WEB_PORT）"

# ── 5. （オプション）指定銘柄のボットを自動起動 ──────────────────────────────
if [ -n "$START_BOTS_SYMBOLS" ]; then
    log "指定銘柄のボットを起動します: $START_BOTS_SYMBOLS"
    IFS=',' read -ra SYMS <<< "$START_BOTS_SYMBOLS"
    for sym in "${SYMS[@]}"; do
        log "  起動: $sym"
        resp=$(curl -s -w "\n%{http_code}" -X POST "http://localhost:$WEB_PORT/api/bots/$sym/start")
        code=$(echo "$resp" | tail -n1)
        if [ "$code" != "200" ]; then
            log "  ⚠️  $sym の起動に失敗しました（HTTP $code）。銘柄コードがdata/symbols.jsonに登録されているか確認してください。"
        fi
        sleep 1
    done
    log "✅ ボット起動要求を送信しました"
else
    log "ℹ️  ボットは自動起動していません（--start-bots=US.XXX,US.YYY で指定銘柄のみ自動起動できます）"
fi

echo ""
log "起動完了"
