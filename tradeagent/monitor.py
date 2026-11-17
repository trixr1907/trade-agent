"""Stop-Monitor: leichte Sync-Schicht für Exchange-native Stop/Target-Orders.

**Rollen-Aufteilung:**
- Trigger:      Exchange (Stop-Order lebt dort, feuert ohne Agent-Prozess)
- Sync-Schicht: dieser Monitor — erkennt Fills, hält State-Store aktuell.

Der Monitor läuft als Hintergrund-Thread und pollt fetch_order per
REST (robust, fail-safe, kein WebSocket-Komplexität).

Crash des Monitors ändert nichts am Trigger auf der Exchange.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .execution import make_broker
from .state import OpenPosition, StateStore, get_state_store

logger = logging.getLogger(__name__)


# ── P&L-Berechnung (realem Fill-Preis, Slippage bewusst) ─────────────────────

def calculate_pnl(pos: OpenPosition, fill_price: float) -> float:
    """Berechnet realisierten P&L aus echtem Fill-Preis (nicht stop_price).

    BUY-Position: P&L = (fill_price - entry_price) * qty
    SELL-Position: P&L = (entry_price - fill_price) * qty

    Slippage ist bereits im fill_price enthalten.
    """
    if pos.side.lower() == "buy":
        return round((fill_price - pos.entry_price) * pos.qty, 6)
    else:
        return round((pos.entry_price - fill_price) * pos.qty, 6)


# ── Fill-Erkennung ────────────────────────────────────────────────────────────

def _is_filled(order: Dict[str, Any]) -> bool:
    status = str(order.get("status", "")).lower()
    return status in ("closed", "filled")


def _extract_fill_price(order: Dict[str, Any], fallback_price: float) -> float:
    """Extrahiert den tatsächlichen Fill-Preis aus einer Order.

    Priorität: average > price > stopPrice > triggerPrice > fallback.
    Stop-Market-Orders haben oft keinen 'price', aber 'average' nach Fill.
    """
    for key in ("average", "price", "stopPrice", "triggerPrice"):
        val = order.get(key)
        if val and float(val) > 0:
            return float(val)
    return fallback_price


# ── Sync-Zyklus ───────────────────────────────────────────────────────────────

def sync_once(
    state: StateStore,
    cfg: Config,
    *,
    on_close: Optional[Callable[[OpenPosition, float, float], None]] = None,
) -> List[str]:
    """Ein einzelner Sync-Zyklus: prüft alle offenen Positionen.

    Für jede Position:
    - fetch_order(stop_order_id) + fetch_order(target_order_id)
    - Wenn eine der Orders gefüllt ist:
        - Gegenorder canceln (manuelles OCO)
        - close_position() + P&L im State
        - optional: on_close(pos, fill_price, pnl) Callback

    Im Paper-Modus: fetch_order gibt None zurück → kein automatischer Close
    (Paper-Positionen ohne IDs werden übersprungen).

    Gibt Liste der geschlossenen proposal_ids zurück.
    """
    broker = make_broker(cfg)
    closed_ids: List[str] = []

    for pos in state.open_positions():
        stop_id = pos.stop_order_id
        target_id = pos.target_order_id

        # Kein Stop-Order-ID → nichts zu prüfen
        if not stop_id and not target_id:
            continue

        filled_order: Optional[Dict[str, Any]] = None
        filled_type: Optional[str] = None  # "stop" oder "target"
        cancel_id: Optional[str] = None

        # Stop-Order prüfen
        if stop_id:
            order = broker.fetch_order(pos.symbol, stop_id)
            if order and _is_filled(order):
                filled_order = order
                filled_type = "stop"
                cancel_id = target_id

        # Target-Order prüfen (nur wenn Stop nicht schon gefüllt)
        if filled_order is None and target_id:
            order = broker.fetch_order(pos.symbol, target_id)
            if order and _is_filled(order):
                filled_order = order
                filled_type = "target"
                cancel_id = stop_id

        if filled_order is None:
            continue  # Noch offen — kein Close

        # Gegenorder canceln (manuelles OCO)
        if cancel_id:
            try:
                broker.cancel_order(pos.symbol, cancel_id)
            except Exception as exc:
                logger.warning("Cancel von %s für %s fehlgeschlagen: %s", cancel_id, pos.symbol, exc)

        # Fill-Preis + P&L (aus realem Fill, Slippage bewusst)
        fallback = pos.stop_price if filled_type == "stop" else pos.target_price
        fill_price = _extract_fill_price(filled_order, fallback)
        pnl = calculate_pnl(pos, fill_price)

        # State aktualisieren — Doppel-Close verhindert durch atomares close_position
        closed = state.close_position(pos.proposal_id, fill_price, pnl)
        if closed is None:
            logger.debug("Position %s bereits geschlossen — überspringe.", pos.proposal_id)
            continue

        closed_ids.append(pos.proposal_id)
        logger.info(
            "Position geschlossen: %s %s fill=%.4f pnl=%.4f (%s)",
            pos.symbol, pos.side, fill_price, pnl, filled_type,
        )

        if on_close is not None:
            try:
                on_close(pos, fill_price, pnl)
            except Exception as exc:
                logger.warning("on_close Callback fehlgeschlagen: %s", exc)

    return closed_ids


def cancel_stop_orders(pos: OpenPosition, cfg: Config) -> None:
    """Cancelt Stop- und Target-Orders einer Position (bei manuellem Close).

    Fail-safe: Fehler werden geloggt, nicht weitergeworfen.
    """
    if not pos.stop_order_id and not pos.target_order_id:
        return

    broker = make_broker(cfg)
    for order_id in filter(None, [pos.stop_order_id, pos.target_order_id]):
        try:
            broker.cancel_order(pos.symbol, order_id)
            logger.info("Order %s für %s gecancelt.", order_id, pos.symbol)
        except Exception as exc:
            logger.warning("Cancel %s fehlgeschlagen: %s", order_id, exc)


# ── Hintergrund-Thread ────────────────────────────────────────────────────────

class StopMonitor:
    """Hintergrund-Thread, der in regelmässigen Abständen sync_once() aufruft.

    - Intervall: konfigurierbar (Standard: 15 s)
    - Fail-safe: Exceptions im Sync-Zyklus werden geloggt, nie weitergeworfen
    - Restart-sicher: Stop-Orders auf Exchange überleben; der Monitor nimmt
      den State beim nächsten Zyklus wieder auf.
    """

    def __init__(
        self,
        cfg: Config,
        state: Optional[StateStore] = None,
        interval_s: float = 15.0,
        on_close: Optional[Callable[[OpenPosition, float, float], None]] = None,
    ):
        self._cfg = cfg
        self._state = state or get_state_store()
        self._interval_s = interval_s
        self._on_close = on_close
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="StopMonitor",
            daemon=True,
        )
        self._thread.start()
        logger.info("StopMonitor gestartet (Intervall: %.0fs)", self._interval_s)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval_s + 2)
        logger.info("StopMonitor gestoppt.")

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                closed = sync_once(self._state, self._cfg, on_close=self._on_close)
                if closed:
                    logger.debug("Sync: %d Position(en) geschlossen: %s", len(closed), closed)
            except Exception as exc:
                # Fail-safe: Monitor crasht nicht — Exchange-Trigger läuft weiter
                logger.error("StopMonitor Fehler (wird ignoriert): %s", exc)
            self._stop_event.wait(self._interval_s)