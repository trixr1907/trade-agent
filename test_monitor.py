"""Tests für monitor.py (Sync-Schicht) und execution.py (Stop-Platzierung).

Alle Exchange-Calls werden gemockt — kein Netz erforderlich.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from typing import Optional
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tradeagent.config import Config, LiveLimits, TradingMode
from tradeagent.models import ApprovalState, Signal, SignalDirection, TradeProposal
from tradeagent.state import OpenPosition, StateStore


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

def _make_db(initial_equity: float = 1000.0) -> StateStore:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return StateStore(path=path, initial_equity=initial_equity)


def _make_config(live: bool = False, max_open: int = 5) -> Config:
    cfg = Config()
    cfg.live_limits = LiveLimits(
        max_open_positions=max_open,
        max_position_notional_usd=10_000.0,
        symbol_allowlist=["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    )
    cfg.trading = TradingMode(mode="live" if live else "paper")
    if live:
        cfg.exchange.api_key = "dummy"
        cfg.exchange.api_secret = "dummy"
        cfg.exchange.api_passphrase = "dummy"
    return cfg


def _sample_pos(
    proposal_id: str = "prop-001",
    symbol: str = "BTC/USDT",
    stop_order_id: Optional[str] = None,
    target_order_id: Optional[str] = None,
    side: str = "buy",
    entry_price: float = 50_000.0,
    stop_price: float = 48_000.0,
    target_price: float = 54_000.0,
    qty: float = 0.001,
) -> OpenPosition:
    return OpenPosition(
        proposal_id=proposal_id,
        symbol=symbol,
        side=side,
        qty=qty,
        entry_price=entry_price,
        notional_usd=qty * entry_price,
        stop_price=stop_price,
        target_price=target_price,
        opened_at="2026-01-01T00:00:00+00:00",
        stop_order_id=stop_order_id,
        target_order_id=target_order_id,
    )


def _make_signal(symbol: str = "BTC/USDT") -> Signal:
    return Signal(
        symbol=symbol,
        direction=SignalDirection.BUY,
        price=50_000.0,
        stop_price=48_000.0,
        target_price=54_000.0,
        score=70,
    )


def _make_approved_proposal(symbol: str = "BTC/USDT") -> TradeProposal:
    p = TradeProposal(
        id=f"prop-{symbol[:3].lower()}",
        signal=_make_signal(symbol),
        proposed_size_usd=100.0,
        stop_price=48_000.0,
        target_price=54_000.0,
        risk_usd=20.0,
    )
    p.state = ApprovalState.APPROVED
    return p


# ═════════════════════════════════════════════════════════════════════════════
# 1. P&L-Berechnung (realer Fill-Preis / Slippage)
# ═════════════════════════════════════════════════════════════════════════════

class TestCalculatePnL(unittest.TestCase):

    def setUp(self):
        from tradeagent.monitor import calculate_pnl
        self.calculate_pnl = calculate_pnl

    def test_buy_win(self):
        pos = _sample_pos(entry_price=50_000.0, qty=0.001, side="buy")
        self.assertAlmostEqual(self.calculate_pnl(pos, 52_000.0), 2.0, places=4)

    def test_buy_loss(self):
        pos = _sample_pos(entry_price=50_000.0, qty=0.001, side="buy")
        self.assertAlmostEqual(self.calculate_pnl(pos, 48_000.0), -2.0, places=4)

    def test_sell_win(self):
        pos = _sample_pos(entry_price=50_000.0, qty=0.001, side="sell")
        self.assertAlmostEqual(self.calculate_pnl(pos, 48_000.0), 2.0, places=4)

    def test_sell_loss(self):
        pos = _sample_pos(entry_price=50_000.0, qty=0.001, side="sell")
        self.assertAlmostEqual(self.calculate_pnl(pos, 52_000.0), -2.0, places=4)

    def test_slippage_reflected(self):
        """P&L muss den echten Fill-Preis nutzen, nicht stop_price."""
        pos = _sample_pos(entry_price=50_000.0, qty=0.001, side="buy", stop_price=48_000.0)
        pnl_slip = self.calculate_pnl(pos, 47_800.0)
        pnl_no   = self.calculate_pnl(pos, 48_000.0)
        self.assertLess(pnl_slip, pnl_no)


# ═════════════════════════════════════════════════════════════════════════════
# 2. sync_once — Fill-Erkennung, close_position, record_pnl
# ═════════════════════════════════════════════════════════════════════════════

class TestSyncOnce(unittest.TestCase):

    def setUp(self):
        self.store = _make_db()
        self.cfg = _make_config(live=False)

    def tearDown(self):
        self.store.close()

    def _add_pos(self, stop_id="stop-001", target_id="target-001"):
        pos = _sample_pos(proposal_id="prop-001", stop_order_id=stop_id, target_order_id=target_id)
        self.store.add_position(pos)
        return pos

    def test_stop_fill_closes_position(self):
        from tradeagent.monitor import sync_once
        self._add_pos()

        def mock_fetch(symbol, order_id):
            return {"status": "closed", "average": 47_800.0} if order_id == "stop-001" else {"status": "open"}

        with patch("tradeagent.monitor.make_broker") as mb:
            broker = MagicMock()
            broker.fetch_order.side_effect = mock_fetch
            broker.cancel_order.return_value = True
            mb.return_value = broker
            closed = sync_once(self.store, self.cfg)

        self.assertIn("prop-001", closed)
        self.assertEqual(self.store.open_count(), 0)

    def test_target_fill_closes_position(self):
        from tradeagent.monitor import sync_once
        self._add_pos()

        def mock_fetch(symbol, order_id):
            return {"status": "closed", "average": 54_000.0} if order_id == "target-001" else {"status": "open"}

        with patch("tradeagent.monitor.make_broker") as mb:
            broker = MagicMock()
            broker.fetch_order.side_effect = mock_fetch
            broker.cancel_order.return_value = True
            mb.return_value = broker
            closed = sync_once(self.store, self.cfg)

        self.assertIn("prop-001", closed)
        self.assertEqual(self.store.open_count(), 0)

    def test_pnl_from_real_fill_price(self):
        """P&L muss aus echtem Fill-Preis berechnet werden, nicht stop_price."""
        from tradeagent.monitor import sync_once
        self._add_pos()

        # Slippage: Stop-Fill bei 47_800 statt 48_000
        def mock_fetch(symbol, order_id):
            return {"status": "closed", "average": 47_800.0} if order_id == "stop-001" else {"status": "open"}

        with patch("tradeagent.monitor.make_broker") as mb:
            broker = MagicMock()
            broker.fetch_order.side_effect = mock_fetch
            broker.cancel_order.return_value = True
            mb.return_value = broker
            sync_once(self.store, self.cfg)

        # entry=50k, fill=47.8k, qty=0.001 → pnl = (47.8k-50k)*0.001 = -2.2
        pnl = self.store.realized_pnl_total()
        self.assertAlmostEqual(pnl, -2.2, places=3)

    def test_no_fill_does_not_close(self):
        from tradeagent.monitor import sync_once
        self._add_pos()

        with patch("tradeagent.monitor.make_broker") as mb:
            broker = MagicMock()
            broker.fetch_order.return_value = {"status": "open"}
            mb.return_value = broker
            closed = sync_once(self.store, self.cfg)

        self.assertEqual(closed, [])
        self.assertEqual(self.store.open_count(), 1)

    def test_no_order_ids_skipped(self):
        """Positionen ohne Stop-Order-IDs werden übersprungen."""
        from tradeagent.monitor import sync_once
        self.store.add_position(_sample_pos(proposal_id="prop-002"))

        with patch("tradeagent.monitor.make_broker") as mb:
            broker = MagicMock()
            mb.return_value = broker
            closed = sync_once(self.store, self.cfg)

        broker.fetch_order.assert_not_called()
        self.assertEqual(closed, [])

    def test_on_close_callback_called(self):
        from tradeagent.monitor import sync_once
        self._add_pos()
        calls = []

        def on_close(pos, fill_price, pnl):
            calls.append((pos.proposal_id, fill_price, pnl))

        def mock_fetch(symbol, order_id):
            return {"status": "closed", "average": 48_000.0} if order_id == "stop-001" else {"status": "open"}

        with patch("tradeagent.monitor.make_broker") as mb:
            broker = MagicMock()
            broker.fetch_order.side_effect = mock_fetch
            broker.cancel_order.return_value = True
            mb.return_value = broker
            sync_once(self.store, self.cfg, on_close=on_close)

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "prop-001")


# ═════════════════════════════════════════════════════════════════════════════
# 3. Kein Double-Close
# ═════════════════════════════════════════════════════════════════════════════

class TestNoDoubleClose(unittest.TestCase):

    def test_double_close_prevented(self):
        store = _make_db()
        try:
            store.add_position(_sample_pos("p1"))
            first  = store.close_position("p1", 48_000.0, -2.0)
            second = store.close_position("p1", 48_000.0, -2.0)
            self.assertIsNotNone(first)
            self.assertIsNone(second)
            # P&L nur einmal gebucht
            self.assertAlmostEqual(store.realized_pnl_total(), -2.0)
        finally:
            store.close()


# ═════════════════════════════════════════════════════════════════════════════
# 4. Entry + Stop platziert (graph.execute → place_stop_orders)
# ═════════════════════════════════════════════════════════════════════════════

class TestEntryStopPlacement(unittest.TestCase):

    def test_paper_entry_calls_place_stop_orders(self):
        """Nach einem Paper-Fill soll place_stop_orders aufgerufen werden."""
        from tradeagent.graph import ProposalStore, execute

        store = _make_db()
        cfg = _make_config(live=False, max_open=5)
        pstore = ProposalStore()
        proposal = _make_approved_proposal("BTC/USDT")
        pstore.add(proposal)

        with patch("tradeagent.execution.PaperBroker.place_stop_orders",
                   return_value=("paper-stop-aaa", "paper-target-bbb")) as mock_stops:
            result = execute(pstore, proposal.id, cfg, state=store)

        self.assertIn(result.status, ("filled", "submitted"))
        mock_stops.assert_called_once()

        pos = store.open_position_for_symbol("BTC/USDT")
        self.assertIsNotNone(pos)
        self.assertEqual(pos.stop_order_id, "paper-stop-aaa")
        self.assertEqual(pos.target_order_id, "paper-target-bbb")
        store.close()

    def test_stop_placement_failure_does_not_prevent_state_save(self):
        """Wenn place_stop_orders fehlschlägt, soll Entry trotzdem gespeichert werden."""
        from tradeagent.graph import ProposalStore, execute

        store = _make_db()
        cfg = _make_config(live=False, max_open=5)
        pstore = ProposalStore()
        proposal = _make_approved_proposal("ETH/USDT")
        pstore.add(proposal)

        with patch("tradeagent.execution.PaperBroker.place_stop_orders",
                   side_effect=Exception("Exchange down")):
            result = execute(pstore, proposal.id, cfg, state=store)

        self.assertIn(result.status, ("filled", "submitted"))
        self.assertEqual(store.open_count(), 1)
        pos = store.open_position_for_symbol("ETH/USDT")
        self.assertIsNotNone(pos)
        self.assertIsNone(pos.stop_order_id)
        store.close()


# ═════════════════════════════════════════════════════════════════════════════
# 5. Dedupe: zweiter Stop für selbes Symbol verhindert
# ═════════════════════════════════════════════════════════════════════════════

class TestDedupeStop(unittest.TestCase):

    def test_no_duplicate_stop_for_same_symbol(self):
        from tradeagent.graph import ProposalStore, execute

        store = _make_db()
        cfg = _make_config(live=False, max_open=5)

        # Bestehende Position mit stop_order_id vorbelegen
        store.add_position(_sample_pos(
            proposal_id="existing-prop",
            symbol="BTC/USDT",
            stop_order_id="existing-stop-123",
        ))

        pstore = ProposalStore()
        proposal = _make_approved_proposal("BTC/USDT")
        pstore.add(proposal)

        with patch("tradeagent.execution.PaperBroker.place_stop_orders",
                   return_value=("new-stop", "new-target")) as mock_stops:
            result = execute(pstore, proposal.id, cfg, state=store)

        # Symbol-Duplikat → rejected durch execute()
        self.assertEqual(result.status, "rejected")
        mock_stops.assert_not_called()
        store.close()


# ═════════════════════════════════════════════════════════════════════════════
# 6. Cancel bei manuellem Close
# ═════════════════════════════════════════════════════════════════════════════

class TestCancelOnManualClose(unittest.TestCase):

    def test_cancel_stop_orders_called(self):
        from tradeagent.monitor import cancel_stop_orders

        cfg = _make_config(live=False)
        pos = _sample_pos(stop_order_id="stop-xyz", target_order_id="target-xyz")

        with patch("tradeagent.monitor.make_broker") as mb:
            broker = MagicMock()
            broker.cancel_order.return_value = True
            mb.return_value = broker
            cancel_stop_orders(pos, cfg)

        self.assertEqual(broker.cancel_order.call_count, 2)

    def test_cancel_no_ids_does_nothing(self):
        from tradeagent.monitor import cancel_stop_orders

        cfg = _make_config(live=False)
        pos = _sample_pos(stop_order_id=None, target_order_id=None)

        with patch("tradeagent.monitor.make_broker") as mb:
            broker = MagicMock()
            mb.return_value = broker
            cancel_stop_orders(pos, cfg)

        broker.cancel_order.assert_not_called()


# ═════════════════════════════════════════════════════════════════════════════
# 7. StopMonitor Hintergrund-Thread
# ═════════════════════════════════════════════════════════════════════════════

class TestStopMonitorThread(unittest.TestCase):

    def test_monitor_starts_and_stops(self):
        from tradeagent.monitor import StopMonitor

        store = _make_db()
        cfg = _make_config(live=False)
        monitor = StopMonitor(cfg, state=store, interval_s=0.1)

        with patch("tradeagent.monitor.sync_once", return_value=[]):
            monitor.start()
            time.sleep(0.3)
            monitor.stop()

        self.assertFalse(monitor._thread.is_alive())
        store.close()

    def test_monitor_calls_sync_once_repeatedly(self):
        from tradeagent.monitor import StopMonitor

        store = _make_db()
        cfg = _make_config(live=False)
        call_count = {"n": 0}

        def mock_sync(state, cfg, on_close=None):
            call_count["n"] += 1
            return []

        monitor = StopMonitor(cfg, state=store, interval_s=0.05)
        with patch("tradeagent.monitor.sync_once", side_effect=mock_sync):
            monitor.start()
            time.sleep(0.3)
            monitor.stop()

        self.assertGreater(call_count["n"], 1)
        store.close()

    def test_monitor_survives_sync_exception(self):
        """Ein Crash in sync_once darf den Monitor-Thread nicht killen."""
        from tradeagent.monitor import StopMonitor

        store = _make_db()
        cfg = _make_config(live=False)
        call_count = {"n": 0}

        def failing_sync(state, cfg, on_close=None):
            call_count["n"] += 1
            raise RuntimeError("Simulierter Exchange-Fehler")

        monitor = StopMonitor(cfg, state=store, interval_s=0.05)
        with patch("tradeagent.monitor.sync_once", side_effect=failing_sync):
            monitor.start()
            time.sleep(0.3)
            monitor.stop()

        self.assertGreater(call_count["n"], 1)
        self.assertFalse(monitor._thread.is_alive())
        store.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)