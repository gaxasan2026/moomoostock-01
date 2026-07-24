#!/bin/bash
# MACD Trader 起動スクリプト
# 使い方: bash start.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$SCRIPT_DIR/macd_trader"

# 既存のサーバーを停止
if lsof -ti :5001 >/dev/null 2>&1; then
    echo "既存のサーバーを停止中..."
    kill $(lsof -ti :5001)
    sleep 1
fi

# symbols.json が存在するか確認
if [ ! -f "$APP_DIR/data/symbols.json" ]; then
    echo "⚠️  data/symbols.json が見つかりません"
    echo "   symbols.example.json をコピーして編集してください:"
    echo "   cp $APP_DIR/data/symbols.example.json $APP_DIR/data/symbols.json"
    exit 1
fi

# サーバー起動
echo "MACD Trader を起動中..."
cd "$APP_DIR"
python3 web_app.py &

sleep 2
echo ""
echo "✅ 起動完了"
echo "   ブラウザで開く: http://localhost:5001"
echo ""
echo "⚠️  先に moomoo OpenD (/Applications/moomoo_OpenD.app) が起動していることを確認してください"
