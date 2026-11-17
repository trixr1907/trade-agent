"""Datenzugriff via CCXT (Bitget).

Erzeugt OHLCV-DataFrames für die Signal-Engine. Funktioniert **ohne**
API-Keys für öffentlich market data — nur für Live-Trading braucht man Keys.
"""
from __future__ import annotations

import time
from typing import Optional

import ccxt
import pandas as pd

from .config import ExchangeConfig


def create_exchange(cfg: ExchangeConfig):
    """Erstellt einen CCXT-Exchange (Bitget). Public data braucht keine Keys."""
    ex_cls = getattr(ccxt, cfg.exchange)
    exchange = ex_cls(
        {
            "apiKey": cfg.api_key or None,
            "secret": cfg.api_secret or None,
            "password": cfg.api_passphrase or None,
            "enableRateLimit": True,
        }
    )
    # Bitget benötigt nonce/timeout für sign — für Public-Data unnötig.
    return exchange


def fetch_ohlcv(
    symbol: str,
    timeframe: str = "1h",
    limit: int = 120,
    cfg: Optional[ExchangeConfig] = None,
) -> pd.DataFrame:
    """Lädt OHLCV-Daten und normalisiert zu einem DataFrame."""
    cfg = cfg or ExchangeConfig()
    exchange = create_exchange(cfg)
    ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)

    df = pd.DataFrame(
        ohlcv, columns=["ts", "open", "high", "low", "close", "volume"]
    )
    df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.set_index("datetime")
    df = df[["open", "high", "low", "close", "volume"]].astype(float)
    return df


def fetch_price(symbol: str, cfg: Optional[ExchangeConfig] = None) -> float:
    cfg = cfg or ExchangeConfig()
    exchange = create_exchange(cfg)
    ticker = exchange.fetch_ticker(symbol)
    return float(ticker["last"])
