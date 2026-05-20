# MT5 Engulfing Bot - Deployment Checklist

## 🚀 FINAL OPERATIONAL CONFIGURATION

### Asset & Timeframe
- **Symbol**: EURUSD
- **Timeframe**: H1 (1-hour)
- **Strategy Mode**: RSI_PULLBACK

### Core Strategy Parameters
- **RSI Settings**: Period 14, Pullback thresholds 45/55
- **Volume Filter**: 1.1x average volume
- **SR Tolerance**: 0.0050 (0.50%)
- **Min Risk-Reward**: 0.28:1

### Trade Management
- **Break-Even**: Trigger at 1.0R (exact)
- **Stepped Trailing**: At 1.5R → Move SL to +0.5R
- **Final TP**: 0.28R (or ATR trailing stop)
- **No Partial Closes**: Full position held to completion ("allow_partial_close": false)

---

## 📋 DEPLOYMENT CHECKLIST

### 🔴 CRITICAL - Must Complete Before Launch

#### [ ] VPS Setup (Priority: HIGH)
- [ ] Rent VPS with <50ms latency to MT5 server
- [ ] Install Windows Server 2019/2022
- [ ] Install MT5 terminal
- [ ] Configure automatic startup on boot
- [ ] Test remote desktop access
- [ ] Verify 99.9% uptime capability

#### [ ] Timezone Synchronization (Priority: MEDIUM)
- [ ] Set VPS timezone to UTC+2 (MetaQuotes time)
- [ ] Configure NTP synchronization
- [ ] Verify candle alignment with live market
- [ ] Test during London/NY crossover

#### [ ] Demo Launch (Priority: CRITICAL)
- [ ] Deploy bot to demo account
- [ ] Verify all safety guards are active
- [ ] Test news filter functionality
- [ ] Confirm break-even logic triggers at 1.0R
- [ ] Validate stepped trailing at 1.5R

#### [ ] Trade #1 Logging (Priority: MEDIUM)
- [ ] Record expected vs actual entry price
- [ ] Document slippage in pips
- [ ] Note spread at entry
- [ ] Capture exit reason (SL/TP/BE/TRAIL)
- [ ] Save trade execution screenshots

---

## 📊 FORWARD-TESTING SUCCESS METRICS

### 20-Trade Sample or 30-Day Test

#### Slippage Variance Monitoring
- **Target**: < 1.5 pips average slippage
- **Measurement**: (Backtest Entry Price - Live Entry Price)
- **Action**: If > 1.5 pips, recalculate profit factor
- **Tool**: `forward_test_tracker.py`

#### Spread Sensitivity Analysis
- **Monitor**: NY/London crossover spread widening
- **Alert**: Trades stopped out by spread spikes
- **Document**: Maximum spread encountered
- **Impact**: Adjust max_spread_points if needed

#### Terminal Uptime Verification
- **Target**: 99.9% VPS connectivity
- **Monitor**: Connection drops during critical hours
- **Test**: Break-even logic reliability
- **Backup**: Auto-restart mechanism

---

## 🛡️ SAFETY GUARDRAILS (Emergency Stops)

### Risk Limits
- **Max Daily Loss**: 1.5% (Flash crash protection)
- **Max Open Trades**: 1 (Single pair focus)
- **Max Risk Per Trade**: 0.5% (Conservative sizing)

### News Protection
- **High-Impact News Filter**: 30 minutes before/after
- **Red Folder Events**: CPI, NFP, FOMC
- **Auto-Pause**: Trading halts during news windows
- **Resume**: Automatic after news window

### Connection Safety
- **Heartbeat**: Every 60 seconds
- **Fail-Safe**: Close all trades on disconnect > 5 minutes
- **Recovery**: Auto-reconnect and state restoration
- **Alerts**: Telegram/email notifications

---

## 📈 PERFORMANCE BENCHMARKS

### Backtest Reference (Achieved RSI-Pullback Champion Setup)
- **Win Rate**: 79.29%
- **Profit Factor**: 1.06
- **Expectancy**: $0.93 per trade (positive expectancy verified)
- **Max Drawdown**: 4.87% (extremely robust)
- **Signal Frequency**: 2-3 signals per week per symbol on H1

### Forward-Test Targets (Calibrated Champion Baseline)
- **Win Rate**: ≥ 75.0%
- **Profit Factor**: ≥ 1.06 (or ≥ 1.05 minimum acceptable)
- **Expectancy**: ≥ $0.20 per trade
- **Slippage**: ≤ 1.5 pips average
- **Uptime**: ≥ 99.9%

---

## 🚨 EMERGENCY PROCEDURES

### Manual Override
1. **Kill Switch**: `bot_switch.txt` set to "off"
2. **Force Close**: `mt5_bot.py --close-all`
3. **Emergency Stop**: VPS shutdown
4. **Broker Alert**: Call dealing desk

### Recovery Steps
1. **Diagnose**: Check logs for error source
2. **Isolate**: Determine if market or technical issue
3. **Communicate**: Alert stakeholders of status
4. **Document**: Record incident for post-mortem

---

## ✅ PRE-LAUNCH VERIFICATION

### Configuration Validation
- [ ] `config_rsi_pullback.json` matches final parameters
- [ ] Safety guardrails active in MT5 system
- [ ] News filter connected to reliable feed
- [ ] Telegram notifications working

### System Health Check
- [ ] VPS resources adequate (CPU, RAM, disk)
- [ ] MT5 terminal authorized and stable
- [ ] Python environment with all dependencies
- [ ] Log rotation configured

### Market Readiness
- [ ] Current market conditions suitable
- [ ] No high-impact news in next 24 hours
- [ ] Spread within normal ranges
- [ ] Liquidity conditions normal

---

## 📞 CONTACT INFORMATION

### Primary Support
- **System Admin**: [Contact details]
- **Trading Lead**: [Contact details]
- **VPS Provider**: [Contact details]
- **Broker Support**: [Contact details]

### Escalation Path
1. **Level 1**: System Admin (Technical issues)
2. **Level 2**: Trading Lead (Strategy issues)
3. **Level 3**: Emergency Committee (Risk management)

---

## 📋 POST-LAUNCH MONITORING

### Daily Checks (First 30 Days)
- [ ] Review trade execution quality
- [ ] Verify slippage within limits
- [ ] Check news filter effectiveness
- [ ] Monitor drawdown levels
- [ ] Validate profit factor maintenance

### Weekly Reviews
- [ ] Performance vs benchmark analysis
- [ ] System uptime and connectivity
- [ ] Error log analysis
- [ ] Optimization opportunities

### Monthly Assessment
- [ ] Forward-test completion status
- [ ] Live deployment readiness
- [ ] Parameter adjustment needs
- [ ] Risk management review

---

## 🎯 SUCCESS CRITERIA

### Go/No-Go Decision Points

#### After 20 Trades
- ✅ **GO**: Win rate ≥ 75.0% AND Profit factor ≥ 1.06
- ❌ **NO-GO**: Win rate < 70.0% OR Profit factor < 1.00

#### After 30 Days
- ✅ **GO**: All metrics within target expectations (Win rate ≥ 75.0%, Profit factor ≥ 1.06)
- ❌ **NO-GO**: Any metric below risk-protection expectations (Win rate < 70.0% OR Profit factor < 1.00)

#### Final Decision
- ✅ **LIVE READY**: Consistent performance, low slippage, stable system
- ❌ **NEEDS WORK**: Significant deviation from backtest expectations

---

*Last Updated: 2026-02-04*
*Version: 1.0 - Final Operational Configuration*
