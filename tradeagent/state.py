"""Persistente State-Schicht: offene Positionen + P&L / Drawdown-Tracking.

SQLite-basiert — überlebt Server-Restart. Thread-safe via check_same_thread=False
+ explizite Locks für konkurrierende Requests.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

_INITIAL_EQUITY = 1000.0  # Startkapital für Drawdown-Basis; überschreibbar via StateStore(initial_equity=...)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Datenmodell ──────────────────────────────────────────────────────────────

@dataclass
class OpenPosition:
    proposal_id: str
    symbol: str
    side: str            # "buy" | "sell"
    qty: float
    entry_price: float
    notional_usd: float
    stop_price: float
    target_price: float
    opened_at: str       # ISO-8601
    # Exchange-Order-IDs für Stop/Target — None wenn Paper oder noch nicht gesetzt
    stop_order_id: Optional[str] = None
    target_order_id: Optional[str] = None


def _row_to_open_pos(r) -> "OpenPosition":
    """Konvertiert eine SQLite-Row in ein OpenPosition-Dataclass."""
    # stop_order_id / target_order_id können fehlen (ältere DB ohne Migration)
    keys = r.keys() if hasattr(r, "keys") else []
    return OpenPosition(
        proposal_id=r["proposal_id"],
        symbol=r["symbol"],
        side=r["side"],
        qty=r["qty"],
        entry_price=r["entry_price"],
        notional_usd=r["notional_usd"],
        stop_price=r["stop_price"],
        target_price=r["target_price"],
        opened_at=r["opened_at"],
        stop_order_id=r["stop_order_id"] if "stop_order_id" in keys else None,
        target_order_id=r["target_order_id"] if "target_order_id" in keys else None,
    )


# ─── StateStore ───────────────────────────────────────────────────────────────

def _epoch_to_iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()


class StateStore:
    """Persistente State: offene Positionen + realisierter P&L / Equity-Kurve.

    Alle Schreiboperationen sind transaktional (SQLite). Thread-safe.
    """

    def __init__(self, path: str = "trade-agent-state.db", initial_equity: float = _INITIAL_EQUITY):
        self._path = path
        self._initial_equity = initial_equity
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    # ── Schema ────────────────────────────────────────────────────────────────

    def _migrate(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.executescript("""
                CREATE TABLE IF NOT EXISTS open_positions (
                    proposal_id    TEXT PRIMARY KEY,
                    symbol         TEXT NOT NULL,
                    side           TEXT NOT NULL,
                    qty            REAL NOT NULL,
                    entry_price    REAL NOT NULL,
                    notional_usd   REAL NOT NULL,
                    stop_price     REAL NOT NULL,
                    target_price   REAL NOT NULL,
                    opened_at      TEXT NOT NULL,
                    stop_order_id  TEXT,
                    target_order_id TEXT
                );

                CREATE TABLE IF NOT EXISTS closed_positions (
                    proposal_id  TEXT PRIMARY KEY,
                    symbol       TEXT NOT NULL,
                    side         TEXT NOT NULL,
                    qty          REAL NOT NULL,
                    entry_price  REAL NOT NULL,
                    exit_price   REAL NOT NULL,
                    notional_usd REAL NOT NULL,
                    stop_price   REAL NOT NULL,
                    target_price REAL NOT NULL,
                    opened_at    TEXT NOT NULL,
                    closed_at    TEXT NOT NULL,
                    pnl_usd      REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS pnl_ledger (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    pnl_usd    REAL NOT NULL,
                    equity     REAL NOT NULL,
                    recorded_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS equity_meta (
                    key   TEXT PRIMARY KEY,
                    value REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS control_state (
                    id          INTEGER PRIMARY KEY CHECK (id = 1),
                    state       TEXT NOT NULL,
                    changed_by  TEXT,
                    changed_at  TEXT,
                    reason      TEXT
                );

                -- Relay-Belege. Eine Zeile pro (Intent, Rolle). Der Unique-Constraint
                -- ist der Idempotenz-Schutz: ein zweiter Reserve-Versuch legt nichts an.
                CREATE TABLE IF NOT EXISTS order_receipts (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    intent_id       TEXT NOT NULL,
                    role            TEXT NOT NULL,
                    client_order_id TEXT NOT NULL UNIQUE,
                    symbol          TEXT NOT NULL,
                    side            TEXT NOT NULL,
                    qty             REAL NOT NULL,
                    status          TEXT NOT NULL,
                    order_id        TEXT,
                    fill_price      REAL,
                    reason          TEXT,
                    created_at      TEXT NOT NULL,
                    updated_at      TEXT NOT NULL,
                    UNIQUE (intent_id, role)
                );

                CREATE TABLE IF NOT EXISTS alarms (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind             TEXT NOT NULL,
                    message          TEXT NOT NULL,
                    ref              TEXT,
                    created_at       TEXT NOT NULL,
                    acknowledged_at  TEXT,
                    acknowledged_by  TEXT
                );

                -- Abgeschlossene Scan-Läufe (Direktive 5A). Ein Eintrag pro Job.
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id            TEXT NOT NULL UNIQUE,
                    status            TEXT NOT NULL,
                    started_at        TEXT NOT NULL,
                    finished_at       TEXT NOT NULL,
                    total             INTEGER NOT NULL,
                    processed         INTEGER NOT NULL,
                    failed            INTEGER NOT NULL,
                    signals_found     INTEGER NOT NULL,
                    proposals_created INTEGER NOT NULL,
                    error             TEXT,
                    meta              TEXT NOT NULL
                );
            """)
            # Equity-Peak initialisieren falls noch nicht vorhanden
            cur.execute(
                "INSERT OR IGNORE INTO equity_meta (key, value) VALUES (?, ?)",
                ("equity_peak", self._initial_equity),
            )
            self._conn.commit()

    # ── Positions ─────────────────────────────────────────────────────────────

    def open_positions(self) -> list[OpenPosition]:
        """Gibt alle aktuell offenen Positionen zurück."""
        with self._lock:
            cur = self._conn.execute("SELECT * FROM open_positions")
            rows = cur.fetchall()
        return [_row_to_open_pos(r) for r in rows]

    def open_position_for_symbol(self, symbol: str) -> Optional[OpenPosition]:
        """Gibt die offene Position für ein Symbol zurück, oder None."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM open_positions WHERE symbol = ? LIMIT 1",
                (symbol.upper(),),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return _row_to_open_pos(row)

    def open_count(self) -> int:
        """Anzahl aktuell offener Positionen."""
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM open_positions")
            return int(cur.fetchone()[0])

    def add_position(self, pos: OpenPosition) -> None:
        """Fügt eine neue offene Position hinzu (transaktional)."""
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO open_positions
                    (proposal_id, symbol, side, qty, entry_price, notional_usd,
                     stop_price, target_price, opened_at, stop_order_id, target_order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos.proposal_id,
                    pos.symbol.upper(),
                    pos.side,
                    pos.qty,
                    pos.entry_price,
                    pos.notional_usd,
                    pos.stop_price,
                    pos.target_price,
                    pos.opened_at,
                    pos.stop_order_id,
                    pos.target_order_id,
                ),
            )
            self._conn.commit()

    def update_stop_order_ids(
        self,
        proposal_id: str,
        stop_order_id: Optional[str],
        target_order_id: Optional[str],
    ) -> None:
        """Aktualisiert die Exchange-Order-IDs für Stop/Target nach dem Place."""
        with self._lock:
            self._conn.execute(
                """
                UPDATE open_positions
                SET stop_order_id = ?, target_order_id = ?
                WHERE proposal_id = ?
                """,
                (stop_order_id, target_order_id, proposal_id),
            )
            self._conn.commit()

    # ── Order-Belege (Relay) ──────────────────────────────────────────────────

    def reserve_receipt(
        self,
        intent_id: str,
        role: str,
        client_order_id: str,
        symbol: str,
        side: str,
        qty: float,
    ) -> Tuple[Dict[str, Any], bool]:
        """Reserviert einen Beleg im Status 'pending'.

        Gibt (beleg, reserviert) zurück. reserviert=False heisst: Für diesen
        Intent und diese Rolle existiert schon ein Beleg, es darf keine Order
        gesendet werden. Ausnahme: Ein Beleg im Status 'rejected' (nichts
        gesendet) wird atomar wieder auf 'pending' gesetzt.
        """
        now = _now_iso()
        with self._lock:
            try:
                self._conn.execute(
                    """
                    INSERT INTO order_receipts
                        (intent_id, role, client_order_id, symbol, side, qty,
                         status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                    """,
                    (intent_id, role, client_order_id, symbol, side, qty, now, now),
                )
                self._conn.commit()
                reserved = True
            except sqlite3.IntegrityError:
                self._conn.rollback()
                cur = self._conn.execute(
                    """
                    UPDATE order_receipts
                    SET status = 'pending', reason = NULL, qty = ?, updated_at = ?
                    WHERE intent_id = ? AND role = ? AND status = 'rejected'
                    """,
                    (qty, now, intent_id, role),
                )
                self._conn.commit()
                reserved = cur.rowcount == 1
            cur = self._conn.execute(
                "SELECT * FROM order_receipts WHERE intent_id = ? AND role = ?",
                (intent_id, role),
            )
            row = cur.fetchone()
        if row is None:
            raise RuntimeError(
                f"Beleg für {intent_id}/{role} nicht reservierbar (client_order_id-Kollision)."
            )
        return dict(row), reserved

    def update_receipt(
        self,
        intent_id: str,
        role: str,
        status: str,
        order_id: Optional[str] = None,
        fill_price: Optional[float] = None,
        reason: Optional[str] = None,
    ) -> None:
        """Aktualisiert Status und Ergebnis eines Belegs. None-Werte bleiben unverändert."""
        with self._lock:
            self._conn.execute(
                """
                UPDATE order_receipts
                SET status = ?,
                    order_id = COALESCE(?, order_id),
                    fill_price = COALESCE(?, fill_price),
                    reason = COALESCE(?, reason),
                    updated_at = ?
                WHERE intent_id = ? AND role = ?
                """,
                (status, order_id, fill_price, reason, _now_iso(), intent_id, role),
            )
            self._conn.commit()

    def get_receipt(self, intent_id: str, role: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM order_receipts WHERE intent_id = ? AND role = ?",
                (intent_id, role),
            )
            row = cur.fetchone()
        return dict(row) if row is not None else None

    def list_receipts(self, intent_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            if intent_id is None:
                cur = self._conn.execute("SELECT * FROM order_receipts ORDER BY id")
            else:
                cur = self._conn.execute(
                    "SELECT * FROM order_receipts WHERE intent_id = ? ORDER BY id",
                    (intent_id,),
                )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    # ── Alarme ────────────────────────────────────────────────────────────────

    def raise_alarm(self, kind: str, message: str, ref: Optional[str] = None) -> int:
        """Legt einen unbestätigten Alarm an und gibt seine ID zurück."""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO alarms (kind, message, ref, created_at) VALUES (?, ?, ?, ?)",
                (kind, message, ref, _now_iso()),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def open_alarms(self) -> List[Dict[str, Any]]:
        """Alle noch nicht bestätigten Alarme."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM alarms WHERE acknowledged_at IS NULL ORDER BY id"
            )
            rows = cur.fetchall()
        return [dict(r) for r in rows]

    def acknowledge_alarm(self, alarm_id: int, actor: str) -> bool:
        """Bestätigt einen Alarm. False, wenn er nicht existiert oder schon bestätigt ist."""
        with self._lock:
            cur = self._conn.execute(
                """
                UPDATE alarms SET acknowledged_at = ?, acknowledged_by = ?
                WHERE id = ? AND acknowledged_at IS NULL
                """,
                (_now_iso(), actor, alarm_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def close_position(
        self,
        proposal_id: str,
        exit_price: float,
        pnl_usd: float,
    ) -> Optional[OpenPosition]:
        """Schliesst eine offene Position, schreibt sie ins Closed-Log,
        und aktualisiert den P&L-Ledger. Gibt die geschlossene Position zurück."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM open_positions WHERE proposal_id = ?",
                (proposal_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None

            pos = _row_to_open_pos(row)
            closed_at = _now_iso()
            new_equity = self._equity_now_unlocked() + pnl_usd

            self._conn.execute(
                """
                INSERT OR REPLACE INTO closed_positions
                    (proposal_id, symbol, side, qty, entry_price, exit_price,
                     notional_usd, stop_price, target_price, opened_at, closed_at, pnl_usd)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pos.proposal_id, pos.symbol, pos.side, pos.qty,
                    pos.entry_price, exit_price, pos.notional_usd,
                    pos.stop_price, pos.target_price,
                    pos.opened_at, closed_at, pnl_usd,
                ),
            )
            self._conn.execute(
                "DELETE FROM open_positions WHERE proposal_id = ?",
                (proposal_id,),
            )
            self._conn.execute(
                "INSERT INTO pnl_ledger (pnl_usd, equity, recorded_at) VALUES (?, ?, ?)",
                (pnl_usd, new_equity, closed_at),
            )
            # Peak aktualisieren
            cur2 = self._conn.execute(
                "SELECT value FROM equity_meta WHERE key = 'equity_peak'"
            )
            peak_row = cur2.fetchone()
            current_peak = float(peak_row[0]) if peak_row else self._initial_equity
            if new_equity > current_peak:
                self._conn.execute(
                    "INSERT OR REPLACE INTO equity_meta (key, value) VALUES (?, ?)",
                    ("equity_peak", new_equity),
                )
            self._conn.commit()
        return pos

    # ── P&L / Drawdown ────────────────────────────────────────────────────────

    def realized_pnl_total(self) -> float:
        """Summe aller realisierten P&L-Einträge."""
        with self._lock:
            cur = self._conn.execute("SELECT SUM(pnl_usd) FROM pnl_ledger")
            val = cur.fetchone()[0]
        return float(val) if val is not None else 0.0

    def equity_peak(self) -> float:
        """Historischer Equity-Peak (inklusive Startkapital)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT value FROM equity_meta WHERE key = 'equity_peak'"
            )
            row = cur.fetchone()
        return float(row[0]) if row else self._initial_equity

    def _equity_now_unlocked(self) -> float:
        """Aktuelle Equity — darf nur innerhalb eines bestehenden Locks aufgerufen werden."""
        cur = self._conn.execute("SELECT SUM(pnl_usd) FROM pnl_ledger")
        val = cur.fetchone()[0]
        realized = float(val) if val is not None else 0.0
        return self._initial_equity + realized

    def current_equity(self) -> float:
        """Aktuelle Equity (Startkapital + realisierter P&L)."""
        return self._initial_equity + self.realized_pnl_total()

    def current_drawdown_pct(self) -> float:
        """Aktueller Drawdown als Bruchteil (0.0 – 1.0).

        drawdown = (peak – equity) / peak
        Gibt 0.0 zurück, wenn kein Drawdown vorhanden.
        """
        peak = self.equity_peak()
        equity = self.current_equity()
        if peak <= 0:
            return 0.0
        dd = (peak - equity) / peak
        return max(dd, 0.0)

    def record_pnl(self, pnl_usd: float) -> None:
        """Direkter P&L-Eintrag (z.B. manuell oder aus Stop-Monitor).
        Aktualisiert Realisierung + Equity-Peak.
        """
        with self._lock:
            new_equity = self._equity_now_unlocked() + pnl_usd
            now = _now_iso()
            self._conn.execute(
                "INSERT INTO pnl_ledger (pnl_usd, equity, recorded_at) VALUES (?, ?, ?)",
                (pnl_usd, new_equity, now),
            )
            cur = self._conn.execute(
                "SELECT value FROM equity_meta WHERE key = 'equity_peak'"
            )
            row = cur.fetchone()
            current_peak = float(row[0]) if row else self._initial_equity
            if new_equity > current_peak:
                self._conn.execute(
                    "INSERT OR REPLACE INTO equity_meta (key, value) VALUES (?, ?)",
                    ("equity_peak", new_equity),
                )
            self._conn.commit()

    # ── Housekeeping ──────────────────────────────────────────────────────────

    # ── Control Plane (Halt/Resume) ───────────────────────────────────────────

    # ── Scan-Läufe (Direktive 5A) ─────────────────────────────────────────────

    def record_scan_summary(
        self,
        *,
        job_id: str,
        status: str,
        started_at: float,
        finished_at: float,
        total: int,
        processed: int,
        failed: int,
        signals_found: int,
        proposals_created: int,
        error: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Speichert das Ergebnis eines abgeschlossenen Scans."""
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO scan_runs (
                    job_id, status, started_at, finished_at, total, processed,
                    failed, signals_found, proposals_created, error, meta
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    status,
                    _epoch_to_iso(started_at),
                    _epoch_to_iso(finished_at),
                    int(total),
                    int(processed),
                    int(failed),
                    int(signals_found),
                    int(proposals_created),
                    error,
                    json.dumps(meta or {}, default=str),
                ),
            )
            self._conn.commit()

    def last_scan_summary(self) -> Optional[Dict[str, Any]]:
        """Letzter abgeschlossener Scan oder None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id, status, started_at, finished_at, total, processed, failed, "
                "signals_found, proposals_created, error, meta "
                "FROM scan_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        summary = dict(row)
        try:
            summary["meta"] = json.loads(summary.get("meta") or "{}")
        except ValueError:
            summary["meta"] = {}
        return summary

    def realized_pnl_by_symbol(self) -> Dict[str, Dict[str, Any]]:
        """Realisierter P&L und Anzahl geschlossener Trades pro Symbol."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol, SUM(pnl_usd) AS pnl, COUNT(*) AS trades "
                "FROM closed_positions GROUP BY symbol"
            ).fetchall()
        return {
            r["symbol"]: {
                "realized_pnl_usd": round(float(r["pnl"] or 0.0), 2),
                "trades": int(r["trades"]),
            }
            for r in rows
        }

    def get_control_state(self) -> Optional[dict]:
        """Globaler Trading-State oder None, wenn er nie gesetzt wurde."""
        with self._lock:
            row = self._conn.execute(
                "SELECT state, changed_by, changed_at, reason FROM control_state WHERE id = 1"
            ).fetchone()
        return dict(row) if row is not None else None

    def transition_control_state(
        self,
        *,
        expected: str,
        new_state: str,
        changed_by: str,
        reason: Optional[str] = None,
        default_state: str = "running",
    ) -> bool:
        """Atomares Compare-and-Set: schreibt nur, wenn der aktuelle State `expected` ist."""
        with self._lock:
            row = self._conn.execute(
                "SELECT state FROM control_state WHERE id = 1"
            ).fetchone()
            current = row["state"] if row is not None else default_state
            if current != expected:
                return False
            self._conn.execute(
                "INSERT OR REPLACE INTO control_state (id, state, changed_by, changed_at, reason) "
                "VALUES (1, ?, ?, ?, ?)",
                (new_state, changed_by, _now_iso(), reason),
            )
            self._conn.commit()
            return True

    def close(self) -> None:
        """SQLite-Verbindung schliessen."""
        with self._lock:
            self._conn.close()

    def reset_for_testing(self) -> None:
        """Löscht alle Daten — nur für Tests."""
        with self._lock:
            self._conn.executescript("""
                DELETE FROM open_positions;
                DELETE FROM closed_positions;
                DELETE FROM pnl_ledger;
                DELETE FROM equity_meta;
                DELETE FROM control_state;
            """)
            self._conn.execute(
                "INSERT INTO equity_meta (key, value) VALUES (?, ?)",
                ("equity_peak", self._initial_equity),
            )
            self._conn.commit()


# ─── Singleton-Factory (App-weit) ─────────────────────────────────────────────

_store_instance: Optional[StateStore] = None
_store_lock = threading.Lock()


def get_state_store(db_path: str = "trade-agent-state.db", initial_equity: float = _INITIAL_EQUITY) -> StateStore:
    """Gibt den app-weiten StateStore zurück (lazy singleton)."""
    global _store_instance
    with _store_lock:
        if _store_instance is None:
            _store_instance = StateStore(path=db_path, initial_equity=initial_equity)
    return _store_instance