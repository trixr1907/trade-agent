"""Signal-Engine — erzeugt Trade-Signale aus TechnicalSnapshots.

**Deterministisch.** Keine LLM-Einwirkung. Die Scoring-Logik ist
explizit, testbar und reproduzierbar. Ein Signal mit Score >= min_score
wird als Antragsgrundlage angenommen.
"""
from __future__ import annotations

from typing import List, Optional

from .config import RiskConfig
from .indicators import compute_snapshot
from .models import Signal, SignalDirection


def _score_buy(snap: dict) -> tuple[int, List[str]]:
    score = 0
    reasons: List[str] = []

    # Trend: schneller EMA über langsamem EMA
    if snap["ema_fast"] > snap["ema_slow"]:
        score += 20
        reasons.append("EMA21 > EMA50 (Aufwärtstrend)")

    # Struktur
    if snap["structure"] == "uptrend":
        score += 15
        reasons.append("SMC-Struktur: higher highs/lows")

    # RSI: positiv, aber nicht im übergeheat Overbought
    if 45 <= snap["rsi"] <= 70:
        score += 10
        reasons.append(f"RSI im gesunden Bereich ({snap['rsi']:.0f})")

    # MACD-Impuls
    if snap["macd"] > snap["macd_signal"]:
        score += 15
        reasons.append("MACD über Signal (positives Impuls)")

    # Volumen-Bestätigung (relativ zur 20-Bar-Basis)
    if snap["volume_avg_20"] > 0:
        score += 5
        reasons.append("Volumen-Basis vorhanden")

    return score, reasons


def _score_sell(snap: dict) -> tuple[int, List[str]]:
    score = 0
    reasons: List[str] = []

    if snap["ema_fast"] < snap["ema_slow"]:
        score += 20
        reasons.append("EMA21 < EMA50 (Abwärtstrend)")

    if snap["structure"] == "downtrend":
        score += 15
        reasons.append("SMC-Struktur: lower lows/highs")

    if 30 <= snap["rsi"] <= 55:
        score += 10
        reasons.append(f"RSI im schwachen Bereich ({snap['rsi']:.0f})")

    if snap["macd"] < snap["macd_signal"]:
        score += 15
        reasons.append("MACD unter Signal (negatives Impuls)")

    if snap["volume_avg_20"] > 0:
        score += 5
        reasons.append("Volumen-Basis vorhanden")

    return score, reasons


def analyze_symbol(symbol: str, df) -> Optional[Signal]:
    """Analysiert ein Symbol und gibt ein Signal zurück (oder None)."""
    snap = compute_snapshot(df)
    if snap is None:
        return None

    # Kauf-Score ermitteln; Verkauf nur bei umgekehrter, starker Dynamik.
    buy_score, buy_reasons = _score_buy(snap)
    sell_score, sell_reasons = _score_sell(snap)

    # Struktur-basierte Vorauswahl: nur in passender Richtung traden.
    if snap["structure"] == "uptrend":
        score, reasons, direction = buy_score, buy_reasons, SignalDirection.BUY
    elif snap["structure"] == "downtrend":
        score, reasons, direction = sell_score, sell_reasons, SignalDirection.SELL
    else:
        # Range: nur die stärkere Seite, sonst kein Signal.
        if buy_score >= sell_score:
            score, reasons, direction = buy_score, buy_reasons, SignalDirection.BUY
        else:
            score, reasons, direction = sell_score, sell_reasons, SignalDirection.SELL

    price = snap["price"]
    atr_val = snap["atr"]

    if direction == SignalDirection.BUY:
        stop_price = price - 2.5 * atr_val
        target_price = price + 3.0 * atr_val
    else:
        stop_price = price + 2.5 * atr_val
        target_price = price - 3.0 * atr_val

    return Signal(
        symbol=symbol,
        direction=direction,
        price=price,
        score=min(score, 100),
        reasons=reasons,
        stop_price=stop_price,
        target_price=target_price,
        atr=atr_val,
    )


def signals_above_threshold(signals: List[Signal], min_score: int) -> List[Signal]:
    return [s for s in signals if s.score >= min_score]
