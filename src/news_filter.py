from __future__ import annotations

import csv
import io
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

import urllib.request

DEFAULT_NEWS_URLS: Tuple[str, ...] = (
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_thisweek.csv",
    "https://nfs.faireconomy.media/ff_calendar_thisweek.xml",
)

DEFAULT_REQUEST_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/135.0 Safari/537.36"
    ),
    "Accept": "application/json,text/csv,application/xml,text/xml,text/plain,*/*",
    "Referer": "https://www.forexfactory.com/",
}

FF_FEED_TZ = ZoneInfo("America/New_York") if ZoneInfo is not None else timezone.utc

@dataclass
class NewsFilterConfig:
    enabled: bool = True
    url: str = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
    urls: Tuple[str, ...] = DEFAULT_NEWS_URLS
    avoid_minutes: int = 90
    impact_levels: Tuple[str, ...] = ("High",)
    request_timeout_seconds: int = 10
    cache_ttl_seconds: int = 15 * 60


class NewsFilter:
    def __init__(self, config: NewsFilterConfig) -> None:
        self.config = config
        self.logger = logging.getLogger("news")
        self._cache_ts: float = 0.0
        self._cache_events: List[dict] = []
        self._cache_source: str = ""

    @staticmethod
    def _normalize_impact(value: str) -> str:
        raw = str(value or "").strip().lower()
        if raw in {"high", "red"}:
            return "High"
        if raw in {"medium", "orange"}:
            return "Medium"
        if raw in {"low", "yellow"}:
            return "Low"
        return str(value or "").strip().title()

    @staticmethod
    def _format_event_details(ev: dict) -> str:
        parts = []
        for k in ("actual", "forecast", "previous"):
            v = ev.get(k)
            if v is None or v == "":
                continue
            parts.append(f"{k}={v}")
        return (" | " + " ".join(parts)) if parts else ""

    @staticmethod
    def _symbol_currencies(symbol: str) -> Tuple[str, str]:
        sym = str(symbol).strip().upper()
        if len(sym) >= 6:
            return sym[:3], sym[3:6]
        return "", ""

    @staticmethod
    def _request_headers() -> Dict[str, str]:
        return dict(DEFAULT_REQUEST_HEADERS)

    @staticmethod
    def _content_kind(url: str, content_type: str) -> str:
        raw = str(content_type or "").lower()
        path = str(url or "").lower().strip()
        if "json" in raw or path.endswith(".json"):
            return "json"
        if "csv" in raw or path.endswith(".csv"):
            return "csv"
        if "xml" in raw or path.endswith(".xml"):
            return "xml"
        return "text"

    def _normalize_event(self, ev: dict, source_url: str) -> dict:
        title = str(ev.get("title", "") or ev.get("event", "") or "").strip()
        currency = str(ev.get("currency", "") or ev.get("country", "") or "").strip().upper()
        date_value = ev.get("date", "")
        time_value = ev.get("time", "")
        return {
            "title": title,
            "event": title,
            "currency": currency,
            "country": currency,
            "date": str(date_value or "").strip(),
            "time": str(time_value or "").strip(),
            "impact": self._normalize_impact(str(ev.get("impact", "") or "")),
            "forecast": ev.get("forecast", ""),
            "previous": ev.get("previous", ""),
            "actual": ev.get("actual", ""),
            "source_url": str(source_url or "").strip(),
            "event_url": str(ev.get("url", "") or ev.get("event_url", "") or "").strip(),
        }

    def _parse_json_events(self, raw: str, source_url: str) -> List[dict]:
        data = json.loads(raw or "[]")
        if not isinstance(data, list):
            return []
        return [self._normalize_event(ev, source_url) for ev in data if isinstance(ev, dict)]

    def _parse_csv_events(self, raw: str, source_url: str) -> List[dict]:
        reader = csv.DictReader(io.StringIO(raw or ""))
        events: List[dict] = []
        for row in reader:
            if not isinstance(row, dict):
                continue
            mapped = {
                "title": row.get("Title", ""),
                "country": row.get("Country", ""),
                "date": row.get("Date", ""),
                "time": row.get("Time", ""),
                "impact": row.get("Impact", ""),
                "forecast": row.get("Forecast", ""),
                "previous": row.get("Previous", ""),
                "actual": row.get("Actual", ""),
                "url": row.get("URL", ""),
            }
            events.append(self._normalize_event(mapped, source_url))
        return events

    def _parse_xml_events(self, raw: str, source_url: str) -> List[dict]:
        root = ET.fromstring(raw or "")
        events: List[dict] = []
        for node in root.findall(".//event"):
            mapped = {
                "title": node.findtext("title", default=""),
                "country": node.findtext("country", default=""),
                "date": node.findtext("date", default=""),
                "time": node.findtext("time", default=""),
                "impact": node.findtext("impact", default=""),
                "forecast": node.findtext("forecast", default=""),
                "previous": node.findtext("previous", default=""),
                "actual": node.findtext("actual", default=""),
                "url": node.findtext("url", default=""),
            }
            events.append(self._normalize_event(mapped, source_url))
        return events

    def _parse_events(self, raw: str, source_url: str, content_type: str) -> List[dict]:
        kind = self._content_kind(source_url, content_type)
        if kind == "json":
            return self._parse_json_events(raw, source_url)
        if kind == "csv":
            return self._parse_csv_events(raw, source_url)
        if kind == "xml":
            return self._parse_xml_events(raw, source_url)
        raise ValueError(f"Unsupported feed format for {source_url}")

    def _fetch_from_url(self, url: str) -> List[dict]:
        headers = self._request_headers()
        if requests is not None:
            resp = requests.get(url, headers=headers, timeout=self.config.request_timeout_seconds)
            resp.raise_for_status()
            return self._parse_events(resp.text, url, resp.headers.get("Content-Type", ""))

        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=self.config.request_timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            content_type = str(getattr(resp, "headers", {}).get("Content-Type", "") or "")
        return self._parse_events(raw, url, content_type)

    def _fetch(self) -> List[dict]:
        if not self.config.enabled:
            return []
        now = time.time()
        if now - self._cache_ts < float(self.config.cache_ttl_seconds) and self._cache_events:
            return self._cache_events
        errors: List[str] = []
        for url in self.config.urls:
            try:
                events = self._fetch_from_url(url)
                if not events:
                    errors.append(f"{url} returned no events")
                    continue
                self._cache_events = events
                self._cache_ts = now
                self._cache_source = url
                return events
            except Exception as exc:
                errors.append(f"{url} -> {exc}")

        if self._cache_events:
            self.logger.warning(
                "News fetch failed for all configured URLs; using stale cache from %s. Errors: %s",
                self._cache_source or "cache",
                " | ".join(errors),
            )
            return self._cache_events

        self.logger.error("News fetch failed: %s", " | ".join(errors) if errors else "no URLs configured")
        return []

    @staticmethod
    def _parse_event_time(ev: dict) -> Optional[datetime]:
        # Prefer timestamp if present
        ts = ev.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 0:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc)

        date_s = str(ev.get("date", "") or "").strip()
        time_s = str(ev.get("time", "") or "").strip()
        if not date_s:
            return None
        try:
            iso_dt = datetime.fromisoformat(date_s.replace("Z", "+00:00"))
            if iso_dt.tzinfo is None:
                iso_dt = iso_dt.replace(tzinfo=FF_FEED_TZ)
            return iso_dt.astimezone(timezone.utc)
        except Exception:
            pass

        if not time_s:
            return None
        if time_s.lower() in {"all day", "tentative"}:
            return None

        # Common formats seen in FF feeds vary; attempt a few.
        candidates = [
            "%m-%d-%Y %I:%M%p",
            "%m-%d-%Y %I:%M %p",
            "%b %d, %Y %H:%M",
            "%b %d %Y %H:%M",
            "%Y-%m-%d %H:%M",
            "%b %d %H:%M",
        ]

        for fmt in candidates:
            try:
                dt = datetime.strptime(f"{date_s} {time_s}", fmt)
                # If year missing, assume current year
                if dt.year == 1900:
                    dt = dt.replace(year=datetime.now(timezone.utc).year)
                return dt.replace(tzinfo=FF_FEED_TZ).astimezone(timezone.utc)
            except Exception:
                continue
        return None

    def is_blocked(self, symbol: str, now: Optional[datetime] = None) -> Tuple[bool, str]:
        if not self.config.enabled or self.config.avoid_minutes <= 0:
            self.logger.debug("News filter disabled or avoid_minutes=%d", self.config.avoid_minutes)
            return False, ""

        now_dt = now or datetime.now(timezone.utc)
        base, quote = self._symbol_currencies(symbol)
        if not base or not quote:
            self.logger.error("Cannot parse currencies from symbol: %s", symbol)
            return False, ""

        events = self._fetch()
        window = timedelta(minutes=int(self.config.avoid_minutes))

        self.logger.debug(
            "[NEWS_FILTER] Checking %s (base=%s quote=%s) against %d events | window=%d mins | now=%s | impact_levels=%s",
            symbol, base, quote, len(events), self.config.avoid_minutes, now_dt.isoformat(), self.config.impact_levels
        )

        for ev in events:
            impact = self._normalize_impact(str(ev.get("impact", "") or ""))
            if impact and impact not in self.config.impact_levels:
                self.logger.debug(
                    "Event '%s' skipped: impact=%s not in %s",
                    ev.get("title", "?"), impact, self.config.impact_levels
                )
                continue
            currency = str(ev.get("country", "") or ev.get("currency", "") or "").strip().upper()
            # Professional: Monitor Base, Quote, AND USD (Global reserve currency)
            if currency not in {base, quote, "USD"}:
                self.logger.debug(
                    "Event '%s' skipped: currency=%s not in {%s,%s,USD}",
                    ev.get("title", "?"), currency, base, quote
                )
                continue

            ev_time = self._parse_event_time(ev)
            if ev_time is None:
                self.logger.debug("Event '%s' skipped: cannot parse time", ev.get("title", "?"))
                continue

            time_diff = abs(ev_time - now_dt)
            self.logger.debug(
                "Checking event: title='%s' currency=%s impact=%s time=%s diff_from_now=%s <= window=%s?",
                ev.get("title", "?"), currency, impact, ev_time.isoformat(), time_diff, window
            )

            if time_diff <= window:
                title = str(ev.get("title", "") or ev.get("event", "") or "news")
                details = self._format_event_details(ev)
                source_url = str(ev.get("source_url", "") or self._cache_source or self.config.url or "")
                remaining_mins = (window - time_diff).total_seconds() / 60.0
                self.logger.warning(
                    "[NEWS_FILTER_BLOCKED] %s | EVENT=%s | CURRENCY=%s | IMPACT=%s | TIME=%s | REMAINING_MINS=%.1f | DETAILS=%s",
                    symbol, title, currency, impact, ev_time.isoformat(), remaining_mins, details
                )
                return (
                    True,
                    f"Blocked by {impact} news ({currency}): {title} @ {ev_time.isoformat()} | {remaining_mins:.1f} mins remaining | Details:{details}",
                )

        self.logger.debug("[NEWS_FILTER_CLEAR] %s passed all news checks", symbol)
        return False, ""


def build_news_config_from_env(getenv) -> NewsFilterConfig:
    def _split_urls(raw: str) -> Tuple[str, ...]:
        text = str(raw or "").replace(";", ",").replace("\n", ",")
        return tuple(url.strip() for url in text.split(",") if url.strip())

    enabled_raw = str(getenv("NEWS_FILTER_ENABLED", "true")).strip().lower()
    enabled = enabled_raw in {"1", "true", "yes", "y", "on"}
    url = str(getenv("NEWS_FILTER_URL", DEFAULT_NEWS_URLS[0]) or "")
    urls_raw = str(getenv("NEWS_FILTER_URLS", "") or "")
    configured_urls = _split_urls(urls_raw)
    if configured_urls:
        urls = configured_urls
    elif url:
        urls = tuple(dict.fromkeys((url, *DEFAULT_NEWS_URLS)))
    else:
        urls = DEFAULT_NEWS_URLS
    avoid = int(float(getenv("AVOID_NEWS_MINUTES", 60) or 60))
    return NewsFilterConfig(enabled=enabled, url=url, urls=urls, avoid_minutes=avoid)
