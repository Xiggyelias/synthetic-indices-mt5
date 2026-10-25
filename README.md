# ⚡ RSI-Pullback Engulfing Bot — v2.4

> **Institutional-grade algorithmic trading system for MetaTrader 5.**
> H1 RSI-Pullback strategy on EURUSD & XAUUSD. 70%+ win-rate target. Full Telegram control. Live Streamlit dashboard.

---

## 🎯 Strategy Overview

The bot trades **high-probability engulfing candles at RSI pullbacks**, filtered by 8 confluence layers:

| Layer | Rule |
|-------|------|
| **Trend** | Price above/below 200 EMA (dominant trend only) |
| **RSI Pullback** | RSI dipped ≤45 (buy) or ≥55 (sell) within last 10 candles |
| **Engulfing** | Body ≥40% of candle range — no doji engulfings |
| **Volume** | ≥1.10× 20-period average (institutional conviction) |
| **S/R Proximity** | Within 0.5% of a support or resistance level |
| **Market Regime** | Skip entries in ranging/choppy conditions (ADX-based) |
| **HTF Alignment** | D1 must agree with H1 trend direction |
| **ADX** | ≥20 confirms directional market |

**Expected performance:** 65–72% win rate · 1.4+ profit factor · 2–4 signals/week per symbol

---

## 🚀 Key Features

### Strategy
- **RSI-Pullback Engulfing** — buy dips (RSI ≤45) in uptrends, sell rallies (RSI ≥55) in downtrends
- **Wilder's EWM RSI** — correct formula using `ewm(com=period-1, adjust=False)` (not simple MA)
- **Multi-timeframe confirmation** — D1 trend must align with H1 signal
- **12-signal confluence scoring** — EMA, RSI, MACD, Stochastic, Volume, S/R, ADX, Regime, HTF

### Risk Management
- **1% risk per trade** (configurable), 2% max
- **Max 3 open trades** simultaneously
- **6% daily loss limit**, 15% overall drawdown limit
- **Capital Protection Mode** — reduces risk to 0.6% after 6 consecutive losses
- **Dynamic risk scaling** — 82% risk in volatile regimes, 75% near hard daily limit
- **Margin safety gate** — skips trades if margin level < 200%

### Trade Management (TradeSupervisor)
- **Breakeven at 1R** — SL moves to entry once price extends 1R
- **1.5R partial close** — 50% of position closed at 1.5R, remainder runs
- **Swing-based trailing** — after partial close, SL trails to fractal swing lows/highs
- **Structure invalidation** — auto-exits if price closes through recent swing level against trade

### Execution Quality
- **ATR-relative spread check** — blocks entry if spread > 15% of ATR (scales for both EURUSD and XAUUSD)
- **Max slippage**: 10 points
- **Losing streak pause** — 45-minute cooldown after 6 consecutive losses

### Infrastructure
- **Live Streamlit dashboard** — real-time scanner feed, equity curve, regime status
- **Full Telegram control** — 11 commands, inline keyboard, two-way conversation
- **MySQL + JSON fallback** — state persists across restarts
- **SSL-secured** Telegram API calls
- **Graceful shutdown** on SIGTERM/SIGINT
- **Exponential backoff** on MT5 reconnect

---

## 📁 Project Structure

```
files (1)/
├── src/
│   ├── mt5_system.py              # Main orchestrator & Telegram commands
│   ├── trading_bot_engulfing_v2.py # Strategy logic & confluence scoring
│   ├── trade_supervisor.py        # Position management (BE, trail, partial)
│   ├── trade_manager.py           # State persistence layer
│   ├── mt5_connection.py          # MT5 API wrapper
│   ├── telegram_notifier.py       # Two-way Telegram interface
│   ├── market_regime.py           # RegimeClassifier (ADX/ATR/BB)
│   ├── news_filter.py             # Economic calendar block
│   ├── performance_tracker.py     # Trade analytics
│   └── trade_journal.py           # Detailed trade logging
├── config/
│   └── config_rsi_pullback.json   # All strategy parameters (v2.4)
├── data/
│   ├── bot_switch.txt             # on/off switch (hot-reload)
│   ├── trades_state.json          # Live trade state
│   ├── performance_state.json     # Closed trade history
│   └── trade_journal.json         # Full trade journal
├── scripts/
│   ├── start_bot.bat              # Launch bot + dashboards
│   ├── install_mysql.bat          # MySQL setup
│   └── check_bot_status.py        # CLI status check
├── logs/
│   └── bot_run.log                # Rotating log (all activity)
├── docs/
│   ├── VPS_SETUP_GUIDE.md
│   ├── DEPLOYMENT_CHECKLIST.md
│   ├── GO_NO_GO_EVALUATION.md
│   └── FORWARD_TEST_CHECKLIST.md
├── dashboard.py                   # Streamlit dashboard
├── mt5_bot.py                     # Entry point
├── .env                           # Credentials (never commit)
└── requirements.txt
```

---

## 📱 Telegram Commands

Send any of these to your bot. Inline keyboard auto-appears for unrecognised input.

| Command | Alias | Description |
|---------|-------|-------------|
| `/status` | | Full account snapshot, bot health, daily P&L |
| `/balance` | | Account balance, equity, margin level |
| `/positions` | | All open trades with entry/SL/TP/BE status |
| `/report` | | Equity curve chart image + stats |
| `/stop` | `/pause` | Pause all trading immediately |
| `/start` | `/resume` | Resume signal scanning |
| `/risk` | | Current risk mode, capital protection status |
| `/help` | `/menu` | Interactive keyboard menu |

---

## 🛠️ Getting Started

### 1. Requirements
```bash
# Dashboard only / Streamlit Community Cloud
pip install -r requirements.txt

# Full Windows/VPS bot with MetaTrader 5
pip install -r requirements-bot.txt
```

### 2. Configure `.env`
```env
# MetaTrader 5
MT5_LOGIN=your_account_number
MT5_PASSWORD=your_password
MT5_SERVER=your_broker_server
MT5_DEMO=true

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Optional: MySQL
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=trading_bot
DB_USER=root
DB_PASSWORD=your_password
```

### 3. Launch
```powershell
# Full stack (bot + both dashboards)
.\scripts\start_bot.bat

# Bot only
python mt5_bot.py --system --loop --confirm-trading

# Dashboard only
streamlit run dashboard.py --server.port 8502
```

### 4. Dashboard
Open `http://localhost:8502` in your browser.

- **🔴 Live Scanner** tab — real-time regime status, activity feed, open positions
- **📊 Overview** — equity curve, KPIs
- **📈 Performance** — win rate, R-multiples, session breakdown

---

## ⚙️ Configuration

All parameters live in `config/config_rsi_pullback.json`. Key values:

```json
"rsi_settings": {
    "rsi_pullback_buy_threshold":  45,
    "rsi_pullback_sell_threshold": 55,
    "rsi_lookback_candles":        10
},
"volume_settings": {
    "volume_multiplier": 1.10
},
"risk_management": {
    "risk_per_trade":   0.01,
    "max_daily_loss":   0.06,
    "max_drawdown":     0.15,
    "max_open_trades":  3
}
```

Hot-reload: edit the JSON while the bot runs — changes apply on the next scan cycle.

---

## Free Streamlit Community Cloud Deployment

Use this for the public dashboard only. The live MT5 trading process still needs a Windows machine or Windows VPS with the MetaTrader 5 terminal installed and logged in.

1. Push this repo to GitHub.
2. Go to `https://share.streamlit.io` and choose **Streamlit Community Cloud**.
3. Select the repository and branch.
4. Set the main file path to `dashboard.py`.
5. In Advanced settings, add this secret/environment value:
   ```toml
   CLOUD_DASHBOARD = "true"
   ```
6. Click **Deploy**.

Community Cloud will install `requirements.txt`, which intentionally excludes `MetaTrader5` because the cloud dashboard runs on Linux. For the actual trading bot, install `requirements-bot.txt` on Windows/VPS.

---

## 🌐 VPS Deployment

See `docs/VPS_SETUP_GUIDE.md` for full VPS instructions.

**Quick steps:**
1. RDP into Windows VPS
2. Install Python 3.12, MetaTrader 5, MT5 Python package
3. Clone/copy project to `C:\TradingBot\`
4. Install NSSM: `choco install nssm`
5. Register as service: `nssm install TradingBot python mt5_bot.py --system --loop --confirm-trading`
6. `nssm start TradingBot`

---

## 📊 Production Readiness Status

| Category | Status | Notes |
|----------|--------|-------|
| Strategy logic | ✅ Ready | Wilder RSI, ATR spread, ADX, margin gate |
| Risk management | ✅ Ready | Daily/drawdown limits, capital protection |
| Trade management | ✅ Ready | BE, trail, partial close, structure exit |
| Telegram interface | ✅ Ready | 11 commands, inline keyboard |
| Dashboard | ✅ Ready | Live scanner, equity curve, performance |
| State persistence | ✅ Ready | JSON (MySQL optional) |
| Error handling | ✅ Ready | Graceful shutdown, reconnect backoff |
| **Demo tested** | ⏳ In progress | Running on demo — awaiting 30+ trades |
| **MySQL** | ⚠️ Optional | Bot runs on JSON fallback without it |
| **NSSM service** | ⚠️ Recommended | For 24/7 auto-restart on VPS |
| **Forward test** | ⏳ Needed | 30+ live demo trades required before going live |

---

## 🧠 Quantum Brain v2 (Deriv Volatility Indices, Rise/Fall research)

A separate research and decision system for five-minute Rise/Fall contracts on Deriv
Volatility Indices: Deriv WebSocket data collection, contract-aware labels, walk-forward
validation with purging and a protected holdout, a deterministic risk governor, a model
registry that needs human approval, and a read-only dashboard. It defaults to paper mode
and has no real-money mode. See [docs/QUANTUM_BRAIN_V2.md](docs/QUANTUM_BRAIN_V2.md).

```bash
python quantum_brain.py migrate && python quantum_brain.py discover
streamlit run dashboard_quantum.py
```

## ⚠️ Disclaimer

Trading involves significant risk of loss. This software is provided for educational and research purposes. Always test thoroughly on a demo account before considering any live deployment. Past performance does not guarantee future results.
