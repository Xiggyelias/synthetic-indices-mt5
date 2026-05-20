# How the RSI-Pullback Trading Bot Works
### A Complete Plain-English Guide — v2.4

---

## Table of Contents

1. [What Is This Bot?](#1-what-is-this-bot)
2. [System Architecture — The Big Picture](#2-system-architecture--the-big-picture)
3. [The Strategy — How Signals Are Generated](#3-the-strategy--how-signals-are-generated)
4. [The 12-Point Confluence Scoring System](#4-the-12-point-confluence-scoring-system)
5. [Risk Management — Position Sizing](#5-risk-management--position-sizing)
6. [Trade Management — What Happens After Entry](#6-trade-management--what-happens-after-entry)
7. [Capital Protection System](#7-capital-protection-system)
8. [The Market Regime Classifier](#8-the-market-regime-classifier)
9. [Telegram Control Interface](#9-telegram-control-interface)
10. [The Dashboard](#10-the-dashboard)
11. [Data Flow — Start to Finish](#11-data-flow--start-to-finish)
12. [The Configuration File Explained](#12-the-configuration-file-explained)
13. [How to Run Everything](#13-how-to-run-everything)
14. [What to Expect Day-to-Day](#14-what-to-expect-day-to-day)
15. [File-by-File Reference](#15-file-by-file-reference)
16. [Glossary](#16-glossary)

---

## 1. What Is This Bot?

This is a **fully automated algorithmic trading bot** that connects to MetaTrader 5 (MT5) and trades two currency pairs:
- **EURUSD** (Euro vs US Dollar)
- **XAUUSD** (Gold vs US Dollar)

It runs 24/5 (forex market hours), scans for trading opportunities every ~60 seconds on **1-hour candles (H1)**, and automatically:
- Enters trades when all conditions are met
- Sets a Stop Loss (SL) and Take Profit (TP)
- Moves the SL to breakeven when the trade is profitable
- Closes 50% of the position at 1.5× the original risk
- Trails the SL using swing points to lock in further profits
- Exits early if market structure breaks against the trade

You control it from **Telegram** (send commands from your phone) and monitor it through a **live web dashboard**.

**Target performance:** 65–72% win rate, 1.4+ profit factor, $40+ expectancy per trade.

---

## 2. System Architecture — The Big Picture

```
┌─────────────────────────────────────────────────────────────────┐
│                         YOUR COMPUTER                           │
│                                                                 │
│  ┌─────────────┐    ┌──────────────────────────────────────┐   │
│  │ MetaTrader 5│◄──►│           mt5_bot.py                 │   │
│  │  Terminal   │    │        (Entry Point)                 │   │
│  └─────────────┘    └──────────────┬───────────────────────┘   │
│                                    │                            │
│                     ┌──────────────▼───────────────────────┐   │
│                     │         MT5BotSystem                 │   │
│                     │         (mt5_system.py)              │   │
│                     │                                      │   │
│                     │  ┌────────────┐  ┌───────────────┐  │   │
│                     │  │EngulfingBot│  │TradeSupervisor│  │   │
│                     │  │  Strategy  │  │  (manages SL) │  │   │
│                     │  └────────────┘  └───────────────┘  │   │
│                     │                                      │   │
│                     │  ┌────────────┐  ┌───────────────┐  │   │
│                     │  │RegimeClass.│  │  NewsFilter   │  │   │
│                     │  │(trending?) │  │(block events) │  │   │
│                     │  └────────────┘  └───────────────┘  │   │
│                     │                                      │   │
│                     │  ┌────────────┐  ┌───────────────┐  │   │
│                     │  │TradeManager│  │  Performance  │  │   │
│                     │  │(state/JSON)│  │   Tracker     │  │   │
│                     │  └────────────┘  └───────────────┘  │   │
│                     └──────────────────────────────────────┘   │
│                                    │                            │
│              ┌─────────────────────┼─────────────────────┐     │
│              │                     │                     │     │
│  ┌───────────▼──────┐  ┌───────────▼──────┐  ┌──────────▼──┐  │
│  │  Telegram Bot    │  │  Dashboard       │  │  Log File   │  │
│  │  (your phone)    │  │  (browser :8502) │  │  (bot_run   │  │
│  └──────────────────┘  └──────────────────┘  │   .log)     │  │
│                                               └─────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

**How the pieces connect:**

| File | Role |
|------|------|
| `mt5_bot.py` | Entry point. Parses CLI arguments, connects to MT5, starts the system |
| `src/mt5_system.py` | The brain. Orchestrates everything — scans, trades, Telegram, risk gates |
| `src/trading_bot_engulfing_v2.py` | The strategy. Calculates indicators, checks all conditions, generates BUY/SELL signals |
| `src/trade_supervisor.py` | The trade manager. Moves SL to breakeven, trails stops, does partial closes |
| `src/mt5_connection.py` | The MT5 bridge. All communication with MetaTrader 5 goes through here |
| `src/telegram_notifier.py` | Two-way Telegram. Sends alerts, receives commands, shows inline keyboard |
| `src/market_regime.py` | Market classifier. Detects if market is trending, ranging, or volatile |
| `src/news_filter.py` | Economic calendar. Pauses trading 30 min before/after major news events |
| `src/trade_manager.py` | State storage. Saves/loads trade state to JSON so nothing is lost on restart |
| `src/performance_tracker.py` | Analytics. Records every closed trade for dashboard statistics |
| `src/trade_journal.py` | Detailed logging. Saves full details of every trade to a journal file |
| `dashboard.py` | Streamlit web dashboard. Live scanner view, equity curve, performance stats |
| `config/config_rsi_pullback.json` | All settings. Change parameters here without touching code |

---

## 3. The Strategy — How Signals Are Generated

The strategy is called **RSI-Pullback Engulfing**. It only takes trades that satisfy ALL of the following conditions:

### Step 1 — Wait for a New Closed H1 Candle

The bot wakes up approximately every 60 seconds. It fetches the last 300 H1 candles for EURUSD and XAUUSD. It looks at the **second-to-last candle** (the most recently fully closed one) — it never trades on an open/forming candle.

### Step 2 — Identify the Market Regime

Before anything else, the `RegimeClassifier` checks whether the market is:
- **TRENDING** — ADX > 25, price trending in one direction → trading is allowed
- **RANGING/CHOPPY** — ADX < 20, price bouncing sideways → trading is blocked
- **VOLATILE** — ATR unusually high → risk is reduced by 18%

If the market is ranging, the scan stops immediately. No trade.

### Step 3 — Check the Daily Trend (200 EMA)

The bot calculates a 200-period Exponential Moving Average on the H1 chart:
- Price **above** the 200 EMA = uptrend → only **BUY** signals allowed
- Price **below** the 200 EMA = downtrend → only **SELL** signals allowed

This ensures you only trade **with the dominant trend**, never against it.

### Step 4 — Higher Timeframe Alignment (D1)

The bot also checks the **daily (D1)** chart:
- It fetches D1 candles and checks the D1 EMA trend
- The D1 trend must agree with the H1 trend
- If H1 says bullish but D1 says bearish → no trade

This "multi-timeframe" check eliminates trades that go against the big picture.

### Step 5 — Engulfing Candle Pattern

An engulfing candle is a candle where:
- **Bullish engulfing**: A green candle whose body completely contains the previous red candle's body (the open of the new candle is below the close of the previous, and the close of the new candle is above the open of the previous)
- **Bearish engulfing**: The opposite — a red candle that completely swallows the previous green candle

**Quality filter**: The candle body must be at least 40% of the total candle range (high to low). This filters out "doji" engulfings where the wick makes up most of the candle — those are weak signals.

### Step 6 — RSI Pullback Filter (The Core Edge)

This is the most important filter. RSI (Relative Strength Index) measures momentum on a 0–100 scale.

**For BUY signals:**
- Within the last 10 candles, RSI must have touched or dipped **below 45** (a pullback/dip occurred)
- Current RSI must now be recovering **above 45** but still **below 62** (not overbought)
- This catches the "buy the dip" setup — price pulled back, RSI confirmed it, now recovering

**For SELL signals:**
- Within the last 10 candles, RSI must have touched or risen **above 55** (a rally occurred)
- Current RSI must now be pulling back **below 55** but still **above 38** (not oversold)
- This catches the "sell the rally" setup

**Why this works:** In a trending market, price doesn't move in a straight line. It trends up, pulls back to the mean, then continues up. The RSI pullback catches those pullbacks within the trend — the highest probability entry point.

**RSI calculation used:** Wilder's Exponential Moving Average (`ewm(com=period-1, adjust=False)`) — the same formula as on every trading platform. Earlier versions used a simple average which gave wrong values; this was fixed.

### Step 7 — Volume Filter

The candle's volume must be at least **1.10× the 20-period average volume**.

Why: Low-volume candles are noise. Engulfing candles with above-average volume indicate institutions (banks, hedge funds) are participating — much higher probability the move continues.

Calibration note: 1.75× was the original setting but it blocked 100% of trades on live H1 data. 1.10× is the measured actual multiplier for institutional H1 candles on EURUSD and XAUUSD.

### Step 8 — Support/Resistance Proximity

The bot calculates support and resistance levels by looking at the last 50 candles and identifying significant swing highs (resistance) and swing lows (support).

The entry price must be within **0.5%** of a support level (for buys) or resistance level (for sells).

Why: Entries at S/R levels have a natural barrier. Buying at support means the stop loss is just below a key level, maximising the risk/reward ratio.

### Step 9 — Spread Check (ATR-Relative)

The bot checks the current spread (difference between buy and sell price) relative to the ATR (Average True Range):

```
If spread > ATR × 0.15  →  skip trade
```

This scales automatically for each symbol:
- EURUSD (small spread ≈ 1-2 pips) — ATR ≈ 15 pips → allows up to 2.25 pip spread
- XAUUSD (larger spread ≈ 30-150 points) — ATR ≈ $20 → allows up to $3 spread

A fixed point-based check would block all XAUUSD trades (the original bug).

### Step 10 — Margin Safety Check

If your account margin level falls below 200%, no new trades are placed. This prevents being margin-called.

### Step 11 — Score the Trade

Once all hard filters pass, the bot scores the signal using 12 binary signals (each adds 1 point):

1. EMA 200 alignment (trending)
2. EMA 20 alignment (short-term momentum)
3. MACD histogram direction
4. RSI pullback confirmed
5. RSI not in extreme zone
6. Volume above 1.10× average
7. Stochastic oversold/overbought alignment
8. Support/Resistance proximity
9. Market regime is TRENDING
10. Higher timeframe (D1) alignment
11. RSI value within active range (45-62 for buys / 38-55 for sells)
12. ADX ≥ 20 (confirming directional market)

**Minimum score to trade:**
- Score ≥ 7 → Full position (100% of calculated risk)
- Score ≥ 5 → Half position (50% of calculated risk)
- Score < 5 → No trade

### Step 12 — Place the Trade

If everything passes:
1. Calculate Stop Loss (SL) — placed below the engulfing candle's low (buy) or above its high (sell)
2. Calculate Take Profit (TP) — placed at a minimum risk:reward ratio of 0.28× (very conservative; the trailing stop does the heavy lifting)
3. Calculate position size (lots) based on exactly 1% risk of account balance
4. Send the order to MT5

---

## 4. The 12-Point Confluence Scoring System

| # | Signal | Description |
|---|--------|-------------|
| 1 | EMA 200 trend | Price above/below 200 EMA |
| 2 | EMA 20 alignment | Short-term trend confirms direction |
| 3 | MACD direction | MACD histogram positive (buy) or negative (sell) |
| 4 | RSI pullback | RSI visited the pullback zone within 10 candles |
| 5 | RSI zone | RSI currently in the active range (45–62 buy, 38–55 sell) |
| 6 | Volume | Volume ≥ 1.10× 20-period average |
| 7 | Stochastic | Stochastic in oversold (buy) or overbought (sell) zone |
| 8 | S/R proximity | Price within 0.5% of a key level |
| 9 | Regime trending | RegimeClassifier says TRENDING |
| 10 | HTF alignment | Daily chart trend agrees with H1 |
| 11 | RSI value | Current RSI within 45–62 (buy) or 38–55 (sell) |
| 12 | ADX strength | ADX ≥ 20 (directional market confirmed) |

**Maximum possible score: 12**
**Required for full entry: 7**
**Required for half entry: 5**

---

## 5. Risk Management — Position Sizing

### How Lot Size Is Calculated

```
Risk Amount  = Account Balance × 1%
               (e.g., $10,000 × 0.01 = $100 at risk)

Price Risk   = |Entry Price - Stop Loss Price|
               (e.g., EURUSD: |1.0850 - 1.0800| = 0.0050 = 50 pips)

Value/Unit   = Tick Value ÷ Tick Size
               (broker-specific dollar value per pip per lot)

Risk Per Lot = Price Risk × Value Per Unit
               (e.g., 50 pips × $10/pip = $500 per lot)

Lot Size     = Risk Amount ÷ Risk Per Lot
               = $100 ÷ $500 = 0.20 lots
```

This is **fixed fractional position sizing** — you always risk exactly 1% of your current balance per trade, not more.

### Risk Limits

| Limit | Value | Action |
|-------|-------|--------|
| Max risk per trade | 2% | Hard cap even if scorer gives half-entry |
| Max total open risk | 7.5% | No new trades if 3 positions open |
| Max open trades | 3 | Hard limit regardless of signals |
| Daily loss limit | 6% | Bot halts for the day |
| Max drawdown | 15% | Bot halts completely |

---

## 6. Trade Management — What Happens After Entry

Once a trade is open, the `TradeSupervisor` monitors it every scan cycle (every ~60 seconds). It only moves the SL forward (never backward), and only acts on fully closed candles.

### Phase 1 — Initial Hold (0 to 1R)

`R` = the initial risk on the trade (distance from entry to stop loss in dollars).

During this phase: nothing changes. The SL stays where it was placed. The trade just runs.

### Phase 2 — Breakeven (price reaches 1R profit)

When the last closed candle's **close price** reaches entry + 1R (for buys):
- SL is moved to entry price (breakeven)
- You can no longer lose money on this trade
- Telegram notification: "BE@1R updated"

### Phase 3 — Partial Close + Trailing (price reaches 1.5R profit)

When price reaches entry + 1.5R:
1. **50% of the position is closed** — locking in real profit
2. The remaining 50% continues running
3. SL is moved to entry + 0.5R (in profit zone)
4. After partial close, the SL trails using **swing lows/highs**:
   - For buys: SL moves to the most recent fractal swing low
   - For sells: SL moves to the most recent fractal swing high
5. Telegram notification: "Scaled out 50% at 1.5R"

### Phase 4 — Swing-Based Trailing (beyond 1.5R)

After partial close, the bot looks back at the last 10 candles and finds the most recent **fractal swing point** (a candle with higher lows on both sides = swing low for buys). SL is always moved UP to lock in more profit as new swing lows form.

This means winning trades can run far beyond 1.5R while protecting most of the gain.

### Structure Invalidation (Emergency Exit)

Separately, `check_structure_invalidation()` runs every cycle. It looks at the last 5 closed candles:
- **For buys**: If the latest candle closes **below the lowest low** of the previous 4 candles → structure break → position closed immediately
- **For sells**: If the latest candle closes **above the highest high** of the previous 4 candles → structure break → position closed immediately

This is a safety exit for when the market makes a decisive move against the trade before the trailing stop would trigger.

### Trade Close Detection

The `TradeSupervisor.sync_with_terminal()` method runs every cycle. It compares the bot's tracked positions against what's actually open in MT5. If a trade disappears (closed by TP, SL, or manual close):
1. Commission and swap costs are fetched
2. Net P&L is calculated (gross P&L + commission + swap)
3. Win/loss is determined from net P&L
4. R-multiple is calculated (net P&L ÷ initial risk)
5. Telegram notification sent with results
6. Performance tracker and journal are updated
7. Position removed from state

---

## 7. Capital Protection System

This is a multi-layer defensive system that automatically adjusts risk when the bot is in drawdown.

### Layer 1 — Dynamic Risk Scaling

If daily drawdown reaches soft threshold (3%):
- All trade sizes multiplied by **0.90** (10% reduction)

If daily drawdown reaches hard threshold (5.5%):
- All trade sizes multiplied by **0.75** (25% reduction)

### Layer 2 — Capital Protection Mode

Triggered when ANY of these occur:
- Daily loss ≥ 6%
- Weekly loss ≥ 14%
- 6 consecutive losses

In Capital Protection Mode:
- Risk per trade drops to **0.6%** (from 1%)
- Minimum signal score raised to **7** (only high-confidence trades)
- Maximum trades per day limited to **5**

### Layer 3 — Losing Streak Pause

After 6 consecutive losses:
- Bot pauses for **45 minutes** (2,700 seconds)
- This prevents revenge trading and chasing losses
- After the cooldown, trading resumes normally

### Layer 4 — Daily/Drawdown Halt

If daily loss hits 6% or overall drawdown hits 15%:
- Bot is halted completely
- No new trades placed until manually reset
- Telegram alert sent immediately

---

## 8. The Market Regime Classifier

The `RegimeClassifier` (`src/market_regime.py`) runs every scan cycle and classifies each symbol's current market condition into one of three states:

### TRENDING
**Conditions:** ADX > 25 AND price is moving consistently in one direction
- The bot trades normally
- Full confluence scoring applies

### RANGING / CHOPPY
**Conditions:** ADX < 20 OR price bouncing between support and resistance without direction
- The bot **skips all entries** for this symbol
- Why: Engulfing patterns in ranging markets fail frequently — they look like trend starts but are just noise

### VOLATILE
**Conditions:** ATR significantly above the 20-period average ATR
- Trading is allowed but position size is **multiplied by 0.82** (18% smaller)
- Why: In highly volatile conditions, stop losses get hit more easily, so we take less risk

**How ADX works:** ADX (Average Directional Index) measures trend strength on a 0–100 scale, regardless of direction. ADX < 20 = no trend. ADX 20–40 = moderate trend. ADX > 40 = strong trend.

---

## 9. Telegram Control Interface

### Two-Way Communication

The bot polls the Telegram API every 3 seconds for new messages. It handles:
- Text commands (type `/status`)
- Inline keyboard button presses (tap a button)
- Unknown input → shows the main menu automatically

### Available Commands

| Command | What It Does |
|---------|-------------|
| `/status` | Full snapshot: balance, equity, daily P&L, open trades, bot status, daily drawdown |
| `/balance` | Account balance, equity, free margin, margin level |
| `/positions` | All open trades with entry price, SL, TP, breakeven status, floating P&L |
| `/report` | Generates and sends an equity curve chart image + trade statistics |
| `/stop` or `/pause` | Immediately pauses trading (writes "off" to bot_switch.txt) |
| `/start` or `/resume` | Resumes trading (writes "on" to bot_switch.txt) |
| `/risk` | Shows current risk mode, capital protection status, daily drawdown % |
| `/help` or `/menu` | Shows the inline keyboard menu |

### The Inline Keyboard

When you type anything the bot doesn't recognise (or type `/menu`), it shows a button grid:

```
[📊 Status]   [💰 Balance]
[📈 Positions] [📋 Report]
[⏸ Pause Bot] [▶️ Resume Bot]
[⚙️ Risk]     [❓ Help]
```

Tap any button — it triggers the same command as typing it.

### Heartbeat

Every 12 hours the bot sends an automatic status update to Telegram showing:
- Balance
- Whether it's active or paused
- Current daily drawdown %

This confirms the bot is alive even when no trades are happening.

---

## 10. The Dashboard

The Streamlit web dashboard runs at `http://localhost:8502` and has 6 tabs:

### 🔴 Live Scanner (Default Tab)

**Left column:**
- **Symbol Status cards** — shows EURUSD and XAUUSD with live regime badge (green = TRENDING, yellow = RANGING, red = VOLATILE) and the time of the last scan
- **Open Positions** — live coloured cards for each open trade showing direction, entry, SL, TP, breakeven status, partial close status

**Right column:**
- **Activity Feed** — real-time stream of events parsed from the bot log:
  - Blue: scan events (regime checks)
  - Green: trade events (entries, exits, trailing)
  - Yellow: warnings (risk limits, pauses)
  - Red: errors
  - Purple: system events (heartbeat)
- **Risk State panel** — daily drawdown %, halt status, peak equity

### 📊 Overview

- 5 KPI cards: Win Rate, Total P&L, Profit Factor, Max Drawdown, Avg R
- 4 secondary cards: Total Trades, Avg Win, Avg Loss, Payoff Ratio
- Full equity curve chart with drawdown subplot
- Account state (daily start, peak, open positions, halt status)

### 📋 Trades

- Full table of all closed trades with filtering by symbol, direction, outcome
- Columns: ticket, symbol, direction, entry, exit, volume, P&L, R-multiple, confluence score, session, times, win/loss
- Quick filtered stats at the bottom

### 📈 Performance

- Monthly P&L bar chart
- Win/Loss pie chart + R-multiple distribution side by side
- Rolling 20-trade win rate chart with 70% target line
- Session breakdown table (London vs New York)
- Symbol breakdown table (EURUSD vs XAUUSD stats)

### 🧪 Forward Test

- Progress bar toward 20-trade forward test target
- 4 metrics vs targets: Win Rate, Profit Factor, Expectancy, P&L
- Per-trade P&L chart
- Execution quality metrics (slippage, spread)
- Pass/Fail verdict when 20 trades are completed

### 📜 Logs

- Raw bot log with search filter, level filter (ALL/INFO/WARNING/ERROR), and line count slider
- Error and warning summary (MySQL connection warnings are filtered out since they're harmless)

### Sidebar Controls

- Start/Stop bot toggle (writes to bot_switch.txt — the bot picks it up on the next scan cycle)
- Database status (MySQL or JSON fallback)
- Manual refresh button + Auto-refresh (10 seconds) toggle
- Strategy parameters summary

---

## 11. Data Flow — Start to Finish

Here is the complete journey of one trade from scan to close:

```
EVERY ~60 SECONDS:
│
├─ 1. mt5_system.py: evaluate_and_trade(symbol) called
│
├─ 2. Fetch D1 rates → check HTF trend alignment
│      └── If D1 disagrees with H1 direction → STOP, skip symbol
│
├─ 3. Fetch H1 rates (300 candles) → calculate_indicators()
│      └── Calculates: EMA 50/200/20, RSI(14), MACD, Stochastic,
│              ATR, Bollinger Bands, ADX, volume MA
│
├─ 4. MarketRegime.classify(df) → TRENDING / RANGING / VOLATILE
│      └── If RANGING → STOP, skip symbol
│
├─ 5. news_filter.is_blocked(symbol) → check economic calendar
│      └── If major news within 30 min → STOP, pause scan
│
├─ 6. Check margin level → if < 200% → STOP
│
├─ 7. Check spread → if > ATR × 15% → STOP
│
├─ 8. Check risk limits → daily loss, drawdown, open trade count
│      └── If any limit hit → STOP
│
├─ 9. Check bot switch → if OFF → STOP
│
├─ 10. EngulfingBotV2.generate_signal(df, idx)
│       ├── is_bullish_engulfing() OR is_bearish_engulfing()
│       ├── check_strict_trend() → 200 EMA
│       ├── check_rsi_pullback() → RSI visited zone within 10 candles
│       ├── check_volume() → volume ≥ 1.10× avg
│       ├── check_rsi() → not overbought/oversold
│       ├── is_near_support_resistance()
│       └── Returns Signal.BUY / Signal.SELL / Signal.HOLD
│
├─ 11. confluence_score(df, idx, signal) → integer 0–12
│       └── If score < 5 → STOP (weak signal)
│           If score 5–6 → half position size
│           If score ≥ 7 → full position size
│
├─ 12. Calculate SL and TP prices
│
├─ 13. compute_volume_from_risk() → calculate lot size (1% risk)
│
├─ 14. MT5Connection.send_market_order() → ORDER SENT TO MT5
│
├─ 15. ManagedTrade created and saved to trades_state.json
│
├─ 16. Telegram notification: "BUY EURUSD @ 1.0850 SL:1.0800 TP:1.0914"
│
│
EVERY ~60 SECONDS (while trade is open):
│
├─ 17. TradeSupervisor.sync_with_terminal()
│       └── Adopt any orphaned positions, detect closed trades
│
├─ 18. TradeSupervisor.manage_position(ticket)
│       ├── Fetch latest candle
│       ├── Calculate current R-multiple
│       ├── If 1R reached → move SL to breakeven
│       ├── If 1.5R reached → partial close 50% + trail SL
│       └── If SL changed → MT5Connection.set_position_sltp()
│
├─ 19. TradeSupervisor.check_structure_invalidation(ticket)
│       └── If structure break → MT5Connection.close_position()
│
│
WHEN TRADE CLOSES (TP hit / SL hit / manual):
│
├─ 20. sync_with_terminal() detects ticket disappeared from MT5
│
├─ 21. Calculate: commission + swap + net P&L + R-multiple
│
├─ 22. PerformanceTracker.record_trade() → saves to performance_state.json
│
├─ 23. TradeJournal.add_entry() → saves detailed entry to trade_journal.json
│
├─ 24. Telegram notification: "💰 WIN: EURUSD BUY $47.30 | +0.94R"
│
└─ 25. TradeManager.remove(ticket) → cleans up state
```

---

## 12. The Configuration File Explained

`config/config_rsi_pullback.json` — edit this to change strategy behaviour without touching code. The bot reads it fresh every scan cycle (hot-reload).

```json
{
  "risk_management": {
    "risk_per_trade": 0.01,          // 1% of balance per trade
    "max_risk_per_trade": 0.02,      // Hard cap: never more than 2%
    "min_risk_reward": 0.28,         // Minimum TP/SL ratio
    "max_open_trades": 3,            // Max simultaneous positions
    "max_daily_loss": 0.06,          // 6% daily loss = halt
    "max_drawdown": 0.15,            // 15% drawdown = halt
    "max_total_open_risk": 0.075,    // 7.5% total exposure across all open trades
    "initial_capital": 10000.0,      // Reference capital for R calculations

    // Capital protection - reduces risk after losses
    "capital_protection_enabled": true,
    "capital_protection_daily_loss_trigger": 0.06,    // Trigger at 6% daily loss
    "capital_protection_weekly_loss_trigger": 0.14,   // Trigger at 14% weekly loss
    "capital_protection_consecutive_losses_trigger": 6, // Trigger after 6 losses in a row
    "capital_protection_risk_fraction": 0.006,        // Reduced to 0.6% risk in protection mode
    "capital_protection_min_signal_score": 7,         // Only take high-score signals

    // Losing streak pause
    "losing_streak_pause_after": 6,          // Pause after 6 consecutive losses
    "losing_streak_cooldown_seconds": 2700,  // 45-minute pause

    // Dynamic risk scaling based on daily drawdown
    "dynamic_risk_daily_dd_soft": 0.03,      // Reduce risk at 3% daily DD
    "dynamic_risk_daily_dd_hard": 0.055,     // Reduce more at 5.5% daily DD
    "dynamic_risk_soft_multiplier": 0.9,     // ×0.9 at soft threshold
    "dynamic_risk_hard_multiplier": 0.75,    // ×0.75 at hard threshold
    "volatile_regime_risk_multiplier": 0.82  // ×0.82 in volatile regimes
  },

  "strategy_parameters": {
    "timeframe": "H1",               // Trade on 1-hour candles
    "strategy_mode": "RSI_PULLBACK", // The active strategy
    "ema_fast": 50,                  // Fast EMA period
    "ema_slow": 200,                 // Slow EMA (trend filter)

    "rsi_settings": {
      "rsi_period": 14,                    // Standard RSI period
      "rsi_overbought": 62,                // Above this = overbought (no buys)
      "rsi_oversold": 38,                  // Below this = oversold (no sells)
      "rsi_pullback_buy_threshold": 45,    // Buy when RSI dipped ≤45
      "rsi_pullback_sell_threshold": 55,   // Sell when RSI rose ≥55
      "rsi_lookback_candles": 10           // Look back 10 candles for the dip
    },

    "volume_settings": {
      "volume_multiplier": 1.10,  // Need 1.10× average volume
      "volume_period": 20         // Average over last 20 candles
    },

    "support_resistance": {
      "sr_lookback": 50,          // Look back 50 candles for S/R levels
      "sr_tolerance": 0.005       // Within 0.5% of a level
    },

    "confluence_settings": {
      "min_confluence_score": 3,  // Absolute minimum (scoring gates at 5 and 7)
      "ema_trend": 20,            // Short-term EMA for confluence
      "macd_fast": 12,
      "macd_slow": 26,
      "macd_signal": 9,
      "stoch_k": 14,
      "stoch_d": 3
    }
  }
}
```

---

## 13. How to Run Everything

### Starting the Full Stack

**Option A — Run the startup script (recommended):**
```powershell
cd "C:\Users\emmanuel\Desktop\files (1)"
.\scripts\start_bot.bat
```

This starts the bot AND both dashboards.

**Option B — Manual:**
```powershell
# Terminal 1: The bot
python mt5_bot.py --system --loop --confirm-trading

# Terminal 2: Dashboard
streamlit run dashboard.py --server.port 8502
```

### Stopping Everything
- **From Telegram**: Send `/stop` (pauses trading but keeps bot running)
- **From dashboard**: Click ⏹ Stop in the sidebar
- **Hard stop**: Close the terminal window or Ctrl+C in the PowerShell window

### Bot Switch (Pause Without Stopping)
The file `data/bot_switch.txt` contains either `on` or `off`.
- When `off`: the bot loop keeps running but skips all scans and trades
- When `on`: full operation resumes on the next cycle
- The dashboard Start/Stop buttons write to this file
- `/stop` and `/start` Telegram commands also write to this file

### Running on VPS 24/7

For always-on operation on a Windows VPS:
```powershell
# Install NSSM (service manager)
choco install nssm

# Register as Windows service
nssm install TradingBot "C:\Python312\python.exe" "mt5_bot.py --system --loop --confirm-trading"
nssm set TradingBot AppDirectory "C:\TradingBot"
nssm start TradingBot

# Bot now starts automatically on Windows boot, restarts if it crashes
```

---

## 14. What to Expect Day-to-Day

### Signal Frequency

- **2–4 signals per symbol per week** on H1
- Both EURUSD and XAUUSD = roughly **4–8 signals per week total**
- Not all signals become trades — the confluence score may reject some
- On demo with $10,000: expect 2–5 trades per week

### When the Bot Is Most Active

- **London session** (7:00–16:00 UTC): Highest liquidity, best engulfing patterns
- **New York session** (12:00–21:00 UTC): Also active, especially overlap with London (12–16 UTC)
- **Asian session** (00:00–07:00 UTC): Fewer signals, XAUUSD sometimes active
- **News events**: Bot pauses automatically 30 minutes before/after high-impact news

### Log Activity (Normal)

What you'll see every ~60 seconds in the log:
```
Market Regime for EURUSD: TRENDING
Market Regime for XAUUSD: TRENDING
```
This is normal — the bot is scanning and the regime check is passing.

What a trade entry looks like:
```
[INFO] Placing BUY EURUSD vol=0.20 entry=1.08500 sl=1.08000 tp=1.09114 score=8
[INFO] BUY EURUSD opened: ticket=12345 entry=1.08502
```

What a trade close looks like:
```
[INFO] Detected closed position ticket=12345
[INFO] Position closed (WIN): EURUSD BUY Net PnL: +$47.30 | R: +0.94R
```

### Monthly Profit Expectations

At 1% risk per trade with $10,000 account:
- 8 trades/week × 4 weeks = ~32 trades/month
- At 68% win rate with 0.8R average: ~$410/month ≈ 4.1% monthly return
- With compounding over 12 months: roughly 63% annual return

These are projections based on the strategy's design targets. Actual results depend on market conditions.

---

## 15. File-by-File Reference

### `mt5_bot.py` — Entry Point
Parses all command-line arguments. Connects to MT5. Routes to the right mode:
- `--system --loop --confirm-trading` → full production mode (runs `MT5BotSystem.run_forever()`)
- `--backtest-v2` → runs historical backtest and prints stats
- `--smoke-trade` → places a small test trade to verify execution works
- `--scan-signals` → scans historical candles for signals (read-only, no trades)
- `--show-positions` → prints current open positions

### `src/mt5_system.py` — The Brain
- `MT5BotSystem.__init__()` — creates all components, loads config, sets up Telegram commands
- `run_forever()` — the main loop (runs until stopped)
- `evaluate_and_trade(symbol)` — the full signal pipeline (called every cycle per symbol)
- `_risk_limits_gate()` — checks all risk limits before any trade
- `_record_trade_result()` — callback when a trade closes
- `_cmd_status()`, `_cmd_balance()` etc. — Telegram command handlers
- `_send_heartbeat()` — sends 12-hourly status update

### `src/trading_bot_engulfing_v2.py` — The Strategy
- `EngulfingBotV2` — the main strategy class
- `calculate_indicators(df)` — adds all indicators to the candle dataframe
- `generate_signal(df, idx)` — runs all filters, returns BUY/SELL/HOLD
- `is_bullish_engulfing()` / `is_bearish_engulfing()` — pattern detection
- `check_rsi_pullback()` — the RSI pullback filter
- `check_strict_trend()` — 200 EMA trend check
- `check_volume()` — volume filter
- `confluence_score()` — 12-signal scoring
- `calculate_stop_take_profit()` — SL/TP calculation
- `backtest()` — historical simulation

### `src/trade_supervisor.py` — Trade Manager
- `sync_with_terminal()` — detects closed trades, triggers callbacks
- `manage_position(ticket)` — moves SL to BE, trails stop, does partial close
- `check_structure_invalidation(ticket)` — emergency exit on structure break
- `_find_swing_point()` — finds fractal swing low/high for trailing

### `src/mt5_connection.py` — MT5 Bridge
All MT5 API calls are wrapped here:
- `initialize()` / `shutdown()` — connect/disconnect
- `send_market_order()` — place trade
- `close_position()` — close trade (full or partial)
- `set_position_sltp()` — modify SL/TP
- `get_rates()` — fetch OHLCV candles
- `get_account_summary()` — balance, equity, margin
- `get_positions()` — all open trades
- `calculate_position_costs()` — commission + swap for closed trade
- `validate_stops()` — verify SL/TP are valid before sending
- `normalize_volume()` — round lot size to broker's allowed increments

### `src/telegram_notifier.py` — Telegram Interface
- `send()` — send a plain text message
- `send_menu()` — send a message with inline keyboard buttons
- `poll_updates()` — check for new messages/button presses
- `register_command()` — map a command string to a handler function
- `register_keyboard_handler()` — map a callback_data to a handler
- `_handle_callback_query()` — process button presses
- `_api_request()` — low-level Telegram API call (JSON-encoded, SSL-secured)

### `src/market_regime.py` — Regime Classifier
- `RegimeClassifier.classify(df, news_blocked)` — returns MarketRegime enum
- Uses ATR, ADX, and Bollinger Band width to determine regime

### `src/news_filter.py` — Economic Calendar
- `NewsFilter.is_blocked(symbol)` — returns (True/False, reason)
- Fetches economic calendar from a news API
- Blocks trading 30 minutes before and after high-impact events for the currency pair

### `src/trade_manager.py` — State Persistence
- `TradeManager.load()` — reads trades_state.json on startup
- `TradeManager.upsert(trade)` — saves/updates a trade in state
- `TradeManager.remove(ticket)` — removes a closed trade from state
- `TradeManager.adopt_position(pos)` — creates a ManagedTrade from an MT5 position (handles reconnects)

### `src/performance_tracker.py` — Analytics
- Records every closed trade to performance_state.json
- Calculates running win rate, profit factor, R-multiple average
- Powers the dashboard performance charts

---

## 16. Glossary

| Term | Meaning |
|------|---------|
| **ATR** | Average True Range — measures average candle size (volatility) |
| **ADX** | Average Directional Index — measures trend strength 0–100 |
| **Confluence** | Multiple signals agreeing in the same direction |
| **Drawdown** | The peak-to-trough decline from the highest account value |
| **Engulfing** | A candle pattern where the body completely contains the previous candle's body |
| **EMA** | Exponential Moving Average — a smoothed average that weights recent prices more |
| **Equity** | Balance + unrealised P&L on open trades |
| **H1** | 1-hour candle timeframe |
| **D1** | Daily candle timeframe |
| **HTF** | Higher Time Frame (D1 in our case) |
| **Lot** | 1 lot = 100,000 units of base currency (0.01 lot = 1,000 units = micro lot) |
| **MACD** | Moving Average Convergence Divergence — momentum indicator |
| **Magic Number** | A unique ID the bot stamps on its trades (20260202) to distinguish them from manual trades |
| **Margin Level** | Equity ÷ Used Margin × 100% — how much buffer before broker closes trades |
| **Partial Close** | Closing 50% of a position to lock in profit while letting the rest run |
| **Pip** | Smallest standard price movement (0.0001 for EURUSD, 0.01 for XAUUSD) |
| **Profit Factor** | Gross wins ÷ Gross losses. >1.5 is considered good |
| **R / R-Multiple** | The unit of risk. 1R = initial risk amount. +2R means made 2× what you risked |
| **RSI** | Relative Strength Index — momentum oscillator 0–100 |
| **S/R** | Support and Resistance — price levels where buying/selling historically clusters |
| **SL** | Stop Loss — price where trade closes automatically if wrong |
| **Stochastic** | Momentum oscillator comparing close to high-low range |
| **Swing High/Low** | A local peak/trough in price — a candle with lower highs/higher lows on both sides |
| **TP** | Take Profit — price where trade closes automatically when right |
| **Wilder's EWM** | The correct RSI formula: exponential moving average with `com = period - 1` |
| **Win Rate** | % of trades that close in profit |

---

*This document reflects the bot as of v2.4. For changes, see `config/config_rsi_pullback.json` version field and the git commit history.*
