"""Tests für LiveBroker.fetch_order/cancel_order mit Bitget-Trigger-Orders. Kein Netz."""
from __future__ import annotations

import unittest
from typing import Any, Dict, List, Optional

import ccxt

from tradeagent.config import Config
from tradeagent.execution import LiveBroker
from tradeagent.monitor import _extract_fill_price, _is_filled


class FakeTriggerExchange:
    def __init__(self) -> None:
        self.open_orders: List[Dict[str, Any]] = []
        self.history: List[Dict[str, Any]] = []
        self.fail_open: Optional[Exception] = None
        self.fail_history: Optional[Exception] = None
        self.calls: List[Dict[str, Any]] = []

    def fetch_open_orders(self, symbol=None, since=None, limit=None, params=None):
        self.calls.append({"call": "fetch_open_orders", "symbol": symbol, "params": dict(params or {})})
        if self.fail_open:
            raise self.fail_open
        return [dict(o) for o in self.open_orders]

    def fetch_canceled_and_closed_orders(self, symbol=None, since=None, limit=None, params=None):
        self.calls.append({"call": "history", "symbol": symbol, "params": dict(params or {})})
        if self.fail_history:
            raise self.fail_history
        return [dict(o) for o in self.history]

    def fetch_order(self, id, symbol=None, params=None):  # noqa: A002
        self.calls.append({"call": "fetch_order", "id": id, "symbol": symbol})
        raise ccxt.OrderNotFound(id)

    def cancel_order(self, id, symbol=None, params=None):  # noqa: A002
        self.calls.append({"call": "cancel_order", "id": id, "symbol": symbol, "params": dict(params or {})})
        return {"id": id}


def _broker(exchange: FakeTriggerExchange) -> LiveBroker:
    broker = LiveBroker.__new__(LiveBroker)
    broker.cfg = Config()
    broker.exchange = exchange
    return broker


class TestLiveBrokerTriggerOrders(unittest.TestCase):
    def setUp(self) -> None:
        self.ex = FakeTriggerExchange()
        self.broker = _broker(self.ex)

    def test_open_trigger_order_found_on_perp_symbol(self) -> None:
        self.ex.open_orders = [{"id": "s1", "status": "open", "info": {"planType": "pos_loss"}}]
        order = self.broker.fetch_order("BTC/USDT", "s1")
        self.assertIsNotNone(order)
        self.assertFalse(_is_filled(order))
        first = self.ex.calls[0]
        self.assertEqual(first["symbol"], "BTC/USDT:USDT")
        self.assertEqual(first["params"], {"planType": "profit_loss"})

    def test_executed_trigger_in_history_counts_as_filled(self) -> None:
        self.ex.history = [
            {"id": "s1", "status": "closed", "price": None, "average": None, "triggerPrice": 95.0}
        ]
        order = self.broker.fetch_order("BTC/USDT", "s1")
        self.assertIsNotNone(order)
        self.assertTrue(_is_filled(order))
        self.assertEqual(_extract_fill_price(order, 0.0), 95.0)

    def test_not_found_anywhere_returns_none(self) -> None:
        self.ex.fail_open = ccxt.NetworkError("weg")
        self.ex.fail_history = ccxt.NetworkError("weg")
        self.assertIsNone(self.broker.fetch_order("BTC/USDT", "s1"))

    def test_invalid_symbol(self) -> None:
        self.assertIsNone(self.broker.fetch_order("BTCUSDT", "s1"))
        self.assertFalse(self.broker.cancel_order("BTCUSDT", "s1"))
        self.assertEqual(self.ex.calls, [])

    def test_cancel_open_trigger_uses_plan_type(self) -> None:
        self.ex.open_orders = [{"id": "t1", "status": "open", "info": {"planType": "pos_profit"}}]
        self.assertTrue(self.broker.cancel_order("BTC/USDT", "t1"))
        cancel = [c for c in self.ex.calls if c["call"] == "cancel_order"][0]
        self.assertEqual(cancel["symbol"], "BTC/USDT:USDT")
        self.assertEqual(cancel["params"], {"trigger": True, "planType": "pos_profit"})

    def test_cancel_non_trigger_uses_normal_cancel(self) -> None:
        self.assertTrue(self.broker.cancel_order("BTC/USDT", "n1"))
        cancel = [c for c in self.ex.calls if c["call"] == "cancel_order"][0]
        self.assertEqual(cancel["params"], {})


if __name__ == "__main__":
    unittest.main()