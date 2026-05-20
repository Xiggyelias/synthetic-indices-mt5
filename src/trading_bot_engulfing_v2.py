"""
Engulfing Candlestick Trading Bot v2.0 - RSI-Pullback Strategy
===============================================================
Enhanced version with High-Probability RSI-Pullback filter
for improved win rate and reduced drawdown.

KEY UPGRADES:
1. RSI Pullback Filter - Buy dips in uptrends, sell rallies in downtrends
2. Strict Trend Filter - Must trade WITH the 200 EMA
3. Conviction Filter - 2.0x volume requirement for institutional moves
4. Tighter S/R Levels - 0.1% tolerance for precision entries

Author: Trading Bot System v2.0
Version: 2.0.0
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from enum import Enum
import json
import warnings
from market_regime import MarketRegime
warnings.filterwarnings('ignore')

class Signal(Enum):
    """Trading signal types"""
    BUY = 1
    SELL = -1
    HOLD = 0

class TimeFrame(Enum):
    """Supported timeframes"""
    M5 = "5min"
    M15 = "15min"
    H1 = "1h"
    H4 = "4h"

@dataclass
class TradingConfig:
    """Configuration for the trading bot v2.0"""
    # Risk Management
    risk_per_trade: float = 0.01
    min_risk_reward: float = 0.28          # FIX: was 2.2 — small TP/SL is achievable (78%+ breakeven WR)
    max_drawdown: float = 0.15
    capital_allocation: float = 0.0
    max_risk_per_trade: float = 0.02
    max_total_open_risk: float = 0.075    # FIX: was 0.005 — allow up to 3 concurrent trades
    max_daily_loss: float = 0.06          # FIX: was 0.015 — aligned with config
    allow_partial_close: bool = False

    # Behavioural risk controls
    capital_protection_enabled: bool = True
    cp_daily_loss_trigger: float = 0.06   # FIX: was 0.05
    cp_weekly_loss_trigger: float = 0.14  # FIX: was 0.12
    cp_consecutive_losses_trigger: int = 6  # FIX: was 5
    cp_risk_fraction: float = 0.006
    cp_min_signal_score: int = 7
    cp_max_trades_per_day: int = 5        # FIX: was 4
    losing_streak_pause_after: int = 6   # FIX: was 5
    losing_streak_cooldown_seconds: float = 2700.0  # FIX: was 3600
    dynamic_risk_daily_dd_soft: float = 0.03   # FIX: was 0.025
    dynamic_risk_daily_dd_hard: float = 0.055  # FIX: was 0.05
    dynamic_risk_soft_multiplier: float = 0.90  # FIX: was 0.88
    dynamic_risk_hard_multiplier: float = 0.75  # FIX: was 0.72
    volatile_regime_risk_multiplier: float = 0.82  # FIX: was 0.78
    
    # Strategy Parameters - UPGRADED DEFAULTS
    timeframe: TimeFrame = TimeFrame.H4  # Filter noise
    ema_fast: int = 50
    ema_slow: int = 200
    use_rsi_filter: bool = True
    rsi_period: int = 14
    rsi_overbought: float = 62   # FIX: was 48 — inverted! overbought must be > oversold
    rsi_oversold: float = 38     # FIX: was 52 — inverted! oversold must be < overbought
    
    # RSI PULLBACK SETTINGS (NEW)
    rsi_pullback_enabled: bool = True
    rsi_pullback_buy_threshold: float = 45   # CALIBRATED: 40 too extreme for H1; 45 catches more dips
    rsi_pullback_sell_threshold: float = 55  # CALIBRATED: 60 too extreme for H1; 55 catches more rallies
    rsi_lookback_candles: int = 10  # CALIBRATED: 5 too narrow; 10 candles gives realistic recovery window
    
    # CONFLUENCE SCORING
    min_confluence_score: int = 3  # Minimum signals required for entry
    
    # EMA 20 for confluence
    ema_trend: int = 20
    
    # MACD settings
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    
    # Stochastic settings
    stoch_k: int = 14
    stoch_d: int = 3
    stoch_smooth: int = 3
    
    # Volume confirmation — institutional filter
    volume_multiplier: float = 1.10   # CALIBRATED: 1.75 blocked all trades; H1 market runs at ~1.1x avg
    volume_period: int = 20

    max_spread_points: int = 0     # 0 = disabled; ATR-relative check used in mt5_system
    max_slippage_points: int = 10

    min_body_atr_mult: float = 0.15
    flat_atr_mult: float = 0.1
    ema_slope_lookback: int = 10
    ema_slope_atr_mult: float = 0.02
    
    # Support/Resistance — precision entry
    sr_lookback: int = 50
    sr_tolerance: float = 0.005   # CALIBRATED: measured min distance is 0.148% (EURUSD) / 0.456% (XAUUSD)
    
    # News filter
    avoid_news_minutes: int = 30
    
    # NEW: ATR Trailing Stop
    use_atr_trailing: bool = True
    atr_trailing_mult: float = 2.0  # 2.0x ATR for trailing
    
    # NEW: Multi-Timeframe Analysis
    use_mtf_filter: bool = True
    mtf_timeframe: str = "D1"
    mtf_ema_period: int = 50
    
    # NEW: Friday Close
    friday_close_enabled: bool = True
    friday_close_hour_utc: int = 20  # 8 PM UTC on Friday
    
    # Account
    initial_capital: float = 10000.0
    
    # Strategy mode
    strategy_mode: str = "RSI_PULLBACK"

    # Skip ranging/choppy markets to protect win rate
    skip_weak_range_entries: bool = True  # FIX: was False — skip chop to protect win rate

    # Selectivity gates
    min_trade_score_full_entry: int = 7
    min_trade_score_half_entry: int = 6   # FIX: was 5 — raise bar for half-size entries
    min_planned_rr_execution: float = 0.26  # FIX: was 0.3 — slightly relaxed to match config
    
@dataclass
class Trade:
    """Individual trade record"""
    entry_time: datetime
    entry_price: float
    direction: Signal
    stop_loss: float
    take_profit: float
    position_size: float
    rsi_at_entry: float = 0.0
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    profit_loss: float = 0.0
    status: str = "OPEN"
    moved_to_break_even: bool = False
    fees: float = 0.0
    planned_rr: float = 0.0
    r_multiple: float = 0.0
    regime: str = ""
    initial_stop_loss: float = 0.0
    
    # Partial profit taking fields
    partial_closed: bool = False
    partial_close_size: float = 0.0  # Amount of position closed at 1R
    partial_close_pnl: float = 0.0   # P&L from partial close
    remaining_size: float = 0.0      # Remaining position size after partial close
    
    def close_trade(
        self,
        exit_time: datetime,
        exit_price: float,
        fees: float = 0.0,
        r_multiple: float = 0.0,
        profit_loss_override: Optional[float] = None,
    ):
        """Close the trade and calculate P&L"""
        self.exit_time = exit_time
        self.exit_price = exit_price
        self.status = "CLOSED"
        self.fees = float(fees or 0.0)
        self.r_multiple = float(r_multiple or 0.0)

        if profit_loss_override is not None:
            self.profit_loss = float(profit_loss_override)
            return
        
        # Calculate P&L based on full position
        if self.direction == Signal.BUY:
            self.profit_loss = (exit_price - self.entry_price) * self.position_size
        else:
            self.profit_loss = (self.entry_price - exit_price) * self.position_size
        
        self.profit_loss -= self.fees
    
    def partial_close(
        self,
        close_size: float,
        exit_price: float,
        fees: float = 0.0,
    ) -> float:
        """Close part of the position and return P&L from partial close"""
        if self.partial_closed:
            return 0.0  # Already partially closed
        
        self.partial_closed = True
        self.partial_close_size = close_size
        self.remaining_size = self.position_size - close_size
        
        # Calculate P&L for partial close
        if self.direction == Signal.BUY:
            partial_pnl = (exit_price - self.entry_price) * close_size
        else:
            partial_pnl = (self.entry_price - exit_price) * close_size
        
        # Proportional fees
        partial_fees = fees * (close_size / self.position_size)
        partial_pnl -= partial_fees
        
        self.partial_close_pnl = partial_pnl
        return partial_pnl

class EngulfingBotV2:
    """
    Enhanced trading bot with RSI-Pullback strategy
    """
    
    def __init__(self, config: TradingConfig):
        self.config = config
        self.trades: List[Trade] = []
        self.equity_curve: List[float] = []
        self.current_capital = config.initial_capital
        self.peak_capital = config.initial_capital
        
    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate all technical indicators"""
        # EMAs for trend direction
        df['EMA_20'] = df['close'].ewm(span=self.config.ema_trend, adjust=False).mean()
        df['EMA_50'] = df['close'].ewm(span=self.config.ema_fast, adjust=False).mean()
        df['EMA_200'] = df['close'].ewm(span=self.config.ema_slow, adjust=False).mean()
        
        # RSI — Wilder's EMA smoothing (alpha = 1/period), NOT simple rolling mean.
        # com = period-1 gives alpha = 1/(1+com) = 1/period, matching Wilder's formula.
        _rsi_period = int(self.config.rsi_period)
        _delta = df['close'].diff()
        _gain  = _delta.where(_delta > 0, 0.0)
        _loss  = (-_delta.where(_delta < 0, 0.0))
        _avg_gain = _gain.ewm(com=_rsi_period - 1, min_periods=_rsi_period, adjust=False).mean()
        _avg_loss = _loss.ewm(com=_rsi_period - 1, min_periods=_rsi_period, adjust=False).mean()
        _rs = _avg_gain / _avg_loss.replace(0.0, np.finfo(float).eps)
        df['RSI'] = 100.0 - (100.0 / (1.0 + _rs))
        
        # MACD
        ema_fast = df['close'].ewm(span=self.config.macd_fast, adjust=False).mean()
        ema_slow = df['close'].ewm(span=self.config.macd_slow, adjust=False).mean()
        df['MACD'] = ema_fast - ema_slow
        df['MACD_Signal'] = df['MACD'].ewm(span=self.config.macd_signal, adjust=False).mean()
        df['MACD_Hist'] = df['MACD'] - df['MACD_Signal']
        
        # Stochastic
        low_min = df['low'].rolling(window=self.config.stoch_k).min()
        high_max = df['high'].rolling(window=self.config.stoch_k).max()
        range_val = high_max - low_min
        range_val = range_val.where(range_val > 0, 1e-10)
        df['Stoch_K'] = 100 * (df['close'] - low_min) / range_val
        df['Stoch_D'] = df['Stoch_K'].rolling(window=self.config.stoch_d).mean()
        
        # Volume average
        df['Volume_Avg'] = df['volume'].rolling(window=self.config.volume_period).mean()

        prev_close = df['close'].shift(1)
        tr1 = (df['high'] - df['low']).abs()
        tr2 = (df['high'] - prev_close).abs()
        tr3 = (df['low'] - prev_close).abs()
        df['TR'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df['ATR'] = df['TR'].rolling(window=14).mean()
        
        # ADX calculation
        plus_dm = df['high'].diff()
        minus_dm = df['low'].diff()
        plus_dm[plus_dm < 0] = 0
        minus_dm[minus_dm > 0] = 0
        minus_dm = abs(minus_dm)
        
        tr_14 = df['TR'].rolling(window=14).sum()
        plus_di_14 = 100 * (plus_dm.rolling(window=14).sum() / tr_14)
        minus_di_14 = 100 * (minus_dm.rolling(window=14).sum() / tr_14)
        dx = 100 * (abs(plus_di_14 - minus_di_14) / (plus_di_14 + minus_di_14))
        df['ADX'] = dx.rolling(window=14).mean()
        
        # Support and Resistance levels
        df['Support'], df['Resistance'] = self.identify_support_resistance(df)
        
        # Fair Value Gaps (FVG)
        df['FVG_Bull'] = (df['low'] > df['high'].shift(2))
        df['FVG_Bear'] = (df['high'] < df['low'].shift(2))
        
        return df

    def _classify_regime(self, df: pd.DataFrame, idx: int) -> str:
        curr = df.iloc[idx]
        atr = float(curr.get('ATR') or 0.0)
        ema50 = curr.get('EMA_50')
        ema200 = curr.get('EMA_200')
        if ema50 is None or ema200 is None or pd.isna(ema50) or pd.isna(ema200):
            return "UNKNOWN"
        gap = abs(float(ema50) - float(ema200))
        # Reduced thresholds for more sensitive regime detection
        flat_threshold = atr * float(self.config.flat_atr_mult) * 1.5  # Loosened from 1.0x to 1.5x ATR
        if atr > 0 and gap < flat_threshold:
            return "WEAK_RANGE"
        lookback = int(getattr(self.config, 'ema_slope_lookback', 10) or 10)
        if atr > 0 and lookback > 0 and idx - lookback >= 0:
            prev_ema200 = df.iloc[idx - lookback].get('EMA_200')
            if prev_ema200 is not None and not pd.isna(prev_ema200):
                slope = abs(float(curr['EMA_200']) - float(prev_ema200))
                slope_threshold = atr * float(self.config.ema_slope_atr_mult) * 2.0  # Loosened from 1.0x to 2.0x ATR
                if slope < slope_threshold:
                    return "WEAK_RANGE"
        # Check for weak trend vs strong trend
        if gap > flat_threshold * 2:
            return "STRONG_TREND"
        return "TREND"
    
    def _backtest_regime_to_enum(self, regime_str: str) -> MarketRegime:
        """Map internal backtest regime labels to MarketRegime used by generate_signal / stops."""
        if regime_str in ("STRONG_TREND", "TREND"):
            return MarketRegime.TRENDING
        if regime_str == "WEAK_RANGE":
            return MarketRegime.RANGING
        return MarketRegime.TRENDING

    @staticmethod
    def _backtest_is_london_ny(timestamp) -> bool:
        """UTC hour gates aligned with mt5_system London/NY liquidity window."""
        ts = pd.Timestamp(timestamp)
        hour = int(ts.hour)
        in_london = 7 <= hour < 16
        in_ny = 12 <= hour < 21
        return bool(in_london or in_ny)

    def identify_support_resistance(self, df: pd.DataFrame) -> Tuple[pd.Series, pd.Series]:
        """Identify support and resistance levels using pivot points"""
        lookback = self.config.sr_lookback
        
        support = pd.Series(index=df.index, dtype=float)
        resistance = pd.Series(index=df.index, dtype=float)
        
        for i in range(lookback, len(df)):
            recent_lows = df['low'].iloc[i-lookback:i]
            recent_highs = df['high'].iloc[i-lookback:i]
            
            support.iloc[i] = recent_lows.quantile(0.1)
            resistance.iloc[i] = recent_highs.quantile(0.9)
        
        return support, resistance
    
    def is_bullish_engulfing(self, df: pd.DataFrame, idx: int) -> bool:
        """Check if current candle forms a bullish engulfing pattern"""
        if idx < 1:
            return False
        
        curr = df.iloc[idx]
        prev = df.iloc[idx - 1]
        
        prev_bearish = prev['close'] < prev['open']
        curr_bullish = curr['close'] > curr['open']
        engulfs = (curr['open'] <= prev['close'] and curr['close'] >= prev['open'])
        
        return prev_bearish and curr_bullish and engulfs
    
    def is_bearish_engulfing(self, df: pd.DataFrame, idx: int) -> bool:
        """Check if current candle forms a bearish engulfing pattern"""
        if idx < 1:
            return False
        
        curr = df.iloc[idx]
        prev = df.iloc[idx - 1]
        
        prev_bullish = prev['close'] > prev['open']
        curr_bearish = curr['close'] < curr['open']
        engulfs = (curr['open'] >= prev['close'] and curr['close'] <= prev['open'])
        
        return prev_bullish and curr_bearish and engulfs
    
    def check_strict_trend(self, df: pd.DataFrame, idx: int, signal: Signal) -> bool:
        """
        UPGRADED: Strict trend filter - MUST trade WITH the 200 EMA
        No more trading against the major trend!
        """
        curr = df.iloc[idx]
        
        if pd.isna(curr['EMA_200']):
            return False
        
        if pd.isna(curr.get('EMA_50')):
            return False

        atr = float(curr.get('ATR') or 0.0)
        ema_gap = abs(float(curr['EMA_50']) - float(curr['EMA_200']))
        if atr > 0 and ema_gap < (atr * float(self.config.flat_atr_mult)):
            return False

        lookback = int(getattr(self.config, 'ema_slope_lookback', 10) or 10)
        if lookback > 0 and idx - lookback >= 0:
            prev_ema200 = df.iloc[idx - lookback].get('EMA_200')
            if prev_ema200 is not None and not pd.isna(prev_ema200):
                slope = abs(float(curr['EMA_200']) - float(prev_ema200))
                if atr > 0 and slope < (atr * float(self.config.ema_slope_atr_mult)):
                    return False

        if signal == Signal.BUY:
            return (curr['EMA_50'] > curr['EMA_200']) and (curr['close'] > curr['EMA_200'])

        if signal == Signal.SELL:
            return (curr['EMA_50'] < curr['EMA_200']) and (curr['close'] < curr['EMA_200'])
        
        return False

    def check_mtf_trend(self, mtf_df: pd.DataFrame, signal: Signal) -> bool:
        """Check if signal aligns with higher timeframe trend (D1)."""
        if mtf_df is None or len(mtf_df) < 50:
            return True # Cannot confirm, so don't block
        
        # Calculate MTF EMA
        mtf_ema = mtf_df['close'].ewm(span=self.config.mtf_ema_period, adjust=False).mean()
        current_mtf_close = mtf_df['close'].iloc[-1]
        current_mtf_ema = mtf_ema.iloc[-1]
        
        if signal == Signal.BUY:
            return current_mtf_close > current_mtf_ema
        elif signal == Signal.SELL:
            return current_mtf_close < current_mtf_ema
        
        return False

    def check_min_body_size(self, df: pd.DataFrame, idx: int) -> bool:
        curr = df.iloc[idx]
        prev = df.iloc[idx - 1] if idx >= 1 else None
        if prev is None:
            return False

        curr_body = abs(float(curr['close']) - float(curr['open']))
        prev_body = abs(float(prev['close']) - float(prev['open']))
        if curr_body <= 0:
            return False

        atr = float(curr.get('ATR') or 0.0)
        if atr > 0 and curr_body < (atr * float(self.config.min_body_atr_mult)):
            return False
        if curr_body < prev_body:
            return False
        # Body must be ≥ 40 % of total range — rejects doji/spinning-top engulfings
        total_range = float(curr['high']) - float(curr['low'])
        if total_range > 0 and (curr_body / total_range) < 0.40:
            return False
        return True
    
    def check_rsi_pullback(self, df: pd.DataFrame, idx: int, signal: Signal) -> bool:
        """
        UPGRADED: RSI Pullback Filter
        
        BUY Logic: In an UPTREND, wait for RSI to dip to/below 40 (buying the dip)
        SELL Logic: In a DOWNTREND, wait for RSI to rally to/above 60 (selling the rally)
        
        This prevents buying at tops and selling at bottoms!
        """
        if not self.config.rsi_pullback_enabled:
            return True
        
        # Look back over recent candles to find pullback
        lookback = self.config.rsi_lookback_candles
        start_idx = max(0, idx - lookback)
        recent_rsi = df['RSI'].iloc[start_idx:idx+1]
        
        if recent_rsi.isnull().all():
            return False
        
        if signal == Signal.BUY:
            # BUY: We want to see RSI recently touched/went below 40 (a dip in the uptrend)
            rsi_touched_low = (recent_rsi <= self.config.rsi_pullback_buy_threshold).any()
            current_rsi = df.iloc[idx]['RSI']
            
            # Current RSI should be recovering but not overbought
            rsi_recovering = current_rsi > self.config.rsi_pullback_buy_threshold
            rsi_not_overbought = current_rsi < self.config.rsi_overbought
            
            return rsi_touched_low and rsi_recovering and rsi_not_overbought
        
        elif signal == Signal.SELL:
            # SELL: We want to see RSI recently touched/went above 60 (a rally in the downtrend)
            rsi_touched_high = (recent_rsi >= self.config.rsi_pullback_sell_threshold).any()
            current_rsi = df.iloc[idx]['RSI']
            
            # Current RSI should be pulling back but not oversold
            rsi_pulling_back = current_rsi < self.config.rsi_pullback_sell_threshold
            rsi_not_oversold = current_rsi > self.config.rsi_oversold
            
            return rsi_touched_high and rsi_pulling_back and rsi_not_oversold
        
        return False
    
    def check_rsi(self, df: pd.DataFrame, idx: int, signal: Signal) -> bool:
        """
        UPGRADED: Enhanced RSI filter
        Works in conjunction with RSI pullback filter
        """
        if not self.config.use_rsi_filter:
            return True
        
        curr = df.iloc[idx]
        
        if pd.isna(curr['RSI']):
            return True
        
        if signal == Signal.BUY:
            return float(curr['RSI']) <= float(self.config.rsi_overbought)
        if signal == Signal.SELL:
            return float(curr['RSI']) >= float(self.config.rsi_oversold)
        
        return True
    
    def check_volume(self, df: pd.DataFrame, idx: int) -> bool:
        """
        UPGRADED: Doubled volume requirement
        Only trade on heavyweight institutional moves
        """
        curr = df.iloc[idx]
        
        if pd.isna(curr['Volume_Avg']):
            return True
        
        # UPGRADED: 2.0x multiplier for conviction
        return curr['volume'] >= curr['Volume_Avg'] * self.config.volume_multiplier
    
    def is_near_support_resistance(self, df: pd.DataFrame, idx: int, signal: Signal) -> bool:
        """
        UPGRADED: Extremely tight tolerance
        Price must be "touching" the level
        """
        curr = df.iloc[idx]
        tolerance = self.config.sr_tolerance  # Now 0.001 (0.1%)
        
        if signal == Signal.BUY:
            if pd.isna(curr['Support']):
                return True
            distance = abs(curr['low'] - curr['Support']) / curr['Support']
            return distance <= tolerance
        
        elif signal == Signal.SELL:
            if pd.isna(curr['Resistance']):
                return True
            distance = abs(curr['high'] - curr['Resistance']) / curr['Resistance']
            return distance <= tolerance
        
        return True
    
    def check_macd_confluence(self, df: pd.DataFrame, idx: int, signal: Signal) -> bool:
        """MACD confluence: histogram aligned with trade direction"""
        curr = df.iloc[idx]
        if pd.isna(curr.get('MACD_Hist')):
            return False
        hist = float(curr['MACD_Hist'])
        if signal == Signal.BUY:
            return hist > 0
        return hist < 0

    def check_stochastic_confluence(self, df: pd.DataFrame, idx: int, signal: Signal) -> bool:
        """Stochastic confluence: oversold for buys, overbought for sells"""
        curr = df.iloc[idx]
        stoch_k = curr.get('Stoch_K')
        if stoch_k is None or pd.isna(stoch_k):
            return False
        if signal == Signal.BUY:
            return float(stoch_k) < 30
        return float(stoch_k) > 70

    def check_ema20_alignment(self, df: pd.DataFrame, idx: int, signal: Signal) -> bool:
        """EMA 20 alignment: price above EMA20 for buys, below for sells"""
        curr = df.iloc[idx]
        ema20 = curr.get('EMA_20')
        if ema20 is None or pd.isna(ema20):
            return False
        if signal == Signal.BUY:
            return float(curr['close']) > float(ema20)
        return float(curr['close']) < float(ema20)

    def check_structure_break(self, df: pd.DataFrame, idx: int, signal: Signal) -> bool:
        """Market structure shift: break of structure confirming direction"""
        if idx < 5:
            return False
        curr = df.iloc[idx]
        lookback = df.iloc[idx-5:idx]
        if signal == Signal.BUY:
            prev_high = lookback['high'].max()
            return float(curr['close']) > prev_high
        prev_low = lookback['low'].min()
        return float(curr['close']) < prev_low

    def check_adx_strength(self, df: pd.DataFrame, idx: int) -> bool:
        """ADX ≥ 20 confirms the market is in a directional move, not flat noise."""
        adx = df.iloc[idx].get('ADX')
        if adx is None or pd.isna(adx):
            return False
        return float(adx) >= 20.0

    def check_fvg_confluence(self, df: pd.DataFrame, idx: int, signal: Signal) -> bool:
        """Check if signal is supported by a recent Fair Value Gap."""
        lookback = 3
        start = max(0, idx - lookback)
        if signal == Signal.BUY:
            return df['FVG_Bull'].iloc[start:idx+1].any()
        return df['FVG_Bear'].iloc[start:idx+1].any()

    def check_liquidity_hunting(self, df: pd.DataFrame, idx: int, signal: Signal) -> bool:
        """
        Check for liquidity hunting signs:
        - Deep wicks without follow-through.
        - Double top/bottom near key levels.
        - Obvious stop clusters (consolidation near S/R).
        """
        if idx < 10:
            return False
            
        curr = df.iloc[idx]
        atr = curr.get('ATR', 0)
        
        # 1. Deep wick without follow-through
        if atr > 0:
            if signal == Signal.BUY:
                for i in range(idx-2, idx+1):
                    c = df.iloc[i]
                    body = abs(c['close'] - c['open'])
                    lower_wick = min(c['close'], c['open']) - c['low']
                    if lower_wick > body * 1.5 and lower_wick > atr * 0.5:
                        return True
            else:
                for i in range(idx-2, idx+1):
                    c = df.iloc[i]
                    body = abs(c['close'] - c['open'])
                    upper_wick = c['high'] - max(c['close'], c['open'])
                    if upper_wick > body * 1.5 and upper_wick > atr * 0.5:
                        return True

        # 2. Double top/bottom near key level
        lookback = df.iloc[max(0, idx-20):idx]
        if signal == Signal.BUY:
            support = curr.get('Support')
            if not pd.isna(support):
                lows = lookback[abs(lookback['low'] - support) / support < 0.002]['low']
                if len(lows) >= 1: # One previous touch + current touch
                    return True
        else:
            resistance = curr.get('Resistance')
            if not pd.isna(resistance):
                highs = lookback[abs(lookback['high'] - resistance) / resistance < 0.002]['high']
                if len(highs) >= 1:
                    return True
                    
        return False

    def calculate_trade_score(
        self, 
        df: pd.DataFrame, 
        idx: int, 
        signal: Signal, 
        conf_score: int, 
        planned_rr: float, 
        is_london_ny: bool, 
        is_volatile: bool,
        htf_trend_aligned: bool
    ) -> int:
        """Rate potential trade from 0-10"""
        score = 0
        
        # TREND ALIGNMENT (+2/-2)
        score += 2 if htf_trend_aligned else -2
        
        # KEY LEVEL (+2/+1)
        is_at_level = self.is_near_support_resistance(df, idx, signal)
        score += 2 if is_at_level else 1
        
        # CONFLUENCE COUNT (+1/+2/+3/+4) — reward stacked confirmation for selective setups
        if conf_score >= 8:
            score += 4
        elif conf_score >= 5:
            score += 3
        elif conf_score == 4:
            score += 2
        elif conf_score == 3:
            score += 1
            
        # R:R RATIO — reward achievable targets that support 70%+ win rate
        if planned_rr >= 1.5:
            score += 3
        elif planned_rr >= 0.8:
            score += 2
        elif planned_rr >= 0.28:
            score += 1
            
        # SESSION TIMING (+1/-1)
        score += 1 if is_london_ny else -1
        
        # VOLATILITY (+1/-1)
        score += -1 if is_volatile else 1
        
        return max(0, min(10, score))

    def confluence_score(self, df: pd.DataFrame, idx: int, signal: Signal, regime: MarketRegime = MarketRegime.TRENDING) -> Tuple[int, Dict[str, bool]]:
        """Calculate confluence score: count how many signals align"""
        signals = {
            "key_level":       self.is_near_support_resistance(df, idx, signal),
            "volume":          self.check_volume(df, idx),
            "momentum_rsi":    self.check_rsi(df, idx, signal),
            "candlestick":     True,  # Already confirmed engulfing before scoring
            "ema_alignment":   self.check_strict_trend(df, idx, signal) if regime == MarketRegime.TRENDING else True,
            "structure_break": self.check_structure_break(df, idx, signal),
            "macd":            self.check_macd_confluence(df, idx, signal),
            "stochastic":      self.check_stochastic_confluence(df, idx, signal),
            "ema20":           self.check_ema20_alignment(df, idx, signal) if regime == MarketRegime.TRENDING else True,
            "rsi_pullback":    self.check_rsi_pullback(df, idx, signal) if self.config.rsi_pullback_enabled else True,
            "fvg_confluence":  self.check_fvg_confluence(df, idx, signal),
            "adx_trend":       self.check_adx_strength(df, idx),   # ADX ≥ 20 edge
        }
        score = sum(1 for v in signals.values() if v)
        return score, signals

    def calculate_position_size(self, entry_price: float, stop_loss: float) -> float:
        """Calculate position size based on risk per trade"""
        effective_balance = float(self.current_capital)
        if float(getattr(self.config, "capital_allocation", 0.0) or 0.0) > 0:
            effective_balance = min(effective_balance, float(self.config.capital_allocation))

        risk_fraction = float(getattr(self.config, "risk_per_trade", 0.0) or 0.0)
        risk_fraction = min(risk_fraction, float(getattr(self.config, "max_risk_per_trade", 0.01) or 0.01))
        risk_amount = effective_balance * risk_fraction
        price_risk = abs(entry_price - stop_loss)
        
        if price_risk == 0:
            return 0
        
        position_size = risk_amount / price_risk
        return position_size
    
    def calculate_stop_take_profit(
        self,
        df: pd.DataFrame,
        idx: int,
        signal: Signal,
        entry_price: Optional[float] = None,
        regime: MarketRegime = MarketRegime.TRENDING,
    ) -> Tuple[float, float]:
        """Calculate stop loss and take profit levels"""
        curr = df.iloc[idx]
        prev = df.iloc[idx - 1]
        
        rr = float(self.config.min_risk_reward)
        atr = float(curr.get('ATR') or 0.0)
        
        # Widen stops for VOLATILE regime
        atr_mult = 0.5 if regime == MarketRegime.VOLATILE else 2.0
        atr_buf = atr * atr_mult if atr > 0 else 0.0

        if signal == Signal.BUY:
            stop_loss = float(min(curr['low'], prev['low'])) - float(atr_buf)
            ep = float(curr['close']) if entry_price is None else float(entry_price)
            risk = ep - float(stop_loss)
            take_profit = ep + (risk * rr)
            
        else:  # Signal.SELL
            stop_loss = float(max(curr['high'], prev['high'])) + float(atr_buf)
            ep = float(curr['close']) if entry_price is None else float(entry_price)
            risk = float(stop_loss) - ep
            take_profit = ep - (risk * rr)
        
        return stop_loss, take_profit
    
    def generate_signal(
        self, 
        df: pd.DataFrame, 
        idx: int, 
        regime: MarketRegime = MarketRegime.TRENDING,
        is_london_ny: bool = True,
        htf_trend_aligned: bool = True
    ) -> Tuple[Optional[Signal], int, float]:
        """
        UPGRADED: Generate signal with liquidity checks and scoring
        Returns: (Signal, score, risk_fraction_multiplier)
        """
        is_bullish = self.is_bullish_engulfing(df, idx)
        is_bearish = self.is_bearish_engulfing(df, idx)

        if not (is_bullish or is_bearish):
            return None, 0, 0.0

        if not self.check_min_body_size(df, idx):
            return None, 0, 0.0

        signal = Signal.BUY if is_bullish else Signal.SELL
        
        # NEW: Liquidity Hunting Check
        if self.check_liquidity_hunting(df, idx, signal):
            # Liquidity hunt detected: we wait for sweep and reclaim.
            # For simplicity in this bot, we treat the current signal as the 'reclaim'
            # but we could add more complex state tracking.
            pass

        score_conf, details = self.confluence_score(df, idx, signal, regime=regime)
        min_conf = int(getattr(self.config, "min_confluence_score", 3) or 3)
        if score_conf < min_conf:
            return None, 0, 0.0
        
        # Calculate RR for scoring
        sl, tp = self.calculate_stop_take_profit(df, idx, signal, regime=regime)
        entry = df.iloc[idx]['close']
        planned_rr = abs(tp - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
        
        is_volatile = (regime == MarketRegime.VOLATILE)
        
        # Calculate final trade score (0-10)
        final_score = self.calculate_trade_score(
            df, idx, signal, score_conf, planned_rr, is_london_ny, is_volatile, htf_trend_aligned
        )

        full_min = int(getattr(self.config, "min_trade_score_full_entry", 7) or 7)
        half_min = int(getattr(self.config, "min_trade_score_half_entry", 5) or 0)
        min_rr_gate = float(getattr(self.config, "min_planned_rr_execution", 0.0) or 0.0)
        if min_rr_gate > 0 and planned_rr < min_rr_gate:
            return None, final_score, 0.0

        risk_mult = 0.0
        if final_score >= full_min:
            risk_mult = 1.0
        elif half_min > 0 and final_score >= half_min:
            risk_mult = 0.5

        if risk_mult <= 0:
            return None, final_score, 0.0

        return signal, final_score, risk_mult
    
    def check_drawdown(self) -> bool:
        """Check if current drawdown exceeds maximum allowed"""
        if self.current_capital < self.peak_capital * (1 - self.config.max_drawdown):
            return True
        return False
    
    def check_trend_alignment(self, df: pd.DataFrame, idx: int, signal: Signal) -> bool:
        """Check if price is properly aligned with 200 EMA trend"""
        curr = df.iloc[idx]
        ema200 = curr.get('EMA_200')
        close_price = curr['close']
        
        if ema200 is None or pd.isna(ema200):
            return False
        
        if signal == Signal.BUY:
            # For longs, price must be above 200 EMA
            return float(close_price) > float(ema200)
        else:
            # For shorts, price must be below 200 EMA
            return float(close_price) < float(ema200)
    
    def manage_open_trades(
        self,
        df: pd.DataFrame,
        idx: int,
        value_per_price_unit: float = 0.0,
        spread: float = 0.0,
        commission_per_lot: float = 0.0,
    ):
        """Manage open trades - check for stop loss or take profit hits"""
        curr = df.iloc[idx]
        prev_close = float(df.iloc[idx - 1]['close']) if idx - 1 >= 0 else float(curr['close'])
        prev_low = float(df.iloc[idx - 1]['low']) if idx - 1 >= 0 else float(curr['low'])
        prev_high = float(df.iloc[idx - 1]['high']) if idx - 1 >= 0 else float(curr['high'])
        
        for trade in self.trades:
            if trade.status != "OPEN":
                continue

            # NEW: Smart Partial Profit Taking (Close 50% at 1R to lock in gains)
            if (not trade.partial_closed) and trade.entry_price and trade.stop_loss:
                if trade.direction == Signal.BUY:
                    target_1r = float(trade.entry_price) + (float(trade.entry_price) - float(trade.stop_loss))
                    if prev_close >= target_1r:
                        # Close 50% at 1R
                        partial_size = trade.position_size * 0.5
                        fees = float(commission_per_lot or 0.0) * float(trade.position_size or 0.0)
                        partial_pnl = trade.partial_close(partial_size, target_1r, fees)
                        self.current_capital += partial_pnl
                        
                        # Move remaining 50% to break-even + 0.1R
                        be_plus_01r = float(trade.entry_price) + 0.1 * (float(trade.entry_price) - float(trade.stop_loss))
                        trade.stop_loss = be_plus_01r
                        trade.moved_to_break_even = True
                        
                        print(f"PARTIAL CLOSE: 50% at 1R ({target_1r:.5f}) | PnL: ${partial_pnl:.2f} | SL moved to BE+0.1R")
                else:
                    target_1r = float(trade.entry_price) - (float(trade.stop_loss) - float(trade.entry_price))
                    if prev_close <= target_1r:
                        # Close 50% at 1R
                        partial_size = trade.position_size * 0.5
                        fees = float(commission_per_lot or 0.0) * float(trade.position_size or 0.0)
                        partial_pnl = trade.partial_close(partial_size, target_1r, fees)
                        self.current_capital += partial_pnl
                        
                        # Move remaining 50% to break-even + 0.1R
                        be_plus_01r = float(trade.entry_price) - 0.1 * (float(trade.stop_loss) - float(trade.entry_price))
                        trade.stop_loss = be_plus_01r
                        trade.moved_to_break_even = True
                        
                        print(f"PARTIAL CLOSE: 50% at 1R ({target_1r:.5f}) | PnL: ${partial_pnl:.2f} | SL moved to BE+0.1R")

            # NEW: Break-even at 1.0R for sooner protection
            if (not trade.moved_to_break_even) and trade.entry_price and trade.stop_loss:
                if trade.direction == Signal.BUY:
                    target_1r = float(trade.entry_price) + (float(trade.entry_price) - float(trade.stop_loss))
                    if prev_close >= target_1r:
                        trade.stop_loss = float(trade.entry_price)
                        trade.moved_to_break_even = True
                        print(f"BREAK-EVEN: Moved to BE at 1.0R ({target_1r:.5f})")
                else:
                    target_1r = float(trade.entry_price) - (float(trade.stop_loss) - float(trade.entry_price))
                    if prev_close <= target_1r:
                        trade.stop_loss = float(trade.entry_price)
                        trade.moved_to_break_even = True
                        print(f"BREAK-EVEN: Moved to BE at 1.0R ({target_1r:.5f})")

            # NEW: Stepped trailing at 1.5R - move SL to 0.5R
            if trade.moved_to_break_even and trade.entry_price and trade.stop_loss:
                if trade.direction == Signal.BUY:
                    target_1r5 = float(trade.entry_price) + 1.5 * (float(trade.entry_price) - float(trade.stop_loss))
                    target_0r5 = float(trade.entry_price) + 0.5 * (float(trade.entry_price) - float(trade.stop_loss))
                    if prev_close >= target_1r5 and float(trade.stop_loss) < target_0r5:
                        trade.stop_loss = target_0r5
                        print(f"TRAILING: Moved SL to 0.5R ({target_0r5:.5f}) at 1.5R")
                else:
                    target_1r5 = float(trade.entry_price) - 1.5 * (float(trade.stop_loss) - float(trade.entry_price))
                    target_0r5 = float(trade.entry_price) - 0.5 * (float(trade.stop_loss) - float(trade.entry_price))
                    if prev_close <= target_1r5 and float(trade.stop_loss) > target_0r5:
                        trade.stop_loss = target_0r5
                        print(f"TRAILING: Moved SL to 0.5R ({target_0r5:.5f}) at 1.5R")

            if trade.moved_to_break_even:
                if trade.direction == Signal.BUY:
                    # Original BE logic
                    desired_sl = max(float(trade.stop_loss), float(prev_low), float(trade.entry_price))
                    
                    # NEW: ATR-based trailing (if enabled)
                    if self.config.use_atr_trailing:
                        atr_sl = float(curr['close']) - (float(curr['ATR']) * self.config.atr_trailing_mult)
                        desired_sl = max(desired_sl, atr_sl)
                    
                    if desired_sl > float(trade.stop_loss):
                        trade.stop_loss = float(desired_sl)
                else:
                    current_sl = float(trade.stop_loss or 0.0)
                    # Original BE logic
                    desired_sl = min(
                        float(current_sl) if current_sl > 0 else float(prev_high),
                        float(prev_high),
                        float(trade.entry_price),
                    )
                    
                    # NEW: ATR-based trailing (if enabled)
                    if self.config.use_atr_trailing:
                        atr_sl = float(curr['close']) + (float(curr['ATR']) * self.config.atr_trailing_mult)
                        if current_sl <= 0 or atr_sl < desired_sl:
                            desired_sl = atr_sl
                    
                    if current_sl <= 0 or desired_sl < current_sl:
                        trade.stop_loss = float(desired_sl)
            
            if trade.direction == Signal.BUY:
                if curr['low'] <= trade.stop_loss:
                    # Full position exit (no partial closes)
                    fees = float(commission_per_lot or 0.0) * float(trade.position_size or 0.0)
                    exit_price = float(trade.stop_loss)
                    r_mult = 0.0
                    if value_per_price_unit > 0:
                        initial_sl = float(getattr(trade, "initial_stop_loss", 0.0) or 0.0) or float(trade.stop_loss)
                        initial_risk = abs(float(trade.entry_price) - float(initial_sl)) * float(trade.position_size) * float(value_per_price_unit)
                        pnl = (exit_price - float(trade.entry_price)) * float(trade.position_size) * float(value_per_price_unit) - fees
                        r_mult = (pnl / initial_risk) if initial_risk > 0 else 0.0
                        trade.close_trade(
                            curr.name,
                            exit_price,
                            fees=fees,
                            r_multiple=r_mult,
                            profit_loss_override=float(pnl),
                        )
                    else:
                        trade.close_trade(curr.name, exit_price, fees=fees, r_multiple=r_mult)
                    self.current_capital += trade.profit_loss
                elif curr['high'] >= trade.take_profit:
                    # Full position exit (no partial closes)
                    fees = float(commission_per_lot or 0.0) * float(trade.position_size or 0.0)
                    exit_price = float(trade.take_profit)
                    r_mult = 0.0
                    if value_per_price_unit > 0:
                        initial_sl = float(getattr(trade, "initial_stop_loss", 0.0) or 0.0) or float(trade.stop_loss)
                        initial_risk = abs(float(trade.entry_price) - float(initial_sl)) * float(trade.position_size) * float(value_per_price_unit)
                        pnl = (exit_price - float(trade.entry_price)) * float(trade.position_size) * float(value_per_price_unit) - fees
                        r_mult = (pnl / initial_risk) if initial_risk > 0 else 0.0
                        trade.close_trade(
                            curr.name,
                            exit_price,
                            fees=fees,
                            r_multiple=r_mult,
                            profit_loss_override=float(pnl),
                        )
                    else:
                        trade.close_trade(curr.name, exit_price, fees=fees, r_multiple=r_mult)
                    self.current_capital += trade.profit_loss
                    
            else:  # Signal.SELL
                sl_trigger = float(trade.stop_loss) - float(spread)
                tp_trigger = float(trade.take_profit) - float(spread)
                if curr['high'] >= sl_trigger:
                    # Full position exit (no partial closes)
                    fees = float(commission_per_lot or 0.0) * float(trade.position_size or 0.0)
                    exit_price = float(trade.stop_loss)
                    r_mult = 0.0
                    if value_per_price_unit > 0:
                        initial_sl = float(getattr(trade, "initial_stop_loss", 0.0) or 0.0) or float(trade.stop_loss)
                        initial_risk = abs(float(trade.entry_price) - float(initial_sl)) * float(trade.position_size) * float(value_per_price_unit)
                        pnl = (float(trade.entry_price) - exit_price) * float(trade.position_size) * float(value_per_price_unit) - fees
                        r_mult = (pnl / initial_risk) if initial_risk > 0 else 0.0
                        trade.close_trade(
                            curr.name,
                            exit_price,
                            fees=fees,
                            r_multiple=r_mult,
                            profit_loss_override=float(pnl),
                        )
                    else:
                        trade.close_trade(curr.name, exit_price, fees=fees, r_multiple=r_mult)
                    self.current_capital += trade.profit_loss
                elif curr['low'] <= tp_trigger:
                    # Full position exit (no partial closes)
                    fees = float(commission_per_lot or 0.0) * float(trade.position_size or 0.0)
                    exit_price = float(trade.take_profit)
                    r_mult = 0.0
                    if value_per_price_unit > 0:
                        initial_sl = float(getattr(trade, "initial_stop_loss", 0.0) or 0.0) or float(trade.stop_loss)
                        initial_risk = abs(float(trade.entry_price) - float(initial_sl)) * float(trade.position_size) * float(value_per_price_unit)
                        pnl = (float(trade.entry_price) - exit_price) * float(trade.position_size) * float(value_per_price_unit) - fees
                        r_mult = (pnl / initial_risk) if initial_risk > 0 else 0.0
                        trade.close_trade(
                            curr.name,
                            exit_price,
                            fees=fees,
                            r_multiple=r_mult,
                            profit_loss_override=float(pnl),
                        )
                    else:
                        trade.close_trade(curr.name, exit_price, fees=fees, r_multiple=r_mult)
                    self.current_capital += trade.profit_loss
            
            if self.current_capital > self.peak_capital:
                self.peak_capital = self.current_capital
    
    def backtest(
        self,
        df: pd.DataFrame,
        value_per_price_unit: float = 0.0,
        point: float = 0.0,
        spread_points: float = 0.0,
        commission_per_lot: float = 0.0,
    ) -> Dict:
        """Run backtest on historical data"""
        print(f"Starting backtest - Strategy: {self.config.strategy_mode}")
        print(f"Initial Capital: ${self.config.initial_capital:,.2f}")
        print(f"Timeframe: {self.config.timeframe.value}")
        print(f"Volume Requirement: {self.config.volume_multiplier}x average")
        print(f"S/R Tolerance: {self.config.sr_tolerance*100}%")
        print("-" * 60)
        
        df = self.calculate_indicators(df)
        start_idx = max(self.config.ema_slow, self.config.sr_lookback) + 1
        
        spread = float(spread_points or 0.0) * float(point or 0.0)
        # Log regime on every candle for analysis
        regime_log = []
        for idx in range(start_idx, len(df)):
            current_regime = self._classify_regime(df, idx)
            regime_log.append((df.iloc[idx].name, current_regime))
            self.manage_open_trades(
                df,
                idx,
                value_per_price_unit=float(value_per_price_unit or 0.0),
                spread=float(spread),
                commission_per_lot=float(commission_per_lot or 0.0),
            )
            
            if self.check_drawdown():
                print(f"\n⚠️  Maximum drawdown exceeded at {df.iloc[idx].name}")
                break

            if bool(getattr(self.config, "skip_weak_range_entries", False)) and current_regime == "WEAK_RANGE":
                self.equity_curve.append(self.current_capital)
                continue

            mr = self._backtest_regime_to_enum(current_regime)
            ts = df.index[idx]
            is_ln = self._backtest_is_london_ny(ts)

            is_bull = self.is_bullish_engulfing(df, idx)
            is_bear = self.is_bearish_engulfing(df, idx)
            if not (is_bull or is_bear):
                self.equity_curve.append(self.current_capital)
                continue

            provisional = Signal.BUY if is_bull else Signal.SELL
            htf_ok = self.check_strict_trend(df, idx, provisional)

            sig, trade_score, risk_mult = self.generate_signal(
                df, idx, regime=mr, is_london_ny=is_ln, htf_trend_aligned=htf_ok
            )

            if sig not in (Signal.BUY, Signal.SELL) or float(risk_mult or 0.0) <= 0:
                self.equity_curve.append(self.current_capital)
                continue

            if any(t.status == "OPEN" for t in self.trades):
                self.equity_curve.append(self.current_capital)
                continue

            signal = sig
            mid_entry = float(df.iloc[idx]["close"])
            entry_price = float(mid_entry + spread) if signal == Signal.BUY else float(mid_entry)
            stop_loss, take_profit = self.calculate_stop_take_profit(
                df, idx, signal, entry_price=entry_price, regime=mr
            )
            planned_rr = 0.0
            try:
                denom = abs(float(entry_price) - float(stop_loss))
                planned_rr = (
                    abs(float(take_profit) - float(entry_price)) / denom if denom > 0 else 0.0
                )
            except Exception:
                planned_rr = 0.0

            position_size = 0.0
            rm = float(risk_mult or 0.0)
            if float(value_per_price_unit or 0.0) > 0:
                effective_balance = float(self.current_capital)
                if float(getattr(self.config, "capital_allocation", 0.0) or 0.0) > 0:
                    effective_balance = min(effective_balance, float(self.config.capital_allocation))
                risk_fraction = float(getattr(self.config, "risk_per_trade", 0.0) or 0.0)
                risk_fraction = min(risk_fraction, float(getattr(self.config, "max_risk_per_trade", 0.01) or 0.01))
                risk_fraction *= rm
                risk_money = effective_balance * risk_fraction
                price_risk = abs(float(entry_price) - float(stop_loss))
                risk_per_lot = price_risk * float(value_per_price_unit)
                position_size = (risk_money / risk_per_lot) if risk_per_lot > 0 else 0.0
            else:
                position_size = self.calculate_position_size(entry_price, stop_loss) * rm

            if position_size > 0:
                trade = Trade(
                    entry_time=df.iloc[idx].name,
                    entry_price=entry_price,
                    direction=signal,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    position_size=position_size,
                    rsi_at_entry=df.iloc[idx]["RSI"],
                    planned_rr=float(planned_rr),
                    regime=str(current_regime),
                    initial_stop_loss=float(stop_loss),
                )
                self.trades.append(trade)

                print(
                    f"{'BUY' if signal == Signal.BUY else 'SELL'} @ {entry_price:.2f} "
                    f"| score={trade_score} rm={rm:.2f} | RSI: {trade.rsi_at_entry:.1f} | SL: {stop_loss:.2f} | TP: {take_profit:.2f}"
                )

            self.equity_curve.append(self.current_capital)
        
        # Close any remaining open trades
        for trade in self.trades:
            if trade.status == "OPEN":
                last_bid = float(df.iloc[-1]['close'])
                exit_price = float(last_bid) if trade.direction == Signal.BUY else float(last_bid + spread)
                fees = float(commission_per_lot or 0.0) * float(trade.position_size or 0.0)
                r_mult = 0.0
                if float(value_per_price_unit or 0.0) > 0:
                    initial_sl = float(getattr(trade, "initial_stop_loss", 0.0) or 0.0) or float(trade.stop_loss)
                    initial_risk = abs(float(trade.entry_price) - float(initial_sl)) * float(trade.position_size) * float(value_per_price_unit)
                    if trade.direction == Signal.BUY:
                        pnl = (exit_price - float(trade.entry_price)) * float(trade.position_size) * float(value_per_price_unit) - fees
                    else:
                        pnl = (float(trade.entry_price) - exit_price) * float(trade.position_size) * float(value_per_price_unit) - fees
                    r_mult = (pnl / initial_risk) if initial_risk > 0 else 0.0
                    trade.close_trade(df.iloc[-1].name, exit_price, fees=fees, r_multiple=r_mult, profit_loss_override=float(pnl))
                else:
                    trade.close_trade(df.iloc[-1].name, exit_price, fees=fees, r_multiple=r_mult)
                self.current_capital += trade.profit_loss
        
        return self.calculate_statistics()
    
    def calculate_statistics(self) -> Dict:
        """Calculate comprehensive trading statistics"""
        closed_trades = [t for t in self.trades if t.status == "CLOSED"]
        
        if not closed_trades:
            return {
                'total_trades': 0,
                'winning_trades': 0,
                'losing_trades': 0,
                'win_rate': 0,
                'total_profit': 0,
                'total_loss': 0,
                'net_profit': 0,
                'profit_factor': 0,
                'average_win': 0,
                'average_loss': 0,
                'expectancy': 0,
                'max_drawdown': 0,
                'final_capital': self.current_capital,
                'return_pct': 0,
                'avg_rsi_at_entry': 0,
                'avg_planned_rr': 0,
                'avg_r_multiple': 0,
                'breakeven_pct': 0,
                'monthly': {},
                'monthly_profitable_pct': 0,
                'regime_breakdown': {},
            }
        
        wins = [t for t in closed_trades if t.profit_loss > 0]
        losses = [t for t in closed_trades if t.profit_loss <= 0]
        
        total_profit = sum(t.profit_loss for t in wins) if wins else 0
        total_loss = abs(sum(t.profit_loss for t in losses)) if losses else 0
        
        if total_loss > 0:
            profit_factor = total_profit / total_loss
        elif total_profit > 0:
            profit_factor = 999.99
        else:
            profit_factor = 0.0
        
        avg_win = total_profit / len(wins) if wins else 0
        avg_loss = total_loss / len(losses) if losses else 0
        
        win_rate = len(wins) / len(closed_trades) if closed_trades else 0
        expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

        avg_planned_rr = sum(float(getattr(t, "planned_rr", 0.0) or 0.0) for t in closed_trades) / len(closed_trades)
        avg_r_multiple = sum(float(getattr(t, "r_multiple", 0.0) or 0.0) for t in closed_trades) / len(closed_trades)
        breakeven_pct = (len([t for t in closed_trades if abs(float(getattr(t, "r_multiple", 0.0) or 0.0)) < 1e-9]) / len(closed_trades)) * 100.0

        monthly: Dict[str, float] = {}
        for t in closed_trades:
            dt = t.exit_time or t.entry_time
            key = dt.strftime("%Y-%m") if hasattr(dt, "strftime") else str(dt)[:7]
            monthly[key] = float(monthly.get(key, 0.0)) + float(t.profit_loss)
        profitable_months = len([v for v in monthly.values() if float(v) > 0])
        monthly_profitable_pct = (profitable_months / len(monthly) * 100.0) if monthly else 0.0

        def _regime_bucket(reg: str) -> str:
            r = str(reg or "").strip().upper()
            if r in ("STRONG_TREND", "TREND"):
                return "TREND"
            if r in ("WEAK_RANGE", "RANGE", "RANGING"):
                return "RANGE"
            return "UNKNOWN"

        regime_breakdown: Dict[str, dict] = {}
        for regime in ("TREND", "RANGE", "UNKNOWN"):
            subset = [t for t in closed_trades if _regime_bucket(str(getattr(t, "regime", "") or "")) == regime]
            if not subset:
                continue
            w = [t for t in subset if t.profit_loss > 0]
            l = [t for t in subset if t.profit_loss <= 0]
            tp = sum(t.profit_loss for t in w) if w else 0.0
            tl = abs(sum(t.profit_loss for t in l)) if l else 0.0
            if tl > 0:
                pf = tp / tl
            elif tp > 0:
                pf = 999.99
            else:
                pf = 0.0
            wr = (len(w) / len(subset) * 100.0) if subset else 0.0
            regime_breakdown[regime] = {
                "trades": len(subset),
                "win_rate": wr,
                "profit_factor": pf,
                "net_profit": sum(t.profit_loss for t in subset),
                "avg_r_multiple": sum(float(getattr(t, "r_multiple", 0.0) or 0.0) for t in subset) / len(subset),
            }
        
        # Calculate maximum drawdown
        peak = self.config.initial_capital
        max_dd = 0
        for equity in self.equity_curve:
            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
        
        net_profit = self.current_capital - self.config.initial_capital
        return_pct = (net_profit / self.config.initial_capital) * 100
        
        # Average RSI at entry
        avg_rsi = sum(t.rsi_at_entry for t in closed_trades) / len(closed_trades)
        
        return {
            'total_trades': len(closed_trades),
            'winning_trades': len(wins),
            'losing_trades': len(losses),
            'win_rate': win_rate * 100,
            'total_profit': total_profit,
            'total_loss': total_loss,
            'net_profit': net_profit,
            'profit_factor': profit_factor,
            'average_win': avg_win,
            'average_loss': avg_loss,
            'expectancy': expectancy,
            'max_drawdown': max_dd * 100,
            'final_capital': self.current_capital,
            'return_pct': return_pct,
            'avg_rsi_at_entry': avg_rsi,
            'avg_planned_rr': float(avg_planned_rr),
            'avg_r_multiple': float(avg_r_multiple),
            'breakeven_pct': float(breakeven_pct),
            'monthly': monthly,
            'monthly_profitable_pct': float(monthly_profitable_pct),
            'regime_breakdown': regime_breakdown,
        }
    
    def print_statistics(self, stats: Dict):
        """Print formatted statistics"""
        print("\n" + "=" * 60)
        print(f"BACKTEST RESULTS - {self.config.strategy_mode}")
        print("=" * 60)
        
        print(f"\n📊 TRADE STATISTICS")
        print(f"Total Trades:      {stats['total_trades']}")
        print(f"Winning Trades:    {stats['winning_trades']}")
        print(f"Losing Trades:     {stats['losing_trades']}")
        print(f"Win Rate:          {stats['win_rate']:.2f}%")
        print(f"Avg RSI at Entry:  {stats['avg_rsi_at_entry']:.1f}")
        
        print(f"\n💰 PROFIT/LOSS")
        print(f"Total Profit:      ${stats['total_profit']:,.2f}")
        print(f"Total Loss:        ${stats['total_loss']:,.2f}")
        print(f"Net Profit:        ${stats['net_profit']:,.2f}")
        print(f"Return:            {stats['return_pct']:.2f}%")
        
        print(f"\n📈 PERFORMANCE METRICS")
        print(f"Profit Factor:     {stats['profit_factor']:.2f}")
        print(f"Average Win:       ${stats['average_win']:,.2f}")
        print(f"Average Loss:      ${stats['average_loss']:,.2f}")
        print(f"Expectancy:        ${stats['expectancy']:,.2f}")
        print(f"Max Drawdown:      {stats['max_drawdown']:.2f}%")
        
        print(f"\n💼 CAPITAL")
        print(f"Initial Capital:   ${self.config.initial_capital:,.2f}")
        print(f"Final Capital:     ${stats['final_capital']:,.2f}")
        
        print("\n" + "=" * 60)
    
    def export_trades(self, filename: str = "trade_log_v2.csv"):
        """Export trade log to CSV"""
        if not self.trades:
            print("No trades to export")
            return
        
        trade_data = []
        for trade in self.trades:
            trade_data.append({
                'Entry Time': trade.entry_time,
                'Entry Price': trade.entry_price,
                'Direction': 'BUY' if trade.direction == Signal.BUY else 'SELL',
                'RSI at Entry': trade.rsi_at_entry,
                'Stop Loss': trade.stop_loss,
                'Take Profit': trade.take_profit,
                'Position Size': trade.position_size,
                'Exit Time': trade.exit_time,
                'Exit Price': trade.exit_price,
                'Profit/Loss': trade.profit_loss,
                'Status': trade.status
            })
        
        df_trades = pd.DataFrame(trade_data)
        df_trades.to_csv(filename, index=False)
        print(f"\n✅ Trade log exported to {filename}")


def generate_sample_data(days: int = 365, timeframe: TimeFrame = TimeFrame.H1) -> pd.DataFrame:
    """Generate realistic sample OHLCV data for backtesting"""
    print(f"Generating {days} days of sample data ({timeframe.value} timeframe)...")
    
    candles_per_day = {
        TimeFrame.M5: 288,
        TimeFrame.M15: 96,
        TimeFrame.H1: 24,
        TimeFrame.H4: 6
    }
    
    num_candles = days * candles_per_day[timeframe]
    
    freq_map = {
        TimeFrame.M5: '5min',
        TimeFrame.M15: '15min',
        TimeFrame.H1: '1h',
        TimeFrame.H4: '4h'
    }
    
    dates = pd.date_range(end=datetime.now(), periods=num_candles, freq=freq_map[timeframe])
    
    np.random.seed(42)
    price = 100.0
    prices = [price]
    
    trend = 0.0001
    volatility = 0.015
    
    for i in range(1, num_candles):
        change = trend + np.random.normal(0, volatility)
        if i % 100 == 0:
            change += (100 - price) * 0.001
        price = price * (1 + change)
        prices.append(price)
    
    close = np.array(prices)
    high = close * (1 + np.abs(np.random.normal(0, 0.005, num_candles)))
    low = close * (1 - np.abs(np.random.normal(0, 0.005, num_candles)))
    open_prices = np.roll(close, 1)
    open_prices[0] = close[0]
    open_prices = open_prices * (1 + np.random.normal(0, 0.001, num_candles))
    
    high = np.maximum(high, np.maximum(open_prices, close))
    low = np.minimum(low, np.minimum(open_prices, close))
    
    price_range = high - low
    volume = np.random.lognormal(10, 1, num_candles) * (1 + price_range / close * 10)
    
    df = pd.DataFrame({
        'open': open_prices,
        'high': high,
        'low': low,
        'close': close,
        'volume': volume
    }, index=dates)
    
    return df


if __name__ == "__main__":
    print("="*60)
    print("ENGULFING BOT V2.0 - RSI-PULLBACK STRATEGY")
    print("="*60)
    print("\nUpgraded with High-Probability filters:")
    print("✓ RSI Pullback Logic (buy dips, sell rallies)")
    print("✓ Strict Trend Filter (200 EMA alignment)")
    print("✓ 2.0x Volume Conviction Filter")
    print("✓ 0.1% S/R Tolerance\n")
