"""Tests für BITGET_UTA: fester Bitget-Kontotyp in config.py und relay._build_exchange. Kein Netz."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from tradeagent.config import load_config  # noqa: E402
from tradeagent.data import create_exchange  # noqa: E402
from tradeagent.execution import LiveBroker  # noqa: E402
from tradeagent.relay import _build_exchange  # noqa: E402

_VARS = (
    "TRADING_MODE",
    "BITGET_DEMO",
    "BITGET_UTA",
    "BITGET_API_KEY",
    "BITGET_API_SECRET",
    "BITGET_API_PASSPHRASE",
    "BITGET_DEMO_API_KEY",
    "BITGET_DEMO_API_SECRET",
    "BITGET_DEMO_API_PASSPHRASE",
    "BITGET_MARGIN_MODE",
    "BITGET_LEVERAGE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    try:
        import dotenv

        monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)
    except ImportError:
        pass
    for name in _VARS:
        monkeypatch.delenv(name, raising=False)


def _set_live_keys(monkeypatch) -> None:
    monkeypatch.setenv("BITGET_API_KEY", "live-key")
    monkeypatch.setenv("BITGET_API_SECRET", "live-secret")
    monkeypatch.setenv("BITGET_API_PASSPHRASE", "live-pass")


def _set_demo_keys(monkeypatch) -> None:
    monkeypatch.setenv("BITGET_DEMO_API_KEY", "demo-key")
    monkeypatch.setenv("BITGET_DEMO_API_SECRET", "demo-secret")
    monkeypatch.setenv("BITGET_DEMO_API_PASSPHRASE", "demo-pass")


def test_default_uta_is_false(monkeypatch):
    _set_live_keys(monkeypatch)
    cfg = load_config()
    assert cfg.exchange.uta is False
    assert _build_exchange(cfg).options["uta"] is False


def test_bitget_uta_true_sets_option(monkeypatch):
    _set_live_keys(monkeypatch)
    monkeypatch.setenv("BITGET_UTA", "true")
    cfg = load_config()
    assert cfg.exchange.uta is True
    assert _build_exchange(cfg).options["uta"] is True


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("true", True),
        ("True", True),
        ("1", True),
        ("false", False),
        ("False", False),
        ("0", False),
        (" true ", True),
        ("", False),
    ],
)
def test_bitget_uta_parsing(monkeypatch, raw, expected):
    monkeypatch.setenv("BITGET_UTA", raw)
    assert load_config().exchange.uta is expected


@pytest.mark.parametrize("mode", ["paper", "live"])
@pytest.mark.parametrize("raw", ["yes", "no", "TRUE", "on", "2", "flase", "true,false"])
def test_bitget_uta_invalid_fails_closed(monkeypatch, mode, raw):
    _set_live_keys(monkeypatch)
    monkeypatch.setenv("TRADING_MODE", mode)
    monkeypatch.setenv("BITGET_UTA", raw)
    with pytest.raises(RuntimeError, match="BITGET_UTA"):
        load_config()


def test_demo_mode_keeps_fixed_uta(monkeypatch):
    monkeypatch.setenv("BITGET_DEMO", "true")
    _set_demo_keys(monkeypatch)
    exchange = _build_exchange(load_config())
    assert exchange.options.get("sandboxMode") is True
    assert exchange.options["uta"] is False


@pytest.mark.parametrize("raw, expected", [("false", False), ("true", True)])
def test_auto_detection_does_not_override(monkeypatch, raw, expected):
    _set_live_keys(monkeypatch)
    monkeypatch.setenv("BITGET_UTA", raw)
    exchange = _build_exchange(load_config())
    calls = []

    def fake_settings(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Auto-Erkennung über /api/v3/account/settings darf nicht laufen")

    exchange.privateUtaGetV3AccountSettings = fake_settings
    for method in ("createOrder", "cancelOrder", "fetchOrder", "fetchOpenOrders", "fetchBalance"):
        uta, _ = exchange.handle_uta_and_params({}, method, not expected)
        assert uta is expected, method
    assert calls == []
    assert exchange.options["uta"] is expected


@pytest.mark.parametrize("raw, expected", [("false", False), ("true", True)])
def test_create_exchange_uses_fixed_uta(monkeypatch, raw, expected):
    monkeypatch.setenv("BITGET_UTA", raw)
    assert create_exchange(load_config().exchange).options["uta"] is expected


def test_create_exchange_default_uta_false(monkeypatch):
    assert create_exchange(load_config().exchange).options["uta"] is False


@pytest.mark.parametrize("raw, expected", [("false", False), ("true", True)])
def test_live_broker_uses_fixed_uta(monkeypatch, raw, expected):
    _set_live_keys(monkeypatch)
    monkeypatch.setenv("BITGET_UTA", raw)
    exchange = LiveBroker(load_config()).exchange
    calls = []

    def fake_settings(*args, **kwargs):
        calls.append(args)
        raise AssertionError("Auto-Erkennung über /api/v3/account/settings darf nicht laufen")

    exchange.privateUtaGetV3AccountSettings = fake_settings
    for method in ("cancelOrder", "fetchOrder", "fetchOpenOrders"):
        uta, _ = exchange.handle_uta_and_params({}, method, not expected)
        assert uta is expected, method
    assert calls == []
    assert exchange.options["uta"] is expected


def test_live_broker_demo_keeps_fixed_uta(monkeypatch):
    monkeypatch.setenv("BITGET_DEMO", "true")
    _set_demo_keys(monkeypatch)
    exchange = LiveBroker(load_config()).exchange
    assert exchange.options.get("sandboxMode") is True
    assert exchange.options["uta"] is False