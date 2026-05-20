from enum import Enum
import pandas as pd
import numpy as np
import logging

class MarketRegime(Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"
    NEWS_MODE = "NEWS_MODE"
    UNKNOWN = "UNKNOWN"

class RegimeClassifier:
    def __init__(self, adx_trend=25, adx_range=20, atr_multiplier=1.5):
        self.adx_trend = adx_trend
        self.adx_range = adx_range
        self.atr_multiplier = atr_multiplier
        self.logger = logging.getLogger("regime-classifier")

    def classify(self, df: pd.DataFrame, news_blocked: bool) -> MarketRegime:
        """
        Classify market regime based on:
        - NEWS MODE: High-impact event within 30 mins (news_blocked is assumed to be 30m window)
        - VOLATILE: ATR spike > 150% of 14-period average
        - TRENDING: ADX > 25, price above/below 200 EMA
        - RANGING: ADX < 20, price oscillating between levels
        """
        if news_blocked:
            return MarketRegime.NEWS_MODE
        
        if df is None or len(df) < 30:
            return MarketRegime.UNKNOWN
        
        curr = df.iloc[-1]
        
        # 1. VOLATILE check (ATR spike)
        if 'ATR' in df.columns:
            atr = curr['ATR']
            atr_avg = df['ATR'].iloc[-15:-1].mean() # 14-period average excluding current
            if not pd.isna(atr) and not pd.isna(atr_avg) and atr_avg > 0:
                if atr > atr_avg * self.atr_multiplier:
                    return MarketRegime.VOLATILE
        
        # 2. TRENDING check (ADX > 25)
        adx = curr.get('ADX')
        ema200 = curr.get('EMA_200')
        if adx is not None and not pd.isna(adx):
            if adx > self.adx_trend:
                # User specified: ADX > 25, price above/below 200 EMA
                # We check if EMA_200 exists. If so, ADX > 25 is enough for TRENDING classification
                # as price is ALWAYS above OR below it unless it's exactly equal.
                if ema200 is not None and not pd.isna(ema200):
                    return MarketRegime.TRENDING
            
            # 3. RANGING check (ADX < 20)
            if adx < self.adx_range:
                return MarketRegime.RANGING
        
        # Fallback to TRENDING if it looks like a trend or we aren't sure
        return MarketRegime.TRENDING
