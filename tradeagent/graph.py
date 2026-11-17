"""Approval-Workflow als Zustandsgraph (LangGraph).

Flow: signal -> proposal -> (risk-check) -> approval -> execution

**fail-closed:** Eine Ausführung ist nur über die `approve`-Transition
möglich, die eine gültige, verifizierte Freigabe erfordert. Ohne Freigabe
bleibt der Antrag im Zustand `proposed` und wird nicht ausgeführt.
"""
from __future__ import annotations

import uuid
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
    signal: Signal, cfg: Config, *, account_balance_usd: float = 1000.0,
    current_drawdown_pct: float = 0.0,
) -> Optional[TradeProposal]:
    """Erstellt einen Antrag aus Signal + Risiko-Sizing (fail-closed)."""
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
    current_drawdown_pct: float = 0.0,
) -> list[TradeProposal]:
    """Läuft die Signale durch Risiko-Prüfung und legt Anträge an."""
    proposals: list[TradeProposal] = []
    for sig in signals:
        proposal = build_proposal(
            sig, cfg, account_balance_usd=account_balance_usd,
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


def reject(store: ProposalStore, proposal_id: str, reason: str = "") -> Optional[TradeProposal]:
    proposal = store.get(proposal_id)
    if proposal is None:
        return None
    if proposal.state != ApprovalState.PROPOSED:
        return proposal
    proposal.state = ApprovalState.REJECTED
    return proposal


def execute(
    store: ProposalStore, proposal_id: str, cfg: Config
) -> Optional[ExecutionResult]:
    """Ausführungs-Transition. Erfordert status=approved (fail-closed)."""
    proposal = store.get(proposal_id)
    if proposal is None:
        return None
    if proposal.state != ApprovalState.APPROVED:
        return None  # Nicht freigegeben -> nicht ausführbar

    qty = proposal.proposed_size_usd / proposal.signal.price
    broker = make_broker(cfg)
    result = broker.place_order(
        symbol=proposal.signal.symbol,
        side=proposal.signal.direction.value,
        qty=qty,
        price=proposal.signal.price,
    )
    result.proposal_id = proposal_id

    if result.status in ("filled", "submitted"):
        proposal.state = ApprovalState.EXECUTED
        proposal.executed_at = now_utc()
        proposal.fill = result.raw
    return result
