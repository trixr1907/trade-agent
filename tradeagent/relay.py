"""Bitget-Relay: die letzte fail-geschlossene Schicht vor echtem Kapital.

Direktive: prompts/phase4-directive-bitget-relay.md

Ablauf pro Intent (proposal_id):
1. Gates: Halt-Status, Seite, Symbol-Mapping + Allowlist, Notional-Limit,
   Positionsanzahl, Lage von Stop/Target, Stop-Abstand vor der Liquidation.
2. Beleg 'entry' reservieren (Idempotenz). Existiert er schon, wird nichts gesendet.
3. Margin-Modus und Hebel für das Symbol setzen.
4. Market-Entry mit deterministischer clientOid.
5. Stop (pos_loss) und Target (pos_profit) als getrennte Orders.
6. Scheitert der Stop: Notschliessung (reduceOnly), halt(), Alarm.

Der Relay trifft keine Handelsentscheidung und ersetzt keine Freigabe.
Annahme: Das Bitget-Konto läuft im One-Way-Modus (nicht hedged).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional

import ccxt

from .config import Config
from .control import ControlPlane, ControlState, TransitionError
from .models import ExecutionResult
from .state import StateStore
from .universe import live_symbol_allowed

logger = logging.getLogger(__name__)

RELAY_ACTOR = "relay"

# Der Stop muss innerhalb dieses Anteils der groben Liquidationsdistanz (1 / Hebel)
# liegen. Bei 3x: Liquidation grob bei 33 %, Stop also höchstens 16,7 % vom Entry.
# Der Puffer deckt Maintenance-Margin, Gebühren und Slippage ab.
LIQUIDATION_BUFFER = 0.5

ROLE_ENTRY = "entry"
ROLE_STOP = "stop"
ROLE_TARGET = "target"
ROLE_EMERGENCY = "emergency_close"

_MAX_REASON_LEN = 500

_SIDE_MAP = {"buy": "buy", "long": "buy", "sell": "sell", "short": "sell"}


class AlarmsOpenError(Exception):
    """resume() verweigert, solange unbestätigte Alarme existieren."""

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(
            f"Resume verweigert: {count} unbestätigte(r) Alarm(e). Erst bestätigen, dann fortsetzen."
        )


# ── Reine Hilfsfunktionen ─────────────────────────────────────────────────────

def to_perp_symbol(symbol: str) -> str:
    """Mappt ein Engine-Symbol auf das USDT-Perp-Format: BTC/USDT -> BTC/USDT:USDT."""
    s = (symbol or "").strip().upper()
    if "/" not in s or s.startswith("/") or s.endswith("/"):
        raise ValueError(f"Symbol {symbol!r} ungültig (erwartet BASE/QUOTE).")
    if ":" in s:
        return s
    quote = s.split("/", 1)[1]
    return f"{s}:{quote}"


def client_order_id(intent_id: str, role: str) -> str:
    """Deterministische clientOid pro (Intent, Rolle). Gleicher Input ergibt gleiche ID."""
    digest = hashlib.sha256(f"{intent_id}:{role}".encode("utf-8")).hexdigest()
    return f"ta{digest[:30]}"


def normalize_side(side: Optional[str]) -> Optional[str]:
    """buy/long -> buy, sell/short -> sell, alles andere -> None."""
    return _SIDE_MAP.get((side or "").strip().lower())


def stop_clear_of_liquidation(
    price: float, stop_price: float, leverage: int, buffer: float = LIQUIDATION_BUFFER
) -> bool:
    """True, wenn der Stop-Abstand klar vor der groben Liquidationsdistanz liegt."""
    if price <= 0 or leverage < 1:
        return False
    return abs(price - stop_price) / price < buffer / leverage


def _protection_error(
    side: str, price: float, stop_price: float, target_price: Optional[float]
) -> Optional[str]:
    if side == "buy":
        if not stop_price < price:
            return "Stop muss bei buy unter dem Entry liegen."
        if target_price is not None and not target_price > price:
            return "Target muss bei buy über dem Entry liegen."
    else:
        if not stop_price > price:
            return "Stop muss bei sell über dem Entry liegen."
        if target_price is not None and not target_price < price:
            return "Target muss bei sell unter dem Entry liegen."
    return None


def _positive(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


def resume_trading(state: StateStore, actor: str, reason: str = "") -> ControlState:
    """Resume nur ohne offene Alarme (Direktive). Wirft AlarmsOpenError oder TransitionError."""
    pending = state.open_alarms()
    if pending:
        raise AlarmsOpenError(len(pending))
    return ControlPlane(state).resume(actor, reason)


def _build_exchange(cfg: Config) -> Any:
    if not cfg.exchange.has_live_credentials:
        raise RuntimeError("Relay ohne API-Keys nicht möglich (fail-closed).")
    ex_cls = getattr(ccxt, cfg.exchange.exchange)
    exchange = ex_cls(
        {
            "apiKey": cfg.exchange.api_key,
            "secret": cfg.exchange.api_secret,
            "password": cfg.exchange.api_passphrase,
            "enableRateLimit": True,
            "options": {"defaultType": "swap"},
        }
    )
    # Demo: CCXT sendet dann PAPTRADING: 1 bei jedem Request (gleiche Domain, gleiche Symbole).
    exchange.set_sandbox_mode(bool(cfg.exchange.demo))
    return exchange


# ── Relay ─────────────────────────────────────────────────────────────────────

class BitgetRelay:
    """Sendet freigegebene Entries an Bitget-Perpetuals, fail-geschlossen und idempotent."""

    def __init__(self, cfg: Config, state: StateStore, exchange: Any = None) -> None:
        self.cfg = cfg
        self.state = state
        self.exchange = exchange if exchange is not None else _build_exchange(cfg)

    def place_entry(
        self,
        intent_id: str,
        symbol: str,
        side: str,
        qty: float,
        price: Optional[float],
        stop_price: Optional[float],
        target_price: Optional[float],
    ) -> ExecutionResult:
        """Entry + Schutz-Orders für einen freigegebenen Intent.

        Status im Ergebnis:
        - rejected:         nichts offen auf der Exchange (oder Ausgang unbekannt, dann HALTED)
        - filled/submitted: Entry offen, Stop platziert
        - emergency_closed: Stop gescheitert, Position notgeschlossen, HALTED
        - unprotected:      Stop und Notschliessung gescheitert, HALTED, manuell eingreifen
        """
        def reject(error: str, notional: float = 0.0) -> ExecutionResult:
            return self._result(intent_id, symbol, side, qty, price or 0.0, notional, "", "rejected", {"error": error})

        # ── Gates (nichts wird gesendet) ──────────────────────────────────────
        if ControlPlane(self.state).is_halted():
            return reject("Trading ist angehalten (HALTED).")
        if not intent_id:
            return reject("Intent-ID fehlt.")
        norm_side = normalize_side(side)
        if norm_side is None:
            return reject(f"Seite {side!r} ungültig (erlaubt: buy, sell).")
        try:
            perp = to_perp_symbol(symbol)
        except ValueError as exc:
            return reject(str(exc))
        if not live_symbol_allowed(self.cfg, perp):
            return reject(f"Symbol {perp} nicht in der Allowlist.")
        if price is None or price <= 0 or qty <= 0:
            return reject("Preis und Menge müssen > 0 sein.")
        notional = qty * price
        if notional > self.cfg.live_limits.max_position_notional_usd:
            return reject("Positionsgröße über Limit.", notional)
        if self.state.open_count() >= self.cfg.live_limits.max_open_positions:
            return reject("max_open_positions erreicht.", notional)
        if self.state.open_position_for_symbol(symbol) is not None:
            return reject("Symbol hat bereits offene Position.", notional)
        if stop_price is None:
            return reject("Kein Stop-Preis: Entry ohne Schutz-Stop ist nicht erlaubt.", notional)
        geometry_error = _protection_error(norm_side, price, stop_price, target_price)
        if geometry_error:
            return reject(geometry_error, notional)
        if not stop_clear_of_liquidation(price, stop_price, self.cfg.exchange.leverage):
            return reject(
                f"Stop-Abstand liegt nicht klar vor der Liquidation bei {self.cfg.exchange.leverage}x.",
                notional,
            )

        # ── Idempotenz ────────────────────────────────────────────────────────
        entry_coid = client_order_id(intent_id, ROLE_ENTRY)
        try:
            receipt, reserved = self.state.reserve_receipt(
                intent_id, ROLE_ENTRY, entry_coid, perp, norm_side, qty
            )
        except Exception as exc:
            return reject(f"Beleg nicht reservierbar: {exc}", notional)
        if not reserved:
            return reject(
                f"Beleg für diesen Intent existiert bereits (Status {receipt.get('status')}). Keine zweite Order.",
                notional,
            )

        # ── Margin-Modus und Hebel ────────────────────────────────────────────
        try:
            self._configure_symbol(perp, norm_side)
        except Exception as exc:
            self.state.update_receipt(
                intent_id, ROLE_ENTRY, "rejected", reason=f"Margin/Hebel: {exc}"[:_MAX_REASON_LEN]
            )
            return reject(f"Margin-Modus/Hebel nicht gesetzt: {exc}", notional)

        # ── Entry senden ──────────────────────────────────────────────────────
        entry_params = {"clientOid": entry_coid, "marginMode": self.cfg.exchange.margin_mode}
        try:
            order = self.exchange.create_order(perp, "market", norm_side, qty, None, entry_params)
        except ccxt.NetworkError as exc:
            # Ausgang unbekannt: Die Order kann trotzdem angekommen sein.
            found = self._lookup_by_client_id(perp, entry_coid)
            if found is None:
                message = (
                    f"Entry {perp} ({intent_id}): Ausgang unbekannt nach Netzwerkfehler ({exc}). "
                    "Position auf Bitget prüfen. Trading angehalten."
                )
                self.state.update_receipt(intent_id, ROLE_ENTRY, "unknown", reason=str(exc)[:_MAX_REASON_LEN])
                self._halt_with_alarm("entry_unknown", message, intent_id)
                return reject(message, notional)
            order = found
        except Exception as exc:
            # Exchange hat geantwortet und abgelehnt: nichts offen, Retry erlaubt.
            self.state.update_receipt(intent_id, ROLE_ENTRY, "rejected", reason=str(exc)[:_MAX_REASON_LEN])
            return reject(f"Entry von Bitget abgelehnt: {exc}", notional)

        entry_order_id = str(order.get("id") or "")
        order = self._refresh(perp, entry_order_id, order)
        fill_price = _positive(order.get("average")) or _positive(order.get("price")) or price
        filled_qty = _positive(order.get("filled")) or qty
        entry_status = "filled" if order.get("status") == "closed" else "submitted"
        self.state.update_receipt(
            intent_id, ROLE_ENTRY, entry_status, order_id=entry_order_id, fill_price=fill_price
        )

        # ── Schutz-Orders ─────────────────────────────────────────────────────
        close_side = "sell" if norm_side == "buy" else "buy"
        stop_id = self._place_protective(
            intent_id, ROLE_STOP, perp, close_side, filled_qty, {"stopLossPrice": stop_price}
        )
        if stop_id is None:
            return self._emergency_close(
                intent_id, symbol, perp, norm_side, close_side, filled_qty, fill_price, entry_order_id
            )

        target_id: Optional[str] = None
        if target_price is not None:
            target_id = self._place_protective(
                intent_id, ROLE_TARGET, perp, close_side, filled_qty, {"takeProfitPrice": target_price}
            )
            if target_id is None:
                self.state.raise_alarm(
                    "target_failed",
                    f"Target für {perp} ({intent_id}) nicht platziert. Stop ist aktiv, Position bleibt offen.",
                    intent_id,
                )

        return self._result(
            intent_id,
            symbol,
            norm_side,
            filled_qty,
            fill_price,
            filled_qty * fill_price,
            entry_order_id,
            entry_status,
            {
                "perp_symbol": perp,
                "client_order_id": entry_coid,
                "fill_price": fill_price,
                "stop_order_id": stop_id,
                "target_order_id": target_id,
            },
        )

    # ── Interna ───────────────────────────────────────────────────────────────

    def _configure_symbol(self, perp: str, side: str) -> None:
        mode = self.cfg.exchange.margin_mode
        self.exchange.set_margin_mode(mode, perp)
        params: Dict[str, Any] = {}
        if mode == "isolated":
            params["holdSide"] = "long" if side == "buy" else "short"
        self.exchange.set_leverage(self.cfg.exchange.leverage, perp, params)

    def _lookup_by_client_id(self, perp: str, coid: str) -> Optional[Dict[str, Any]]:
        try:
            found = self.exchange.fetch_order("", perp, {"clientOrderId": coid})
        except Exception:
            return None
        return found if isinstance(found, dict) and found.get("id") else None

    def _refresh(self, perp: str, order_id: str, order: Dict[str, Any]) -> Dict[str, Any]:
        if not order_id:
            return order
        try:
            fresh = self.exchange.fetch_order(order_id, perp)
        except Exception as exc:
            logger.warning("fetch_order für %s fehlgeschlagen, nutze Create-Antwort: %s", order_id, exc)
            return order
        return fresh if isinstance(fresh, dict) else order

    def _place_protective(
        self,
        intent_id: str,
        role: str,
        perp: str,
        close_side: str,
        qty: float,
        trigger_params: Dict[str, Any],
    ) -> Optional[str]:
        coid = client_order_id(intent_id, role)
        try:
            receipt, reserved = self.state.reserve_receipt(intent_id, role, coid, perp, close_side, qty)
        except Exception as exc:
            logger.error("Beleg %s/%s nicht reservierbar: %s", intent_id, role, exc)
            return None
        if not reserved:
            existing = receipt.get("order_id")
            return str(existing) if existing else None

        params = dict(trigger_params)
        params["clientOid"] = coid
        try:
            order = self.exchange.create_order(perp, "market", close_side, qty, None, params)
        except Exception as exc:
            self.state.update_receipt(intent_id, role, "rejected", reason=str(exc)[:_MAX_REASON_LEN])
            logger.error("%s für %s (%s) gescheitert: %s", role, perp, intent_id, exc)
            return None

        order_id = str(order.get("id") or "")
        if not order_id:
            self.state.update_receipt(intent_id, role, "rejected", reason="Keine Order-ID in der Antwort.")
            return None
        self.state.update_receipt(intent_id, role, "submitted", order_id=order_id)
        return order_id

    def _emergency_close(
        self,
        intent_id: str,
        symbol: str,
        perp: str,
        side: str,
        close_side: str,
        qty: float,
        fill_price: float,
        entry_order_id: str,
    ) -> ExecutionResult:
        coid = client_order_id(intent_id, ROLE_EMERGENCY)
        close_order_id = ""
        error = ""
        try:
            self.state.reserve_receipt(intent_id, ROLE_EMERGENCY, coid, perp, close_side, qty)
            order = self.exchange.create_order(
                perp,
                "market",
                close_side,
                qty,
                None,
                {"reduceOnly": True, "clientOid": coid, "marginMode": self.cfg.exchange.margin_mode},
            )
            close_order_id = str(order.get("id") or "")
            self.state.update_receipt(intent_id, ROLE_EMERGENCY, "submitted", order_id=close_order_id)
        except Exception as exc:
            error = str(exc)[:_MAX_REASON_LEN]
            try:
                self.state.update_receipt(intent_id, ROLE_EMERGENCY, "rejected", reason=error)
            except Exception:
                logger.exception("Beleg für Notschliessung %s nicht aktualisierbar", intent_id)

        if not error:
            kind = "stop_failed"
            status = "emergency_closed"
            message = (
                f"Stop für {perp} ({intent_id}) nicht platziert. Position per Market-Order "
                "(reduceOnly) geschlossen. Trading angehalten."
            )
        else:
            kind = "emergency_close_failed"
            status = "unprotected"
            message = (
                f"Stop für {perp} ({intent_id}) nicht platziert und Notschliessung gescheitert ({error}). "
                "Position ist OHNE Schutz-Stop offen. Sofort manuell schliessen. Trading angehalten."
            )
        self._halt_with_alarm(kind, message, intent_id)
        return self._result(
            intent_id,
            symbol,
            side,
            qty,
            fill_price,
            qty * fill_price,
            entry_order_id,
            status,
            {"error": message, "perp_symbol": perp, "emergency_order_id": close_order_id or None},
        )

    def _halt_with_alarm(self, kind: str, message: str, ref: str) -> None:
        logger.critical(message)
        try:
            ControlPlane(self.state).halt(RELAY_ACTOR, message)
        except TransitionError:
            pass  # bereits HALTED
        except Exception:
            logger.exception("halt() fehlgeschlagen")
        try:
            self.state.raise_alarm(kind, message, ref)
        except Exception:
            logger.exception("Alarm %s nicht speicherbar", kind)

    @staticmethod
    def _result(
        intent_id: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        notional: float,
        order_id: str,
        status: str,
        raw: Dict[str, Any],
    ) -> ExecutionResult:
        return ExecutionResult(
            proposal_id=intent_id,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            notional_usd=notional,
            mode="live",
            order_id=order_id,
            status=status,
            raw=raw,
        )