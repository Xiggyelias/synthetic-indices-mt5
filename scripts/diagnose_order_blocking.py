#!/usr/bin/env python3
"""
Diagnostic script to investigate why orders are being blocked.
Run this to get detailed information about:
1. Current news events in the economic calendar
2. Which events are blocking trades
3. Time until events expire
4. System configuration
"""

import sys
import os
from datetime import datetime, timezone, timedelta

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from news_filter import NewsFilter, build_news_config_from_env
import logging

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)

def diagnose_news_events():
    """Fetch and display all active news events blocking trades."""
    print("\n" + "="*90)
    print("🔍 ORDER BLOCKING DIAGNOSIS - News Filter Analysis")
    print("="*90)
    
    # Build config from environment
    news_config = build_news_config_from_env(os.getenv)
    
    print("\n📋 NEWS FILTER CONFIGURATION:")
    print(f"  Enabled: {news_config.enabled}")
    print(f"  Avoid Minutes: {news_config.avoid_minutes}")
    print(f"  Impact Levels: {news_config.impact_levels}")
    print(f"  Cache TTL: {news_config.cache_ttl_seconds}s")
    print(f"  URLs: {news_config.urls}")
    
    # Create filter instance
    news_filter = NewsFilter(news_config)
    
    # Fetch events
    print("\n📡 Fetching economic calendar events...")
    events = news_filter._fetch()
    
    print(f"✅ Fetched {len(events)} total events from {news_filter._cache_source}")
    
    if not events:
        print("⚠️  No events fetched! Check network connection and URLs.")
        return
    
    # Analyze events
    now = datetime.now(timezone.utc)
    window = timedelta(minutes=int(news_config.avoid_minutes))
    
    print(f"\n⏰ Current Time (UTC): {now.isoformat()}")
    print(f"⏰ Avoid Window: ±{news_config.avoid_minutes} minutes")
    print(f"⏰ Active Window: {(now - window).isoformat()} to {(now + window).isoformat()}")
    
    blocking_events = []
    upcoming_events = []
    past_events = []
    
    print(f"\n📊 Analyzing {len(events)} events...")
    for idx, ev in enumerate(events):
        impact = news_filter._normalize_impact(str(ev.get("impact", "") or ""))
        
        # Skip if not in configured impact levels
        if impact and impact not in news_config.impact_levels:
            continue
        
        title = str(ev.get("title", "") or ev.get("event", "") or "?")
        currency = str(ev.get("country", "") or ev.get("currency", "") or "").upper()
        ev_time = news_filter._parse_event_time(ev)
        
        if ev_time is None:
            continue
        
        time_diff = abs(ev_time - now)
        mins_from_now = (ev_time - now).total_seconds() / 60.0
        
        # Categorize
        if time_diff <= window:
            blocking_events.append({
                'title': title,
                'currency': currency,
                'impact': impact,
                'time': ev_time,
                'time_diff': time_diff,
                'mins_from_now': mins_from_now,
            })
        elif ev_time > now:
            upcoming_events.append({
                'title': title,
                'currency': currency,
                'impact': impact,
                'time': ev_time,
                'mins_from_now': mins_from_now,
            })
        else:
            past_events.append({
                'title': title,
                'currency': currency,
                'impact': impact,
                'time': ev_time,
                'mins_ago': -mins_from_now,
            })
    
    # Report blocking events
    print("\n" + "="*90)
    print(f"🚫 BLOCKING EVENTS (within ±{news_config.avoid_minutes} mins): {len(blocking_events)}")
    print("="*90)
    
    if blocking_events:
        for ev in sorted(blocking_events, key=lambda x: x['mins_from_now']):
            status = "🔴 CURRENTLY BLOCKING" if -5 <= ev['mins_from_now'] <= 120 else "🟡 ABOUT TO BLOCK"
            print(f"\n{status}")
            print(f"  Event: {ev['title']}")
            print(f"  Currency: {ev['currency']}")
            print(f"  Impact: {ev['impact']}")
            print(f"  Time: {ev['time'].isoformat()}")
            print(f"  Status: {ev['mins_from_now']:+.1f} mins from now")
            remaining = (window - ev['time_diff']).total_seconds() / 60.0
            print(f"  Remaining Block Time: {remaining:.1f} minutes")
    else:
        print("✅ No events currently blocking trades!")
    
    # Report upcoming events
    print("\n" + "="*90)
    print(f"📅 UPCOMING EVENTS (next {news_config.avoid_minutes} mins): {len(upcoming_events)}")
    print("="*90)
    
    for ev in sorted(upcoming_events, key=lambda x: x['mins_from_now'])[:5]:
        print(f"\n  Event: {ev['title']}")
        print(f"  Currency: {ev['currency']}")
        print(f"  Impact: {ev['impact']}")
        print(f"  In: {ev['mins_from_now']:.1f} minutes")
    
    # Report recently passed events
    print("\n" + "="*90)
    print(f"✅ RECENTLY PASSED (last {news_config.avoid_minutes} mins): {len(past_events)}")
    print("="*90)
    
    for ev in sorted(past_events, key=lambda x: x['mins_ago'])[:5]:
        print(f"\n  Event: {ev['title']}")
        print(f"  Currency: {ev['currency']}")
        print(f"  Impact: {ev['impact']}")
        print(f"  {ev['mins_ago']:.1f} minutes ago")
    
    # Summary
    print("\n" + "="*90)
    print("📈 SUMMARY & RECOMMENDATIONS")
    print("="*90)
    
    total_blocking_minutes = sum((window - ev['time_diff']).total_seconds() / 60.0 
                                  for ev in blocking_events)
    
    print(f"\n  Total blocking events: {len(blocking_events)}")
    print(f"  Total minutes blocked: {total_blocking_minutes:.1f}")
    
    if len(blocking_events) >= 3:
        print("\n  ⚠️  HIGH NUMBER OF BLOCKING EVENTS!")
        print("     This is normal on high-impact days (e.g., FOMC, jobs data).")
        print("     Options:")
        print("     1. Set AVOID_NEWS_MINUTES=30 (reduce from 120) in .env")
        print("     2. Set NEWS_FILTER_ENABLED=false to disable news filter temporarily")
        print("     3. Only trade Medium impact events (set impact levels in config)")
    elif len(blocking_events) == 0:
        print("\n  ✅ No news events blocking trades currently.")
        print("     If orders are still not executing, check other blocking conditions.")
    
    # Check specific symbols
    print("\n" + "="*90)
    print("🔗 SYMBOL-SPECIFIC ANALYSIS")
    print("="*90)
    
    test_symbols = ["EURUSD", "XAUUSD"]
    for sym in test_symbols:
        is_blocked, reason = news_filter.is_blocked(sym)
        status = "🚫 BLOCKED" if is_blocked else "✅ CLEAR"
        print(f"\n  {sym}: {status}")
        if is_blocked:
            print(f"    Reason: {reason}")

if __name__ == "__main__":
    diagnose_news_events()
