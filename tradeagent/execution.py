"""Ausführungsschicht — Paper-Trading (Standard) & Live-Trading (genehmigt).

**Prinzip:** Paper ist immer erlaubt. Live ist fail-closed und nur nach
validiertem Antrag + Freigabe + Limits möglich.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any, Dict, Optional

from .config import Config
from .models import ExecutionResult


class PaperBroker:
    """Simuliert Order-Ausführung ohne echtes Kapital.

    Fill-Preis = aktueller Mark-Preis; Spread/Liquidity bleiben einfach,
    aber der Code-Pfad ist identisch mit Live — das erlaubt später den
    Wechsel ohne logische Änderung.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def place_order(
        self, symbol: str, side: str, qty: float, price: float
    ) -> ExecutionResult:
        order_id = f"paper-{uuid.uuid4().hex[:12]}"
        return ExecutionResult(
            proposal_id="",
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            notional_usd=round(qty * price, 2),
            mode="paper",
            order_id=order_id,
            status="filled",
            raw={"simulated": True},
        )


class LiveBroker:
    """Echte Ausführung via CCXT. Nur nach Freigabe + Limits nutzbar."""

    def __init__(self, cfg: Config):
        if not cfg.exchange.has_live_credentials:
            raise RuntimeError("LiveBroker ohne API-Keys nicht möglich.")
        self.cfg = cfg
        from .data import create_exchange

        self.exchange = create_exchange(cfg.exchange)

    def place_order(
        self, symbol: str, side: str, qty: float, price: Optional[float]
    ) -> ExecutionResult:
        # Limit-Check fail-closed
        notional = qty * (price or self.exchange.fetch_ticker(symbol)["last"])
        if notional > self.cfg.live_limits.max_position_notional_usd:
            return ExecutionResult(
                proposal_id="",
                symbol=symbol,
                side=side,
                qty=qty,
                price=price or 0.0,
                notional_usd=notional,
                mode="live",
                order_id="",
                status="rejected",
                raw={"error": "Positionsgröße über Limit"},
            )

        if not self.cfg.live_limits.symbol_allowed(symbol):
            return ExecutionResult(
                proposal_id="",
                symbol=symbol,
                side=side,
                qty=qty,
                price=price or 0.0,
                notional_usd=notional,
                mode="live",
                order_id="",
                status="rejected",
                raw={"error": "Symbol nicht in Allowlist"},
            )

        # Market-Order platzieren
        order = self.exchange.create_market_order(symbol, side, qty)
        return ExecutionResult(
            proposal_id="",
            symbol=symbol,
            side=side,
            qty=qty,
            price=price or 0.0,
            notional_usd=notional,
            mode="live",
            order_id=str(order.get("id", "")),
            status="submitted",
            raw=asdict(order) if hasattr(order, "__dict__") else dict(order),
        )


def make_broker(cfg: Config):
    """Returns Paper (default) or Live broker depending on mode."""
    if cfg.trading.is_live:
        return LiveBroker(cfg)
    return PaperBroker(cfg)
