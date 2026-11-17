"""Risiko-Management: Kelly-Sizing, ATR-Stops, Max-Drawdown-Budget.

Risiko-Management ist Kern, nicht Zusatz. Ein Signal ohne Sizing wird
nicht ausgeführt.
"""
from __future__ import annotations

import math
from typing import Optional

from .config import RiskConfig
from .models import Signal


def kelly_fraction(win_rate: float, win_loss_ratio: float) -> float:
    """Kelly-Kriterium als Bruchteil (0..1). Negative Werte auf 0 gesetzt."""
    if win_loss_ratio <= 0:
        return 0.0
    f = (win_rate * win_loss_ratio - (1 - win_rate)) / win_loss_ratio
    return max(f, 0.0)


def position_size_usd(
    signal: Signal,
    *,
    account_balance_usd: float,
    risk_pct: float,
    kelly_scale: float,
    capital_fraction: float = 0.5,
) -> float:
    """Berechnet die Positionsgröße in USD.

    Risikobasiert: Risiko = account_balance * risk_pct.
    Die Risikodistanz ist price - stop (BUY) bzw. stop - price (SELL).
    Größe = Risiko-USD / Risikodistanz, dann Kelly-Skalierung + Kapital-Kap.
    """
    if signal.stop_price is None or signal.price is None:
        return 0.0

    risk_dist = abs(signal.price - signal.stop_price)
    if risk_dist <= 0:
        return 0.0

    risk_usd = account_balance_usd * risk_pct
    qty = risk_usd / risk_dist
    notional = qty * signal.price

    # Kelly-Skalierung als Risiko-Dämpfer (nicht als volles Kelly)
    notional *= max(0.0, min(kelly_scale, 1.0))
    # Kapital-Kap: max share des Accounts, die eingenommen wird
    notional = min(notional, account_balance_usd * capital_fraction)

    return round(notional, 2)


def check_drawdown_budget(
    current_drawdown_pct: float, max_drawdown_pct: float
) -> bool:
    """True, wenn das Drawdown-Budget noch nicht überschritten ist."""
    return current_drawdown_pct < max_drawdown_pct


def validate_proposal(
    signal: Signal,
    *,
    position_usd: float,
    account_balance_usd: float,
    risk_cfg: RiskConfig,
    current_drawdown_pct: float = 0.0,
) -> tuple[bool, Optional[str]]:
    """Validiert einen Antrag vor der Ausführung. Fail-closed."""
    if not check_drawdown_budget(current_drawdown_pct, risk_cfg.max_drawdown_pct):
        return False, (
            f"Max-Drawdown-Budget ({risk_cfg.max_drawdown_pct:.0%}) erreicht — "
            "keine neuen Positionen."
        )

    if position_usd <= 0:
        return False, "Positionsgröße ist 0 oder invalid — kein Sizing."

    if signal.score < risk_cfg.min_signal_score:
        return False, (
            f"Signal-Score {signal.score} unter Schwellenwert "
            f"{risk_cfg.min_signal_score}."
        )

    # Risikodistanz muss sinnvoll sein
    if signal.stop_price is None:
        return False, "Kein Stop-Preis vorhanden."

    return True, None
