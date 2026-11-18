"""Tests für Risiko-Checks, Alert-Webhook und last_sync des StopMonitor (Direktive 5C).

Alle Exchange- und HTTP-Calls sind gemockt, kein Netz erforderlich.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tradeagent.config import Config, LiveLimits, TradingMode
from tradeagent.monitor import (
    ALARM_DRAWDOWN,
    ALARM_RISK_LIMIT,
    StopMonitor,
    check_drawdown,
    check_liquidation_distance,
    check_position_limits,
    run_risk_checks,
)
from tradeagent.state import OpenPosition, StateStore

WEBHOOK_URL = "https://alerts.example.test/hook"


def _make_db() -> StateStore:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return StateStore(path=path, initial_equity=1000.0)


def _make_config(webhook: str = "", max_open: int = 5, max_notional: float = 10_000.0) -> Config:
    cfg = Config()
    cfg.live_limits = LiveLimits(
        max_open_positions=max_open,
        max_position_notional_usd=max_notional,
        symbol_allowlist=["BTC/USDT"],
    )
    cfg.trading = TradingMode(mode="paper")
    cfg.alert_webhook_url = webhook
    return cfg


def _pos(proposal_id: str, notional: float = 50.0) -> OpenPosition:
    return OpenPosition(
        proposal_id=proposal_id,
        symbol="BTC/USDT",
        side="buy",
        qty=0.001,
        entry_price=50_000.0,
        notional_usd=notional,
        stop_price=48_000.0,
        target_price=54_000.0,
        opened_at="2026-01-01T00:00:00+00:00",
        stop_order_id="stop-1",
        target_order_id="target-1",
    )


def _open_kinds(store: StateStore) -> list:
    """Arten aller nicht aufgelösten Alarme (state != 'resolved')."""
    return [alarm["kind"] for alarm in store.list_alarms() if alarm["state"] != "resolved"]


class TestDrawdownCheck(unittest.TestCase):
    def setUp(self):
        self.store = _make_db()
        self.addCleanup(self.store.close)

    def test_breach_raises_alarm_and_notifies_once(self):
        cfg = _make_config(webhook=WEBHOOK_URL)
        with patch.object(self.store, "current_drawdown_pct", return_value=0.20), \
                patch("tradeagent.monitor.httpx.post") as post:
            check_drawdown(self.store, cfg)
            check_drawdown(self.store, cfg)

        self.assertEqual(_open_kinds(self.store).count(ALARM_DRAWDOWN), 1)
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["json"]["kind"], ALARM_DRAWDOWN)

    def test_recovery_resolves_unacknowledged_alarm(self):
        cfg = _make_config()
        with patch.object(self.store, "current_drawdown_pct", return_value=0.20):
            check_drawdown(self.store, cfg)
        with patch.object(self.store, "current_drawdown_pct", return_value=0.01):
            check_drawdown(self.store, cfg)

        self.assertNotIn(ALARM_DRAWDOWN, _open_kinds(self.store))

    def test_webhook_failure_keeps_alarm_and_does_not_raise(self):
        cfg = _make_config(webhook=WEBHOOK_URL)
        with patch.object(self.store, "current_drawdown_pct", return_value=0.20), \
                patch("tradeagent.monitor.httpx.post", side_effect=RuntimeError("down")):
            check_drawdown(self.store, cfg)

        self.assertIn(ALARM_DRAWDOWN, _open_kinds(self.store))

    def test_no_webhook_call_when_url_empty(self):
        cfg = _make_config(webhook="")
        with patch.object(self.store, "current_drawdown_pct", return_value=0.20), \
                patch("tradeagent.monitor.httpx.post") as post:
            check_drawdown(self.store, cfg)

        post.assert_not_called()
        self.assertIn(ALARM_DRAWDOWN, _open_kinds(self.store))


class TestPositionLimits(unittest.TestCase):
    def setUp(self):
        self.store = _make_db()
        self.addCleanup(self.store.close)

    def test_too_many_open_positions_raise_alarm(self):
        cfg = _make_config(max_open=1)
        with patch.object(self.store, "open_positions", return_value=[_pos("p1"), _pos("p2")]):
            check_position_limits(self.store, cfg)

        self.assertIn(ALARM_RISK_LIMIT, _open_kinds(self.store))

    def test_notional_over_limit_raises_alarm_per_position(self):
        cfg = _make_config(max_notional=100.0)
        with patch.object(self.store, "open_positions", return_value=[_pos("p1", notional=500.0)]):
            check_position_limits(self.store, cfg)

        self.assertEqual(_open_kinds(self.store).count(ALARM_RISK_LIMIT), 1)


class TestLiquidationCheck(unittest.TestCase):
    def setUp(self):
        self.store = _make_db()
        self.addCleanup(self.store.close)

    def test_paper_broker_is_skipped(self):
        cfg = _make_config()
        with patch.object(self.store, "open_positions", return_value=[_pos("p1")]):
            check_liquidation_distance(self.store, cfg, object())

        self.assertEqual(_open_kinds(self.store), [])


class TestRiskCheckIsolation(unittest.TestCase):
    def setUp(self):
        self.store = _make_db()
        self.addCleanup(self.store.close)

    def test_failing_check_does_not_stop_other_checks(self):
        cfg = _make_config()
        with patch("tradeagent.monitor.make_broker", return_value=MagicMock()), \
                patch("tradeagent.monitor.check_drawdown", side_effect=RuntimeError("boom")), \
                patch("tradeagent.monitor.check_position_limits") as limits:
            run_risk_checks(self.store, cfg)

        limits.assert_called_once()

    def test_unavailable_broker_skips_all_checks_without_raising(self):
        cfg = _make_config()
        with patch("tradeagent.monitor.make_broker", side_effect=RuntimeError("no broker")), \
                patch("tradeagent.monitor.check_drawdown") as drawdown:
            run_risk_checks(self.store, cfg)

        drawdown.assert_not_called()


class TestLastSync(unittest.TestCase):
    def test_sync_cycle_writes_last_sync(self):
        store = _make_db()
        self.addCleanup(store.close)
        cfg = _make_config()
        monitor = StopMonitor(cfg, state=store, interval_s=0.05)

        with patch("tradeagent.monitor.sync_once", return_value=[]), \
                patch("tradeagent.monitor.run_risk_checks"):
            monitor.start()
            time.sleep(0.2)
            monitor.stop()

        meta = store.get_monitor_meta("last_sync")
        self.assertIsNotNone(meta)
        self.assertIn("at", meta)


if __name__ == "__main__":
    import unittest as _unittest

    _unittest.main(verbosity=2)