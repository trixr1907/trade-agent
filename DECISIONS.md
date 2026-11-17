# Architektur-Entscheidungen — Crypto-Trading-Signal-Tool

Diese Datei ist der **Entscheidungsprotokoll** für das Projekt. Jede Entscheidung
wird begründet und ist bewusst **fail-safe** ausgelegt. Wenn etwas überprüft oder
geändert werden soll, wird hier der Grund aufgezeichnet.

## Ziel-Stack (genehmigt)

| Bereich         | Wahl                                     | Warum                                                        |
| --------------- | ---------------------------------------- | ----------------------------------------------------------- |
| Formfaktor      | **Web-Dashboard** (FastAPI + HTML/JS)    | Übersichtlich, mehrfach nutzbar, für Freigabe-Workflow ideal |
| Ausführung      | **Live-Trading mit menschlicher Freigabe** | Approval-Workflow ist Teil der Anforderung                   |
| Exchange        | **Bitget** (via CCXT)                    | Einheitlicher API-Zugriff, AURA-Vorbild                     |
| Analyse-Ansatz  | **Hybrid**: deterministische Engine + LLM als Analyst | LLM entscheidet NICHT; er begründet und summarisiert |

## Die 6 prägenden Entscheidungen

### 1. Signal-Engine ist deterministisch, LLM ist nur Analyst
Der Kauf-/Verkaufsbeschluss entsteht **nie** im LLM. Er entsteht in
`signals.py` als reiner, reproduzierbarer Python-Code (Indikatoren + Struktur).
Der LLM (`analyst.py`) nimmt die vorbereiteten Signale und formuliert
Begründung, Risikohinweise und ein Narrative. **Prüfbarkeit > Schlauheit.**

### 2. Approval-Workflow als Zustandsgraph (LangGraph)
Der Workflow «Signal → Antrag → Prüfung → Freigabe → Ausführung」wird als
Graph mit expliziten Knoten und Kanten modelliiert (`graph.py`).
- Jede Übergangsbedingung ist explicit.
- **fail-closed**: Eine Ausführung ohne gültige Freigabe ist unmöglich.
- Checkpointing erlaubt 复盘 (Replay / Audit).

### 3. Freigabe ein-stufig, fail-closed, Server-Trust
- Freigabe endet **nur** über einen verifizierten Server-Endpunkt
  (`POST /approve`), nicht per Client-Flag.
- Sitzungen / Token sind an das Account gebunden (AURA-v3-Prinzip).
- `Approval` Objekt enthält Provenance: wer, wann, zu welchen Konditionen.

### 4. CCXT, Paper-first
- Daten & Ausführung laufen über **CCXT** (einheitliche Schnittstelle).
- **Paper-Trading ist der Standard-Modus.** Live aktiviert erst nach
  expliziter, geprüfter Freigabe und verschärften Limits.
- Paper-Fills sind **simulated**: sie spiegeln reale Marktkonditionen wider
  (Spread, Liquidity), ohne echtes Kapital zu setzen.

### 5. Backtesting mit Walk-Forward-Validierung
Naive Historical-Backtests überschätzen Performance massiv. Legen Sie diese
Logik (Walk-Forward + Deflated Sharpe Ratio) für die Validierung an.
Dieser Teil gehört in `backtest/` und ist für den Aufbau phasen 2–3 geplant.

### 6. Risiko-Management ist Kern, kein Zusatz
- **Kelly-Sizing** (mit Kap für viele Positionen) + **ATR-Stops** +
  **Max-Drawdown-Budget** sind Teil der Engine (`risk.py`).
- Ein Signal ohne Risiko-Sizing wird **nicht** ausgeführt.

## Sicherheits-Prinzipien (von Tag 1)
- API-Keys **nur** aus Env/Secrets — niemals im Quellcode.
- Client-Eingaben niemals als Trust-Quelle für Ausführung.
- Live-Modus hat harte Limits (Max-Order-Größe, VP-Limit, Allowlist der Symbole).
- Logs redact sensitive Daten (Keys, vollständige Order-IDs).
