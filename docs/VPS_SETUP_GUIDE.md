# VPS Setup Guide - MT5 Engulfing Bot

## 🚀 Immediate Actions Checklist

### ⬜ Setup VPS with <50ms latency to MT5 server

#### Recommended VPS Providers
**Tier 1 (Best Latency):**
- **Contabo** (Germany/Frankfurt): ~15-25ms to MetaQuotes
- **Hetzner** (Germany/Nuremberg): ~20-30ms to MetaQuotes
- **Vultr** (Frankfurt): ~25-35ms to MetaQuotes

**Tier 2 (Acceptable):**
- **DigitalOcean** (Frankfurt): ~30-45ms to MetaQuotes
- **Linode** (Frankfurt): ~35-50ms to MetaQuotes
- **AWS** (Frankfurt): ~40-55ms to MetaQuotes

#### VPS Specifications
```
Minimum Requirements:
- CPU: 2+ cores
- RAM: 4GB+ 
- Storage: 80GB+ SSD
- Network: 1Gbps connection
- OS: Windows Server 2019/2022

Recommended:
- CPU: 4 cores
- RAM: 8GB
- Storage: 160GB SSD
- Network: 1Gbps with low latency
- OS: Windows Server 2022
```

#### Latency Testing
```powershell
# Test latency to MetaQuotes servers
Test-Connection -ComputerName 78.140.180.198 -Count 10
Test-Connection -ComputerName 78.140.180.199 -Count 10

# Acceptable results: <50ms average
# Target results: <30ms average
```

---

### ⬜ Sync VPS timezone to UTC+2

#### Timezone Configuration
```powershell
# Set timezone to UTC+2 (MetaQuotes time)
Set-TimeZone -Id "E. Europe Standard Time"

# Verify timezone
Get-TimeZone

# Sync with NTP servers
w32tm /resync
w32tm /query /status
```

#### Automatic Time Sync
```powershell
# Enable automatic time sync
Set-Service -Name w32time -StartupType Automatic
Start-Service -Name w32time

# Configure NTP servers
w32tm /config /manualpeerlist:"time.windows.com,0x8" /syncfromflags:manual /reliable:no /update
```

---

### ⬜ Deploy the "Revert & Protect" configuration

#### File Structure Setup
```
C:\MT5Bot\
├── trading_bot_engulfing_v2.py
├── mt5_bot.py
├── mt5_system.py
├── mt5_connection.py
├── telegram_notifier.py
├── forward_test_tracker.py
├── config_rsi_pullback.json
├── .env
├── requirements.txt
├── DEPLOYMENT_CHECKLIST.md
└── VPS_SETUP_GUIDE.md
```

#### MT5 Terminal Setup
```powershell
# Create MT5 installation directory
New-Item -ItemType Directory -Path "C:\Program Files\MetaTrader 5" -Force

# Download MT5 (URL from your broker)
# Install with default settings
# Configure login credentials
```

#### Python Environment Setup
```powershell
# Install Python 3.9+ if not present
python --version

# Create virtual environment
python -m venv C:\MT5Bot\venv

# Activate virtual environment
C:\MT5Bot\venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt
```

---

### ⬜ Ensure AutoTrading ON and safety guardrails active

#### MT5 Terminal Configuration
1. **Enable AutoTrading**:
   - Tools → Options → Expert Advisors
   - Check "Allow algorithmic trading"
   - Check "Allow DLL imports"

2. **Verify Safety Settings**:
   - Check `config_rsi_pullback.json` settings:
     ```json
     "max_daily_loss": 0.015,
     "max_open_trades": 1,
     "avoid_news_minutes": 30
     ```

3. **Test Safety Guards**:
   ```powershell
   # Run safety check
   python mt5_bot.py --safety-check
   ```

---

### ⬜ Launch demo forward-testing

#### Environment Configuration
```bash
# Create .env file
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Test Telegram integration
python test_telegram_integration.py
```

#### Launch Commands
```powershell
# Start bot in demo mode
python mt5_bot.py --system --demo --config config_rsi_pullback.json

# Verify all systems active
python mt5_bot.py --status-check
```

---

## 📋 Verification Steps

### ⬜ Run Telegram Integration Test
```powershell
python test_telegram_integration.py
```

**Expected Results:**
- ✅ Basic message sent
- ✅ Trade execution notification received
- ✅ Trade exit notification received
- ✅ Alert notification received
- ✅ Milestone notification received

### ⬜ Confirm Telegram Message Types

#### Trade Entries/Exits
```
🟢 [TRADE EXECUTED] - EURUSD H1 RSI_PULLBACK
Trade #1 BUY @ 1.08567
SL: 1.08345 | TP: 1.09012
RR: 2.2:1 | Volume: 1.3x
```

#### Slippage Alerts
```
⚠️ [WARNING] - EURUSD H1 RSI_PULLBACK
Alert Type: HIGH_SLIPPAGE
Message: Slippage 2.1 pips exceeds 1.5 pips limit
```

#### Daily Loss Alerts
```
⚠️ [WARNING] - EURUSD H1 RSI_PULLBACK
Alert Type: DAILY_LOSS_LIMIT
Message: Daily loss $-175.50 approaching 1.5% limit
```

#### Milestone Notifications
```
🎯 [MILESTONE] - EURUSD H1 RSI_PULLBACK
Trade #10 Reached
📊 Current Performance:
Total Trades: 10
Win Rate: 45.0%
Profit Factor: 1.15
```

### ⬜ Log Trade #1 Details

Create trade log entry:
```
=== TRADE #1 LOG ===
Date: 2026-02-04 20:15:30
Direction: BUY
Entry Price: 1.08567
Expected Entry: 1.08565
Slippage: 0.2 pips
Spread: 3 points
RSI: 38.5 (pullback to 40)
Volume: 1.3x confirmed
SR Distance: 0.0032 (within tolerance)
News Filter: Active (no high-impact news)
Safety Status: Daily Loss 0.0%, Open Trades 1/1
```

---

## 📊 Forward-Test Monitoring

### ⬜ Track 20 Trades or 30 Days

#### Monitoring Dashboard
```powershell
# Check current status
python forward_test_tracker.py --status

# Generate daily report
python forward_test_tracker.py --daily-report

# Full progress report
python forward_test_tracker.py --full-report
```

### ⬜ Key Metrics Monitoring

#### Target Metrics
| Metric | Target | Minimum Acceptable |
|--------|--------|-------------------|
| Win Rate | ≥ 45.8% | ≥ 41.2% |
| Profit Factor | ≥ 1.12 | ≥ 1.01 |
| Slippage | ≤ 1.5 pips | ≤ 2.0 pips |
| Max Drawdown | ≤ 4.23% | ≤ 4.5% |
| VPS Uptime | ≥ 99.9% | ≥ 99.0% |

#### Daily Monitoring Script
```powershell
# Create daily monitoring task
# Run every 6 hours
python monitor_forward_test.py --check-all
```

---

## 🎯 Go/No-Go Evaluation

### ⬜ After 20 Trades/30 Days Review

#### Performance Analysis
```powershell
# Generate evaluation report
python evaluate_forward_test.py --final
```

#### Decision Criteria
**✅ GO Decision:**
- Win Rate ≥ 41.2%
- Profit Factor ≥ 1.01
- Slippage ≤ 1.5 pips average
- Max Drawdown ≤ 4.5%
- No critical system failures

**❌ NO-GO Decision:**
- Win Rate < 35%
- Profit Factor < 0.9
- Slippage > 2.5 pips consistently
- Max Drawdown > 6%
- System reliability issues

### ⬜ Optional Optimization

#### Minor Tweaks Only
```json
// If PF < 1.1 consistently
"volume_multiplier": 1.35,  // Increase from 1.3

// If trade frequency too low
"sr_tolerance": 0.0040,     // Increase from 0.0035

// If slippage too high
"max_spread_points": 12     // Reduce from 15
```

#### Avoid Over-Optimization
- ❌ Don't change RSI thresholds
- ❌ Don't modify break-even logic
- ❌ Don't alter core strategy
- ✅ Only minor parameter adjustments

---

## 🚨 Emergency Procedures

### Immediate Stop Conditions
1. **Daily Loss > 1.5%**: Auto-stop triggered
2. **VPS Downtime > 1 hour**: Manual intervention required
3. **Slippage > 3 pips consistently**: Pause trading
4. **Multiple failed trades**: Review market conditions

### Manual Override
```powershell
# Emergency stop
echo "off" > bot_switch.txt

# Force close all positions
python mt5_bot.py --close-all

# Check system status
python mt5_bot.py --emergency-status
```

---

## 📞 Support & Troubleshooting

### Common Issues
1. **MT5 Connection Failed**: Check login credentials
2. **Telegram Not Working**: Verify bot token and chat ID
3. **High Slippage**: Check VPS latency and spread conditions
4. **No Trades**: Verify market hours and news filter settings

### Log Locations
```
C:\MT5Bot\logs\
├── mt5_connection.log
├── telegram_notifier.log
├── forward_test.log
└── system_monitor.log
```

### Daily Checklist
- [ ] Check VPS connectivity
- [ ] Verify MT5 terminal running
- [ ] Confirm Telegram notifications working
- [ ] Review trade execution quality
- [ ] Monitor daily loss limits
- [ ] Check milestone progress

---

## ✅ Deployment Summary

**Status:** Ready for forward-testing deployment
**Configuration:** Revert & Protect (RSI 40/60, Volume 1.3x, BE at 1.0R)
**Safety Guards:** Max 1.5% daily loss, 1 trade max, 30min news filter
**Monitoring:** Telegram notifications + forward-test tracker
**Duration:** 20 trades or 30 days
**Success Criteria:** Win Rate ≥ 41.2%, PF ≥ 1.01, Slippage ≤ 1.5 pips

*Next Step: Execute VPS setup and begin forward-testing with comprehensive monitoring.*
