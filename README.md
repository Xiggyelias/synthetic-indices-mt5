# ⚡ QUANTA-SYNTH & QUANTUM BRAIN v2

> **Autonomous Quantitative Trading & Statistical Edge Engine for Synthetic Volatility Indices**
> Designed for 24/7/365 uninterrupted markets (Deriv & MetaTrader 5 Synthetic Indices). Zero central bank dependency. Pure mathematical edge.

---

## 🌌 Overview

Unlike traditional forex and equities, **Synthetic Volatility Indices** operate 24/7/365 on continuous pseudo-random number generator (PRNG) algorithms with fixed calibrated volatilities (e.g., Volatility 10/25/50/75/100, Jump, and Step indices). They are immune to macroeconomic news releases, weekend gaps, and central bank surprises.

This repository hosts a two-tier quantitative trading ecosystem:

1. **QUANTA-SYNTH (`synthetic_bot.py`)**: Autonomous MetaTrader 5 execution engine with dynamic regime detection, statistical volatility breakout/pullback models, adaptive calibration, and institutional risk management.
2. **QUANTUM BRAIN v2 (`quantum_brain.py` & `src/qb2/`)**: Research-grade statistical edge engine featuring high-frequency tick ingestion, walk-forward model validation, feature drift monitoring, contract-aware backtesting, and isolated demo execution.

---

## 🏛️ System Architecture

```
files (1)/
├── synthetic_bot.py                 # QUANTA-SYNTH main entry point
├── quantum_brain.py                 # QUANTUM BRAIN v2 CLI orchestrator
├── dashboard.py                     # Unified Streamlit entry point
├── dashboard_synthetic.py           # QUANTA-SYNTH institutional control center
├── dashboard_quantum.py             # QUANTUM BRAIN research & model matrix
│
├── config/
│   ├── config_synthetic.json        # Strategy & risk parameters for MT5 synthetic bot
│   └── config_quantum.json          # Calibration, tick, and drift settings for QB2
│
├── src/
│   ├── synthetic_bot_system.py      # Core execution orchestrator & lifecycle
│   ├── synthetic_strategy.py        # Volatility regime, breakout & momentum models
│   ├── synthetic_risk_manager.py    # Dynamic ATR position sizing & equity circuit breakers
│   ├── synthetic_calibration.py     # Parameter adaptation & statistical deviation checks
│   ├── synthetic_backtester.py      # Bar-by-bar historical backtest simulation
│   ├── quant_brain.py               # Core quant decision engine
│   ├── edge_scanner.py              # Statistical anomaly & edge scanner
│   ├── mt5_connection.py            # Resilient MT5 wrapper with exponential backoff
│   ├── telegram_notifier.py         # Real-time Telegram alerts & execution reports
│   │
│   └── qb2/                         # Modular Quantum Brain v2 Engine
│       ├── collector.py             # Tick collection and live proposal ingestion
│       ├── data_quality.py          # Missing tick & data health auditor
│       ├── decision.py              # Rule-based and model-based decision matrix
│       ├── demo_executor.py         # Isolated demo execution harness
│       ├── deriv_client.py          # WebSocket client for Deriv API
│       ├── drift.py                 # Population stability index (PSI) & feature drift
│       ├── features.py              # Microstructure & return feature extraction
│       ├── labels.py                # Fixed-horizon & barrier label generators
│       ├── models.py                # Classifier and probabilistic edge models
│       ├── patterns.py              # Candlestick and tick pattern extractors
│       ├── pipeline.py              # End-to-end training and feature pipeline
│       ├── regime.py                # Volatility state classifier
│       ├── registry.py              # Model artifact registry (champion/candidate)
│       ├── research.py              # Cross-instrument edge discovery & ranking
│       ├── risk_governor.py         # Drawdown latching and contract-level risk gate
│       ├── storage.py               # SQLite research database (`qb.db`)
│       ├── ticks.py                 # Tick buffer and resampler
│       ├── trader.py                # Live/paper trade loop coordinator
│       └── validation.py            # Combinatorial & walk-forward validator
│
├── scripts/
│   ├── start_synthetic_bot.bat       # Launch autonomous MT5 synthetic bot
│   ├── start_synthetic_dashboard.bat # Launch QUANTA-SYNTH Streamlit UI (Port 8501)
│   ├── start_quantum_dashboard.bat   # Launch Quantum Brain UI (Port 8502)
│   └── bot_runtime_control.py       # Hot-reload switch & runtime management
│
├── tests/
│   ├── test_synthetic_brain.py      # Comprehensive tests for synthetic system
│   ├── test_qb2_core.py             # QB2 core module test suite
│   ├── test_qb2_research.py         # QB2 research & feature pipeline tests
│   └── test_qb2_trading.py          # QB2 execution, paper & demo trade tests
│
├── docs/
│   └── QUANTUM_BRAIN_V2.md          # Full architectural specification of QB2
│
├── requirements.txt                 # Core dependencies
└── requirements-bot.txt             # MT5 & Telegram runtime dependencies
```

---

## ⚡ Quick Start

### 1. Environment Configuration

Copy the sample environment file for your desired runtime:

```bash
# For QUANTA-SYNTH (MetaTrader 5):
copy .env.synthetic.example .env

# For QUANTUM BRAIN v2 (Deriv API):
copy .env.quantum.example .env
```

Fill in your MT5 account credentials or Deriv API token inside `.env`.

### 2. Installing Dependencies

```bash
pip install -r requirements-bot.txt
```

---

## 🚀 Running QUANTA-SYNTH (MetaTrader 5)

### Command Line Interface

```bash
# Start 24/7 autonomous loop across configured synthetic symbols
python synthetic_bot.py --run

# Scan and rank all configured synthetic markets
python synthetic_bot.py --scan

# Run statistical edge scan on historical MT5 bars
python synthetic_bot.py --edge-scan --bars 20000

# Run bar-by-bar backtest on a specific index
python synthetic_bot.py --backtest "Volatility 75 Index" --bars 5000 --timeframe M5

# Show mathematical PRNG formulas for all synthetic indices
python synthetic_bot.py --math-formulas
```

### Windows Batch Launcher
Double-click or run:
```cmd
scripts\start_synthetic_bot.bat
```

### Live Streamlit Dashboard
```cmd
scripts\start_synthetic_dashboard.bat
# Access at http://localhost:8501
```

---

## 🧠 Running QUANTUM BRAIN v2

QUANTUM BRAIN operates against the Deriv API via WebSocket:

```bash
# 1. Initialize or migrate the local SQLite research database
python quantum_brain.py migrate

# 2. Discover available Volatility Indices and contracts
python quantum_brain.py discover

# 3. Ingest historical tick data
python quantum_brain.py backfill --symbols "R_100,R_75" --count 50000

# 4. Run data quality check
python quantum_brain.py quality

# 5. Conduct statistical edge research and ranking
python quantum_brain.py research

# 6. Walk-forward contract-aware backtest
python quantum_brain.py backtest --symbol R_100

# 7. Start paper trading loop
python quantum_brain.py paper
```

### Quantum Brain Research Dashboard
```cmd
scripts\start_quantum_dashboard.bat
# Access at http://localhost:8502
```

---

## 🛡️ Risk Management & Safety Gates

* **Zero Weekend Risk**: Synthetic markets trade 24/7 without weekend gap risk or session rollover spikes.
* **Volatility-Adjusted Position Sizing**: All position sizes scale dynamically using instantaneous ATR and localized variance.
* **Capital Protection**:
  * Hard daily loss limit (configurable in `config_synthetic.json`).
  * Drawdown circuit breakers automatically pause execution if equity drops past threshold.
  * Losing streak cooling period with dynamic risk reduction.
* **Model Drift Sentinel**: Quantum Brain v2 monitors Population Stability Index (PSI) and feature distributions in real time; execution halts if model drift is detected.

---

## 🧪 Running the Test Suite

The entire synthetic test suite runs under `pytest`:

```bash
pytest tests/test_synthetic_brain.py tests/test_qb2_core.py tests/test_qb2_research.py tests/test_qb2_trading.py -v
```

All 121 tests cover:
* Synthetic strategy indicators and signal generation
* Mathematical edge detection and calibration
* Tick buffer processing, feature pipelines, and label generation
* Risk governor latching and trade execution safety
