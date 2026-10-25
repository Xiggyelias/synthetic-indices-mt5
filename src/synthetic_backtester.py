"""
QUANTA-SYNTH Backtesting Engine
===============================
Bar-by-bar simulation of the brain + strategy on synthetic index data.

  - Market data follows Deriv's published index mechanics at tick level, aggregated
    to bars. Nothing in the generator favours any strategy: Volatility indices are
    constant-volatility GBM, Step is a fair ±0.1 walk, Boom/Crash spikes are memoryless
    with drift that offsets the expected spike (zero net drift).
  - Trade handling mirrors SyntheticBotSystem: runner trades bank part at TP1 and trail
    the rest by ATR toward TP2, other trades exit at TP1; breakeven + 0.15R lock,
    drift-harvest exit at +0.8R, spike harvest, stagnation exit on losers, max hold.
  - At each bar's close the entry thesis is re-checked: broken -> exit, intact -> a
    runner's target is pushed out, and a locked-in-profit position may be pyramided.
  - Stops are checked before targets inside a bar, and Boom/Crash stops fill at the
    bar extreme because spikes gap through them.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from quant_brain import CalculativeBrain
from synthetic_risk_manager import SyntheticRiskManager
from synthetic_strategy import SyntheticSignal, SyntheticStrategy, thesis_intact

SECONDS_PER_YEAR = 365 * 24 * 3600


@dataclass
class BacktestReport:
    symbol: str
    total_bars: int
    initial_balance: float
    final_balance: float
    total_return_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    expectancy_r: float
    equity_curve: List[float] = field(default_factory=list)
    trades: List[Dict[str, Any]] = field(default_factory=list)

    def summary_text(self) -> str:
        return (
            f"=================================================================\n"
            f"[QUANT-BACKTEST] REPORT: {self.symbol}\n"
            f"=================================================================\n"
            f"Bars Evaluated:      {self.total_bars}\n"
            f"Initial Capital:     ${self.initial_balance:,.2f}\n"
            f"Final Capital:       ${self.final_balance:,.2f} ({self.total_return_pct:+.2f}%)\n"
            f"Total Trades:        {self.total_trades} (Wins: {self.winning_trades}, Losses: {self.losing_trades})\n"
            f"Win Rate:            {self.win_rate:.1%}\n"
            f"Profit Factor:       {self.profit_factor:.2f}\n"
            f"Max Drawdown:        {self.max_drawdown_pct:.2f}%\n"
            f"Sharpe Ratio:        {self.sharpe_ratio:.2f}\n"
            f"Sortino Ratio:       {self.sortino_ratio:.2f}\n"
            f"Calmar Ratio:        {self.calmar_ratio:.2f}\n"
            f"Expectancy:          {self.expectancy_r:+.2f}R\n"
            f"================================================================="
        )


def _index_number(sym: str, default: int) -> int:
    match = re.search(r"(\d+)", sym)
    return int(match.group(1)) if match else default


def _ticks_to_bars(ticks: np.ndarray, per_bar: int) -> pd.DataFrame:
    n = len(ticks) // per_bar
    t = ticks[: n * per_bar].reshape(n, per_bar)
    return pd.DataFrame({"open": t[:, 0], "high": t.max(axis=1), "low": t.min(axis=1), "close": t[:, -1]})


def generate_synthetic_market_data(
    symbol: str = "Volatility 75 Index",
    n_bars: int = 1000,
    seed: Optional[int] = 42,
    bar_seconds: int = 300,
    start_price: float = 1000.0,
) -> pd.DataFrame:
    """
    Tick-level simulation of a Deriv synthetic index, aggregated to OHLC bars.
      Volatility N (1s): GBM with N% annualized volatility, one tick per 2s (1s variants: 1s)
      Step Index:        ±0.1 per second with equal probability
      Crash / Boom N:    one spike per N ticks on average (memoryless), spikes 0.4-1.2% of
                         price, with tick drift set so the expected net move is zero
    """
    rng = np.random.default_rng(seed)
    sym = symbol.upper()

    if "STEP" in sym:
        per_bar = bar_seconds
        steps = rng.choice(np.array([-0.1, 0.1]), size=n_bars * per_bar)
        ticks = np.round(start_price + np.cumsum(steps), 1)

    elif "CRASH" in sym or "BOOM" in sym:
        per_bar = bar_seconds
        mean_ticks = _index_number(sym, 1000)
        k = n_bars * per_bar
        spikes = rng.random(k) < 1.0 / mean_ticks
        spike_size = rng.uniform(0.004, 0.012, k)                  # log-size of each spike
        drift = 0.008 / mean_ticks                                  # offsets the mean spike
        noise = rng.normal(0.0, 0.00005, k)
        sign = -1.0 if "CRASH" in sym else 1.0                      # direction of spikes
        log_inc = -sign * drift + noise + sign * spikes * spike_size
        ticks = start_price * np.exp(np.cumsum(log_inc))

    else:
        tick_seconds = 1 if "(1S)" in sym or "1HZ" in sym else 2
        per_bar = max(1, bar_seconds // tick_seconds)
        vol = _index_number(sym.replace("(1S)", ""), 75) / 100.0
        dt = tick_seconds / SECONDS_PER_YEAR
        log_inc = rng.normal(-0.5 * vol ** 2 * dt, vol * math.sqrt(dt), n_bars * per_bar)
        ticks = start_price * np.exp(np.cumsum(log_inc))

    return _ticks_to_bars(ticks, per_bar).iloc[:n_bars].reset_index(drop=True)


class SyntheticBacktestEngine:
    """Run the brain bar-by-bar with the same rolling window and exits as the live bot."""

    def __init__(
        self,
        brain: Optional[CalculativeBrain] = None,
        risk_manager: Optional[SyntheticRiskManager] = None,
        initial_balance: float = 10000.0,
        min_math_edge_score: float = 65.0,
        lookback_bars: int = 300,
        be_trigger_r: float = 1.0,
        spike_harvest_r: float = 3.5,
        bar_seconds: int = 300,
        runner_strategies: Sequence[str] = ("FRACTAL_MOMENTUM",),
        runner_close_fraction: float = 0.5,
        runner_lock_r: float = 0.25,
        trail_atr_mult: float = 3.0,
        max_hold_hours: float = 0.0,
        spread: float = 0.0,
        thesis_exit_enabled: bool = True,
        extend_targets: bool = True,
        pyramid_enabled: bool = True,
        pyramid_max_adds: int = 2,
    ):
        self.brain = brain or CalculativeBrain()
        self.risk_manager = risk_manager or SyntheticRiskManager()
        self.strategy = SyntheticStrategy(
            brain=self.brain,
            risk_manager=self.risk_manager,
            min_math_edge_score=min_math_edge_score,
        )
        self.initial_balance = initial_balance
        self.lookback_bars = lookback_bars
        self.be_trigger_r = be_trigger_r
        self.spike_harvest_r = spike_harvest_r
        self.bars_per_year = SECONDS_PER_YEAR / bar_seconds
        self.runner_strategies = set(runner_strategies)
        self.runner_close_fraction = runner_close_fraction
        self.runner_lock_r = runner_lock_r
        self.trail_atr_mult = trail_atr_mult
        self.max_hold_bars = int(max_hold_hours * 3600 / bar_seconds) if max_hold_hours > 0 else 0
        self.spread = spread  # price units charged once per trade (enter at ask, leave at bid)
        self.thesis_exit_enabled = thesis_exit_enabled
        self.extend_targets = extend_targets
        self.pyramid_enabled = pyramid_enabled
        self.pyramid_max_adds = pyramid_max_adds

    @staticmethod
    def _atr_series(df: pd.DataFrame, period: int = 14) -> np.ndarray:
        prev_close = df["close"].shift(1)
        true_range = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(),
                                (df["low"] - prev_close).abs()], axis=1).max(axis=1)
        return true_range.rolling(period).mean().values

    def _check_exit(self, trade: Dict[str, Any], bar: pd.Series, bar_idx: int,
                    is_spiky: bool, atr: float) -> Optional[tuple]:
        """Return (exit_price, reason) if the trade closes on this bar; updates stops otherwise."""
        buy = trade["direction"] == "BUY"
        entry, sl = trade["entry_price"], trade["stop_loss"]
        r_dist = abs(entry - trade["initial_sl"])
        high, low, close = float(bar["high"]), float(bar["low"]), float(bar["close"])

        def tighten(level: float) -> None:
            if (buy and level > trade["stop_loss"]) or (not buy and level < trade["stop_loss"]):
                trade["stop_loss"] = level

        # Stops before targets: the conservative assumption when both are inside one bar
        if (buy and low <= sl) or (not buy and high >= sl):
            fill = (min(sl, close) if buy else max(sl, close)) if is_spiky else sl
            moved = trade["be_locked"] or trade["tp1_reached"] or sl != trade["initial_sl"]
            return fill, "TRAILED_STOP" if moved else "STOP_LOSS"

        tp1, tp2 = trade["take_profit"], trade["take_profit_2"]
        if trade["runner"]:
            if not trade["tp1_reached"] and ((buy and high >= tp1) or (not buy and low <= tp1)):
                r1 = abs(tp1 - entry) / r_dist
                trade["banked_r"] += self.runner_close_fraction * r1
                trade["remaining"] -= self.runner_close_fraction
                trade["tp1_reached"] = True
                tighten(entry + self.runner_lock_r * r_dist if buy else entry - self.runner_lock_r * r_dist)
            if (buy and high >= tp2) or (not buy and low <= tp2):
                return tp2, "TP2_RUNNER_HIT"
        elif (buy and high >= tp1) or (not buy and low <= tp1):
            return tp1, "TAKE_PROFIT"

        held = bar_idx - trade["entry_bar"]
        profit_r = ((close - entry) if buy else (entry - close)) / r_dist
        if self.max_hold_bars and held >= self.max_hold_bars:
            return close, "MAX_HOLD_EXIT"
        if trade["strategy_type"] == "QUIESCENT_DRIFT_HARVEST" and profit_r >= 0.8:
            return close, "DRIFT_HARVEST_WIN"
        if is_spiky and profit_r >= self.spike_harvest_r:
            return close, "SPIKE_HARVEST"
        if held >= trade["half_life_bars"] * 3.5 and profit_r <= -0.25:
            return close, "HL_STAGNATION_EXIT"

        if profit_r >= self.be_trigger_r and not trade["be_locked"]:
            tighten(entry + 0.15 * r_dist if buy else entry - 0.15 * r_dist)
            trade["be_locked"] = True
        if trade["runner"] and np.isfinite(atr) and atr > 0:
            trade["best_price"] = max(trade["best_price"], high) if buy else min(trade["best_price"], low)
            best = trade["best_price"]
            tighten(best - self.trail_atr_mult * atr if buy else best + self.trail_atr_mult * atr)
        return None

    def _close_trade(self, trade: Dict[str, Any], exit_price: float, reason: str, bar_idx: int,
                     balance: float, trades: List[Dict[str, Any]]) -> float:
        """Book a closed trade and return the new balance."""
        entry = trade["entry_price"]
        r_dist = abs(entry - trade["initial_sl"])
        exit_r = ((exit_price - entry) if trade["direction"] == "BUY" else (entry - exit_price)) / r_dist
        pnl_r = trade["banked_r"] + trade["remaining"] * exit_r - self.spread / r_dist
        if self.brain.calibrator is not None:
            self.brain.calibrator.record(trade["strategy_type"], trade["model_win_prob"], pnl_r > 0)
        pnl_dollars = pnl_r * trade["risk_dollars"]
        balance += pnl_dollars
        trades.append({
            "entry_bar": trade["entry_bar"],
            "exit_bar": bar_idx,
            "bars_held": bar_idx - trade["entry_bar"],
            "direction": trade["direction"],
            "strategy_type": trade["strategy_type"],
            "pyramid_add": trade["pyramid_add"],
            "entry_price": entry,
            "exit_price": exit_price,
            "pnl_r": round(pnl_r, 2),
            "pnl_dollars": round(pnl_dollars, 2),
            "exit_reason": reason,
            "balance": round(balance, 2),
        })
        return balance

    @staticmethod
    def _locked(trade: Dict[str, Any]) -> bool:
        entry, sl = trade["entry_price"], trade["stop_loss"]
        return sl >= entry if trade["direction"] == "BUY" else sl <= entry

    def _new_trade(self, sig: SyntheticSignal, bar_idx: int, is_add: bool) -> Dict[str, Any]:
        tp2_beyond = (sig.take_profit_2 > sig.take_profit) if sig.direction == "BUY" else (sig.take_profit_2 < sig.take_profit)
        return {
            "runner": sig.strategy_type in self.runner_strategies and tp2_beyond,
            "take_profit_2": sig.take_profit_2,
            "target_step": abs(sig.take_profit_2 - sig.take_profit),
            "tp1_reached": False,
            "banked_r": 0.0,
            "remaining": 1.0,
            "best_price": sig.entry_price,
            "entry_bar": bar_idx,
            "direction": sig.direction,
            "strategy_type": sig.strategy_type,
            "entry_price": sig.entry_price,
            "stop_loss": sig.stop_loss,
            "initial_sl": sig.stop_loss,
            "take_profit": sig.take_profit,
            "risk_dollars": sig.sizing.risk_dollars,
            "half_life_bars": max(1, sig.half_life_horizon_bars),
            "model_win_prob": float(sig.raw_analysis.metadata.get("model_win_prob", sig.calculated_win_prob))
            if sig.raw_analysis else sig.calculated_win_prob,
            "be_locked": False,
            "pyramid_add": is_add,
        }

    def run(self, df: pd.DataFrame, symbol: str) -> BacktestReport:
        balance = self.initial_balance
        equity_curve: List[float] = [balance]
        trades: List[Dict[str, Any]] = []
        active: List[Dict[str, Any]] = []
        is_spiky = "BOOM" in symbol.upper() or "CRASH" in symbol.upper()
        atr = self._atr_series(df)

        n_bars = len(df)
        warmup = min(self.lookback_bars, max(60, n_bars // 3))

        for bar_idx in range(warmup, n_bars):
            bar = df.iloc[bar_idx]
            close = float(bar["close"])

            # Intrabar exits, using the ATR of bars closed before this one (what the live bot sees)
            still_open = []
            for trade in active:
                exit_info = self._check_exit(trade, bar, bar_idx, is_spiky, float(atr[bar_idx - 1]))
                if exit_info is None:
                    still_open.append(trade)
                else:
                    balance = self._close_trade(trade, *exit_info, bar_idx, balance, trades)
            active = still_open

            if bar_idx < n_bars - 1 and balance > 0:
                window = df.iloc[max(0, bar_idx + 1 - self.lookback_bars): bar_idx + 1]
                analysis = self.brain.analyze(window, symbol=symbol, spread=self.spread)

                # At the close: hold while the thesis holds, exit when it breaks
                if self.thesis_exit_enabled:
                    still_open = []
                    for trade in active:
                        if not thesis_intact(analysis, trade["direction"], trade["strategy_type"]):
                            balance = self._close_trade(trade, close, "THESIS_BROKEN", bar_idx, balance, trades)
                            continue
                        if (self.extend_targets and trade["runner"] and trade["tp1_reached"]
                                and np.isfinite(atr[bar_idx]) and abs(trade["take_profit_2"] - close) <= atr[bar_idx]):
                            step = trade["target_step"]
                            trade["take_profit_2"] += step if trade["direction"] == "BUY" else -step
                        still_open.append(trade)
                    active = still_open

                sig = self.strategy.signal_from_analysis(analysis, account_balance=balance)
                if sig is not None and sig.sizing is not None:
                    can_add = (
                        self.pyramid_enabled
                        and len(active) - 1 < self.pyramid_max_adds
                        and all(t["direction"] == sig.direction and self._locked(t) for t in active)
                    )
                    if not active or can_add:
                        active.append(self._new_trade(sig, bar_idx, is_add=bool(active)))

            equity_curve.append(balance)

        return self._report(symbol, n_bars, balance, equity_curve, trades)

    def _report(self, symbol: str, n_bars: int, balance: float,
                equity_curve: List[float], trades: List[Dict[str, Any]]) -> BacktestReport:
        total = len(trades)
        wins = [t for t in trades if t["pnl_r"] > 0]
        losses = [t for t in trades if t["pnl_r"] <= 0]
        gross_profit = sum(t["pnl_dollars"] for t in wins)
        gross_loss = abs(sum(t["pnl_dollars"] for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (99.0 if gross_profit > 0 else 1.0)
        total_return_pct = (balance - self.initial_balance) / self.initial_balance * 100.0

        eq = np.array(equity_curve)
        peak = np.maximum.accumulate(eq)
        max_dd_pct = float(np.max((peak - eq) / peak)) * 100.0 if len(eq) else 0.0

        returns = np.diff(eq) / eq[:-1] if len(eq) > 1 else np.array([])
        mean_ret = float(np.mean(returns)) if len(returns) else 0.0
        std_ret = float(np.std(returns)) if len(returns) else 0.0
        downside = returns[returns < 0]
        downside_std = float(np.std(downside)) if len(downside) else 0.0
        annual = math.sqrt(self.bars_per_year)

        return BacktestReport(
            symbol=symbol,
            total_bars=n_bars,
            initial_balance=round(self.initial_balance, 2),
            final_balance=round(balance, 2),
            total_return_pct=round(total_return_pct, 2),
            total_trades=total,
            winning_trades=len(wins),
            losing_trades=len(losses),
            win_rate=round(len(wins) / total, 3) if total else 0.0,
            profit_factor=round(profit_factor, 2),
            max_drawdown_pct=round(max_dd_pct, 2),
            sharpe_ratio=round(mean_ret / std_ret * annual, 2) if std_ret > 1e-12 else 0.0,
            sortino_ratio=round(mean_ret / downside_std * annual, 2) if downside_std > 1e-12 else 0.0,
            calmar_ratio=round(total_return_pct / max_dd_pct, 2) if max_dd_pct > 0.01 else 0.0,
            expectancy_r=round(float(np.mean([t["pnl_r"] for t in trades])), 2) if total else 0.0,
            equity_curve=[round(float(v), 2) for v in equity_curve],
            trades=trades,
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="QUANTA-SYNTH Historical Backtester")
    parser.add_argument("--symbol", default="Volatility 75 Index", help="Synthetic symbol")
    parser.add_argument("--bars", type=int, default=1200, help="Number of simulated bars")
    args = parser.parse_args()

    data = generate_synthetic_market_data(symbol=args.symbol, n_bars=args.bars)
    print(SyntheticBacktestEngine().run(df=data, symbol=args.symbol).summary_text())
