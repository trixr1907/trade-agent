"""Tests für die Schleife des StopMonitor: Intervall, Risiko-Alarm im Loop, Resume-Gate.

Sync, Broker und Webhook sind gemockt, kein Netz erforderlich. Der Monitor läuft mit
0,05 s Intervall direkt als Thread, ohne FastAPI-Lifespan.
"""
from __future__ import annotations

import os
import sys
import time
import unittest
from typing import Any, Callable, Dict, List
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_monitor_risk import _make_config, _make_db
from tradeagent.monitor import ALARM_DRAWDOWN, StopMonitor
from tradeagent.relay import AlarmsOpenError, resume_trading
from tradeagent.state import StateStore

INTERVAL_S = 0.05


def _wait_until(predicate: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Pollt predicate bis True oder Timeout. Gibt das letzte Ergebnis zurück."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _drawdown_alarms(store: StateStore) -> List[Dict[str, Any]]:
    return [alarm for alarm in store.list_alarms() if alarm["kind"] == ALARM_DRAWDOWN]


class TestMonitorLoop(unittest.TestCase):
    def setUp(self):
        self.store = _make_db()
        self.addCleanup(self.store.close)
        self.cfg = _make_config()

    def _monitor(self) -> StopMonitor:
        return StopMonitor(self.cfg, state=self.store, interval_s=INTERVAL_S)

    def test_loop_runs_repeatedly_at_interval(self):
        calls: List[float] = []

        def fake_sync(*_args: Any, **_kwargs: Any) -> list:
            calls.append(time.monotonic())
            return []

        monitor = self._monitor()
        with patch("tradeagent.monitor.sync_once", side_effect=fake_sync), \
                patch("tradeagent.monitor.run_risk_checks") as risk:
            monitor.start()
            try:
                reached = _wait_until(lambda: len(calls) >= 3)
            finally:
                monitor.stop()

        self.assertTrue(reached, "Loop lief nicht mindestens dreimal.")
        # Drei Zyklen liegen mindestens zwei Intervalle auseinander: kein Busy-Loop.
        self.assertGreaterEqual(calls[2] - calls[0], 2 * INTERVAL_S * 0.9)
        # Jeder Zyklus führt nach dem Sync die Risiko-Checks aus.
        self.assertEqual(risk.call_count, len(calls))

    def test_loop_raises_and_resolves_alarm_and_gate_waits_for_ack(self):
        drawdown = {"value": self.cfg.risk.max_drawdown_pct + 0.05}

        monitor = self._monitor()
        with patch("tradeagent.monitor.sync_once", return_value=[]), \
                patch("tradeagent.monitor.make_broker", return_value=object()), \
                patch("tradeagent.monitor.httpx.post"), \
                patch.object(self.store, "current_drawdown_pct",
                             side_effect=lambda: drawdown["value"]):
            monitor.start()
            try:
                raised = _wait_until(
                    lambda: any(a["state"] == "open" for a in _drawdown_alarms(self.store))
                )
                self.assertTrue(raised, "Loop hat keinen Drawdown-Alarm angelegt.")
                with self.assertRaises(AlarmsOpenError):
                    resume_trading(self.store, "test")

                drawdown["value"] = 0.0
                resolved = _wait_until(
                    lambda: all(a["state"] == "resolved" for a in _drawdown_alarms(self.store))
                )
                self.assertTrue(resolved, "Loop hat den Alarm nicht aufgelöst.")
            finally:
                monitor.stop()

        alarms = _drawdown_alarms(self.store)
        # Idempotent: mehrere Zyklen mit Verstoß ergeben genau einen Alarm.
        self.assertEqual(len(alarms), 1)
        # Strict fail-closed: aufgelöst, aber unbestätigt blockiert das Gate weiter.
        with self.assertRaises(AlarmsOpenError):
            resume_trading(self.store, "test")

        self.assertTrue(self.store.acknowledge_alarm(alarms[0]["id"], "test"))
        self.assertEqual(self.store.open_alarms(), [])

    def test_sync_failure_does_not_stop_loop(self):
        monitor = self._monitor()
        with patch("tradeagent.monitor.sync_once",
                   side_effect=RuntimeError("exchange down")) as sync, \
                patch("tradeagent.monitor.run_risk_checks") as risk:
            monitor.start()
            try:
                reached = _wait_until(lambda: risk.call_count >= 2)
            finally:
                monitor.stop()

        self.assertTrue(reached, "Loop lief nach Sync-Fehler nicht weiter.")
        self.assertGreaterEqual(sync.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)