# Direktive 5C — Monitoring & Alerting

**Ziel:** Das System überwacht den Vollzug (Positionen, Risiko-Limits, System-Zustand) und führt einen nachverfolgbaren Alarm-Lebenszyklus. Fail-closed; Alarme lösen keine Trades aus (LLM und Alarme entscheiden niemals Trades). Demo/Live-Trennung bleibt bestehen.

**Entscheidung: Variante A — Tabelle `alarms` erweitern (keine neue Tabelle `alerts`).**
Begründung: `alarms`, `/control/alarms`, das Resume-409-Gate und das Alarm-Banner sind bereits verbunden. Die Erweiterung lässt das fail-closed-Gate unverändert. Variante B (neue Tabelle) wäre ein fail-open-Risiko, weil das Gate beide Tabellen abdecken müsste.

Hinweis zur Umsetzung: Die Erweiterung braucht eine additive Schema-Änderung (`ALTER TABLE alarms ADD COLUMN ...`). Bestehende Rows bleiben erhalten, das Gate (`open_alarms()` / `resume_trading()`) bleibt unverändert.

## 1. Tabelle `alarms` erweitern

- Neue Spalten: `state` (`open` / `acknowledged` / `resolved`, Default für bestehende Rows: `open`; bereits bestätigte Rows werden auf `acknowledged` nachgezogen), `context` (JSON), `idempotency_key` (gleicher Alarm nicht doppelt aktiv). Zusätzlich `resolved_at` / `resolved_by` für die History.
- Bestehende `kind`-Werte bleiben. Neue `kind`-Werte: Stop ausgelöst, Target ausgelöst, Stop-Fehler nach Entry, Drawdown-Grenze überschritten, Risiko-Limit-Verstoß, Universe veraltet, Scan-Fehler, API/Auth-Fehler, Monitor-Sync-Fehler.
- Auto-Resolve: Ist die Bedingung wieder klar, wird ein nicht bestätigter Alarm auf `resolved` gesetzt. Bestätigte Alarme brauchen ein manuelles Resolve.
- Fail-closed: Ein auto-aufgelöster, aber nie bestätigter Alarm blockiert das Resume-Gate weiter, bis er bestätigt ist (Gate prüft unverändert `acknowledged_at IS NULL`).

## 2. Endpunkte (alle unter `require_session`)

- `GET /control/alarms` (bestehend) — erweitert um History/Filter nach Typ und Zustand; ohne Parameter unverändertes Verhalten.
- `GET /control/alarms/stats` — Anzahl nach Typ/Zustand, letzter Alarm, letzter Sync.
- `POST /control/alarms/{id}/resolve` — manuelles Resolve, nur aus `acknowledged`.
- `/control/resume`-409-Gate bleibt unverändert.

## 3. Monitor-Loop (erweitert `monitor.py`)

- Periodischer Sync (Positionen, Orders, Kontostand), Intervall `MONITOR_INTERVAL_SECONDS` (Default 60), Demo/Live getrennt.
- Risiko-Prüfungen: Drawdown, offene Positionen, Nominal-Limit pro Position, Liquidations-Abstand (Stop vs. Hebel) mit Warnung bei kritischer Nähe.
- Alarm-Erzeugung bei Verstoß. Keine Trade-Entscheidung im Monitor, nur Beobachtung und Alarm.
- Live-Sync nur bei `TRADING_MODE=live` mit gesetzten Keys; Paper-Pfad bleibt unangetastet.

## 4. Alert-Channels

- Dashboard-Banner (bestehend).
- Optionaler Webhook (generischer HTTP-POST, Default aus): `ALERT_WEBHOOK_URL`. Kein Pflicht-Service, keine neue Abhängigkeit.
- Log-Ausgabe mit klarer Typ/Zustand-Struktur.

## 5. Dashboard-Monitor-View

Positions-Monitor (realisierter/unrealisierter P&L, Liquidations-Abstand), Kontostand (Equity, Drawdown-% mit Grenze), System-Health (Universe-Alter, letzter Sync, letzter Scan, offene Alarme). Motion aus 5B wird wiederverwendet, nicht neu gebaut.

## 6. Env-Vars (gegen `config.py` geprüft)

Die Namen existieren bereits und werden unverändert verwendet:

- `RISK_MAX_DRAWDOWN_PCT` → `cfg.risk.max_drawdown_pct`, Bruchteil (Default `0.15` = 15 %).
- `LIVE_MAX_OPEN_POSITIONS` → `cfg.live_limits.max_open_positions` (Default 3).
- `LIVE_MAX_POSITION_NOTIONAL_USD` → `cfg.live_limits.max_position_notional_usd` (Default 500).
- `BITGET_LEVERAGE` → `cfg.exchange.leverage` (Default 3).

Neu: `MONITOR_INTERVAL_SECONDS`, `ALERT_WEBHOOK_URL`.

## 7. Nicht verändern

Signal-Engine, Backtest-Engine, Relay-Logik, Paper-Pfad. Alarme lösen keine Orders.

## Abnahme

- Alle 220 bestehenden Tests bleiben grün. Neue Tests: Lebenzyklus, Idempotenz, Persistenz, Monitor-Risiko-Checks (gemockt), Resolve-Gate, Demo/Live-Trennung.
- Fail-closed: offener, unbestätigter Alarm → `/control/resume` 409 (unverändert).
- `py_compile` grün, JS-Syntax-Check grün, keine neuen externen Abhängigkeiten.
- README und `.env.example` um `MONITOR_INTERVAL_SECONDS` und `ALERT_WEBHOOK_URL` ergänzt.