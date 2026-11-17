"""Tests für Auth (api/auth.py) und Control Plane (control.py).

Kein Netz: Exchange-Pings und Broker werden gemockt, der StateStore läuft auf
einer temporären SQLite-DB.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))

import tradeagent.state as state_mod  # noqa: E402
from tradeagent.api import auth  # noqa: E402
from tradeagent.config import load_config  # noqa: E402
from tradeagent.control import ControlPlane, TradingState, TransitionError  # noqa: E402
from tradeagent.graph import execute  # noqa: E402
from tradeagent.models import ApprovalState  # noqa: E402
from tradeagent.state import StateStore  # noqa: E402

USERNAME = "ivo"
PASSWORD = "korrekt-pferd-batterie-42"
PASSWORD_HASH = auth.hash_password(PASSWORD)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def state_store(tmp_path):
    store = StateStore(path=str(tmp_path / "state.db"))
    yield store
    store.close()


@pytest.fixture()
def api(state_store, monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", USERNAME)
    monkeypatch.setenv("AUTH_PASSWORD_HASH", PASSWORD_HASH)
    monkeypatch.delenv("AUTH_SESSION_TTL_MINUTES", raising=False)
    monkeypatch.setattr(state_mod, "_store_instance", state_store)
    auth._SESSIONS.clear()
    from tradeagent.api import app as app_module
    yield TestClient(app_module.app, raise_server_exceptions=False), app_module
    auth._SESSIONS.clear()


def _login(client) -> str:
    resp = client.post("/login", json={"username": USERNAME, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ═════════════════════════════════════════════════════════════════════════════
# Passwort-Hash
# ═════════════════════════════════════════════════════════════════════════════

class TestPasswordHash:
    def test_hash_contains_no_plaintext(self):
        assert PASSWORD not in PASSWORD_HASH
        assert PASSWORD_HASH.startswith("scrypt$")

    def test_verify_correct_and_wrong(self):
        assert auth.verify_password(PASSWORD, PASSWORD_HASH)
        assert not auth.verify_password("falsch", PASSWORD_HASH)

    def test_salt_differs_per_hash(self):
        assert auth.hash_password(PASSWORD) != PASSWORD_HASH

    def test_malformed_hash_fails_closed(self):
        for stored in ["", "scrypt$zz$zz", "plain", "md5$00$00", PASSWORD]:
            assert not auth.verify_password(PASSWORD, stored)


# ═════════════════════════════════════════════════════════════════════════════
# Login / Logout / Status
# ═════════════════════════════════════════════════════════════════════════════

class TestLogin:
    def test_wrong_password_401(self, api):
        client, _ = api
        resp = client.post("/login", json={"username": USERNAME, "password": "falsch"})
        assert resp.status_code == 401

    def test_wrong_username_401(self, api):
        client, _ = api
        resp = client.post("/login", json={"username": "mallory", "password": PASSWORD})
        assert resp.status_code == 401

    def test_correct_login_returns_token_and_account(self, api):
        client, _ = api
        resp = client.post("/login", json={"username": USERNAME, "password": PASSWORD})
        assert resp.status_code == 200
        data = resp.json()
        assert data["account"] == USERNAME
        assert len(data["token"]) >= 32
        status = client.get("/auth/status", headers=_bearer(data["token"])).json()
        assert status["authenticated"] is True
        assert status["account"] == USERNAME

    def test_status_without_token(self, api):
        client, _ = api
        status = client.get("/auth/status").json()
        assert status["authenticated"] is False
        assert status["account"] is None

    def test_not_configured_503(self, api, monkeypatch):
        client, _ = api
        monkeypatch.delenv("AUTH_PASSWORD_HASH")
        resp = client.post("/login", json={"username": USERNAME, "password": PASSWORD})
        assert resp.status_code == 503

    def test_logout_revokes_session(self, api):
        client, _ = api
        token = _login(client)
        assert client.post("/logout", headers=_bearer(token)).status_code == 200
        assert client.get("/auth/status", headers=_bearer(token)).json()["authenticated"] is False
        assert client.post("/logout", headers=_bearer(token)).status_code == 401


# ═════════════════════════════════════════════════════════════════════════════
# Geschützte Endpunkte
# ═════════════════════════════════════════════════════════════════════════════

PROTECTED = [
    ("POST", "/scan", {}),
    ("POST", "/approve", {"proposal_id": "prop-x"}),
    ("POST", "/reject", {"proposal_id": "prop-x"}),
    ("POST", "/execute", {"proposal_id": "prop-x"}),
    ("GET", "/control/status", None),
    ("POST", "/control/halt", {"reason": "test"}),
    ("POST", "/control/resume", {"reason": "test"}),
]


@pytest.mark.parametrize("method,path,body", PROTECTED)
class TestProtectedEndpoints:
    def test_without_token_401(self, api, method, path, body):
        client, _ = api
        assert client.request(method, path, json=body).status_code == 401

    def test_invalid_token_401(self, api, method, path, body):
        client, _ = api
        resp = client.request(method, path, json=body, headers=_bearer("ungueltig"))
        assert resp.status_code == 401

    def test_wrong_scheme_401(self, api, method, path, body):
        client, _ = api
        token = _login(client)
        resp = client.request(method, path, json=body, headers={"Authorization": f"Basic {token}"})
        assert resp.status_code == 401

    def test_expired_token_401(self, api, method, path, body):
        client, _ = api
        token = _login(client)
        auth._SESSIONS[token].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        resp = client.request(method, path, json=body, headers=_bearer(token))
        assert resp.status_code == 401
        assert token not in auth._SESSIONS


# ═════════════════════════════════════════════════════════════════════════════
# Session-Binding
# ═════════════════════════════════════════════════════════════════════════════

class TestSessionBinding:
    def test_approve_uses_session_account_not_body(self, api):
        client, app_module = api
        token = _login(client)
        with patch.object(app_module, "approve", return_value=None) as approve_mock:
            resp = client.post(
                "/approve",
                json={"proposal_id": "prop-x", "approved_by": "mallory"},
                headers=_bearer(token),
            )
        assert resp.status_code == 404
        assert approve_mock.call_args.args[2] == USERNAME

    def test_halt_actor_is_session_account(self, api):
        client, _ = api
        token = _login(client)
        resp = client.post(
            "/control/halt",
            json={"reason": "Wartung", "actor": "mallory", "changed_by": "mallory"},
            headers=_bearer(token),
        )
        assert resp.status_code == 200
        assert resp.json()["changed_by"] == USERNAME

    def test_logout_cannot_revoke_foreign_session(self, api):
        client, _ = api
        token_a = _login(client)
        token_b = _login(client)
        resp = client.post("/logout", json={"token": token_b}, params={"token": token_b},
                           headers=_bearer(token_a))
        assert resp.status_code == 200
        assert auth.get_session(token_a) is None
        assert auth.get_session(token_b) is not None

    def test_status_ignores_query_account(self, api):
        client, _ = api
        token = _login(client)
        resp = client.get("/auth/status", params={"account": "mallory"}, headers=_bearer(token))
        assert resp.json()["account"] == USERNAME


# ═════════════════════════════════════════════════════════════════════════════
# Control Plane (Statemachine + Persistenz)
# ═════════════════════════════════════════════════════════════════════════════

class TestControlPlane:
    def test_default_is_running(self, state_store):
        plane = ControlPlane(state_store)
        assert plane.get_state().state is TradingState.RUNNING
        assert not plane.is_halted()

    def test_halt_then_resume(self, state_store):
        plane = ControlPlane(state_store)
        halted = plane.halt("ivo", "Wartung")
        assert halted.state is TradingState.HALTED
        assert halted.changed_by == "ivo"
        assert halted.reason == "Wartung"
        assert halted.changed_at
        assert plane.resume("ivo").state is TradingState.RUNNING

    def test_double_halt_raises(self, state_store):
        plane = ControlPlane(state_store)
        plane.halt("ivo")
        with pytest.raises(TransitionError):
            plane.halt("ivo")
        assert plane.is_halted()

    def test_resume_from_running_raises(self, state_store):
        with pytest.raises(TransitionError):
            ControlPlane(state_store).resume("ivo")

    def test_restart_keeps_halted(self, tmp_path):
        path = str(tmp_path / "restart.db")
        first = StateStore(path=path)
        ControlPlane(first).halt("ivo", "Wartung")
        first.close()
        second = StateStore(path=path)
        try:
            state = ControlPlane(second).get_state()
            assert state.state is TradingState.HALTED
            assert state.changed_by == "ivo"
            assert state.reason == "Wartung"
        finally:
            second.close()

    def test_unreadable_state_fails_closed(self):
        broken = MagicMock()
        broken.get_control_state.side_effect = RuntimeError("db weg")
        assert ControlPlane(broken).is_halted()

    def test_unknown_state_value_fails_closed(self):
        store = MagicMock()
        store.get_control_state.return_value = {"state": "bogus"}
        assert ControlPlane(store).is_halted()


# ═════════════════════════════════════════════════════════════════════════════
# execute() fail-closed
# ═════════════════════════════════════════════════════════════════════════════

class _BrokerReached(Exception):
    pass


def _approved_proposal():
    proposal = MagicMock()
    proposal.state = ApprovalState.APPROVED
    proposal.signal.symbol = "BTC/USDT"
    proposal.signal.direction.value = "buy"
    proposal.signal.price = 100.0
    proposal.signal.stop_price = 95.0
    proposal.signal.target_price = 110.0
    proposal.proposed_size_usd = 50.0
    proposal.stop_price = 95.0
    proposal.target_price = 110.0
    return proposal


def _run_execute(state_store):
    proposals = MagicMock()
    proposals.get.return_value = _approved_proposal()
    result = None
    with patch("tradeagent.graph.make_broker", side_effect=_BrokerReached) as make_broker:
        try:
            result = execute(proposals, "prop-x", load_config(), state=state_store)
        except _BrokerReached:
            pass
    return result, make_broker


class TestExecuteFailClosed:
    def test_halted_rejects_without_broker(self, state_store):
        ControlPlane(state_store).halt("ivo")
        result, make_broker = _run_execute(state_store)
        assert result is not None
        assert result.status == "rejected"
        assert "Trading gestoppt" in result.raw["error"]
        assert not make_broker.called

    def test_resumed_reaches_broker(self, state_store):
        plane = ControlPlane(state_store)
        plane.halt("ivo")
        plane.resume("ivo")
        _, make_broker = _run_execute(state_store)
        assert make_broker.called


# ═════════════════════════════════════════════════════════════════════════════
# Control-API + Health
# ═════════════════════════════════════════════════════════════════════════════

class TestControlApi:
    def test_status_and_transitions(self, api):
        client, _ = api
        headers = _bearer(_login(client))
        assert client.get("/control/status", headers=headers).json()["state"] == "running"
        assert client.post("/control/halt", json={"reason": "x"}, headers=headers).json()["state"] == "halted"
        assert client.post("/control/halt", json={"reason": "x"}, headers=headers).status_code == 409
        assert client.post("/control/resume", headers=headers).json()["state"] == "running"
        assert client.post("/control/resume", headers=headers).status_code == 409

    def test_execute_halted_409_without_order(self, api):
        client, app_module = api
        headers = _bearer(_login(client))
        client.post("/control/halt", json={"reason": "x"}, headers=headers)
        with patch.object(app_module, "execute") as execute_mock:
            resp = client.post("/execute", json={"proposal_id": "prop-x"}, headers=headers)
        assert resp.status_code == 409
        assert "Trading gestoppt" in resp.json()["detail"]
        execute_mock.assert_not_called()

    def test_health_shows_trading_state_without_auth(self, api):
        client, app_module = api
        app_module._ping_cache.update({"ts": 0.0, "data": None})
        app_module._keys_cache.update({"ts": 0.0, "data": None})
        with patch.object(app_module, "_ping_exchange", return_value=(True, 1.0)), \
             patch.object(app_module, "_validate_keys", return_value=True):
            assert client.get("/health").json()["trading_state"] == "running"
            client.post("/control/halt", json={"reason": "x"}, headers=_bearer(_login(client)))
            assert client.get("/health").json()["trading_state"] == "halted"