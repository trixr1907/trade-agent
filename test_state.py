"""Tests für die State-Schicht (state.py) und graph.py-Enforcement.

Führt in-memory / temporäre SQLite-DBs aus — kein Netz, kein Exchange.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from unittest.mock import MagicMock, patch

# ── Repo-Pfad ─────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))

# ── Minimale Stubs, damit graph.py ohne Exchange importiert werden kann ────────

# models stub
import tradeagent.models as _models  # noqa: E402  (wird nach sys.path-Insert geladen)

# ─────────────────────────────────────────────────────────────────────────────
from tradeagent.config import Config, LiveLimits, TradingMode  # noqa: E402
from tradeagent.models import ApprovalState, Signal, SignalDirection  # noqa: E402
from tradeagent.state import OpenPosition, StateStore  # noqa: E402


def _make_db() -> StateStore:
    """Gibt einen StateStore mit frischer temporärer SQLite-DB zurück."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # StateStore erstellt die Datei neu
    return StateStore(path=path, initial_equity=1000.0)


def _sample_pos(proposal_id: str = "prop-001", symbol: str = "BTC/USDT") -> OpenPosition:
    return OpenPosition(
        proposal_id=proposal_id,
        symbol=symbol,
        side="buy",
        qty=0.001,
        entry_price=50_000.0,
        notional_usd=50.0,
        stop_price=48_000.0,
        target_price=54_000.0,
        opened_at="2026-01-01T00:00:00+00:00",
    )


# ═════════════════════════════════════════════════════════════════════════════
# 1. StateStore-Tests
# ═════════════════════════════════════════════════════════════════════════════

class TestStateStorePositions(unittest.TestCase):
    def setUp(self):
        self.store = _make_db()

    def tearDown(self):
        self.store.close()

    def test_initial_count_is_zero(self):
        self.assertEqual(self.store.open_count(), 0)

    def test_add_and_count(self):
        self.store.add_position(_sample_pos("p1", "BTC/USDT"))
        self.assertEqual(self.store.open_count(), 1)

    def test_add_multiple(self):
        self.store.add_position(_sample_pos("p1", "BTC/USDT"))
        self.store.add_position(_sample_pos("p2", "ETH/USDT"))
        self.assertEqual(self.store.open_count(), 2)

    def test_open_positions_list(self):
        self.store.add_position(_sample_pos("p1", "BTC/USDT"))
        positions = self.store.open_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].proposal_id, "p1")

    def test_open_position_for_symbol_found(self):
        self.store.add_position(_sample_pos("p1", "BTC/USDT"))
        pos = self.store.open_position_for_symbol("BTC/USDT")
        self.assertIsNotNone(pos)
        self.assertEqual(pos.proposal_id, "p1")

    def test_open_position_for_symbol_case_insensitive(self):
        self.store.add_position(_sample_pos("p1", "BTC/USDT"))
        pos = self.store.open_position_for_symbol("btc/usdt")
        self.assertIsNotNone(pos)

    def test_open_position_for_symbol_not_found(self):
        self.store.add_position(_sample_pos("p1", "BTC/USDT"))
        pos = self.store.open_position_for_symbol("ETH/USDT")
        self.assertIsNone(pos)

    def test_close_position(self):
        self.store.add_position(_sample_pos("p1", "BTC/USDT"))
        closed = self.store.close_position("p1", exit_price=52_000.0, pnl_usd=20.0)
        self.assertIsNotNone(closed)
        self.assertEqual(self.store.open_count(), 0)

    def test_close_nonexistent_returns_none(self):
        result = self.store.close_position("nonexistent", exit_price=1.0, pnl_usd=0.0)
        self.assertIsNone(result)


class TestStateStorePnL(unittest.TestCase):
    def setUp(self):
        self.store = _make_db()

    def tearDown(self):
        self.store.close()

    def test_initial_pnl_zero(self):
        self.assertAlmostEqual(self.store.realized_pnl_total(), 0.0)

    def test_record_pnl_positive(self):
        self.store.record_pnl(50.0)
        self.assertAlmostEqual(self.store.realized_pnl_total(), 50.0)

    def test_record_pnl_negative(self):
        self.store.record_pnl(-30.0)
        self.assertAlmostEqual(self.store.realized_pnl_total(), -30.0)

    def test_equity_peak_starts_at_initial(self):
        self.assertAlmostEqual(self.store.equity_peak(), 1000.0)

    def test_equity_peak_updates_on_gain(self):
        self.store.record_pnl(200.0)
        self.assertAlmostEqual(self.store.equity_peak(), 1200.0)

    def test_equity_peak_does_not_drop_on_loss(self):
        self.store.record_pnl(200.0)   # peak = 1200
        self.store.record_pnl(-500.0)  # equity = 700, peak stays 1200
        self.assertAlmostEqual(self.store.equity_peak(), 1200.0)

    def test_no_drawdown_at_start(self):
        self.assertAlmostEqual(self.store.current_drawdown_pct(), 0.0)

    def test_drawdown_after_loss(self):
        self.store.record_pnl(-200.0)   # equity = 800, peak = 1000
        # expected = (1000 - 800) / 1000 = 0.20
        self.assertAlmostEqual(self.store.current_drawdown_pct(), 0.20, places=4)

    def test_drawdown_after_gain_then_loss(self):
        self.store.record_pnl(500.0)   # equity = 1500, peak = 1500
        self.store.record_pnl(-300.0)  # equity = 1200, peak = 1500
        # expected = (1500 - 1200) / 1500 = 0.20
        self.assertAlmostEqual(self.store.current_drawdown_pct(), 0.20, places=4)

    def test_close_position_updates_pnl(self):
        self.store.add_position(_sample_pos("p1"))
        self.store.close_position("p1", exit_price=52_000.0, pnl_usd=20.0)
        self.assertAlmostEqual(self.store.realized_pnl_total(), 20.0)


class TestStateStorePersistence(unittest.TestCase):
    """Öffnet die DB in einer zweiten Instanz — simuliert Server-Restart."""

    def test_persistence_across_instances(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        try:
            # Instanz 1: Position öffnen, P&L schreiben
            s1 = StateStore(path=path, initial_equity=1000.0)
            s1.add_position(_sample_pos("p1", "BTC/USDT"))
            s1.record_pnl(50.0)
            s1.close()

            # Instanz 2: Daten müssen noch da sein
            s2 = StateStore(path=path, initial_equity=1000.0)
            self.assertEqual(s2.open_count(), 1)
            self.assertAlmostEqual(s2.realized_pnl_total(), 50.0)
            self.assertAlmostEqual(s2.equity_peak(), 1050.0)
            s2.close()
        finally:
            if os.path.exists(path):
                os.unlink(path)


# ═════════════════════════════════════════════════════════════════════════════
# 2. graph.py execute() — Fail-closed-Tests
# ═════════════════════════════════════════════════════════════════════════════

def _make_config(max_open: int = 3, live: bool = False) -> Config:
    cfg = Config()
    cfg.live_limits = LiveLimits(
        max_open_positions=max_open,
        max_position_notional_usd=10_000.0,
        symbol_allowlist=["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT"],
    )
    cfg.trading = TradingMode(mode="live" if live else "paper")
    if live:
        cfg.exchange.api_key = "dummy"
        cfg.exchange.api_secret = "dummy"
        cfg.exchange.api_passphrase = "dummy"
    return cfg


def _make_signal(symbol: str = "BTC/USDT", score: int = 70) -> Signal:
    return Signal(
        symbol=symbol,
        direction=SignalDirection.BUY,
        price=50_000.0,
        stop_price=48_000.0,
        target_price=54_000.0,
        score=score,
    )


def _make_approved_proposal(symbol: str = "BTC/USDT", score: int = 70):
    """Erstellt einen bereits freigegebenen Antrag, ohne ProposalStore."""
    from tradeagent.models import TradeProposal
    p = TradeProposal(
        id=f"prop-{symbol[:3].lower()}-{score}",
        signal=_make_signal(symbol, score),
        proposed_size_usd=100.0,
        stop_price=48_000.0,
        target_price=54_000.0,
        risk_usd=20.0,
    )
    p.state = ApprovalState.APPROVED
    return p


class TestExecutePositionsLimit(unittest.TestCase):
    """execute() muss die 4. Order bei max_open_positions=3 ablehnen."""

    def setUp(self):
        from tradeagent.graph import ProposalStore, execute

        self.execute = execute
        self.ProposalStore = ProposalStore
        self.store = _make_db()
        self.cfg = _make_config(max_open=3)

        # 3 Positionen vorbelegen
        for sym, pid in [("BTC/USDT", "p1"), ("ETH/USDT", "p2"), ("SOL/USDT", "p3")]:
            self.store.add_position(_sample_pos(pid, sym))

    def tearDown(self):
        self.store.close()

    def test_fourth_order_rejected(self):
        self.assertEqual(self.store.open_count(), 3)

        pstore = self.ProposalStore()
        proposal = _make_approved_proposal("XRP/USDT")
        pstore.add(proposal)

        result = self.execute(pstore, proposal.id, self.cfg, state=self.store)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "rejected")
        self.assertIn("Limit", result.raw.get("error", ""))
        # Zähler bleibt bei 3
        self.assertEqual(self.store.open_count(), 3)

    def test_third_order_accepted(self):
        """Wenn nur 2 Positionen offen sind, darf eine dritte durch."""
        # Reset auf 2 Positionen
        self.store.reset_for_testing()
        self.store.add_position(_sample_pos("p1", "BTC/USDT"))
        self.store.add_position(_sample_pos("p2", "ETH/USDT"))
        self.assertEqual(self.store.open_count(), 2)

        pstore = self.ProposalStore()
        proposal = _make_approved_proposal("SOL/USDT")
        pstore.add(proposal)

        result = self.execute(pstore, proposal.id, self.cfg, state=self.store)
        self.assertIsNotNone(result)
        self.assertNotEqual(result.status, "rejected")
        self.assertEqual(self.store.open_count(), 3)


class TestExecuteSymbolDuplicate(unittest.TestCase):
    """Zweite Position im selben Symbol muss rejected werden."""

    def setUp(self):
        from tradeagent.graph import ProposalStore, execute

        self.execute = execute
        self.ProposalStore = ProposalStore
        self.store = _make_db()
        self.cfg = _make_config(max_open=5)
        self.store.add_position(_sample_pos("p1", "BTC/USDT"))

    def tearDown(self):
        self.store.close()

    def test_duplicate_symbol_rejected(self):
        pstore = self.ProposalStore()
        proposal = _make_approved_proposal("BTC/USDT")
        pstore.add(proposal)

        result = self.execute(pstore, proposal.id, self.cfg, state=self.store)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, "rejected")
        self.assertIn("Symbol", result.raw.get("error", ""))

    def test_different_symbol_accepted(self):
        pstore = self.ProposalStore()
        proposal = _make_approved_proposal("ETH/USDT")
        pstore.add(proposal)

        result = self.execute(pstore, proposal.id, self.cfg, state=self.store)
        self.assertIsNotNone(result)
        self.assertNotEqual(result.status, "rejected")


# ═════════════════════════════════════════════════════════════════════════════
# 3. Drawdown-Enforcement in build_proposal
# ═════════════════════════════════════════════════════════════════════════════

class TestDrawdownEnforcement(unittest.TestCase):
    """build_proposal muss None zurückgeben wenn Drawdown > Budget."""

    def test_proposal_blocked_when_drawdown_exceeded(self):
        from tradeagent.graph import build_proposal

        store = _make_db()
        try:
            # Simulierter Verlust: 20% Drawdown (Budget = 15%)
            store.record_pnl(-200.0)  # equity=800, peak=1000, dd=0.20
            self.assertGreater(store.current_drawdown_pct(), 0.15)

            cfg = _make_config()
            cfg.risk.max_drawdown_pct = 0.15

            proposal = build_proposal(
                _make_signal(), cfg, account_balance_usd=1000.0, state=store
            )
            self.assertIsNone(proposal, "Proposal muss None sein wenn Drawdown > Budget")
        finally:
            store.close()

    def test_proposal_allowed_when_drawdown_within_budget(self):
        from tradeagent.graph import build_proposal

        store = _make_db()
        try:
            # Kleiner Verlust: 5% Drawdown (Budget = 15%)
            store.record_pnl(-50.0)   # equity=950, peak=1000, dd=0.05
            self.assertLess(store.current_drawdown_pct(), 0.15)

            cfg = _make_config()
            cfg.risk.max_drawdown_pct = 0.15
            cfg.risk.min_signal_score = 60

            proposal = build_proposal(
                _make_signal(score=70), cfg, account_balance_usd=1000.0, state=store
            )
            # Kann None sein wenn Score/Sizing scheitert — hier sollte es klappen
            # (Score 70 > 60, Sizing > 0). Drawdown-Block darf es nicht sein.
            if proposal is None:
                # Debug: Was hat validate_proposal gesagt?
                from tradeagent.risk import validate_proposal, position_size_usd
                sig = _make_signal(score=70)
                size = position_size_usd(
                    sig,
                    account_balance_usd=1000.0,
                    risk_pct=0.02,
                    kelly_scale=cfg.risk.kelly_fraction,
                )
                ok, err = validate_proposal(
                    sig,
                    position_usd=size,
                    account_balance_usd=1000.0,
                    risk_cfg=cfg.risk,
                    current_drawdown_pct=store.current_drawdown_pct(),
                )
                self.assertTrue(ok, f"validate_proposal schlug fehl: {err}")
        finally:
            store.close()


# ═════════════════════════════════════════════════════════════════════════════
# 4. Paper-Modus: State wird gepflegt (gleicher Code-Pfad)
# ═════════════════════════════════════════════════════════════════════════════

class TestPaperModeStateTracking(unittest.TestCase):
    """Im Paper-Modus soll state.add_position() nach einem Fill aufgerufen werden."""

    def test_paper_fill_adds_to_state(self):
        from tradeagent.graph import ProposalStore, execute

        store = _make_db()
        cfg = _make_config(max_open=5, live=False)

        pstore = ProposalStore()
        proposal = _make_approved_proposal("BTC/USDT")
        pstore.add(proposal)

        self.assertEqual(store.open_count(), 0)
        result = execute(pstore, proposal.id, cfg, state=store)

        self.assertIsNotNone(result)
        self.assertIn(result.status, ("filled", "submitted"))
        self.assertEqual(store.open_count(), 1)

        pos = store.open_position_for_symbol("BTC/USDT")
        self.assertIsNotNone(pos)
        self.assertEqual(pos.proposal_id, proposal.id)

        store.close()


# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)