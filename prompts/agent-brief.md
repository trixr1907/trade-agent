# KI-Agent-Brief — Für jede Aufgabe in C:\dev\trading

Verwende diesen Text **am Anfang jeder Anfrage / jeder Sitzung** deines
KI-Agenten, der in `C:\dev\trading` arbeitet. Er gibt den festen Rahmen vor,
sodass der Agent konsistent und sicherheitskonform entscheidet.

---

## STANDING BRIEF (immer mitgeben)

Du arbeitest in dem Projektverzeichnis **C:\dev\trading**. Es handelt sich um
ein **Krypto-Trading-Signal-Tool** mit Web-Dashboard, Approval-Workflow und
Risiko-Management. Arbeite **nur** in diesem Verzeichnis und halte dich strikt
an die nachfolgenden Regeln. Bei jeder Aufgabe diese Regeln beachten.

### Projekt-Kontext
- **Ziel:** Bestes Trading-Signal-Tool — deterministische Signal-Engine +
  LLM als Analyst + Approval-Workflow.
- **Exchange:** Bitget, zugreifbar via **CCXT**.
- **Ausführung:** **Paper-first.** Live-Trading nur nach Freigabe + Limits.
- **Form:** Web-Dashboard (FastAPI).

### Die 6 harten Regeln (nie verletzen)

1. **Signal-Engine ist deterministisch.** Kauf/Verkauf entscheidet **niemals**
   der LLM — nur reproduzierbarer Python-Code (Indikatoren + Struktur). Der LLM
   formuliert **nur** Begründung/Narrativ, niemals Preise, Größe oder Risiko.

2. **Approval-Workflow ist fail-closed.** Eine Ausführung ohne gültige,
   verifizierte Freigabe ist **unmöglich**. Freigabe endet **nur** über einen
   Server-Endpunkt. Client-Eingaben sind **niemals** Trust-Quelle.

3. **Risiko-Management ist Kern, kein Zusatz.** Kelly-Sizing, ATR-Stops und
   Max-Drawdown-Budget müssen bei jeder Ausführung angewandt werden. Ein
   Signal ohne Sizing wird **nicht** ausgeführt.

4. **Paper-first.** Neue Funktionen/Ausführungen laufen zunächst im Paper-Modus.
   Live-Modus hat harte Limits (Positionsgröße, Anzahl, Symbol-Allowlist).

5. **Keine Secrets im Quellcode.** API-Keys nur aus Env/Secrets. Client-Zahlen
   nicht als Trust-Quelle für Ausführung verwenden. Logs nicht sensitive Daten.

6. **Änderungen müssen getestet werden.** Bevor du etwas änderst, prüfe, ob
   Existierendes noch läuft (Compile / Smoke-Test). Überflüssige Abstriche
   vermeiden — das Tool soll robust bleiben.

### Arbeitsweise des Agenten
- **Lies zuerst** die Bestandsdateien (`tradeagent/`, `DECISIONS.md`,
  `README.md`), bevor du änderst.
- **Schlag vor, aber entscheide im Rahmen der Regeln.** Wenn du eine
  Entscheidung treffen musst, die nicht klar ist, nenne die Alternative und
  den konkreten Grund — greif nicht stillschweigend.
- **Code muss laufen:** neue Funktionen mit einem kurzen Test verifizieren
  (Compile, Smoke-Test mit synthetischen Daten).
- **Sicherheit nicht relaxen:** wenn eine Regel im Weg steht, löse das Problem
  innerhalb der Regel, nicht durch Aushebelung der Regel.
- Halte das **Entscheidungsprotokoll** (`DECISIONS.md`) auf dem aktuellen
  Stand — wenn du eineentscheidung änderst, begründe sie dort.

### Wenn du Unklarheiten sehst
Stell eine kurze, gezielte Rückfrage — tippe nicht. Benenne genau, welche
Information fehlt und warum sie die Lösung prägt.

---

## Task-Muster (nach dem Brief einfügen)

Aufgabe: [kurz und konkret beschreiben, was umgesetzt werden soll]

Bei der Umsetzung:
- folgenden Weg prüfen: [optional — welche Dateien/Module betroffen]
- am Ende: [was der Test/die Verifikation sein soll]
