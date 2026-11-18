"""Ausführungsschicht — Paper-Trading (Standard) & Live-Trading (genehmigt).

**Prinzip:** Paper ist immer erlaubt. Live ist fail-closed und nur nach
validiertem Antrag + Freigabe + Limits möglich.

Nach einem Entry-Fill wird sofort ein Stop-Order (OCO wenn die Exchange es
unterstützt, sonst Stop-Market + separates Target-Limit) platziert.
Die Exchange-native Stop-Order lebt unabhängig vom Agent-Prozess.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import asdict
from typing import Any, Dict, List, Optional, Tuple

from .config import Config
from .models import ExecutionResult

logger = logging.getLogger(__name__)

# Bitget-Filter für Position-TP/SL-Orders (pos_loss/pos_profit), die der Relay platziert.
_TPSL_PLAN_FILTER = "profit_loss"


def _perp_symbol(symbol: str) -> Optional[str]:
    """Engine-Symbol -> Perp-Symbol (BTC/USDT -> BTC/USDT:USDT). None bei ungültigem Symbol."""
    from .relay import to_perp_symbol

    try:
        return to_perp_symbol(symbol)
    except ValueError:
        return None


def _find_by_id(orders: Any, order_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(orders, list):
        return None
    for order in orders:
        if isinstance(order, dict) and str(order.get("id")) == str(order_id):
            return dict(order)
    return None


# ── Hilfsfunktion: Stop-Seite bestimmen ──────────────────────────────────────

def _stop_side(entry_side: str) -> str:
    """Gegenüberliegende Seite für Stop/Target-Orders."""
    return "sell" if entry_side.lower() == "buy" else "buy"


# ── Paper-Broker ──────────────────────────────────────────────────────────────

class PaperBroker:
    """Simuliert Order-Ausführung ohne echtes Kapital.

    Fill-Preis = aktueller Mark-Preis; Spread/Liquidity bleiben einfach,
    aber der Code-Pfad ist identisch mit Live — das erlaubt später den
    Wechsel ohne logische Änderung.

    Stop/Target werden als simulierte IDs zurückgegeben, damit die
    Sync-Schicht (monitor.py) denselben Code-Pfad nutzen kann.
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

    def place_stop_orders(
        self,
        symbol: str,
        entry_side: str,
        qty: float,
        stop_price: float,
        target_price: float,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Simuliert Stop + Target-Orders. Gibt (stop_order_id, target_order_id) zurück."""
        stop_id = f"paper-stop-{uuid.uuid4().hex[:10]}"
        target_id = f"paper-target-{uuid.uuid4().hex[:10]}"
        return stop_id, target_id

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Simuliert Order-Cancel. Immer erfolgreich im Paper-Modus."""
        return True

    def fetch_order(self, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        """Simuliert fetch_order — gibt None zurück (kein Exchange-State im Paper-Modus)."""
        return None


# ── Live-Broker ───────────────────────────────────────────────────────────────

class LiveBroker:
    """Echte Ausführung via CCXT. Nur nach Freigabe + Limits nutzbar."""

    def __init__(self, cfg: Config):
        if not cfg.exchange.has_live_credentials:
            raise RuntimeError("LiveBroker ohne API-Keys nicht möglich.")
        self.cfg = cfg
        from .data import create_exchange
        self.exchange = create_exchange(cfg.exchange)
        # Demo: CCXT sendet PAPTRADING: 1, gleiche Symbole wie Live (siehe relay._build_exchange).
        self.exchange.set_sandbox_mode(bool(cfg.exchange.demo))

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

        from .universe import live_symbol_allowed

        if not live_symbol_allowed(self.cfg, symbol):
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

    def place_stop_orders(
        self,
        symbol: str,
        entry_side: str,
        qty: float,
        stop_price: float,
        target_price: float,
    ) -> Tuple[Optional[str], Optional[str]]:
        """Platziert Stop-Loss + Take-Profit auf der Exchange.

        Versucht zuerst OCO (create_oco_order). Fällt auf
        Stop-Market + Limit-Target zurück, wenn OCO nicht
        unterstützt wird.

        Gibt (stop_order_id, target_order_id) zurück.
        Beide können None sein wenn das Platzieren scheitert — der
        Entry-Trade bleibt trotzdem bestehen (fail-safe).
        """
        close_side = _stop_side(entry_side)
        stop_id: Optional[str] = None
        target_id: Optional[str] = None

        # ── Versuch 1: OCO ────────────────────────────────────────────────────
        try:
            oco = self.exchange.create_oco_order(
                symbol=symbol,
                side=close_side,
                amount=qty,
                price=target_price,
                stopPrice=stop_price,
                params={
                    "stopLossPrice": stop_price,
                    "takeProfitPrice": target_price,
                },
            )
            if isinstance(oco, list) and len(oco) >= 2:
                stop_id = str(oco[0].get("id", ""))
                target_id = str(oco[1].get("id", ""))
            elif isinstance(oco, dict):
                ids = oco.get("orders", oco.get("orderIds", []))
                if isinstance(ids, list) and len(ids) >= 2:
                    stop_id = str(ids[0])
                    target_id = str(ids[1])
                else:
                    stop_id = str(oco.get("id", ""))
            return stop_id, target_id
        except Exception:
            pass  # OCO nicht unterstützt → Fallback

        # ── Fallback: Stop-Market + Limit-Target ──────────────────────────────
        try:
            stop_order = self.exchange.create_order(
                symbol=symbol,
                type="stop_market",
                side=close_side,
                amount=qty,
                params={"stopPrice": stop_price, "reduceOnly": True},
            )
            stop_id = str(stop_order.get("id", ""))
        except Exception:
            pass

        try:
            target_order = self.exchange.create_limit_order(
                symbol=symbol,
                side=close_side,
                amount=qty,
                price=target_price,
                params={"reduceOnly": True},
            )
            target_id = str(target_order.get("id", ""))
        except Exception:
            pass

        return stop_id, target_id

    def _open_tpsl_orders(self, perp: str) -> Optional[List[Dict[str, Any]]]:
        try:
            orders = self.exchange.fetch_open_orders(perp, None, None, {"planType": _TPSL_PLAN_FILTER})
        except Exception as exc:
            logger.warning("Offene TP/SL-Orders für %s nicht abrufbar: %s", perp, exc)
            return None
        return orders if isinstance(orders, list) else None

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancelt eine Order auf der Exchange. Gibt True zurück bei Erfolg.

        Stop/Target des Relays sind Trigger-Orders (pos_loss/pos_profit). Ihr
        planType wird aus der offenen Order gelesen und an den Trigger-Cancel
        übergeben. Andere Orders laufen über den normalen Cancel.
        """
        perp = _perp_symbol(symbol)
        if perp is None:
            return False
        trigger_order = _find_by_id(self._open_tpsl_orders(perp), order_id)
        if trigger_order is not None:
            params: Dict[str, Any] = {"trigger": True}
            plan_type = (trigger_order.get("info") or {}).get("planType")
            if plan_type:
                params["planType"] = plan_type
            try:
                self.exchange.cancel_order(order_id, perp, params)
                return True
            except Exception as exc:
                logger.warning("Trigger-Cancel %s für %s fehlgeschlagen: %s", order_id, perp, exc)
                return False
        try:
            self.exchange.cancel_order(order_id, perp)
            return True
        except Exception:
            return False

    def fetch_order(self, symbol: str, order_id: str) -> Optional[Dict[str, Any]]:
        """Holt den aktuellen Status einer Order von der Exchange.

        Reihenfolge: offene TP/SL-Trigger-Orders, Trigger-Historie (ausgelöst =
        status 'closed'), dann normale Order. None, wenn nichts gefunden wird.
        """
        perp = _perp_symbol(symbol)
        if perp is None:
            return None
        found = _find_by_id(self._open_tpsl_orders(perp), order_id)
        if found is not None:
            return found
        try:
            history = self.exchange.fetch_canceled_and_closed_orders(
                perp, None, None, {"planType": _TPSL_PLAN_FILTER}
            )
        except Exception as exc:
            logger.warning("Trigger-Historie für %s nicht abrufbar: %s", perp, exc)
            history = None
        found = _find_by_id(history, order_id)
        if found is not None:
            return found
        try:
            return dict(self.exchange.fetch_order(order_id, perp))
        except Exception:
            return None


# ── Factory ───────────────────────────────────────────────────────────────────

def make_broker(cfg: Config) -> "PaperBroker | LiveBroker":
    """Returns Paper (default) or Live broker depending on mode."""
    if cfg.trading.is_live:
        return LiveBroker(cfg)
    return PaperBroker(cfg)