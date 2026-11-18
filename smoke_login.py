"""Login für die manuellen Smoke-Skripte (test_api.py, test_backtest.py).

Benutzername: AUTH_USERNAME aus der .env.
Passwort: Umgebungsvariable TRADEAGENT_PASSWORD, nur beim Aufruf setzen.
In der .env steht nur AUTH_PASSWORD_HASH, das Klartext-Passwort kann also nicht von dort kommen.

Aufruf unter WSL im Repo:
    read -rs TRADEAGENT_PASSWORD && export TRADEAGENT_PASSWORD
    .venv/bin/python test_api.py
    .venv/bin/python test_backtest.py
    unset TRADEAGENT_PASSWORD
"""
from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
from dotenv import load_dotenv


@contextmanager
def authed_client(base: str, timeout: float = 10.0) -> Iterator[httpx.Client]:
    """Meldet sich an, liefert einen Client mit Bearer-Header und meldet am Ende ab."""
    load_dotenv(Path(__file__).resolve().parent / ".env")
    username = os.getenv("AUTH_USERNAME", "").strip()
    password = os.getenv("TRADEAGENT_PASSWORD", "")
    if not username or not password:
        sys.exit("AUTH_USERNAME (.env) und TRADEAGENT_PASSWORD (Umgebung) müssen gesetzt sein.")

    with httpx.Client(timeout=timeout) as client:
        try:
            resp = client.post(f"{base}/login", json={"username": username, "password": password})
        except httpx.HTTPError as exc:
            sys.exit(f"Server nicht erreichbar ({base}): {exc}")
        if resp.status_code != 200:
            sys.exit(f"Login fehlgeschlagen: {resp.status_code} {resp.text[:200]}")
        token = resp.json().get("token")
        if not token:
            sys.exit("Login-Antwort enthält kein Token.")

        client.headers["Authorization"] = f"Bearer {token}"
        try:
            yield client
        finally:
            try:
                client.post(f"{base}/logout")
            except httpx.HTTPError:
                pass