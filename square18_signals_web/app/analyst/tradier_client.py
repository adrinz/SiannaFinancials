"""Tradier API client for real-time market data.

Handles fetching spot prices, options chains, and historical OHLCV data.
"""
import os
import time
import requests
from typing import Optional, Dict, Any, List

_BASE_URL_SANDBOX = "https://sandbox.tradier.com/v1"
_BASE_URL_LIVE = "https://api.tradier.com/v1"

def _get_headers() -> Dict[str, str]:
    api_key = os.environ.get("TRADIER_API_KEY", "")
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json"
    }

def _get_base_url() -> str:
    env = os.environ.get("TRADIER_ENV", "sandbox").lower()
    return _BASE_URL_LIVE if env == "live" else _BASE_URL_SANDBOX

def is_configured() -> bool:
    return bool(os.environ.get("TRADIER_API_KEY"))

def get_quotes(symbols: List[str]) -> List[Dict[str, Any]]:
    """Fetch quotes for one or more symbols."""
    if not is_configured() or not symbols:
        return []
    
    url = f"{_get_base_url()}/markets/quotes"
    try:
        resp = requests.get(
            url,
            params={"symbols": ",".join(symbols), "greeks": "true"},
            headers=_get_headers(),
            timeout=10.0
        )
        resp.raise_for_status()
        data = resp.json()
        quotes = data.get("quotes", {}).get("quote", [])
        if isinstance(quotes, dict):
            return [quotes]
        return quotes
    except Exception as e:
        print(f"Tradier get_quotes error: {e}")
        return []

def get_option_expirations(symbol: str) -> List[str]:
    """Fetch available expiration dates for a symbol."""
    if not is_configured():
        return []
        
    url = f"{_get_base_url()}/markets/options/expirations"
    try:
        resp = requests.get(
            url,
            params={"symbol": symbol, "includeAllRoots": "true"},
            headers=_get_headers(),
            timeout=10.0
        )
        resp.raise_for_status()
        data = resp.json()
        if not data or "expirations" not in data or data["expirations"] is None:
            return []
        dates = data.get("expirations", {}).get("date", [])
        if isinstance(dates, str):
            return [dates]
        return dates
    except Exception as e:
        print(f"Tradier get_option_expirations error: {e}")
        return []

def get_option_chain(symbol: str, expiration: str) -> List[Dict[str, Any]]:
    """Fetch the options chain for a specific expiration date."""
    if not is_configured():
        return []
        
    url = f"{_get_base_url()}/markets/options/chains"
    try:
        resp = requests.get(
            url,
            params={"symbol": symbol, "expiration": expiration, "greeks": "true"},
            headers=_get_headers(),
            timeout=15.0
        )
        resp.raise_for_status()
        data = resp.json()
        options = data.get("options", {}).get("option", [])
        if isinstance(options, dict):
            return [options]
        return options
    except Exception as e:
        print(f"Tradier get_option_chain error: {e}")
        return []

def get_historical_quotes(symbol: str, interval: str, start: str, end: str) -> List[Dict[str, Any]]:
    """Fetch historical OHLCV data.
    interval: 'daily', 'weekly', 'monthly'
    """
    if not is_configured():
        return []
        
    url = f"{_get_base_url()}/markets/history"
    try:
        resp = requests.get(
            url,
            params={"symbol": symbol, "interval": interval, "start": start, "end": end},
            headers=_get_headers(),
            timeout=15.0
        )
        resp.raise_for_status()
        data = resp.json()
        history = data.get("history", {}).get("day", [])
        if isinstance(history, dict):
            return [history]
        return history
    except Exception as e:
        print(f"Tradier get_historical_quotes error: {e}")
        return []

def get_timesales(symbol: str, interval: str, start: str, end: str, session_filter: str = "all") -> List[Dict[str, Any]]:
    """Fetch intraday OHLCV data.
    interval: 'tick', '1min', '5min', '15min'
    session_filter: 'all', 'open'
    """
    if not is_configured():
        return []
        
    url = f"{_get_base_url()}/markets/timesales"
    try:
        resp = requests.get(
            url,
            params={"symbol": symbol, "interval": interval, "start": start, "end": end, "session_filter": session_filter},
            headers=_get_headers(),
            timeout=15.0
        )
        resp.raise_for_status()
        data = resp.json()
        series = data.get("series", {}).get("data", [])
        if isinstance(series, dict):
            return [series]
        return series
    except Exception as e:
        print(f"Tradier get_timesales error: {e}")
        return []
