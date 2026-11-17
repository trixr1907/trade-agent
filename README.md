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
| POST    | `/scan`        | Signale scannen & Anträge erstellen   |
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

`/scan`, `/approve`, `/reject`, `/execute` und `/control/*` verlangen den Header
`Authorization: Bearer <token>`. Ohne gültige Session antworten sie mit 401.

## Sicherheit

- API-Keys **nur** aus Env — niemals im Quellcode.
- Client-Eingaben sind **niemals** Trust-Quelle für Ausführung.
- Ausführung ohne gültige Freigabe ist **unmöglich** (fail-closed).
- Live-Modus hat harte Limits (Positionsgröße, Anzahl, Symbol-Allowlist).

## Phasen

1. **Jetzt ( Fundament )** — Engine, Workflow, Dashboard, Paper-Trading.
2. **Phase 2** — Walk-Forward-Backtest + DSR-Validierung (`backtest/`).
3. **Phase 3** — Live-Rollout unter strengen Limits & Monitoring.
