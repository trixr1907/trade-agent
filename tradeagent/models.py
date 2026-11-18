"""Datenmodelle für Signale, Anträge und Ausführung.

Alle Datenklassen sind explizit und serialisierbar — sie werden im
Dashboard und für das Audit verwendet.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class SignalDirection(enum.Enum):
    BUY = "buy"
    SELL = "sell"


class ApprovalState(enum.Enum):
    PROPOSED = "proposed"      # Signal erzeugt, Antrag offen
    APPROVED = "approved"      # freigegeben
    REJECTED = "rejected"
    EXECUTED = "executed"
    CANCELLED = "cancelled"


@dataclass
class TechnicalSnapshot:
    """Bereitet Rohdaten & Indikatoren für eine einzelne Symbol-Analyse vor."""

    symbol: str
    price: float
    atr: float
    rsi: float
    ema_fast: float
    ema_slow: float
    macd: float
    macd_signal: float
    # Struktur (SMC-Annahme): Higher-High / Lower-Low als boolesche Struktur
    structure: str  # "uptrend" | "downtrend" | "range"
    volume_avg_20: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StructureEvent:
    """BOS oder CHoCH: Close bricht ein bestätigtes Swing-Level."""

    kind: str          # "bos" | "choch"
    direction: str     # "bullish" | "bearish"
    index: int         # Bar-Index (Position im DataFrame), an dem der Close das Level bricht
    level: float       # gebrochenes Swing-Level


@dataclass
class Zone:
    """Fair Value Gap oder Orderblock."""

    kind: str          # "fvg" | "orderblock"
    direction: str     # "bullish" | "bearish"
    index: int         # Bar-Index der Entstehung
    bottom: float
    top: float
    mitigated: bool


@dataclass
class LiquiditySweep:
    """Docht über/unter ein Swing-Level mit Close zurück. Kein Bruch."""

    direction: str     # "bullish" = Sell-Side-Liquidität unter Swing-Low genommen; "bearish" = Buy-Side über Swing-High
    index: int
    level: float       # das gesweepte Swing-Level


@dataclass
class SMCResult:
    """Ergebnis der SMC-Analyse, bezogen auf die Richtung eines Signals."""

    last_event: Optional[StructureEvent]
    events: List[StructureEvent]            # höchstens die letzten 5
    fvgs: List[Zone]                        # höchstens die 3 jüngsten, nicht mitigiert
    order_blocks: List[Zone]                # höchstens die 3 jüngsten, nicht mitigiert
    liquidity_sweeps: List[LiquiditySweep]  # höchstens die letzten 3
    confluence_score: float                 # 0..1, bezogen auf die Signalrichtung
    veto: bool
    reasons: List[str]


@dataclass
class Signal:
    """Ein eindeutiges Signal der deterministischen Engine."""

    symbol: str
    direction: SignalDirection
    price: float
    score: int                     # 0-100 Konfidenz
    reasons: List[str] = field(default_factory=list)
    stop_price: Optional[float] = None
    target_price: Optional[float] = None
    atr: float = 0.0
    created_at: datetime = field(default_factory=now_utc)
    smc: Optional[SMCResult] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["direction"] = self.direction.value
        d["created_at"] = self.created_at.isoformat()
        return d


@dataclass
class TradeProposal:
    """Ein Antrag, der aus einem Signal + Risiko-Sizing entsteht.

    Enthält alles, was für die Freigabe nötig ist — undNothing mehr.
    """

    id: str
    signal: Signal
    proposed_size_usd: float
    stop_price: float
    target_price: float
    risk_usd: float
    state: ApprovalState = ApprovalState.PROPOSED
    approved_by: Optional[str] = None
    approved_at: Optional[datetime] = None
    executed_at: Optional[datetime] = None
    fill: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["signal"] = self.signal.to_dict()
        d["state"] = self.state.value
        d["approved_at"] = self.approved_at.isoformat() if self.approved_at else None
        d["executed_at"] = self.executed_at.isoformat() if self.executed_at else None
        return d


@dataclass
class ExecutionResult:
    """Ergebnis einer Ausführung (Paper oder Live)."""

    proposal_id: str
    symbol: str
    side: str
    qty: float
    price: float
    notional_usd: float
    mode: str          # "paper" | "live"
    order_id: str
    status: str
    raw: Dict[str, Any] = field(default_factory=dict)
