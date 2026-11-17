"""Globale Control Plane: Halt/Resume-Statemachine für die Ausführung.

    RUNNING --halt()--> HALTED --resume()--> RUNNING

Jede andere Transition wirft TransitionError (API: 409). Der Zustand liegt im
StateStore (SQLite) und überlebt einen Neustart. Ist er nicht lesbar oder
unbekannt, gilt HALTED (fail-closed).
"""
from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Dict, Optional

_MAX_REASON_LEN = 500
_MAX_ACTOR_LEN = 100


class TradingState(enum.Enum):
    RUNNING = "running"
    HALTED = "halted"


@dataclass
class ControlState:
    state: TradingState
    changed_by: Optional[str] = None
    changed_at: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "changed_by": self.changed_by,
            "changed_at": self.changed_at,
            "reason": self.reason,
        }


class TransitionError(Exception):
    """Transition ist im aktuellen Zustand nicht erlaubt."""

    def __init__(self, current: TradingState, attempted: str) -> None:
        self.current = current
        self.attempted = attempted
        super().__init__(
            f"Transition '{attempted}' im Zustand {current.value.upper()} nicht erlaubt"
        )


class ControlPlane:
    """Globaler Halt/Resume-Statemachine, persistiert via StateStore."""

    def __init__(self, state_store: Any) -> None:
        self._store = state_store

    def get_state(self) -> ControlState:
        try:
            row = self._store.get_control_state()
        except Exception:
            return ControlState(TradingState.HALTED, reason="Control-State nicht lesbar (fail-closed)")
        if row is None:
            return ControlState(TradingState.RUNNING)   # nie gesetzt -> Standard
        try:
            state = TradingState(row.get("state"))
            return ControlState(state, row.get("changed_by"), row.get("changed_at"), row.get("reason"))
        except Exception:
            return ControlState(TradingState.HALTED, reason="Unbekannter Control-State (fail-closed)")

    def is_halted(self) -> bool:
        return self.get_state().state is not TradingState.RUNNING

    def halt(self, actor: str, reason: str = "") -> ControlState:
        return self._transition(TradingState.RUNNING, TradingState.HALTED, "halt", actor, reason)

    def resume(self, actor: str, reason: str = "") -> ControlState:
        return self._transition(TradingState.HALTED, TradingState.RUNNING, "resume", actor, reason)

    def _transition(
        self, source: TradingState, target: TradingState, name: str, actor: str, reason: str
    ) -> ControlState:
        actor = (actor or "").strip()[:_MAX_ACTOR_LEN] or "unknown"
        clean_reason = (reason or "").strip()[:_MAX_REASON_LEN] or None
        ok = self._store.transition_control_state(
            expected=source.value,
            new_state=target.value,
            changed_by=actor,
            reason=clean_reason,
            default_state=TradingState.RUNNING.value,
        )
        if not ok:
            raise TransitionError(self.get_state().state, name)
        return self.get_state()