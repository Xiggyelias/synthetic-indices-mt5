from enum import Enum
import pandas as pd
import numpy as np
import logging
from typing import Dict, Any

class MarketRegime(Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"
    NEWS_MODE = "NEWS_MODE"
    UNKNOWN = "UNKNOWN"


def compute_kaufman_efficiency_ratio(series: pd.Series, period: int = 10) -> float:
    """
    Kaufman Efficiency Ratio (KER):
    KER = |Price_t - Price_{t-n}| / Sum(|Price_i - Price_{i-1}|) for i in 1..n
    Ranges from 0.0 (pure Brownian noise/chop) to 1.0 (pure unidirectional trend).
    """
    if len(series) < period + 1:
        return 0.5
    net_change = abs(float(series.iloc[-1]) - float(series.iloc[-period - 1]))
    sum_abs_changes = float(series.diff().abs().iloc[-period:].sum())
    if sum_abs_changes <= 0:
        return 0.0
    return float(net_change / sum_abs_changes)


def compute_choppiness_index(df: pd.DataFrame, period: int = 14) -> float:
    """
    Choppiness Index (CHOP):
    CHOP = 100 * LOG10(Sum(TrueRange, period) / (MaxHigh(period) - MinLow(period))) / LOG10(period)
    Values > 61.8 indicate consolidating chop; values < 38.2 indicate strong directional trend.
    """
    if df is None or len(df) < period + 1:
        return 50.0

    high = df['high'].iloc[-period:]
    low = df['low'].iloc[-period:]
    prev_close = df['close'].iloc[-period - 1:-1]

    tr1 = high.values - low.values
    tr2 = np.abs(high.values - prev_close.values)
    tr3 = np.abs(low.values - prev_close.values)
    true_range = np.maximum(tr1, np.maximum(tr2, tr3))
    sum_tr = float(np.sum(true_range))

    max_high = float(high.max())
    min_low = float(low.min())
    diff = max_high - min_low
    if diff <= 0 or sum_tr <= 0:
        return 50.0

    chop = 100.0 * (np.log10(sum_tr / diff) / np.log10(period))
    return float(np.clip(chop, 0.0, 100.0))


class RegimeClassifier:
    def __init__(
        self,
        adx_trend: float = 25.0,
        adx_range: float = 20.0,
        atr_multiplier: float = 1.5,
        chop_threshold: float = 61.8,
        ker_trend_threshold: float = 0.35,
        ker_chop_threshold: float = 0.20,
    ):
        self.adx_trend = adx_trend
        self.adx_range = adx_range
        self.atr_multiplier = atr_multiplier
        self.chop_threshold = chop_threshold
        self.ker_trend_threshold = ker_trend_threshold
        self.ker_chop_threshold = ker_chop_threshold
        self.logger = logging.getLogger("regime-classifier")

    def get_regime_metrics(self, df: pd.DataFrame, news_blocked: bool = False) -> Dict[str, Any]:
        """Compute all underlying metrics for regime diagnostics."""
        if df is None or len(df) < 15:
            return {
                "regime": MarketRegime.UNKNOWN.value,
                "ker": 0.5,
                "chop": 50.0,
                "adx": 0.0,
                "atr_ratio": 1.0,
            }

        curr = df.iloc[-1]
        ker = compute_kaufman_efficiency_ratio(df['close'], period=10)
        chop = compute_choppiness_index(df, period=14)
        adx = float(curr.get('ADX') or 0.0) if not pd.isna(curr.get('ADX')) else 0.0

        atr = float(curr.get('ATR') or 0.0) if not pd.isna(curr.get('ATR')) else 0.0
        atr_avg = float(df['ATR'].iloc[-15:-1].mean()) if 'ATR' in df.columns and len(df) >= 15 else 0.0
        atr_ratio = (atr / atr_avg) if (atr_avg > 0 and atr > 0) else 1.0

        regime = self.classify(df, news_blocked)
        return {
            "regime": regime.value,
            "ker": round(ker, 3),
            "chop": round(chop, 1),
            "adx": round(adx, 1),
            "atr_ratio": round(atr_ratio, 2),
            "is_efficient": ker >= self.ker_trend_threshold,
            "is_choppy": chop >= self.chop_threshold or ker < self.ker_chop_threshold,
        }

    def classify(self, df: pd.DataFrame, news_blocked: bool) -> MarketRegime:
        """
        Multi-factor institutional market regime classifier:
        - NEWS_MODE: High impact news block active
        - VOLATILE: ATR spike > atr_multiplier * recent average
        - RANGING: Choppiness Index > 61.8, KER < 0.20, or ADX < 20
        - TRENDING: KER >= 0.35, Choppiness Index < 38.2, or ADX > 25
        """
        if news_blocked:
            return MarketRegime.NEWS_MODE

        if df is None or len(df) < 30:
            return MarketRegime.UNKNOWN

        curr = df.iloc[-1]

        # 1. VOLATILE check (ATR spike)
        if 'ATR' in df.columns:
            atr = curr.get('ATR')
            if not pd.isna(atr):
                atr_avg = df['ATR'].iloc[-15:-1].mean()
                if not pd.isna(atr_avg) and atr_avg > 0:
                    if float(atr) > float(atr_avg) * self.atr_multiplier:
                        return MarketRegime.VOLATILE

        # 2. Advanced Multi-factor metrics
        ker = compute_kaufman_efficiency_ratio(df['close'], period=10)
        chop = compute_choppiness_index(df, period=14)
        adx = curr.get('ADX')
        has_adx = adx is not None and not pd.isna(adx)

        # 3. RANGING check (High chop or low efficiency)
        if chop >= self.chop_threshold or ker < self.ker_chop_threshold:
            return MarketRegime.RANGING

        if has_adx and float(adx) < self.adx_range:
            return MarketRegime.RANGING

        # 4. TRENDING check (High efficiency or low chop or ADX > 25)
        if ker >= self.ker_trend_threshold or chop <= 45.0:
            return MarketRegime.TRENDING

        if has_adx and float(adx) > self.adx_trend:
            return MarketRegime.TRENDING

        return MarketRegime.TRENDING
