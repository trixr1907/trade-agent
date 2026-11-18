"""Tests für relay.py (Bitget-Relay). Alle Exchange-Calls laufen gegen einen Fake, kein Netz."""
from __future__ import annotations

import os
import tempfile
import unittest
from typing import Any, Dict, List, Optional

import ccxt

from tradeagent.config import Config
from tradeagent.control import ControlPlane
from tradeagent.relay import (
    AlarmsOpenError,
    BitgetRelay,
    client_order_id,
    resume_trading,
    stop_clear_of_liquidation,
    to_perp_symbol,
)
from tradeagent.state import StateStore


class FakeExchange:
    """Minimaler Bitget-Ersatz. Die Rolle einer Order wird aus den Params erkannt."""

    def __init__(self) -> None:
        self.fail: Dict[str, Exception] = {}
        self.fail_after_send: Dict[str, Exception] = {}
        self.calls: List[Dict[str, Any]] = []
        self.orders: Dict[str, Dict[str, Any]] = {}
        self._next = 1

    @staticmethod
    def _role(params: Dict[str, Any]) -> str:
        if "stopLossPrice" in params:
            return "stop"
        if "takeProfitPrice" in params:
            return "target"
        if params.get("reduceOnly"):
            return "emergency"
        return "entry"

    def set_margin_mode(self, mode: str, symbol: Optional[str] = None, params: Optional[dict] = None) -> dict:
        self.calls.append({"call": "set_margin_mode", "mode": mode, "symbol": symbol})
        if "margin" in self.fail:
            raise self.fail["margin"]
        return {}

    def set_leverage(self, leverage: int, symbol: Optional[str] = None, params: Optional[dict] = None) -> dict:
        self.calls.append(
            {"call": "set_leverage", "leverage": leverage, "symbol": symbol, "params": dict(params or {})}
        )
        return {}

    def create_order(self, symbol, type, side, amount, price=None, params=None):  # noqa: A002
        params = dict(params or {})
        role = self._role(params)
        self.calls.append(
            {"call": "create_order", "role": role, "symbol": symbol, "type": type,
             "side": side, "amount": amount, "params": params}
        )
        if role in self.fail:
            raise self.fail[role]
        oid = f"o{self._next}"
        self._next += 1
        is_entry = role == "entry"
        order = {
            "id": oid,
            "clientOrderId": params.get("clientOid"),
            "status": "closed" if is_entry else "open",
            "average": 100.5 if is_entry else None,
            "filled": amount if is_entry else 0.0,
        }
        self.orders[oid] = order
        if role in self.fail_after_send:
            raise self.fail_after_send[role]
        return dict(order)

    def fetch_order(self, id, symbol=None, params=None):  # noqa: A002
        coid = (params or {}).get("clientOrderId")
        if coid:
            for order in self.orders.values():
                if order["clientOrderId"] == coid:
                    return dict(order)
            raise ccxt.OrderNotFound(f"clientOid {coid} nicht gefunden")
        if id in self.orders:
            return dict(self.orders[id])
        raise ccxt.OrderNotFound(id)

    def orders_for(self, role: str) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c["call"] == "create_order" and c["role"] == role]


def _make_cfg() -> Config:
    cfg = Config()
    cfg.trading.mode = "live"
    cfg.live_limits.max_position_notional_usd = 500.0
    cfg.live_limits.max_open_positions = 3
    cfg.live_limits.symbol_allowlist = ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    cfg.exchange.margin_mode = "isolated"
    cfg.exchange.leverage = 3
    return cfg


class RelayTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.state = StateStore(path=os.path.join(self._tmp.name, "state.db"))
        self.ex = FakeExchange()
        self.relay = BitgetRelay(_make_cfg(), self.state, exchange=self.ex)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def place(self, intent_id: str = "p1", **overrides: Any):
        args: Dict[str, Any] = dict(
            symbol="BTC/USDT", side="buy", qty=1.0, price=100.0, stop_price=95.0, target_price=110.0
        )
        args.update(overrides)
        return self.relay.place_entry(intent_id, **args)

    def halted(self) -> bool:
        return ControlPlane(self.state).is_halted()

    def alarm_kinds(self) -> List[str]:
        return [a["kind"] for a in self.state.open_alarms()]


class TestHelpers(unittest.TestCase):
    def test_to_perp_symbol(self) -> None:
        self.assertEqual(to_perp_symbol("btc/usdt"), "BTC/USDT:USDT")
        self.assertEqual(to_perp_symbol("ETH/USDT:USDT"), "ETH/USDT:USDT")
        for bad in ("", "BTCUSDT", "/USDT", "BTC/"):
            with self.assertRaises(ValueError):
                to_perp_symbol(bad)

    def test_client_order_id_deterministic(self) -> None:
        a = client_order_id("p1", "entry")
        self.assertEqual(a, client_order_id("p1", "entry"))
        self.assertNotEqual(a, client_order_id("p1", "stop"))
        self.assertEqual(len(a), 32)

    def test_stop_clear_of_liquidation(self) -> None:
        self.assertTrue(stop_clear_of_liquidation(100.0, 85.0, 3))
        self.assertFalse(stop_clear_of_liquidation(100.0, 80.0, 3))
        self.assertFalse(stop_clear_of_liquidation(100.0, 95.0, 0))


class TestRelayHappyPath(RelayTestBase):
    def test_entry_stop_target(self) -> None:
        result = self.place()
        self.assertEqual(result.status, "filled")
        self.assertEqual(result.mode, "live")
        self.assertAlmostEqual(result.price, 100.5)

        entry = self.ex.orders_for("entry")[0]
        self.assertEqual(entry["symbol"], "BTC/USDT:USDT")
        self.assertEqual(entry["type"], "market")
        self.assertEqual(entry["params"]["clientOid"], client_order_id("p1", "entry"))
        self.assertEqual(entry["params"]["marginMode"], "isolated")

        stop = self.ex.orders_for("stop")[0]
        self.assertEqual(stop["side"], "sell")
        self.assertEqual(stop["params"]["stopLossPrice"], 95.0)
        target = self.ex.orders_for("target")[0]
        self.assertEqual(target["params"]["takeProfitPrice"], 110.0)

        leverage = [c for c in self.ex.calls if c["call"] == "set_leverage"][0]
        self.assertEqual(leverage["leverage"], 3)
        self.assertEqual(leverage["params"], {"holdSide": "long"})

        self.assertIsNotNone(result.raw["stop_order_id"])
        self.assertIsNotNone(result.raw["target_order_id"])
        self.assertEqual(self.state.get_receipt("p1", "entry")["status"], "filled")
        self.assertEqual(self.state.get_receipt("p1", "stop")["status"], "submitted")
        self.assertFalse(self.halted())
        self.assertEqual(self.alarm_kinds(), [])

    def test_duplicate_intent_sends_nothing(self) -> None:
        self.place()
        second = self.place()
        self.assertEqual(second.status, "rejected")
        self.assertEqual(len(self.ex.orders_for("entry")), 1)


class TestRelayGates(RelayTestBase):
    def assert_rejected_without_calls(self, result) -> None:
        self.assertEqual(result.status, "rejected")
        self.assertEqual(self.ex.calls, [])

    def test_halted(self) -> None:
        ControlPlane(self.state).halt("test", "manuell")
        self.assert_rejected_without_calls(self.place())

    def test_symbol_not_allowed(self) -> None:
        self.assert_rejected_without_calls(self.place(symbol="SOL/USDT"))

    def test_invalid_side(self) -> None:
        self.assert_rejected_without_calls(self.place(side="hold"))

    def test_notional_limit(self) -> None:
        self.assert_rejected_without_calls(self.place(qty=10.0))

    def test_missing_stop(self) -> None:
        self.assert_rejected_without_calls(self.place(stop_price=None))

    def test_stop_wrong_side(self) -> None:
        self.assert_rejected_without_calls(self.place(stop_price=105.0))

    def test_stop_beyond_liquidation_buffer(self) -> None:
        self.assert_rejected_without_calls(self.place(stop_price=80.0))

    def test_margin_mode_failure(self) -> None:
        self.ex.fail["margin"] = ccxt.ExchangeError("margin mode locked")
        result = self.place()
        self.assertEqual(result.status, "rejected")
        self.assertEqual(self.ex.orders_for("entry"), [])
        self.assertEqual(self.state.get_receipt("p1", "entry")["status"], "rejected")


class TestRelayFailures(RelayTestBase):
    def test_exchange_rejection_allows_retry(self) -> None:
        self.ex.fail["entry"] = ccxt.InsufficientFunds("zu wenig Margin")
        first = self.place()
        self.assertEqual(first.status, "rejected")
        self.assertEqual(self.state.get_receipt("p1", "entry")["status"], "rejected")
        self.assertFalse(self.halted())

        del self.ex.fail["entry"]
        second = self.place()
        self.assertEqual(second.status, "filled")

    def test_network_error_order_found_continues(self) -> None:
        self.ex.fail_after_send["entry"] = ccxt.RequestTimeout("timeout")
        result = self.place()
        self.assertEqual(result.status, "filled")
        self.assertEqual(len(self.ex.orders_for("stop")), 1)
        self.assertFalse(self.halted())

    def test_network_error_order_unknown_halts(self) -> None:
        self.ex.fail["entry"] = ccxt.RequestTimeout("timeout")
        result = self.place()
        self.assertEqual(result.status, "rejected")
        self.assertEqual(self.state.get_receipt("p1", "entry")["status"], "unknown")
        self.assertTrue(self.halted())
        self.assertEqual(self.alarm_kinds(), ["entry_unknown"])

    def test_stop_failure_emergency_close(self) -> None:
        self.ex.fail["stop"] = ccxt.ExchangeError("stop abgelehnt")
        result = self.place()
        self.assertEqual(result.status, "emergency_closed")
        emergency = self.ex.orders_for("emergency")
        self.assertEqual(len(emergency), 1)
        self.assertEqual(emergency[0]["side"], "sell")
        self.assertTrue(emergency[0]["params"]["reduceOnly"])
        self.assertEqual(self.ex.orders_for("target"), [])
        self.assertTrue(self.halted())
        self.assertEqual(self.alarm_kinds(), ["stop_failed"])

    def test_emergency_close_failure_unprotected(self) -> None:
        self.ex.fail["stop"] = ccxt.ExchangeError("stop abgelehnt")
        self.ex.fail["emergency"] = ccxt.NetworkError("weg")
        result = self.place()
        self.assertEqual(result.status, "unprotected")
        self.assertTrue(self.halted())
        self.assertEqual(self.alarm_kinds(), ["emergency_close_failed"])

    def test_target_failure_keeps_position(self) -> None:
        self.ex.fail["target"] = ccxt.ExchangeError("target abgelehnt")
        result = self.place()
        self.assertEqual(result.status, "filled")
        self.assertIsNone(result.raw["target_order_id"])
        self.assertFalse(self.halted())
        self.assertEqual(self.alarm_kinds(), ["target_failed"])


class TestResumeGate(RelayTestBase):
    def test_resume_requires_acknowledged_alarms(self) -> None:
        self.ex.fail["stop"] = ccxt.ExchangeError("stop abgelehnt")
        self.place()
        with self.assertRaises(AlarmsOpenError):
            resume_trading(self.state, "ivo")
        self.assertTrue(self.halted())

        for alarm in self.state.open_alarms():
            self.assertTrue(self.state.acknowledge_alarm(alarm["id"], "ivo"))
        resume_trading(self.state, "ivo", "Alarm geprüft")
        self.assertFalse(self.halted())


if __name__ == "__main__":
    unittest.main()