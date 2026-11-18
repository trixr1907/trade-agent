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

#### Korrektur zum Vergleich: Exit-Aufschlüsselung (nur berichtend, kein Gate)
Die Zahlen der Tabelle oben bleiben unverändert. Ihre Aussagekraft ist aber
geringer, als der Vergleich allein nahelegt. Die Exit-Aufschlüsselung der
Basis-Engine (ohne SMC, gleiche 6 Snapshots, gleiche Config) zeigt, wie die
210 Trades enden:

| Exit-Grund | Trades | Summe R brutto (aus n und Avg R gerechnet) |
| ---------- | ------ | ------------------------------------------ |
| Stop       | 31     | −31,0                                      |
| Target     | 7      | +8,4                                       |
| eod        | 172    | rund −31                                   |
| Summe      | 210    | −53,75 (netto −70,54)                      |

- **eod dominiert.** 172 von 210 Trades (82 %) erreichen weder Stop noch Target.
  `_simulate_trade` hat kein Zeitlimit und schließt solche Trades zum letzten
  Schlusskurs des OOS-Fensters. Ihr Ergebnis hängt davon ab, wo der Kurs am
  Fensterende steht, nicht von der Stop/Target-Strategie.
- **Keine Überlapp-Sperre.** `run_walk_forward` öffnet in jedem OOS-Bar mit
  Signal einen neuen Trade. Viele Trades teilen sich Exit-Preis und Marktphase.
  Die 210 Trades sind keine unabhängigen Beobachtungen.
- **Die Trefferquote ist kein Richtungsmaß.** Die Brutto-Quote von 32 bis 38 %
  besteht fast nur aus eod-Trades mit kleinem Plus (Avg R zwischen −0,03 und
  −0,09 bei 1h). Kosten von 0,04 bis 0,10 R pro Trade kippen sie ins Minus. Das
  erklärt den Abfall von 32,1 % auf 7,1 % bei BTC/USDT 1h. Ein Vergleich mit der
  Breakeven-Quote von etwa 45,5 % ist nicht zulässig.
- **Das Risk-Reward ist nicht das Problem.** Stop 2,5 ATR, Target 3,0 ATR. Stop
  liegt bei −1,0 R brutto, Target bei +1,2 R brutto und +1,09 bis +1,14 R netto.
  Die Kosten machen im Mittel rund 0,08 R pro Trade aus (16,8 R von 70,5 R
  Nettoverlust, etwa 24 %). Die Basis-Engine verliert auch brutto
  (−0,256 R pro Trade gegen −0,336 R netto).
- **Stop-Häufung als Hinweis.** Unter den 38 Trades, die ein Level erreichen,
  enden 31 am Stop (82 %). Das deutet auf schlechtes Entry-Timing hin. Die Daten
  sind zensiert (172 Trades erreichen kein Level) und überlappend. Es ist ein
  Hinweis, kein Beleg.

Bewertung: «SMC tendiert gegen, nicht signifikant» bleibt bestehen, gründet aber
auf weniger als zuvor angenommen. Der Vergleich misst größtenteils den Kursstand
am Fensterende. Die Aufschlüsselung nach Exit-Grund liegt nur für den Lauf ohne
SMC vor, für den Lauf mit SMC nicht. Die Aussage, SMC filtere nur und verbessere
die Trades nicht, ist damit nicht abgesichert.

Offene Methodik-Lücken:
- Kein `max_hold` und keine Sperre gegen überlappende Trades im Backtest.
- Nicht geprüft, ob der Live-Betrieb Positionen anders hält oder schließt als
  der Backtest. Davon hängt ab, ob der Backtest das Live-Verhalten abbildet.
- Die Score-Budget-Hypothese (maximal 65 Punkte bei Schwelle 50) ist nicht
  geprüft.

Nächste Schritte:
1. `max_hold` (z. B. 24 Bars) und Überlapp-Sperre zuerst nur im Diagnoseskript
   ergänzen, nicht im Produktivcode.
2. Alle 6 Snapshots damit zweimal messen, ohne und mit SMC. Erst dieser
   Vergleich kann als Grundlage der SMC-Entscheidung dienen.
3. Der Merge auf main bleibt ausgesetzt. Formel, Gewichte und Schwelle bleiben
   unverändert. Über den Merge entscheidet ein Mensch.

#### Messung mit max_hold und n_folds=1 (neue Konfiguration, nur berichtend, kein Gate)
Diese Messung ist keine Fortsetzung der 6-Snapshot-Tabelle. Sie nutzt eine andere
Konfiguration und ist mit ihr nicht direkt vergleichbar.

- **Konfiguration:** `n_folds=1`, `is_ratio` 0,70, `min_signal_score` 50,
  `max_hold` 24 Bars, `bars_per_year` 252 * 24 bzw. 252. Das OOS-Fenster umfasst
  die letzten rund 150 Bars jedes Snapshots. Die Kosten gelten netto, brutto ohne
  Kosten. Der Patch liegt nur in `snapshots/diag_maxhold.py`. Der Produktivcode
  ist unverändert.
- **Abnahme:** In allen 12 Läufen (6 Snapshots, brutto und netto) beendet max_hold
  Trades. Die Zähler liegen über null, die Messung gilt damit als gültig.

**Revision:** Die Aussagen «Die Basis-Engine verliert schon ohne SMC in allen
6 Snapshots» und «eod dominiert» gelten für das Setup mit 3 Folds und OOS-Fenstern
von rund 50 Bars. Unter dieser Konfiguration gelten sie nicht. Der Abschnitt oben
bleibt unverändert, ist aber durch diese Messung überholt.

Ergebnis, netto, ohne Überlapp-Sperre (Zeile «hold»):

| Snapshot    | Trades ohne SMC | SumR ohne SMC | Avg R ohne SMC | Trades mit SMC | SumR mit SMC | Avg R mit SMC |
| ----------- | --------------- | ------------- | -------------- | -------------- | ------------ | ------------- |
| BTC/USDT 1h | 79              | +21,19        | +0,268         | 20             | −4,75        | −0,238        |
| ETH/USDT 1h | 79              | +19,18        | +0,243         | 23             | +2,68        | +0,116        |
| SOL/USDT 1h | 88              | +12,42        | +0,141         | 29             | +1,17        | +0,040        |
| BTC/USDT 4h | 103             | +41,50        | +0,403         | 40             | +10,41       | +0,260        |
| ETH/USDT 4h | 95              | +33,60        | +0,354         | 32             | +3,78        | +0,118        |
| SOL/USDT 4h | 105             | +48,59        | +0,463         | 57             | +39,58       | +0,694        |
| Summe       | 549             | +176,48       | +0,321         | 201            | +52,87       | +0,263        |

Zähler (netto):

- max_hold-Trades ohne SMC: 29, 16, 19, 16, 8, 10. Mit SMC: 6, 2, 2, 13, 8, 9.
- Überlapp-gesperrte Signale ohne SMC: 73, 70, 78, 94, 88, 98. Mit SMC: 14, 17,
  23, 34, 26, 51.
- Mit Überlapp-Sperre bleiben pro Snapshot nur 6 bis 10 Trades (48 ohne SMC,
  36 mit SMC). Diese Zeilen sind zu klein für eine Aussage.

Befunde:

- Die Summe R ist mit SMC in allen 6 Snapshots niedriger. Das Avg R sinkt in 5
  von 6 Snapshots. Ausnahme ist SOL/USDT 4h.
- Gepoolt liegt das Avg R bei +0,32 R pro Trade ohne SMC und bei +0,26 R mit SMC.
- Ohne SMC enden Trades zu 9 bis 20 pro Snapshot am Fensterende (eod) und zu 8 bis
  29 durch max_hold (time).

Einschränkungen:

- Zwei Dinge ändern sich gegenüber der 6-Snapshot-Tabelle: die Zahl der Folds (3
  auf 1, also Fensterlänge und IS/OOS-Schnitt) und max_hold. Der Unterschied lässt
  sich deshalb nicht allein max_hold oder der Fensterlänge zuschreiben. Ein Lauf mit
  `n_folds=1` ohne max_hold würde das trennen. Den habe ich nicht gemacht.
- Ein OOS-Fenster pro Snapshot, regimeabhängig. Die Snapshots überlappen zeitlich
  und hängen über den gemeinsamen Markt zusammen. Die Trades sind keine unabhängigen
  Beobachtungen.
- Kein Signifikanztest. DSR wurde nicht berechnet.

Verdikt (nur für diese Konfiguration): Die Basis-Engine ist netto positiv. SMC
senkt in dieser Konfiguration die Summe R und das Avg R pro Trade. Die Richtung ist
konsistent, aber nicht signifikant. Formel, Gewichte und Schwelle bleiben unverändert.
Der Merge auf main bleibt ausgesetzt. Über den Merge entscheidet ein Mensch.

#### Attribution: Basis-Umkehr durch Fenster oder Zeitlimit (n_folds=1, ohne max_hold)
Diese Messung trennt die Fensterlänge vom Zeitlimit. Sie nutzt dieselbe Konfiguration
wie der Abschnitt oben, nur ohne `max_hold`.

- **Konfiguration:** `n_folds=1`, `is_ratio` 0,70, `min_signal_score` 50, Überlapp-Sperre
  als separate Zeile. `max_hold` ist deaktiviert (Schalter `--no-max-hold` in
  `snapshots/diag_maxhold.py`). Der Produktivcode ist unverändert.
- **Abnahme:** Alle 12 Läufe liefern Trades. Der max_hold-Zähler ist erwartungsgemäß 0,
  weil das Haltelimit deaktiviert ist.

Ergebnis, netto, Zeile «hold» (ohne Überlapp-Sperre):

| Snapshot    | Trades ohne SMC | SumR ohne SMC | Avg R ohne SMC | Trades mit SMC | SumR mit SMC | Avg R mit SMC |
| ----------- | --------------- | ------------- | -------------- | -------------- | ------------ | ------------- |
| BTC/USDT 1h | 79              | +19,55        | +0,247         | 20             | −5,05        | −0,253        |
| ETH/USDT 1h | 79              | +21,42        | +0,271         | 23             | +2,95        | +0,128        |
| SOL/USDT 1h | 88              | −2,96         | −0,034         | 29             | −0,37        | −0,013        |
| BTC/USDT 4h | 103             | +38,59        | +0,375         | 40             | +5,11        | +0,128        |
| ETH/USDT 4h | 95              | +28,89        | +0,304         | 32             | −0,92        | −0,029        |
| SOL/USDT 4h | 105             | +54,84        | +0,522         | 57             | +45,26       | +0,794        |
| Summe       | 549             | +160,33       | +0,292         | 201            | +46,98       | +0,234        |

Zum Vergleich, gleiche Fenster mit max_hold=24: Summe ohne SMC +176,48 R (+0,321 R/Trade),
mit SMC +52,87 R (+0,263 R/Trade).

Befunde:

- **Attribution:** Die Basis-Engine bleibt ohne Zeitlimit positiv (+160,33 R, +0,292 R/Trade).
  max_hold ändert die Summe nur um 16,15 R. Die Umkehr gegenüber dem 3-Fold-Setup kommt
  also vom Fenster, nicht vom Zeitlimit.
- **Grenze der Attribution:** Mit `n_folds=1` ändern sich Fensterlänge und IS/OOS-Schnitt
  gemeinsam. Dieser Lauf trennt nur das Zeitlimit ab. Ob die Fensterlänge oder der Schnitt
  wirkt, bleibt offen.
- **SMC, Summe R:** Mit SMC niedriger in 5 von 6 Snapshots. Ausnahme ist SOL/USDT 1h
  (−2,96 ohne, −0,37 mit SMC).
- **SMC, Avg R:** Mit SMC niedriger in 4 von 6 Snapshots. Bei SOL/USDT 1h und SOL/USDT 4h
  liegt Avg R mit SMC höher. Gepoolt sinkt Avg R von +0,292 auf +0,234.
- **Korrektur zum Abschnitt oben:** Die Aussage «Avg R sinkt in 5 von 6 Snapshots» hängt an
  der Konfiguration mit max_hold. Ohne max_hold gilt sie nur für 4 von 6.
- **Überlapp-Sperre:** Nur 3 bis 7 Trades pro Snapshot bleiben übrig. Diese Zeilen sind
  keine Grundlage für einen Befund.

Verdikt (nur für diese Konfiguration): Die Umkehr kommt vom Fenster, nicht vom Zeitlimit.
SMC senkt die Summe R in 5 von 6 Snapshots und die gepoolte Avg R. Die Richtung ist
nicht einheitlich, und ein Signifikanztest fehlt. Der Merge auf main bleibt ausgesetzt.
Über den Merge entscheidet ein Mensch.

#### Pre-Registration: Robustheit der Basis-Engine (Schritt B)
Diese Festlegung wird vor der ersten Messung committet. Die Messung folgt erst danach.
Die Konfiguration wird nach dem Blick auf die Zahlen nicht geändert.

- **Symbole und Zeitrahmen:** BTC/USDT, ETH/USDT, SOL/USDT, jeweils 1h und 4h.
- **Zeiträume:** Drei nicht überlappende Blöcke von je rund vier Monaten, UTC, Start und Ende inklusive. Die Blöcke sind nach Marktregime gestreut.
  - A, Abwärtsphase: 2022-05-01 bis 2022-08-31 (1h: rund 2.928 Bars)
  - B, Seitwärtsphase: 2023-06-01 bis 2023-09-30 (1h: rund 2.928 Bars)
  - C, Aufwärtsphase: 2024-01-01 bis 2024-04-30 (1h: rund 2.904 Bars)
  - Die Regime-Zuordnung ist eine Vorab-Einschätzung aus dem Marktverlauf, keine Messung.
- **Walk-Forward:** n_folds=3, is_ratio=0,70, min_signal_score=50, jeder Zeitraum getrennt.
- **OOS-Bars pro Fold:** 1h rund 290 (966 Bars pro Fold, 676 IS). 4h rund 73 (rund 241 Bars pro Fold, 168 IS). Die Zahl 870 aus dem Entwurf ist die Summe über alle Folds, nicht pro Fold.
- **Haltelimit:** max_hold=24 Bars, aktiv. Abnahme pro Lauf: Der max_hold-Zähler liegt über null, sonst ist der Lauf ungültig.
- **Überlapp-Sperre:** separate Variante, nur beschreibend.
- **Kosten:** netto ist die primäre Messung, brutto nur beschreibend.
- **SMC:** jeweils mit und ohne SMC auf denselben Daten. Die Paare werden für Δ verknüpft.
- **Primäre Metrik:** Avg R pro Trade, gepoolt über Zeiträume und Symbole. Sum R, Pro-Snapshot-Werte und Überlapp-Zeilen werden nur beschreibend berichtet.
- **Mindeststichprobe:** 100 gepoolte Trades je Variante. Darunter gilt das Ergebnis als nicht auswertbar.
- **Statistik:** Block-Bootstrap über Kalenderwochen nach Entry-Zeit, Symbole gemeinsam. 10.000 Resamples, Seed 42, 95-%-Perzentil-Intervall. Δ = Avg R mit SMC minus Avg R ohne SMC, mit denselben Wochen resampelt.
- **Datenbeschaffung:** `tradeagent/data.py` bleibt unverändert. `fetch_ohlcv` lädt nur die letzten `limit` Kerzen ohne Zeitbereich. Für feste Zeiträume braucht es eine paginierte Abfrage mit `since`. Sie liegt in einem Diagnose-Skript in `snapshots/`, nicht im Produktivcode. Öffentliche Marktdaten brauchen keine API-Schlüssel. Verlangt der Exchange-Zugang Schlüssel, bricht der Lauf ab.
- **Interpretation (vorab festgelegt):**
  - Die Basis gilt als positiv, wenn die untere CI-Grenze des Avg R über 0 liegt.
  - SMC wirkt nur, wenn das CI von Δ die 0 nicht enthält. Sonst ist das Ergebnis nicht unterscheidbar.
  - Der Merge bleibt ausgesetzt, solange kein Δ-CI eindeutig in Richtung Edge zeigt.
- **Abnahme:**
  1. Diese Pre-Registration ist committet, bevor die erste Messung läuft.
  2. Alle Läufe liefern Trades, und die max_hold-Zähler liegen über null.
  3. Berichtet werden: Avg R der Basis mit CI, Δ Avg R und Δ Sum R für SMC mit CI, Trade-Zahl je Variante, Anzahl der Wochen-Blöcke.
  4. Liegen weniger als drei getrennte Zeiträume mit Daten vor, wird das Ergebnis nicht als robust bewertet.
  5. Ein Ergebnis unter der Schwelle wird als solches berichtet.

#### Ergebnis: Robustheitsmessung 1h (Pre-Registration ce6093a)
Gültige Messung nach den vorab festgelegten Regeln. Alle 1h-Läufe haben die Abnahme erfüllt.

- Trades (netto, gepoolt über 3 Symbole und 3 Zeiträume, 23 Wochen-Blöcke): Basis 4778, mit SMC 2195.
- Avg R Basis: −0,116, 95-%-CI [−0,254; +0,012].
- Δ Avg R (SMC − Basis): −0,045, 95-%-CI [−0,120; +0,032].
- Verdikt: Die Basis ist nicht positiv belegt, weil die untere CI-Grenze unter 0 liegt. SMC ist nicht von 0 unterscheidbar, weil das Δ-CI 0 enthält. Der Merge bleibt ausgesetzt.

**4h: ungültig.** In sechs Läufen ist die Abnahme 2 nicht erfüllt, weil der max_hold-Zähler 0 ist: ETH/USDT 4h Zeitraum C (brutto und netto, mit und ohne SMC) sowie SOL/USDT 4h Zeitraum C (brutto und netto, mit SMC). Für 4h gibt es deshalb kein Ergebnis und kein Verdikt. Die absolute Fensterlänge ist nicht die Ursache: Die OOS-Fenster liegen bei 73 bis 74 Bars. Entscheidend ist das Verhältnis Haltelimit zu Fensterlänge, denn nur die ersten rund 50 Bars eines Fensters bieten 24 Bars Restlaufzeit. Warum ETH/USDT 4h C (SMC aus und an) und SOL/USDT 4h C (SMC an) keinen time-Exit haben, ist nicht geprüft. Eine Re-Registration erfolgt erst nach der Ursachenanalyse und mit eigenem Pre-Registration-Commit.

#### Korrektur: Robustheitsmessung 1h war nicht regelkonform (nur berichtend, kein Gate)
Der Eintrag «Ergebnis: Robustheitsmessung 1h (Pre-Registration ce6093a)» oben ist ungültig und wird durch eine Neumessung ersetzt.

- **Befund:** Die Engine in `tradeagent/backtest.py` (`_simulate_trade`, Zeilen 130 bis 185) kennt nur die Exit-Gründe `stop`, `target` und `eod`. Ein Haltelimit von 24 Bars ist dort nicht implementiert.
- **Ursache des time-Zählers:** Das Label `time` entsteht nur nachträglich. In `snapshots/diag_maxhold.py` (Zeilen 54 bis 55) und in `snapshots/measure_ranges.py` wird ein `eod`-Trade nur dann auf `time` umgeschrieben, wenn sein Exit genau auf Entry + 24 Bars fällt. Das tritt nur auf, wenn ein Trade 24 Bars vor dem Datenende eröffnet wird.
- **Folge:** Die Abnahme 2 («max_hold-Zähler > 0») prüfte deshalb einen Zufallstreffer, nicht die Haltelimit-Regel. Die 1h-Zahlen (Basis −0,116, Δ −0,045) stammen aus einer Messung ohne wirksames Haltelimit. Sie sind nicht als pre-registriertes Ergebnis zu werten.
- **Was unverändert gilt:** Die Pre-Registration ce6093a bleibt gültig. Die Regeln (24 Bars, Block-Bootstrap, Interpretation) werden nicht geändert. Umgesetzt wurden sie nicht.
- **Die 4h-Nullzähler** entstehen durch das Verhältnis Haltelimit zu Fensterlänge. Die Fensterlänge allein (73 bis 74 Bars) erklärt sie nicht, weil nur die ersten rund 50 Bars eines Fensters 24 Bars Restlaufzeit bieten.
- **Nächster Schritt:** Das Haltelimit wird in der Engine erzwungen, mit `exit_reason = "time"` direkt in der Schleife. Dazu gehört ein Engine-Diff zur Prüfung, bevor neu gemessen wird. Die Neumessung von 1h und 4h ist die erste regelkonforme Ausführung. Der Merge bleibt ausgesetzt.

#### Richtigstellung zur Korrektur: Haltelimit wirkt in der Messung
Der Korrektur-Eintrag oben (Commit 57dca48) ist falsch. Die Aussage, das 24-Bar-Haltelimit sei nirgends wirksam, gilt nur für `tradeagent/backtest.py`.

- `snapshots/measure_ranges.py` hängt `_simulate_capped` über `bt._simulate_trade = _simulate_capped` in die Engine ein (Zeile 130). `_simulate_capped` begrenzt jeden Trade auf MAX_HOLD = 24 Bars und markiert einen Exit an dieser Grenze als `time`.
- `run_walk_forward` ruft `_simulate_trade` über den Modulnamen auf (`backtest.py`, Zeile 509). Der Monkeypatch greift daher.
- Die `time`-Labels der 1h-Messung sind damit echte Haltelimit-Exits. Die 1h-Messung ist nicht als nicht-regelkonform zu werten.
- Belegt mit exit_bar und OOS-Grenzen (Stand 2026-11-18): Alle 353 eod-Trades der 4h-Netto-Läufe enden am OOS-Fensterende mit weniger als 24 Bars. Drei Läufe haben keinen time-Exit: ETH/USDT 4h C (SMC aus), ETH/USDT 4h C (SMC an), SOL/USDT 4h C (SMC an).

## Sicherheits-Prinzipien (von Tag 1)
- API-Keys **nur** aus Env/Secrets — niemals im Quellcode.
- Client-Eingaben niemals als Trust-Quelle für Ausführung.
- Live-Modus hat harte Limits (Max-Order-Größe, VP-Limit, Allowlist der Symbole).
- Logs redact sensitive Daten (Keys, vollständige Order-IDs).

## Pre-Registration 2: 4h-Messung mit n_folds=2 (vor dem ersten Lauf committet)

- **Zweck:** Die 4h-Messung unter ce6093a ist ungültig (Abnahme 2, max_hold-Zähler = 0 in drei Läufen). Diese Messung wiederholt die 4h-Auswertung mit einem größeren Fenster.
- **Zeitraum:** D = 2024-05-01 bis 2024-10-31, Timeframe 4h, 6 Monate. Kein Überlapp mit den Zeiträumen A, B und C.
- **Symbole:** BTC/USDT, ETH/USDT, SOL/USDT.
- **Konfiguration:** n_folds=2, is_ratio 0,70, min_signal_score 50, max_hold 24 Bars. Gegenüber ce6093a ändern sich nur n_folds und der Zeitraum.
- **Haltelimit:** 24 Bars sind bei 4h vier Tage, nicht ein Tag. Das Limit gilt in Bars.
- **Erwartung (nicht bindend):** OOS rund 160 Bars je Fold, eod-Anteil rund 15 %. Der Lauf prüft das.
- **Abnahme 1 (Datenlage):** Liefert der Datenlader für D keine lückenlose 4h-Serie, ist kein Lauf zulässig. Eine Lücke erfordert eine neue Pre-Registration.
- **Abnahme 2 (max_hold-Zähler):** Jeder Lauf (Symbol, SMC an/aus, brutto und netto) muss time-Exits größer als 0 haben. Sonst ist der Lauf ungültig und hat kein Verdikt.
- **Auswertung:** Block-Bootstrap über ISO-Kalenderwochen, 10.000 Resamples, Seed 42, 95-%-Perzentil-Intervall. Netto ist primär.
- **Verdikt-Regeln (unverändert zu ce6093a):** Die Basis ist positiv, wenn die untere CI-Grenze des Avg R über 0 liegt. SMC ist unterscheidbar, wenn das Δ-CI 0 nicht enthält.
- **Nicht Teil des Verdikts:** Sharpe, Sortino und DSR.
- **Merge:** Bleibt ausgesetzt.

### Ergebnis Pre-Registration 3 (4h, netto, Symbole gepoolt)

Die Läufe sind mit der festgelegten Konfiguration durchgeführt worden. Die Werte stehen in der Form Basis Trades / mit SMC Trades.

- **Zeitraum A:** 299 / 125 Trades. Basis Avg R −0,091 [−0,241; +0,036]. Δ (SMC − Basis) +0,068 [−0,252; +0,513]. 6 Wochen-Blöcke.
- **Zeitraum B:** 294 / 155 Trades. Basis Avg R −0,204 [−0,550; +0,329]. Δ −0,024 [−0,172; +0,195]. 5 Wochen-Blöcke.
- **Zeitraum C:** 291 / 139 Trades. Basis Avg R +0,114 [−0,189; +0,454]. Δ −0,209 [−0,301; +0,189]. 6 Wochen-Blöcke.
- **Gepoolt A bis C:** 884 / 419 Trades. Basis Avg R −0,061 [−0,255; +0,148]. Δ −0,062 [−0,191; +0,120]. 17 Wochen-Blöcke.
- **Gepoolt A bis D (Berichtsregel, alle vier Zeiträume gültig):** 1466 / 683 Trades. Basis Avg R +0,009 [−0,198; +0,207]. Δ −0,065 [−0,167; +0,061]. 25 Wochen-Blöcke.
- **Abnahmen:** Abnahme 1 erfüllt, lückenlose Serien. Abnahme 2 erfüllt, time-Exits > 0 in allen Läufen. Abnahme 3 erfüllt, gepoolt ≥ 100 Trades je Variante. Abnahme 4 erfüllt, 109 bis 111 OOS-Bars je Fold.
- **Verdikt:** Die Basis ist nicht als positiv belegt, weil die untere CI-Grenze in den gepoolten Auswertungen unter 0 liegt. SMC ist nicht von 0 unterscheidbar, weil das Δ-CI in allen Zusammenstellungen 0 enthält. Der Merge bleibt ausgesetzt. Das 4h-Verdikt aus Pre-Registration 2 ist mit vier gültigen Zeiträumen bestätigt.
- **Einschränkung:** Je Zeitraum gibt es nur 5 bis 6 Wochen-Blöcke. Die CIs der Einzelzeiträume sind grob und nur beschreibend. Zeitraum C hat einen positiven Punktschätzer der Basis, sein CI enthält aber 0.
- **Hinweis zur Auswertung:** analyze_ranges.py enthält die Konstante MIN_PERIODS = 3 und markiert Einzelzeiträume deshalb als „nicht robust bewertet“. Diese Mindestzahl steht weder in Pre-Registration 2 noch in Pre-Registration 3. Die Markierung ist nur Beschreibung und ändert das Verdikt nicht.

### Ergebnis Pre-Registration 2 (4h, Zeitraum D, n_folds=2)

- **Abnahme 1 (Datenlage):** erfüllt. Lückenlose 4h-Serie für D, 1104 Bars je Symbol (2024-05-01 00:00 bis 2024-10-31 20:00 UTC). Eine frühere Fassung dieser Zeile nannte 4416 Bars, das ist die Länge der 1h-Serie.
- **Abnahme 2 (max_hold-Zähler):** erfüllt. Alle sechs Läufe haben time-Exits > 0 (SMC aus: 41, 51, 34; SMC an: 12, 23, 17).
- **OOS-Länge:** 166 Bars je Fold, im Rahmen der Erwartung von rund 160.
- **eod-Anteil (netto):** SMC aus 2,1 % (12 von 582), SMC an 0,4 % (1 von 264). Die Erwartung von rund 15 % wird deutlich unterschritten.
- **Trades (netto):** Basis 582, mit SMC 264.
- **Avg R Basis:** +0,114, 95-%-CI [−0,317; +0,464].
- **Δ Avg R (SMC − Basis):** −0,065, 95-%-CI [−0,226; +0,079].
- **Verdikt (nach den Regeln oben):** Die Basis ist nicht positiv belegt, weil die untere CI-Grenze des Avg R (−0,317) unter 0 liegt. SMC ist nicht von der Basis unterscheidbar, weil das Δ-CI [−0,226; +0,079] die 0 enthält. Der Merge bleibt ausgesetzt. Die Pre-Registration 2 nennt keine Mindestzahl an Zeiträumen. Die Aussagekraft ist trotzdem begrenzt, weil nur ein Zeitraum (D) ausgewertet wurde.
- **Merge:** Bleibt ausgesetzt.

## Pre-Registration 3: 4h-Messung der Zeiträume A bis C mit n_folds=2 (vor dem ersten Lauf committet)

Diese Festlegung wird vor der ersten Messung committet. Die Konfiguration wird nach dem Blick auf die Zahlen nicht geändert.

- **Zweck:** Die 4h-Robustheit stützt sich bisher auf einen Zeitraum (D, Pre-Registration 2). Die 4h-Läufe für A bis C unter ce6093a sind ungültig (n_folds=3, rund 73 OOS-Bars je Fold). Diese Messung wiederholt sie mit der Konfiguration von Pre-Registration 2.
- **Zeiträume (4h, UTC, Start und Ende inklusive):**
  - A: 2022-05-01 bis 2022-08-31 (rund 738 Bars je Symbol)
  - B: 2023-06-01 bis 2023-09-30 (rund 732 Bars je Symbol)
  - C: 2024-01-01 bis 2024-04-30 (rund 726 Bars je Symbol)
  - A, B, C und D (2024-05-01 bis 2024-10-31) überlappen sich nicht.
- **Symbole:** BTC/USDT, ETH/USDT, SOL/USDT.
- **Konfiguration:** wie Pre-Registration 2: n_folds=2, is_ratio 0,70, min_signal_score 50, max_hold 24 Bars. Die einzige Abweichung ist der Zeitraum. Er ist mit rund 4 statt 6 Monaten kürzer, daher liegen die OOS-Fenster bei rund 109 bis 111 statt 166 Bars je Fold (Bars × 0,3 / 2).
- **Erwartung (nicht bindend):** OOS rund 110 Bars je Fold. Zum eod-Anteil gibt es keine Erwartung. Der Lauf berichtet ihn ohne Deutung.
- **Daten:** Die Daten für A bis C wurden bereits unter ce6093a ausgewertet und sind nicht unbeobachtet. Die Konfiguration ist deshalb vorab festgelegt und wird nicht angepasst.
- **Auswertung:** wie Pre-Registration 2. Block-Bootstrap über ISO-Kalenderwochen nach Entry-Zeit, Symbole gemeinsam, 10.000 Resamples, Seed 42, 95-%-Perzentil-Intervall. Δ = Avg R mit SMC minus Avg R ohne SMC, mit denselben Wochen resampelt. Netto ist primär, brutto nur beschreibend.
- **Abnahme 1 (Datenlage):** Liefert der Datenlader für einen Zeitraum und ein Symbol keine lückenlose 4h-Serie, ist für diesen Zeitraum kein Lauf zulässig. Eine Lücke erfordert eine neue Pre-Registration.
- **Abnahme 2 (max_hold-Zähler):** Jeder Lauf (Symbol, Zeitraum, SMC an/aus, brutto und netto) muss time-Exits größer als 0 haben. Sonst ist der Lauf ungültig und hat kein Verdikt.
- **Abnahme 3 (Mindeststichprobe):** 100 gepoolte Trades je Variante. Darunter gilt das Ergebnis als nicht auswertbar.
- **Abnahme 4 (OOS-Länge, neu in dieser Pre-Registration):** Jeder Fold braucht mindestens 100 OOS-Bars. Darunter ist der Zeitraum ungültig. Diese Grenze folgt nicht aus Pre-Registration 2. Sie liegt zwischen den 73 Bars, bei denen unter ce6093a drei Läufe keinen time-Exit hatten, und den erwarteten rund 110 Bars. Dass 100 Bars ausreichen, ist nicht bewiesen. Abnahme 2 prüft es je Lauf.
- **Verdikt-Regeln (unverändert zu Pre-Registration 2):** Die Basis ist positiv, wenn die untere CI-Grenze des Avg R über 0 liegt. SMC ist unterscheidbar, wenn das Δ-CI 0 nicht enthält.
- **Berichtsregel (neu in dieser Pre-Registration):** Ergebnisse werden je Zeitraum und gepoolt berichtet. Eine Robustheitsaussage zum 4h-Verdikt setzt gültige Läufe in allen vier Zeiträumen (A bis D) voraus. Ist ein Zeitraum ungültig, wird nur berichtet.
- **Nicht Teil des Verdikts:** Sharpe, Sortino und DSR.
- **Merge:** Bleibt ausgesetzt.
