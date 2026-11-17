"""Tests für den /health-Endpunkt (app.py).

Alle Exchange-Calls werden gemockt — kein Netz erforderlich.
"""
from __future__ import annotations

import sys
import os
import time
import importlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))


# ── Hilfsfunktion: Cache zurücksetzen zwischen Tests ─────────────────────────

def _reset_caches(app_module):
    """Setzt die Modul-Level-Caches auf Anfangszustand zurück."""
    app_module._ping_cache["ts"] = 0.0
    app_module._ping_cache["data"] = None
    app_module._keys_cache["ts"] = 0.0
    app_module._keys_cache["data"] = None


# ── Fixture: TestClient mit geclearten Caches ─────────────────────────────────

@pytest.fixture()
def client():
    """Liefert einen frischen TestClient. Caches werden vor jedem Test geleert."""
    from tradeagent.api import app as app_module
    _reset_caches(app_module)
    return TestClient(app_module.app, raise_server_exceptions=False)


@pytest.fixture()
def app_mod():
    from tradeagent.api import app as app_module
    return app_module


# ═════════════════════════════════════════════════════════════════════════════
# 1. Exchange nicht erreichbar → 200 + degraded (kein 500)
# ═════════════════════════════════════════════════════════════════════════════

class TestHealthDegraded:
    def test_returns_200_when_exchange_unreachable(self, client, app_mod):
        _reset_caches(app_mod)
        with patch.object(app_mod, "_ping_exchange", side_effect=Exception("Netz weg")):
            resp = client.get("/health")
        assert resp.status_code == 200

    def test_status_degraded_when_exchange_unreachable(self, client, app_mod):
        _reset_caches(app_mod)
        with patch.object(app_mod, "_ping_exchange", side_effect=Exception("Netz weg")):
            resp = client.get("/health")
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["exchange_connected"] is False

    def test_no_500_on_exchange_error(self, client, app_mod):
        """Sicherheitsnetz: kein HTTP-5xx darf durchkommen."""
        _reset_caches(app_mod)
        with patch.object(app_mod, "_ping_exchange", side_effect=RuntimeError("timeout")):
            for _ in range(3):
                resp = client.get("/health")
                assert resp.status_code < 500, f"Unexpected HTTP {resp.status_code}"


# ═════════════════════════════════════════════════════════════════════════════
# 2. Exchange erreichbar → status=ok, latency_ms vorhanden
# ═════════════════════════════════════════════════════════════════════════════

class TestHealthOk:
    def test_status_ok_when_connected(self, client, app_mod):
        _reset_caches(app_mod)
        with patch.object(app_mod, "_ping_exchange", return_value=(True, 42.0)):
            resp = client.get("/health")
        data = resp.json()
        assert data["status"] == "ok"
        assert data["exchange_connected"] is True

    def test_latency_ms_present(self, client, app_mod):
        _reset_caches(app_mod)
        with patch.object(app_mod, "_ping_exchange", return_value=(True, 37.5)):
            resp = client.get("/health")
        data = resp.json()
        assert "latency_ms" in data
        assert data["latency_ms"] == 37.5

    def test_response_has_required_fields(self, client, app_mod):
        _reset_caches(app_mod)
        required = {
            "status", "mode", "exchange", "llm",
            "exchange_connected", "latency_ms",
            "api_keys_configured", "api_keys_valid",
            "timestamp",
        }
        with patch.object(app_mod, "_ping_exchange", return_value=(True, 10.0)):
            resp = client.get("/health")
        assert required.issubset(resp.json().keys())

    def test_timestamp_present(self, client, app_mod):
        _reset_caches(app_mod)
        with patch.object(app_mod, "_ping_exchange", return_value=(True, 5.0)):
            resp = client.get("/health")
        ts = resp.json().get("timestamp", "")
        assert ts.endswith("+00:00") or ts.endswith("Z"), f"Ungültiger Timestamp: {ts}"


# ═════════════════════════════════════════════════════════════════════════════
# 3. Paper-Modus ohne Keys → api_keys_valid=null, kein Fehler
# ═════════════════════════════════════════════════════════════════════════════

class TestHealthPaperNoKeys:
    def test_keys_valid_null_in_paper_mode(self, client, app_mod):
        """Paper-Modus ohne Keys: api_keys_valid muss null bleiben."""
        _reset_caches(app_mod)
        # Sicherstellen: Paper-Modus, keine Keys
        original_mode = app_mod.cfg.trading.mode
        original_key = app_mod.cfg.exchange.api_key
        app_mod.cfg.trading.mode = "paper"
        app_mod.cfg.exchange.api_key = ""
        app_mod.cfg.exchange.api_secret = ""
        app_mod.cfg.exchange.api_passphrase = ""
        try:
            with patch.object(app_mod, "_ping_exchange", return_value=(True, 5.0)):
                resp = client.get("/health")
            assert resp.json()["api_keys_valid"] is None
        finally:
            app_mod.cfg.trading.mode = original_mode
            app_mod.cfg.exchange.api_key = original_key

    def test_api_keys_configured_false_without_keys(self, client, app_mod):
        _reset_caches(app_mod)
        original_key = app_mod.cfg.exchange.api_key
        app_mod.cfg.exchange.api_key = ""
        app_mod.cfg.exchange.api_secret = ""
        app_mod.cfg.exchange.api_passphrase = ""
        try:
            with patch.object(app_mod, "_ping_exchange", return_value=(True, 5.0)):
                resp = client.get("/health")
            assert resp.json()["api_keys_configured"] is False
        finally:
            app_mod.cfg.exchange.api_key = original_key


# ═════════════════════════════════════════════════════════════════════════════
# 4. Caching: zweiter Aufruf soll keinen zweiten Ping auslösen
# ═════════════════════════════════════════════════════════════════════════════

class TestHealthCaching:
    def test_ping_called_only_once_within_cache_window(self, client, app_mod):
        _reset_caches(app_mod)
        call_count = {"n": 0}

        def _mock_ping():
            call_count["n"] += 1
            return True, 20.0

        with patch.object(app_mod, "_ping_exchange", side_effect=_mock_ping):
            client.get("/health")
            client.get("/health")  # zweiter Aufruf — Cache noch frisch

        assert call_count["n"] == 1, (
            f"_ping_exchange wurde {call_count['n']}x aufgerufen; erwartet 1"
        )

    def test_ping_refreshed_after_cache_expires(self, client, app_mod):
        _reset_caches(app_mod)
        call_count = {"n": 0}

        def _mock_ping():
            call_count["n"] += 1
            return True, 20.0

        with patch.object(app_mod, "_ping_exchange", side_effect=_mock_ping):
            client.get("/health")
            # Cache-Timestamp künstlich veralten
            app_mod._ping_cache["ts"] = time.time() - 31
            client.get("/health")

        assert call_count["n"] == 2, (
            f"_ping_exchange wurde {call_count['n']}x aufgerufen; erwartet 2 (Cache-Refresh)"
        )


# ═════════════════════════════════════════════════════════════════════════════
# 5. deep=1 mit Keys → api_keys_valid ist bool
# ═════════════════════════════════════════════════════════════════════════════

class TestHealthDeepCheck:
    def test_deep_with_valid_keys_returns_true(self, client, app_mod):
        _reset_caches(app_mod)
        # Keys simulieren
        app_mod.cfg.exchange.api_key = "testkey"
        app_mod.cfg.exchange.api_secret = "testsecret"
        app_mod.cfg.exchange.api_passphrase = "testpass"
        original_mode = app_mod.cfg.trading.mode
        app_mod.cfg.trading.mode = "paper"  # Paper, aber deep=1 erzwingt Prüfung
        try:
            with patch.object(app_mod, "_ping_exchange", return_value=(True, 5.0)):
                with patch.object(app_mod, "_validate_keys", return_value=True):
                    resp = client.get("/health?deep=1")
            data = resp.json()
            assert isinstance(data["api_keys_valid"], bool)
            assert data["api_keys_valid"] is True
        finally:
            app_mod.cfg.trading.mode = original_mode
            app_mod.cfg.exchange.api_key = ""
            app_mod.cfg.exchange.api_secret = ""
            app_mod.cfg.exchange.api_passphrase = ""

    def test_deep_with_invalid_keys_returns_false(self, client, app_mod):
        _reset_caches(app_mod)
        app_mod.cfg.exchange.api_key = "badkey"
        app_mod.cfg.exchange.api_secret = "badsecret"
        app_mod.cfg.exchange.api_passphrase = "badpass"
        original_mode = app_mod.cfg.trading.mode
        app_mod.cfg.trading.mode = "paper"
        try:
            with patch.object(app_mod, "_ping_exchange", return_value=(True, 5.0)):
                with patch.object(app_mod, "_validate_keys", return_value=False):
                    resp = client.get("/health?deep=1")
            data = resp.json()
            assert data["api_keys_valid"] is False
        finally:
            app_mod.cfg.trading.mode = original_mode
            app_mod.cfg.exchange.api_key = ""
            app_mod.cfg.exchange.api_secret = ""
            app_mod.cfg.exchange.api_passphrase = ""

    def test_no_deep_without_live_mode_skips_key_check(self, client, app_mod):
        """Ohne deep=1 und ohne Live-Modus soll _validate_keys nie aufgerufen werden."""
        _reset_caches(app_mod)
        app_mod.cfg.exchange.api_key = "somekey"
        app_mod.cfg.exchange.api_secret = "somesecret"
        app_mod.cfg.exchange.api_passphrase = "somepass"
        original_mode = app_mod.cfg.trading.mode
        app_mod.cfg.trading.mode = "paper"
        validate_called = {"n": 0}

        def _mock_validate():
            validate_called["n"] += 1
            return True

        try:
            with patch.object(app_mod, "_ping_exchange", return_value=(True, 5.0)):
                with patch.object(app_mod, "_validate_keys", side_effect=_mock_validate):
                    resp = client.get("/health")  # kein deep=1
            assert validate_called["n"] == 0, "_validate_keys darf ohne deep=1/Live nicht aufgerufen werden"
            assert resp.json()["api_keys_valid"] is None
        finally:
            app_mod.cfg.trading.mode = original_mode
            app_mod.cfg.exchange.api_key = ""
            app_mod.cfg.exchange.api_secret = ""
            app_mod.cfg.exchange.api_passphrase = ""