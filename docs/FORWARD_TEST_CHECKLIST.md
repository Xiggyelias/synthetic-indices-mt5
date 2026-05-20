# Phase 4: Forward Testing & Infrastructure Guardrails

## Operational Configuration (Final Logic)
- Asset: EURUSD (H1)
- Primary Filter: RSI(14) Pullback (Thresholds: 40/60)
- RSI Filter: Overbought 65, Oversold 35
- Momentum Filter: Volume Multiplier 1.5x
- Precision: SR Tolerance 0.005
- Management:
  - Break-even: Trigger at 1.0R
  - Stepped trail: Trigger at 1.5R profit -> move SL to +0.5R
  - Final TP: 2.2R

## Baseline Reference (Locked Better-Quality)
- Config: volume 1.5, SR 0.005, RSI 65/35, RR 2.2
- Backtest Window: 2025-08-05 -> 2026-02-05
- Trades: 23
- Win Rate: 47.83%
- Profit Factor: 1.198
- Expectancy: $10.40
- Net Profit: $239.18
- Max Drawdown: 7.12%

## Forward-Test Success Metrics (Live Hurdle)
Track over 20 trades or 30 days:
- Slippage variance: Backtest entry vs live entry
  - If average slippage > 1.5 pips, recalculate profit factor
- Spread sensitivity:
  - Monitor if trades are stopped out by spread widening during NY/London crossover
- Terminal uptime: 99.9% VPS connectivity (maintain 1.0R BE logic)

## Safety Guardrails (Emergency Stops)
- Max daily loss: 1.5%
- Max open trades: 1
- News filter: Pause trading 30 minutes before/after high-impact news (CPI, NFP, FOMC)

## Next Step Checklist
- Setup VPS - High - Pending
- Sync Timezones - Medium - Pending
- Launch Demo - Critical - Pending
- Log Trade #1 - Medium - Pending
