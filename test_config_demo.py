"""Tests für die Demo/Live-Trennung in config.py und relay._build_exchange. Kein Netz."""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from tradeagent.config import load_config  # noqa: E402
from tradeagent.relay import _build_exchange  # noqa: E402

_VARS = (
    "TRADING_MODE",
    "BITGET_DEMO",
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


def _set_demo_keys(monkeypatch, key: str = "demo-key") -> None:
    monkeypatch.setenv("BITGET_DEMO_API_KEY", key)
    monkeypatch.setenv("BITGET_DEMO_API_SECRET", "demo-secret")
    monkeypatch.setenv("BITGET_DEMO_API_PASSPHRASE", "demo-pass")


def test_default_is_not_demo_and_uses_live_keys(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    _set_live_keys(monkeypatch)
    _set_demo_keys(monkeypatch)
    cfg = load_config()
    assert cfg.exchange.demo is False
    assert cfg.exchange.api_key == "live-key"


def test_demo_uses_only_demo_keys(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("BITGET_DEMO", "true")
    _set_live_keys(monkeypatch)
    _set_demo_keys(monkeypatch)
    cfg = load_config()
    assert cfg.exchange.demo is True
    assert cfg.exchange.api_key == "demo-key"
    assert cfg.exchange.api_secret == "demo-secret"
    assert cfg.exchange.api_passphrase == "demo-pass"


def test_demo_without_demo_keys_never_falls_back_to_live(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("BITGET_DEMO", "true")
    _set_live_keys(monkeypatch)
    with pytest.raises(RuntimeError, match="BITGET_DEMO_API"):
        load_config()


def test_demo_key_equal_to_live_key_rejected(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("BITGET_DEMO", "true")
    _set_live_keys(monkeypatch)
    _set_demo_keys(monkeypatch, key="live-key")
    with pytest.raises(RuntimeError, match="identisch"):
        load_config()


def test_invalid_demo_flag_rejected_in_live(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("BITGET_DEMO", "vielleicht")
    _set_live_keys(monkeypatch)
    with pytest.raises(RuntimeError, match="BITGET_DEMO"):
        load_config()


def test_relay_exchange_sandbox_follows_demo_flag(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("BITGET_DEMO", "true")
    _set_demo_keys(monkeypatch)
    demo_ex = _build_exchange(load_config())
    assert demo_ex.options.get("sandboxMode") is True

    monkeypatch.setenv("BITGET_DEMO", "false")
    _set_live_keys(monkeypatch)
    live_ex = _build_exchange(load_config())
    assert live_ex.options.get("sandboxMode") is False


def test_live_broker_sandbox_follows_demo_flag(monkeypatch):
    from tradeagent.execution import LiveBroker

    monkeypatch.setenv("TRADING_MODE", "live")
    monkeypatch.setenv("BITGET_DEMO", "true")
    _set_demo_keys(monkeypatch)
    assert LiveBroker(load_config()).exchange.options.get("sandboxMode") is True

    monkeypatch.setenv("BITGET_DEMO", "false")
    _set_live_keys(monkeypatch)
    assert LiveBroker(load_config()).exchange.options.get("sandboxMode") is False