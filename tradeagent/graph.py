"""Approval-Workflow als Zustandsgraph (LangGraph).

Flow: signal -> proposal -> (risk-check) -> approval -> execution

**fail-closed:** Eine Ausführung ist nur über die `approve`-Transition
möglich, die eine gültige, verifizierte Freigabe erfordert. Ohne Freigabe
bleibt der Antrag im Zustand `proposed` und wird nicht ausgeführt.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .config import Config
from .execution import make_broker
from .models import (
    ApprovalState,
    ExecutionResult,
    Signal,
    TradeProposal,
    now_utc,
)
from .risk import position_size_usd, validate_proposal
from .control import ControlPlane
from .state import OpenPosition, StateStore, get_state_store


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProposalStore:
    """In-Memory-Lager für Anträge (für das Dashboard & Audit)."""

    def __init__(self) -> None:
        self._items: Dict[str, TradeProposal] = {}

    def add(self, proposal: TradeProposal) -> None:
        self._items[proposal.id] = proposal

    def get(self, proposal_id: str) -> Optional[TradeProposal]:
        return self._items.get(proposal_id)

    def list(self) -> list[TradeProposal]:
        return list(self._items.values())


def build_proposal(
    signal: Signal,
    cfg: Config,
    *,
    account_balance_usd: float = 1000.0,
    state: Optional[StateStore] = None,
    current_drawdown_pct: Optional[float] = None,
) -> Optional[TradeProposal]:
    """Erstellt einen Antrag aus Signal + Risiko-Sizing (fail-closed).

    Drawdown wird aus dem StateStore geladen, wenn nicht explizit übergeben.
    """
    if state is None:
        state = get_state_store()

    # Drawdown aus StateStore laden — kein 0.0-Default mehr
    if current_drawdown_pct is None:
        current_drawdown_pct = state.current_drawdown_pct()

    size = position_size_usd(
        signal,
        account_balance_usd=account_balance_usd,
        risk_pct=0.02,          # 2% Risiko pro Trade
        kelly_scale=cfg.risk.kelly_fraction,
    )
    ok, err = validate_proposal(
        signal,
        position_usd=size,
        account_balance_usd=account_balance_usd,
        risk_cfg=cfg.risk,
        current_drawdown_pct=current_drawdown_pct,
    )
    if not ok:
        return None

    return TradeProposal(
        id=f"prop-{uuid.uuid4().hex[:8]}",
        signal=signal,
        proposed_size_usd=size,
        stop_price=signal.stop_price,
        target_price=signal.target_price,
        risk_usd=round(account_balance_usd * 0.02, 2),
    )


def run_pipeline(
    signals: list[Signal],
    cfg: Config,
    store: ProposalStore,
    *,
    account_balance_usd: float = 1000.0,
    state: Optional[StateStore] = None,
    # current_drawdown_pct wird nur noch für Tests/Overrides akzeptiert;
    # im normalen Pfad kommt der Wert aus dem StateStore.
    current_drawdown_pct: Optional[float] = None,
) -> list[TradeProposal]:
    """Läuft die Signale durch Risiko-Prüfung und legt Anträge an."""
    if state is None:
        state = get_state_store()

    proposals: list[TradeProposal] = []
    for sig in signals:
        proposal = build_proposal(
            sig,
            cfg,
            account_balance_usd=account_balance_usd,
            state=state,
            current_drawdown_pct=current_drawdown_pct,
        )
        if proposal is not None:
            store.add(proposal)
            proposals.append(proposal)
    return proposals


def approve(
    store: ProposalStore, proposal_id: str, approved_by: str
) -> Optional[TradeProposal]:
    """Freigabe-Transition. Nur dieser Endpunkt setzt den Zustand approved."""
    proposal = store.get(proposal_id)
    if proposal is None:
        return None
    if proposal.state != ApprovalState.PROPOSED:
        return proposal  # fail-closed: nur proposed kann freigegeben werden

    proposal.state = ApprovalState.APPROVED
    proposal.approved_by = approved_by
    proposal.approved_at = now_utc()
    return proposal


def reject(
    store: ProposalStore, proposal_id: str, reason: str = ""
) -> Optional[TradeProposal]:
    proposal = store.get(proposal_id)
    if proposal is None:
        return None
    if proposal.state != ApprovalState.PROPOSED:
        return proposal
    proposal.state = ApprovalState.REJECTED
    return proposal


def execute(
    store: ProposalStore,
    proposal_id: str,
    cfg: Config,
    state: Optional[StateStore] = None,
) -> Optional[ExecutionResult]:
    """Ausführungs-Transition. Erfordert status=approved (fail-closed).

    Enforced vor broker.place_order():
    1. max_open_positions-Limit
    2. Keine Doppelposition im selben Symbol
    Nach Fill: Position in StateStore persistiert.
    """
    proposal = store.get(proposal_id)
    if proposal is None:
        return None
    if proposal.state != ApprovalState.APPROVED:
        return None  # Nicht freigegeben -> nicht ausführbar

    if state is None:
        state = get_state_store()

    symbol = proposal.signal.symbol

    # ── Fail-closed: globale Control Plane (Halt/Resume) ──────────────────────
    if ControlPlane(state).is_halted():
        return ExecutionResult(
            proposal_id=proposal_id,
            symbol=symbol,
            side=proposal.signal.direction.value,
            qty=0.0,
            price=proposal.signal.price,
            notional_usd=0.0,
            mode="paper" if not cfg.trading.is_live else "live",
            order_id="",
            status="rejected",
            raw={"error": "Trading gestoppt (Control Plane)"},
        )

    # ── Fail-closed: Positions-Limits ─────────────────────────────────────────
    if state.open_count() >= cfg.live_limits.max_open_positions:
        return ExecutionResult(
            proposal_id=proposal_id,
            symbol=symbol,
            side=proposal.signal.direction.value,
            qty=0.0,
            price=proposal.signal.price,
            notional_usd=0.0,
            mode="paper" if not cfg.trading.is_live else "live",
            order_id="",
            status="rejected",
            raw={"error": "Open-Positions-Limit erreicht"},
        )

    if state.open_position_for_symbol(symbol) is not None:
        return ExecutionResult(
            proposal_id=proposal_id,
            symbol=symbol,
            side=proposal.signal.direction.value,
            qty=0.0,
            price=proposal.signal.price,
            notional_usd=0.0,
            mode="paper" if not cfg.trading.is_live else "live",
            order_id="",
            status="rejected",
            raw={"error": "Symbol hat bereits offene Position"},
        )

    # ── Order platzieren ───────────────────────────────────────────────────────
    qty = proposal.proposed_size_usd / proposal.signal.price

    if cfg.trading.is_live:
        # Live: Entry, Stop und Target laufen über den fail-geschlossenen Relay.
        from .relay import BitgetRelay

        live_stop = proposal.stop_price or proposal.signal.stop_price
        live_target = proposal.target_price or proposal.signal.target_price
        try:
            relay = BitgetRelay(cfg, state)
        except Exception as exc:
            return ExecutionResult(
                proposal_id=proposal_id,
                symbol=symbol,
                side=proposal.signal.direction.value,
                qty=0.0,
                price=proposal.signal.price,
                notional_usd=0.0,
                mode="live",
                order_id="",
                status="rejected",
                raw={"error": f"Relay nicht verfügbar: {exc}"},
            )
        result = relay.place_entry(
            intent_id=proposal_id,
            symbol=symbol,
            side=proposal.signal.direction.value,
            qty=qty,
            price=proposal.signal.price,
            stop_price=live_stop,
            target_price=live_target,
        )
        # "unprotected": Position ist real offen (ohne Stop), muss im State sichtbar sein.
        if result.status in ("filled", "submitted", "unprotected"):
            live_pos = OpenPosition(
                proposal_id=proposal_id,
                symbol=symbol.upper(),
                side=proposal.signal.direction.value,
                qty=result.qty,
                entry_price=result.price,
                notional_usd=result.notional_usd,
                stop_price=live_stop,
                target_price=live_target,
                opened_at=_now_iso(),
            )
            live_pos.stop_order_id = result.raw.get("stop_order_id")
            live_pos.target_order_id = result.raw.get("target_order_id")
            state.add_position(live_pos)
            proposal.state = ApprovalState.EXECUTED
            proposal.executed_at = now_utc()
            proposal.fill = result.raw
        return result

    broker = make_broker(cfg)
    result = broker.place_order(
        symbol=symbol,
        side=proposal.signal.direction.value,
        qty=qty,
        price=proposal.signal.price,
    )
    result.proposal_id = proposal_id

    # ── Nach erfolgreichem Fill: Position persistieren ─────────────────────────
    if result.status in ("filled", "submitted"):
        pos = OpenPosition(
            proposal_id=proposal_id,
            symbol=symbol.upper(),
            side=proposal.signal.direction.value,
            qty=qty,
            entry_price=proposal.signal.price,
            notional_usd=result.notional_usd,
            stop_price=proposal.stop_price or proposal.signal.stop_price,
            target_price=proposal.target_price or proposal.signal.target_price,
            opened_at=_now_iso(),
        )
        # ── Stop/Target auf Exchange platzieren (fail-safe) ───────────────────
        stop_order_id: Optional[str] = None
        target_order_id: Optional[str] = None
        try:
            stop_order_id, target_order_id = broker.place_stop_orders(
                symbol=symbol,
                entry_side=proposal.signal.direction.value,
                qty=qty,
                stop_price=pos.stop_price,
                target_price=pos.target_price,
            )
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(
                "place_stop_orders fehlgeschlagen für %s: %s — Position bleibt ohne Exchange-Stop.",
                symbol, exc,
            )

        # Stop-IDs in Position speichern (auch wenn None)
        pos.stop_order_id = stop_order_id
        pos.target_order_id = target_order_id
        state.add_position(pos)
        proposal.state = ApprovalState.EXECUTED
        proposal.executed_at = now_utc()
        proposal.fill = result.raw

    return result