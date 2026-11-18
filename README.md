# Crypto Trading Signal Agent

Ein Krypto-Trading-Signal-Tool mit **Web-Dashboard**, **Approval-Workflow**
und **Risiko-Management**. Aufbauend auf den Entscheidungen in
[`DECISIONS.md`](DECISIONS.md).

## Architektur (kurz)

```
CCXT/Bitget (Daten)
        │  OHLCV
        ▼
  signals.py ──► deterministische Signal-Engine (Indikatoren + Struktur)
        │
        ▼
  risk.py ────► Kelly-Sizing / ATR-Stops / Drawdown-Budget
        │
        ▼
  graph.py ────► Approval-Workflow: proposed → approved → executed  (fail-closed)
        │
        ▼
  analyst.py ──► LLM formuliert Narrativ (entscheidet NIEMALS)
        │
        ▼
  api/app.py ──► Web-Dashboard + REST-Endpunkte
```

## Aufsetzen

```bash
cd trade-agent
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # API-Keys nur hier
python -m tradeagent.api.auth  # Hash für AUTH_PASSWORD_HASH erzeugen, mit AUTH_USERNAME in .env eintragen
python -m tradeagent.api.app   # http://localhost:8000
```

> **Paper-first:** Standardmäßig läuft `TRADING_MODE=paper`. Live wird nur nach
> Freigabe und unter den Limits in `.env` aktiviert (fail-closed).

## Endpunkte

| Methode | Pfad           | Beschreibung                          |
| ------- | -------------- | ------------------------------------- |
| GET     | `/`            | Web-Dashboard                         |
| POST    | `/scan`        | Batch-Scan starten, liefert `job_id` + Fortschritt |
| GET     | `/scan/{job_id}` | Fortschritt; nach Abschluss Signale & Anträge in `result` |
| GET     | `/scan/last`   | Zusammenfassung des letzten abgeschlossenen Scans |
| GET     | `/universe`    | Entdeckte USDT-Perps, Allowlist-Modus, offene Positionen, P&L pro Symbol |
| GET     | `/proposals`   | Liste aller Anträge                   |
| POST    | `/approve`     | Freigabe (nur Server-Trust, fail-closed) |
| POST    | `/reject`      | Antrag ablehnen                       |
| POST    | `/execute`     | Ausführung (nur nach Freigabe)        |
| GET     | `/health`      | Gesundheits-Status inkl. `trading_state` |
| POST    | `/login`       | Anmeldung, liefert Bearer-Session-Token |
| POST    | `/logout`      | Eigene Session beenden                |
| GET     | `/auth/status` | Session-Status                        |
| GET     | `/control/status` | Halt/Resume-Status                 |
| POST    | `/control/halt`   | Trading global stoppen             |
| POST    | `/control/resume` | Trading fortsetzen                 |

`/scan`, `/scan/*`, `/universe`, `/approve`, `/reject`, `/execute` und `/control/*`
verlangen den Header `Authorization: Bearer <token>`. Ohne gültige Session antworten
sie mit 401.

## Symbol-Universe und Batch-Scan

- Die Symbolmenge kommt aus `fetch_markets()` von Bitget, gefiltert auf aktive,
  lineare USDT-Perpetuals (`BTC/USDT:USDT`). Cache: `UNIVERSE_TTL_SECONDS`.
- `POST /scan` ohne `symbols` scannt die ganze Universe in Batches (`batch_size`,
  Standard 20, erlaubt 1–100). Es läuft höchstens ein Scan gleichzeitig; ein
  zweiter Start antwortet mit 409. Fehler einzelner Symbole stoppen den Scan nicht.
- `LIVE_SYMBOL_ALLOWLIST` leer: Live erlaubt alle entdeckten USDT-Perps. Gesetzt:
  nur die gelisteten. Ist die Universe nicht verfügbar und älter als
  `UNIVERSE_MAX_STALE_SECONDS`, wird jedes Symbol abgelehnt (fail-closed).

## Sicherheit

- API-Keys **nur** aus Env — niemals im Quellcode.
- Client-Eingaben sind **niemals** Trust-Quelle für Ausführung.
- Ausführung ohne gültige Freigabe ist **unmöglich** (fail-closed).
- Live-Modus hat harte Limits (Positionsgröße, Anzahl, Symbol-Allowlist).

## Monitor & Alarme (Phase 5)

Der Stop-Monitor läuft als Hintergrund-Thread im Server und startet und stoppt mit der App. Sein Intervall legt `MONITOR_INTERVAL_SECONDS` fest (Standard 60 s). Jeder Zyklus synchronisiert Stop- und Target-Orders und schreibt den Zeitstempel `last_sync`.

Die Risiko-Checks legen bei einem Verstoß einen Alarm an:

- Drawdown über `RISK_MAX_DRAWDOWN_PCT`.
- Anzahl offener Positionen und Nominal pro Position gegen `LIVE_MAX_OPEN_POSITIONS` und `LIVE_MAX_POSITION_NOTIONAL_USD`.
- Stop-Abstand zur Liquidation (nur Live).

Sobald der Verstoß behoben ist, setzt der Monitor den Alarm auf `resolved`. Bis jemand ihn bestätigt (`POST /control/alarms/{id}/ack`), blockiert er `/control/resume` weiterhin mit 409.

Ist `ALERT_WEBHOOK_URL` gesetzt, sendet der Monitor bei jedem neuen Alarm einen JSON-POST mit `kind`, `message`, `ref` und `created_at`. Schlägt der Aufruf fehl, wird nur geloggt. Alarme lösen keine Orders aus.

## Phasen

1. **Jetzt ( Fundament )** — Engine, Workflow, Dashboard, Paper-Trading.
2. **Phase 2** — Walk-Forward-Backtest + DSR-Validierung (`backtest/`).
3. **Phase 3** — Live-Rollout unter strengen Limits & Monitoring.
