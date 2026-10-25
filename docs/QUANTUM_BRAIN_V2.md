# Quantum Brain v2: Statistical Edge Engine

Research, validation and decision system for five-minute Rise/Fall contracts on
Deriv Volatility Indices. It is built to answer one question honestly: **does a
proposed strategy have a defensible statistical edge after Deriv's payout?** It
answers NO_TRADE unless the evidence says otherwise.

> First result on real data (R_100, 24 hours, checked 2026-10-24): no tick
> autocorrelation (Ljung-Box p = 0.83), no volatility clustering (p = 0.66),
> variance ratios ≈ 1.0 at every lag, one HMM state, and both models scored worse
> than the base rate out of sample. Break-even from real proposals is 51.15%.
> Verdict: NO_EVIDENCE_OF_EDGE. That matches Deriv's description of these indices
> as fair random processes. A longer history is needed before drawing conclusions,
> but this is the expected baseline.

---

## 1. Audit of the existing repository (Quantum Brain v1)

### What exists

| Area | Files | State |
|---|---|---|
| MT5 RSI-Pullback Engulfing bot (forex) | `mt5_bot.py`, `src/mt5_*`, `src/trading_bot_engulfing_v2.py`, `src/trade_*` | Working, tested (`tests/test_system.py`). Unrelated to Deriv binary options. |
| Synthetic-index statistics ("calculative brain") | `src/quant_brain.py` | Working. Hurst (bias-corrected), O-U with ADF gate, Poisson/Hawkes, runs tests, Kalman. Sound statistics. |
| Synthetic strategy, Kelly sizing, backtester, calibrator | `src/synthetic_*.py`, `synthetic_bot.py` | Working for **MT5 CFDs with SL/TP**, not Rise/Fall contracts. |
| Edge scanner with Benjamini-Hochberg | `src/edge_scanner.py` | Working; reused by v2 (variance ratio, runs test). |
| Synthetic dashboard | `dashboard_synthetic.py` | Working; reads MT5. |

### What was missing for the master prompt

- No Deriv API (WebSocket) client: v1 trades through MetaTrader 5 only.
- No Rise/Fall contract model, payouts, break-even, or contract-aligned labels.
- No tick storage, data-quality checks, lineage, or reproducible datasets.
- No chronological train/test discipline, purging, holdout protection, or calibration metrics.
- No model registry, promotion workflow, rollback or drift monitoring.
- No deterministic risk governor separate from the strategy (v1 risk is Kelly sizing).

### Unsafe components in v1 (not modified; flagged for your decision)

| Component | Issue |
|---|---|
| `config/config_synthetic.json` | `aggressive_mode: true`, 6.5% risk per trade, half-Kelly, pyramiding, "$5 → $1000" target. This is a ruin-prone profile. |
| `.env.synthetic.example` | `MAX_OPEN_TRADES=6`, `AGGRESSIVE_MODE=true` as template defaults. |
| `dashboard_synthetic.py` | The "▶️ Launch" button starts `synthetic_bot.py --run`, which trades on whatever account MT5 is logged into, real or demo. There is no demo-only check. |
| `src/synthetic_calibration.py` | Live trade outcomes adjust future win probabilities automatically. The adjustment is bounded, but it is the "live outcomes rewrite the model" pattern the master prompt forbids. |
| `src/synthetic_backtester.py` | Generator-based backtests only, with no real-data mode. Results describe the simulator, not the market. |

v2 shares none of these paths. Its dashboard has no buttons that act, and nothing
in v2 imports MT5.

---

## 2. Architecture

```
Deriv WebSocket (new API public endpoint; legacy supported)
   │  deriv_client.py  req_id matching · keepalive · reconnect · circuit-breaker callbacks
   │  schema.py        legacy ⇄ new field names · string|number parsing · Rise/Fall checks
   ▼
collector.py ──► storage.py (SQLite, migrations/) ◄── every proposal stored (historical payouts)
   │               ticks · lineage · quotes · decisions · trades · audit · models · drift · reports
   ▼
data_quality.py ─► features.py (causal, seconds-based) ─► labels.py (Deriv settlement rule)
                                   │
          ┌────────────────────────┼─────────────────────────────┐
          ▼                        ▼                             ▼
   patterns.py              regime.py                     models.py
   (BH + Holm,              (vol × trend states,          (base rate · logistic · GBM ·
    non-overlapping)         CUSUM, causal HMM)            ensemble; Platt; bagged σ)
          └──────────────┬─────────┴─────────────────────────────┘
                         ▼
               validation.py  walk-forward with purging · embargo · holdout guard ·
                              Brier/ECE · bootstrap CIs · stability · acceptance gates
                         ▼
               research.py / pipeline.py ──► registry.py (candidate → validated → [human] → champion)
                         ▼
   backtest.py (contract-aware)      trader.py ──► decision.py ──► risk_governor.py ──► paper fill
                                                                         └──► demo_executor.py (isolated)
                         ▼
               dashboard_quantum.py (read-only)
```

### Key design decisions

- **Labels match settlement.** Deriv's live contract text reads: *"Win payout if
  Volatility 100 Index is strictly higher than entry spot at 5 minutes after contract
  start time."* So entry = first tick after purchase, exit = last tick at or before
  start + 300 s, and a tie loses. Five minutes is about 150 ticks on R_* and about 300
  on 1HZ*; it is never "300 ticks".
- **Expected value uses the real proposal:** `EV = P · payout − stake`, where P is
  lowered by z·σ (model uncertainty) and must clear `min_edge_fraction` of the stake.
- **The model is advisory.** The decision engine has no account access. The risk
  governor approves every order on its own deterministic rules, and a halt is
  latched until a named operator clears it.
- **No automatic promotion.** Training creates candidates. Acceptance gates mark them
  validated or rejected, and only `promote --approver NAME` makes a champion. Drift
  can demote a champion automatically, because demotion only reduces trading.
- **The holdout is used once.** The final 20% segment is scored once per dataset
  hash. Reuse raises an error unless forced, and every forced reuse is logged.
- **Incomplete means incomplete.** Without recorded Deriv proposals, backtests are
  `NO_PAYOUT_DATA`. With `--payout-ratio` they are `INCOMPLETE_ASSUMED_PAYOUT`, and
  the acceptance gates reject any model whose payout was not real.

---

## 3. Safety model

| Setting | Default | Notes |
|---|---|---|
| `TRADING_MODE` | `paper` | Only `paper` and `demo` load; `live` is refused with exit code 2. |
| `DEMO_ONLY` | `true` | `false` is refused. No real-money code path exists. |
| `ENABLE_TRADING` | `false` | Read only by the `demo` command, and only together with `TRADING_MODE=demo`. |
| Contract | Rise/Fall 5 m, USD | Tick durations are refused. |
| Concurrent positions | 1 | |

The demo workflow (`quantum_brain.py demo`) refuses to buy unless every check passes:
1. All flags are set, a token is present, and (new API) `DERIV_ACCOUNT_ID` is set.
2. The account is virtual: `authorize.is_virtual`, a `VR…` loginid on legacy, and on
   the new API the server-assigned OTP endpoint must be `/options/ws/demo`. A real
   account halts the governor and the command exits.
3. `contracts_for` covers the duration, the proposal's contract text confirms
   Rise/Fall, and the quote is fresh and matches the approved stake and payout exactly.
4. The risk governor has approved the order. `buy` is sent with max price = the
   approved stake.
5. Settlement is read from `proposal_open_contract`, then the balance is reconciled.
   A mismatch halts.

Use a **dedicated demo account**: reconciliation assumes no other activity on it.

Risk governor controls: stake ≤ 1% of equity (floored to the cent, so it shrinks
after losses), 3% daily realized loss, 10% drawdown halt, 20 trades per day, 1 open
position, cooldown after 4 consecutive losses, stale tick (> 10 s) and quote (> 3 s)
rejection, idempotency keys, balance reconciliation, and a halt on more than 5
disconnects per hour or 3 abnormal responses. No martingale exists anywhere.

---

## 4. Reproducible research commands

```bash
pip install -r requirements.txt
python quantum_brain.py migrate

# Data (new public endpoint; no login needed for market data)
python quantum_brain.py discover                                  # 13 Volatility Indices found 2026-10-24
python quantum_brain.py backfill --symbols R_100,R_75,1HZ100V --hours 336
python quantum_brain.py stream --symbols R_100,R_75 --quote-every 60   # ticks + proposals (no buying)
python quantum_brain.py quality

# Research
python quantum_brain.py research --symbols R_100,R_75,1HZ100V     # ranking + per-instrument reports
python quantum_brain.py backtest --symbol R_100                   # uses recorded proposals when present
python quantum_brain.py train --symbol R_100                      # candidate; gates decide validated/rejected
python quantum_brain.py models

# Human decisions
python quantum_brain.py promote --version <VERSION> --approver "Your Name" --note "why"
python quantum_brain.py rollback --symbol R_100 --approver "Your Name"

# Monitoring and paper trading
python quantum_brain.py drift --symbol R_100
python quantum_brain.py paper --symbols R_100                     # decisions every 60 s on live quotes
streamlit run dashboard_quantum.py                                # read-only

# Offline dry run (stored as SIM:R_100, never mixed with real data)
python quantum_brain.py simulate --symbol R_100 --days 5
```

Run `stream` for days before training. It builds the history of real proposal
payouts that the acceptance gates require. A useful evaluation needs weeks of ticks:
with 1-minute decisions and 5-minute labels there are only about 288 independent
outcomes per day.

### Acceptance gates (all required for `validated`)
- Payout from real Deriv proposals
- ≥ 2000 out-of-sample predictions on the final segment
- Calibration error (ECE) ≤ 0.03
- Brier improvement over the base rate with a 95% CI above zero
- ≥ 200 simulated trades, with a 95% CI of return per stake above zero after payout
- Positive in ≥ 60% of sub-periods

---

## 5. Files

| File | Purpose |
|---|---|
| `quantum_brain.py` | CLI |
| `dashboard_quantum.py` | Read-only Streamlit dashboard |
| `src/qb2/config.py` | Safe settings, validation |
| `src/qb2/schema.py`, `deriv_client.py`, `collector.py` | Deriv API |
| `src/qb2/storage.py`, `migrations/0001_initial.sql` | Database |
| `src/qb2/data_quality.py`, `ticks.py`, `features.py`, `labels.py` | Data → dataset |
| `src/qb2/patterns.py`, `regime.py`, `models.py` | Research modules |
| `src/qb2/validation.py`, `stats_utils.py`, `research.py`, `pipeline.py` | Anti-overfitting lab and pipeline |
| `src/qb2/decision.py`, `risk_governor.py`, `backtest.py` | Decisions, risk, simulation |
| `src/qb2/registry.py`, `drift.py` | Model lifecycle |
| `src/qb2/trader.py`, `demo_executor.py` | Paper loop, isolated demo execution |
| `config/config_quantum.json`, `.env.quantum.example` | Configuration examples |
| `tests/test_qb2_core.py`, `test_qb2_trading.py`, `test_qb2_research.py` | 78 tests |

---

## 6. Verification status

**Verified**
- 134 tests pass (56 existing + 78 new); pyflakes is clean on all new code.
- Against the live new-API public endpoint (2026-10-24): `active_symbols` field names,
  `contracts_for` shape, `ticks_history` pagination (1000-tick pages), proposal pricing
  and contract text for CALL/PUT 5 m, and Rise/Fall availability on all 13 indices.
- 24 h of real R_100 ticks: no gaps, duplicates or outliers; research pipeline ran end to end.
- Dashboard renders headlessly (Streamlit AppTest) with no exceptions and no buttons.
- Live and real-account configurations are refused, both at configuration load and at demo preflight.

**Not verified (needs your demo credentials)**
- The authenticated new-API flow: the OTP request, whether that connection accepts
  `authorize`, and the `buy`, `proposal_open_contract` and `balance` responses on a
  real demo session. The code fails closed: without the `/options/ws/demo` endpoint
  path, or with a non-virtual account, it refuses and halts.
- The legacy endpoint (`ws.derivws.com`) answered HTTP 520 from this machine, so legacy
  support is implemented from the documented schema but not exercised live.
- PostgreSQL: the schema is portable, but only SQLite has been run.
