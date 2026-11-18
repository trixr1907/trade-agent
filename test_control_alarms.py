"""API-Tests für das Alarm-Gate: Resume nur ohne unbestätigte Alarme. Kein Netz."""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(__file__))

import tradeagent.state as state_mod  # noqa: E402
from tradeagent.api import auth  # noqa: E402
from tradeagent.state import StateStore  # noqa: E402

USERNAME = "ivo"
PASSWORD = "korrekt-pferd-batterie-42"
PASSWORD_HASH = auth.hash_password(PASSWORD)


@pytest.fixture()
def state_store(tmp_path):
    store = StateStore(path=str(tmp_path / "state.db"))
    yield store
    store.close()


@pytest.fixture()
def client(state_store, monkeypatch):
    monkeypatch.setenv("AUTH_USERNAME", USERNAME)
    monkeypatch.setenv("AUTH_PASSWORD_HASH", PASSWORD_HASH)
    monkeypatch.delenv("AUTH_SESSION_TTL_MINUTES", raising=False)
    monkeypatch.setattr(state_mod, "_store_instance", state_store)
    auth._SESSIONS.clear()
    from tradeagent.api import app as app_module
    yield TestClient(app_module.app, raise_server_exceptions=False)
    auth._SESSIONS.clear()


def _headers(client) -> dict:
    resp = client.post("/login", json={"username": USERNAME, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


class TestAlarmGate:
    def test_resume_blocked_until_alarm_acknowledged(self, client, state_store):
        headers = _headers(client)
        assert client.post("/control/halt", json={"reason": "x"}, headers=headers).status_code == 200
        alarm_id = state_store.raise_alarm("unprotected", "Stop konnte nicht platziert werden", "prop-1")

        resp = client.post("/control/resume", headers=headers)
        assert resp.status_code == 409
        assert client.get("/control/status", headers=headers).json()["state"] == "halted"

        alarms = client.get("/control/alarms", headers=headers).json()["alarms"]
        assert [a["id"] for a in alarms] == [alarm_id]

        ack = client.post(f"/control/alarms/{alarm_id}/ack", headers=headers)
        assert ack.status_code == 200
        assert ack.json() == {"acknowledged": alarm_id}
        assert client.get("/control/alarms", headers=headers).json()["alarms"] == []

        assert client.post("/control/resume", headers=headers).json()["state"] == "running"

    def test_ack_twice_or_unknown_is_404(self, client, state_store):
        headers = _headers(client)
        alarm_id = state_store.raise_alarm("relay", "Test")
        assert client.post(f"/control/alarms/{alarm_id}/ack", headers=headers).status_code == 200
        assert client.post(f"/control/alarms/{alarm_id}/ack", headers=headers).status_code == 404
        assert client.post("/control/alarms/99999/ack", headers=headers).status_code == 404

    def test_alarm_routes_require_auth(self, client, state_store):
        alarm_id = state_store.raise_alarm("relay", "Test")
        assert client.get("/control/alarms").status_code == 401
        assert client.post(f"/control/alarms/{alarm_id}/ack").status_code == 401
        assert len(state_store.open_alarms()) == 1