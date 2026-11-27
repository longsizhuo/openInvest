"""Describe provider time evidence without inventing a completed session or a live quote."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
from zoneinfo import ZoneInfo
import math

DEFAULT_QUOTE_MAX_AGE = timedelta(minutes=25)


def observation_now():
    return datetime.now(timezone.utc)


def utc_time(value):
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return datetime.fromtimestamp(value, timezone.utc) if math.isfinite(value) else None
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        return value.astimezone(timezone.utc) if isinstance(value, datetime) and value.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def _metadata_fields(value):
    if not isinstance(value,Mapping):
        return {}
    keys = ('currency','symbol','instrumentType','quoteType','regularMarketTime',
            'exchangeTimezoneName','timezone','marketState','currentTradingPeriod')
    result = {}
    for key in keys:
        item = value.get(key)
        if item is not None:
            result[key] = item
    # yfinance HistoryMetadata advertises a lazy tradingPeriods key even when
    # it was never fetched. Iterating/copying that mapping performs new IO.
    if isinstance(value,dict) and 'tradingPeriods' in value:
        result['tradingPeriods'] = value['tradingPeriods']
    return result


def provider_metadata(ticker):
    """history() populates this provider metadata; failure never manufactures it."""
    try:
        value = ticker.history_metadata
        return _metadata_fields(value)
    except Exception:
        return {}


def _periods(metadata):
    values = []
    current = metadata.get("currentTradingPeriod") or {}
    if isinstance(current, dict) and isinstance(current.get("regular"), dict):
        values.append(current["regular"])
    def collect(value):
        if isinstance(value, dict):
            if "start" in value and "end" in value:
                values.append(value)
            elif "regular" in value:
                collect(value["regular"])
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item)
        elif hasattr(value, 'to_dict'):
            try:
                collect(value.to_dict('records'))
            except (TypeError, ValueError):
                pass
    collect(metadata.get("tradingPeriods", []))
    return values


def history_observation(frame, *, provider="yfinance", metadata=None, interval="1d",
                        price_basis="adjusted_close", now=None, fetch_status="ok",
                        received_at=None,
                        max_quote_age=DEFAULT_QUOTE_MAX_AGE):
    """Prior sessions require a provider period/timezone witness, not host 'today'.

    Earlier daily labels can be certified by an already-started successor
    session. An alert for the latest completed session additionally requires its
    own observed provider end. Historical references never become live quotes.
    """
    now = utc_time(now or observation_now())
    if now is None:
        raise ValueError('observation_clock_requires_timezone')
    captured = utc_time(received_at) if received_at is not None else now
    captured = captured if captured is not None and captured <= now else None
    metadata = _metadata_fields(metadata)
    result = {"contract":"openinvest.market_observation.v1", "provider": provider, "interval": interval, "price_basis": price_basis,
              "fetch_status": fetch_status, "received_at": captured.isoformat() if captured else None,
              "evaluated_at": now.isoformat(),
              "currency": metadata.get('currency'),
              "instrument_type": metadata.get('instrumentType') or metadata.get('quoteType'),
              "provider_symbol": metadata.get('symbol'),
              "quote_asof": None, "bar_asof": None, "exchange_timezone": None,
              "market_state": "unknown", "completed_session_dates": [],
              "completion_ends": {}, "latest_bar_completed": False,
              "daily_alert_eligible": False, "freshness": "reference_only",
              "quote_fresh": False, "session_completion_basis": "provider_time_evidence_unavailable"}
    if frame is None or frame.empty:
        result["freshness"] = "unavailable"
        return result
    result["bar_asof"] = str(frame.index[-1])
    result["session_date"] = frame.index[-1].date().isoformat()
    zone_name = metadata.get("exchangeTimezoneName") or metadata.get("timezone")
    try:
        zone = ZoneInfo(zone_name) if isinstance(zone_name, str) else None
    except (ValueError, KeyError):
        zone = None
    if zone is None:
        return result
    result["exchange_timezone"] = zone_name
    futures = str(metadata.get("instrumentType") or metadata.get("quoteType") or "").upper() == "FUTURE"
    periods = []
    for raw in _periods(metadata):
        start, end = utc_time(raw.get("start")), utc_time(raw.get("end"))
        if start is None or end is None or end <= start:
            continue
        start_date, end_date = start.astimezone(zone).date(), end.astimezone(zone).date()
        label = end_date if futures and end_date != start_date else start_date
        periods.append((start, end, label))
    started = [value for value in periods if captured is not None and value[0] <= captured]
    if not started:
        return result
    latest = max(started, key=lambda value: value[0])
    result['reported_session_state'] = 'open' if latest[0] <= captured < latest[1] else 'ended'
    reported_state = str(metadata.get('marketState','')).upper()
    if fetch_status in ('ok','cached') and now-captured <= max_quote_age:
        result['market_state'] = ('closed' if reported_state == 'CLOSED' else 'open'
            if latest[0] <= captured <= now < latest[1] else 'unknown')
    labels = [(utc_time(index).astimezone(zone).date() if utc_time(index) is not None else index.date())
              for index in frame.index]
    result['session_date'] = labels[-1].isoformat()
    if interval != '1d':
        stamp = utc_time(frame.index[-1])
        matching = [value for value in started if stamp is not None and value[0] <= stamp < value[1]]
        if matching:
            result['session_date'] = max(matching,key=lambda value:value[0])[2].isoformat()
            result['session_label_basis'] = 'provider_period'
    if interval == "1d":
        for label in labels:
            matching = [value for value in started if value[2] == label and value[1] <= captured]
            if matching:
                result["completed_session_dates"].append(label.isoformat())
                result["completion_ends"][label.isoformat()] = max(value[1] for value in matching).isoformat()
            elif label < latest[2] and label < latest[0].astimezone(zone).date():
                result["completed_session_dates"].append(label.isoformat())
        result["latest_bar_completed"] = labels[-1].isoformat() in result["completed_session_dates"]
        result["daily_alert_eligible"] = (labels[-1] == latest[2] and latest[1] <= captured
            and labels[-1].isoformat() in result["completion_ends"] and fetch_status in ('ok','cached'))
        if result["completed_session_dates"]:
            result["session_completion_basis"] = "provider_period_end_or_started_successor"
            result["freshness"] = "completed_session_reference"
    stamp = utc_time(metadata.get("regularMarketTime"))
    stamp_matches = (stamp is not None and (stamp.astimezone(zone).date() == labels[-1]
        or (labels[-1] == latest[2] and latest[0] <= stamp <= latest[1])))
    if (stamp_matches and captured is not None and stamp <= captured
            and now - stamp <= max_quote_age and fetch_status in ('ok','cached')
            and result['market_state'] == 'open'
            and price_basis in ("adjusted_close", "raw_close", "fx_rate")):
        result["quote_asof"] = stamp.isoformat()
        result["quote_fresh"] = True
        result["freshness"] = "observed_quote"
    if 'Close' in frame.columns and not (math.isfinite(float(frame['Close'].iloc[-1])) and float(frame['Close'].iloc[-1]) > 0):
        result.update(quote_fresh=False,daily_alert_eligible=False,freshness='invalid_price')
    return result


def frame_observation(frame, *, now=None):
    defaults = {'received_at':None, 'bar_asof':str(frame.index[-1]) if frame is not None and not frame.empty else None,
                'quote_asof':None, 'completed_session_dates':[], 'quote_fresh':False}
    value = getattr(frame, "attrs", {}).get("source_observation")
    if isinstance(value, dict):
        result = {**defaults, **value}
        result['quote_fresh'] = result.get('quote_fresh') is True
        stamp, captured = utc_time(result.get('quote_asof')), utc_time(result.get('received_at'))
        now = utc_time(now or observation_now())
        failed = bool(getattr(frame,'attrs',{}).get('yf_fetch_failed')) or result.get('fetch_status') in ('failed','unverified')
        if (failed or result.get('contract') != 'openinvest.market_observation.v1'
                or stamp is None or captured is None or not stamp <= captured <= now
                or now-stamp > DEFAULT_QUOTE_MAX_AGE):
            result['quote_fresh'] = False
            result['freshness'] = 'reference_only'
        if failed:
            result['fetch_status'] = 'failed'
            result['daily_alert_eligible'] = False
        return result
    return {**defaults,
        "provider": "unknown", "fetch_status": "failed" if getattr(frame, "attrs", {}).get("yf_fetch_failed") else "unverified",
        "freshness": "reference_only", "quote_fresh": False, "quote_asof": None,
        "price_basis": "unverified", "session_completion_basis": "provider_time_evidence_unavailable"}
