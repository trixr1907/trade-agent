"""Login, Sessions und Passwort-Hashing für die Dashboard-API.

Nur stdlib (hashlib.scrypt, secrets, hmac). Sessions liegen im Speicher und
verfallen nach Ablauf oder Server-Neustart (fail-closed: neu anmelden).

Konfiguration über Env (.env):
    AUTH_USERNAME             Account-Name
    AUTH_PASSWORD_HASH        scrypt-Hash, erzeugt mit: python -m tradeagent.api.auth
    AUTH_SESSION_TTL_MINUTES  Session-Dauer in Minuten (1-1440, Standard 60)

Fehlt Benutzername oder Hash, ist kein Login möglich.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_MAX_PASSWORD_LEN = 1024
_DEFAULT_TTL_MINUTES = 60
_MAX_TTL_MINUTES = 24 * 60


@dataclass
class Session:
    token: str
    account: str
    expires_at: datetime


_SESSIONS: Dict[str, Session] = {}   # Token -> Session (in-memory)
_LOCK = threading.Lock()


# ─── Passwort-Hash ────────────────────────────────────────────────────────────

def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P
    )


def hash_password(password: str, salt: Optional[bytes] = None) -> str:
    """Erzeugt `scrypt$<salt-hex>$<hash-hex>`. Der Klartext wird nicht gespeichert."""
    if not password:
        raise ValueError("Passwort darf nicht leer sein")
    salt = salt or secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${_scrypt(password, salt).hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Prüft das Passwort in konstanter Zeit. Jeder Format-Fehler ergibt False."""
    if not password or len(password) > _MAX_PASSWORD_LEN:
        return False
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        if not salt or not expected:
            return False
        return hmac.compare_digest(_scrypt(password, salt), expected)
    except Exception:
        return False


# ─── Konfiguration ────────────────────────────────────────────────────────────

def _env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1].strip()
    return value


def is_configured() -> bool:
    return bool(_env("AUTH_USERNAME") and _env("AUTH_PASSWORD_HASH"))


def session_ttl_minutes() -> int:
    try:
        ttl = int(_env("AUTH_SESSION_TTL_MINUTES") or _DEFAULT_TTL_MINUTES)
    except ValueError:
        ttl = _DEFAULT_TTL_MINUTES
    return max(1, min(ttl, _MAX_TTL_MINUTES))


def authenticate(username: str, password: str) -> Optional[str]:
    """Gibt den Account-Namen zurück oder None.

    Der Hash wird immer berechnet, auch bei falschem Benutzernamen, damit die
    Antwortzeit nicht verrät, welcher Teil falsch war.
    """
    expected_user = _env("AUTH_USERNAME")
    stored = _env("AUTH_PASSWORD_HASH")
    if not expected_user or not stored:
        return None
    user_ok = hmac.compare_digest(username.encode("utf-8"), expected_user.encode("utf-8"))
    password_ok = verify_password(password, stored)
    return expected_user if (user_ok and password_ok) else None


# ─── Sessions ─────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _purge_expired_unlocked(now: datetime) -> None:
    for token in [t for t, s in _SESSIONS.items() if s.expires_at <= now]:
        del _SESSIONS[token]


def create_session(account: str, ttl_minutes: Optional[int] = None) -> Session:
    ttl = session_ttl_minutes() if ttl_minutes is None else max(1, min(int(ttl_minutes), _MAX_TTL_MINUTES))
    now = _now()
    session = Session(secrets.token_urlsafe(32), account, now + timedelta(minutes=ttl))
    with _LOCK:
        _purge_expired_unlocked(now)
        _SESSIONS[session.token] = session
    return session


def get_session(token: Optional[str]) -> Optional[Session]:
    if not token:
        return None
    with _LOCK:
        session = _SESSIONS.get(token)
        if session is None:
            return None
        if session.expires_at <= _now():
            _SESSIONS.pop(token, None)   # abgelaufen -> fail-closed
            return None
        return session


def revoke_session(token: Optional[str]) -> bool:
    if not token:
        return False
    with _LOCK:
        return _SESSIONS.pop(token, None) is not None


def parse_bearer(header: Optional[str]) -> Optional[str]:
    """Liest das Token aus `Authorization: Bearer <token>`."""
    if not header:
        return None
    scheme, _, value = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


# ─── Setup: Hash erzeugen ─────────────────────────────────────────────────────

def _main() -> int:
    import getpass
    import sys

    password = getpass.getpass("Neues Dashboard-Passwort: ")
    again = getpass.getpass("Passwort wiederholen: ")
    if password != again:
        print("Die Passwörter stimmen nicht überein.", file=sys.stderr)
        return 1
    if len(password) < 12:
        print("Das Passwort braucht mindestens 12 Zeichen.", file=sys.stderr)
        return 1
    print("In .env eintragen (einfache Anführungszeichen beibehalten):")
    print(f"AUTH_PASSWORD_HASH='{hash_password(password)}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())