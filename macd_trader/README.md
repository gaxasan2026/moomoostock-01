# MACD Trader for moomoo

> Automated short-term trading system based on MACD Golden Cross / Dead Cross signals, powered by the moomoo OpenAPI.

[日本語マニュアル](docs/manual_ja.html) | [English Manual](docs/manual_en.html)
[バックテストマニュアル](docs/backtest_manual_ja.html) | [Backtest Manual](docs/backtest_manual_en.html)

---

## Overview

This application connects to **moomoo Securities' OpenD API** to retrieve real-time K-line data, compute MACD indicators, and automatically place buy/sell orders when configurable signal conditions are met.

Key features:
- Multi-symbol concurrent monitoring (one thread per symbol)
- Web-based GUI on `http://localhost:5001` — no command line required after launch
- **Paper Trading mode**: real prices from OpenD, orders sent to moomoo's simulated account
- **Mock mode**: fully offline with generated price data (no OpenD required)
- Per-symbol configuration: entry/exit conditions, risk limits, order settings
- Real-time log streaming and trade history in the browser

---

## ⚠️ Prerequisite: moomoo OpenD

**This application requires moomoo OpenD to be installed and running on your machine.**  
OpenD is moomoo's local API gateway — it is **not included in this repository** and must be set up separately.

### What is OpenD?

OpenD is a daemon process provided by moomoo/Futu that handles authentication and market data. It runs locally on your PC and exposes a TCP API on port `11111` that this application connects to.

### How to install OpenD

1. Go to the [moomoo OpenAPI download page](https://openapi.moomoo.com/moomoo-api-doc/en/quick/opend-base.html)
2. Download the version for your OS (GUI version recommended for first-time users)
3. On **macOS**: the app may be blocked by Gatekeeper on first launch.  
   Open **System Settings → Privacy & Security** and click **"Open Anyway"**, or run:
   ```bash
   sudo xattr -r -d com.apple.quarantine /path/to/moomoo_OpenD.app
   ```
4. Launch the app and log in with your moomoo account credentials

### OpenD installation cautions

| Item | Details |
|---|---|
| **Credentials** | Your moomoo login is entered only in the OpenD app. **Never put your email or password in any file in this repository.** |
| **Port** | OpenD listens on `127.0.0.1:11111` by default. This application uses the same default — change both if you need a different port. |
| **First-time auth** | The first login may require SMS or email verification. Keep your phone nearby. |
| **Always start OpenD first** | Launch OpenD and confirm it shows "Connected" before starting this application's bots. If OpenD is not running, bots will fail to connect. |
| **Market data rights** | Some data (e.g. US real-time quotes) may require a separate subscription in your moomoo account. |
| **One OpenD per machine** | Only one OpenD instance can run per machine. Do not start multiple instances. |

---

## Setup

### 1. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 2. Install and start moomoo OpenD

See the [OpenD installation section](#️-prerequisite-moomoo-opend) above.

### 3. Start the Web GUI

```bash
cd macd_trader
python web_app.py
```

Open `http://localhost:5001` in your browser.

### 4. Add a symbol and start a bot

1. Click **＋ 銘柄追加** (Add symbol)
2. Enter a symbol in Futu format (e.g. `US.NVDA`, `HK.00700`)
3. Confirm **Paper Trading** is checked (recommended for first run)
4. Click **▶ 起動** (Start) to begin monitoring

---

## File structure

```
macd_trader/
├── web_app.py            ← Flask web server (entry point for GUI mode)
├── main.py               ← CLI entry point (single symbol)
├── bot_manager.py        ← Multi-symbol thread management
├── config_loader.py      ← Config dataclasses
├── macd_engine.py        ← MACD calculation & GC/DC detection
├── signal_tracker.py     ← State: GC duration, peak tracking, position
├── trade_engine.py       ← Buy/sell decision logic
├── order_manager.py      ← moomoo API wrapper (connect, K-line, orders)
├── risk_manager.py       ← Daily loss/trade count limits
├── trade_logger.py       ← CSV trade history
├── symbol_store.py       ← Per-symbol JSON config persistence
├── static/index.html     ← Web GUI (single-file, no build step)
├── trading_config.yaml   ← Config template for CLI mode
├── requirements.txt
└── data/
    └── symbols.example.json  ← Copy to symbols.json and edit
```

---

## Operating modes

| Mode | mock_data | paper_trading | Behavior |
|---|---|---|---|
| Mock (offline) | `true` | — | Generated sine-wave prices, no OpenD needed |
| Paper Trading | `false` | `true` | Real prices via OpenD, orders to moomoo simulate account |
| Live Trading | `false` | `false` | Real prices via OpenD, orders to real account |

Start with **Paper Trading** and run for at least 2–4 weeks before switching to live.

---

## Key configuration parameters

See [docs/manual_ja.html](docs/manual_ja.html) or [docs/manual_en.html](docs/manual_en.html) for full parameter descriptions.

| Screen label | Config key | Default | Description |
|---|---|---|---|
| GC継続時間 | `gc_duration_minutes` | 3.0 min | Min time GC must hold before entry |
| ピーク下落率 | `peak_drop_pct` | 1.5% | Trailing stop: % drop from peak to exit |
| 損切りライン | `stop_loss_pct` | 1.5% | Hard stop loss from entry price |
| 利確ライン | `take_profit_pct` | 3.0% | Take profit from entry price |
| 最大保有時間 | `max_hold_minutes` | 60 min | Force exit after this duration |
| 日次最大損失 | `max_daily_loss_pct` | 3.0% | Halt trading for the day if exceeded |

---

## Disclaimer

This software is for educational and personal use.  
**Trading involves risk of loss. Use Paper Trading to validate your strategy before committing real capital.**  
The authors are not responsible for any financial losses.
