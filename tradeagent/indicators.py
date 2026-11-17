"""Deterministische technische Indikatoren.

Reiner, reproduzierbarer Python-Code. Keine LLM-Einwirkung.
Die Engine arbeitet auf Pandas-DataFrames mit OHLCV-Spalten.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / length, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out = 100 - (100 / (1 + rs))
    return out.fillna(50.0)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    macd_line = ema(close, fast) - ema(close, slow)
    signal_line = ema(macd_line, signal)
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def atr(df: pd.DataFrame, length: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [(high - low), (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / length, adjust=False).mean()


def detect_structure(df: pd.DataFrame, lookback: int = 20) -> str:
    """Einfache Strukturerkennung (SMC-Annahme).

    Schaut auf die letzten `lookback` Bars, ob das Struktur-High höher
    (uptrend) oder niedriger (downtrend) als das davor ist, sonst range.
    """
    window = df.tail(lookback + 2)
    if len(window) < lookback + 2:
        return "range"
    first_high = window["high"].iloc[0]
    last_high = window["high"].iloc[-1]
    first_low = window["low"].iloc[0]
    last_low = window["low"].iloc[-1]
    if last_high > first_high and last_low > first_low:
        return "uptrend"
    if last_high < first_high and last_low < first_low:
        return "downtrend"
    return "range"


def compute_snapshot(df: pd.DataFrame) -> Optional[dict]:
    """Berechnet einen vollständigen TechnicalSnapshot aus einem OHLCV-Frame."""
    if df is None or len(df) < 35:
        return None

    close = df["close"]
    fast = ema(close, 21)
    slow = ema(close, 50)
    m_line, m_signal, _ = macd(close)
    vol = df["volume"].rolling(20).mean().iloc[-1]

    return {
        "price": float(close.iloc[-1]),
        "atr": float(atr(df).iloc[-1]),
        "rsi": float(rsi(close).iloc[-1]),
        "ema_fast": float(fast.iloc[-1]),
        "ema_slow": float(slow.iloc[-1]),
        "macd": float(m_line.iloc[-1]),
        "macd_signal": float(m_signal.iloc[-1]),
        "structure": detect_structure(df),
        "volume_avg_20": float(vol) if not pd.isna(vol) else 0.0,
    }
