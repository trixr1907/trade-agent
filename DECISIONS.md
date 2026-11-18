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

## Die 7 prägenden Entscheidungen

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

### 7. SMC bewertet Signale, erzeugt aber keine
Die SMC-Analyse (`smc.py`) ist deterministisch und läuft innerhalb von
`analyze_symbol()`. Sie liefert für die Richtung des bestehenden Signals einen
Konfluenz-Score zwischen 0 und 1 und kann ein Veto auslösen.
- **Formel:** `final = round(min(score, 100) * (0.7 + 0.3 * confluence_score))`.
  Ohne Konfluenz bleiben 70 % des Basis-Scores, bei voller Konfluenz 100 %.
- **Gewichte:** BOS/CHoCH in Signalrichtung 0.35, offene FVG in Reichweite 0.25,
  offener Orderblock in Reichweite 0.25, Liquidity Sweep 0.15.
- **Veto:** Ein CHoCH gegen die Signalrichtung, höchstens 10 Bars alt, setzt den
  Score auf 0. Das Signal fällt damit bei jeder Schwelle heraus.
- **Kein Lookahead:** Ein Swing gilt erst nach 3 rechten Bars als bestätigt.
  Das ist per Test abgesichert.
- Die Signatur `analyze_symbol(symbol, df)` bleibt unverändert. SMC wirkt
  dadurch im Live-Pfad und im Backtest gleich.

#### Backtest-Ergebnis (nur berichtend, kein Gate)
Snapshot BTC/USDT 1h, 500 Bars, 2026-10-28 13:00 bis 2026-11-18 08:00 UTC,
3 Folds, `is_ratio` 0.70, `min_signal_score` 50, `bars_per_year` 252 * 24.
Beide Läufe nutzen denselben Snapshot.

| Kennzahl            | Ohne SMC                 | Mit SMC                  |
| ------------------- | ------------------------ | ------------------------ |
| Trades              | 28 (4 / 14 / 10)         | 19 (3 / 9 / 7)           |
| Durchschnitts-Score | 59,8                     | 54,2                     |
| Win-Rate            | 7,1 % (2 Gewinner)       | 5,3 % (1 Gewinner)       |
| Profit-Faktor       | 0,0010                   | 0,0003                   |
| Sharpe / Sortino    | −33,92 / −34,11          | −28,55 / −28,67          |
| Avg R pro Fold      | −0,588 / −0,182 / −0,588 | −0,405 / −0,207 / −0,698 |
| Kosten              | $57,38                   | $37,96                   |
| DSR                 | 0,0000                   | 0,0000                   |

Mit SMC gibt es weniger Trades und einen niedrigeren Profit-Faktor. Sharpe und
Sortino sind weniger negativ, aber Fold 2 liegt weiter an der Clip-Grenze −50.
Der bessere Wert in Fold 1 beruht auf 3 Trades. Die Stichprobe ist nicht
signifikant: Sie zeigt weder, dass SMC hilft, noch, dass es schadet. Formel und
Gewichte werden an dieser Stichprobe nicht nachjustiert, weil das Kurven-Anpassung
wäre. Bis zur Merge-Entscheidung liegt SMC auf dem Branch `feat/smc-confluence`.
Über den Merge auf main entscheidet ein Mensch.

#### Erweiterter Vergleich: 6 Snapshots (nur berichtend, kein Gate)
Gleiche Config wie oben (3 Folds, `is_ratio` 0.70, `min_signal_score` 50,
`bars_per_year` 252 * 24 bzw. 252). 1h-Snapshots: 500 Bars, 2026-10-28 bis
2026-11-18. 4h-Snapshots: 500 Bars, 2026-08-27 bis 2026-11-18. Der Lauf ohne
SMC ersetzt `analyze_smc` durch ein neutrales Ergebnis (Konfluenz 1.0, kein
Veto). Auf BTC/USDT 1h reproduziert er den Stand vor SMC exakt (28 Trades,
Durchschnitts-Score 59,8). Avg R pro Trade = SumR / Trades.

| Snapshot    | Trades ohne / mit SMC | Avg R pro Trade ohne / mit | PF ohne / mit      | SumR ohne / mit  |
| ----------- | --------------------- | -------------------------- | ------------------ | ---------------- |
| BTC/USDT 1h | 28 / 19               | −0,385 / −0,419            | 0,0010 / 0,0003    | −10,78 / −7,97   |
| ETH/USDT 1h | 38 / 21               | −0,318 / −0,388            | 0,0897 / 0,1017    | −12,07 / −8,14   |
| SOL/USDT 1h | 35 / 16               | −0,066 / −0,169            | 0,7199 / 0,1416    | −2,31 / −2,70    |
| BTC/USDT 4h | 34 / 1                | −0,370 / +0,020            | 0,1153 / (1 Trade) | −12,57 / +0,02   |
| ETH/USDT 4h | 39 / 5                | −0,371 / −0,448            | 0,1277 / 0,0250    | −14,47 / −2,24   |
| SOL/USDT 4h | 36 / 9                | −0,509 / −0,638            | 0,0623 / 0,0000    | −18,34 / −5,74   |
| Summe       | 210 / 71              | −0,336 / −0,377            |                    | −70,54 / −26,77  |

Befunde:
- SMC wählt keine besseren Trades aus. In 5 von 6 Snapshots ist der Avg R pro
  Trade mit SMC schlechter, der sechste besteht aus einem einzigen Trade.
- Der Gesamtverlust sinkt nur, weil weniger gehandelt wird (71 statt 210 Trades).
  Bei 4h liegen die Rohscores bei etwa 52 bis 54. Mit dem Faktor 0,7 bis 1,0 und
  der Schwelle 50 kommt ein Signal dort nur bei sehr hoher Konfluenz durch.
- Die Basis-Engine verliert schon ohne SMC in allen 6 Snapshots.

Signifikanz: Der höchste DSR liegt bei 0,0061. Die Snapshots überlappen sich
zeitlich und hängen über den gemeinsamen Markt zusammen. 210 Trades sind deshalb
keine 210 unabhängigen Beobachtungen. Die Evidenz spricht leicht gegen SMC, ist
aber nicht signifikant. Formel, Gewichte und Schwelle bleiben unverändert. Über
den Merge auf main entscheidet weiterhin ein Mensch.

## Sicherheits-Prinzipien (von Tag 1)
- API-Keys **nur** aus Env/Secrets — niemals im Quellcode.
- Client-Eingaben niemals als Trust-Quelle für Ausführung.
- Live-Modus hat harte Limits (Max-Order-Größe, VP-Limit, Allowlist der Symbole).
- Logs redact sensitive Daten (Keys, vollständige Order-IDs).
