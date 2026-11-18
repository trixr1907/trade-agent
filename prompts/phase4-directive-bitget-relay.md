# Phase 4 Direktive: Bitget-Relay

Status: Entwurf, noch nicht implementiert.
Geltungsbereich: Live-Ausführung über Bitget (CCXT). Paper-Modus bleibt unverändert.

## 1. Zweck

Der Relay ist die Ausführungsschicht zwischen dem Freigabe-Workflow (`graph.py`) und der Exchange. Jede Order an Bitget läuft über den Relay. Er ist das letzte fail-geschlossene Gate vor echtem Kapital. Er trifft keine Handelsentscheidung.

## 2. Nicht-Ziele

- Keine Signal- oder Trade-Entscheidung. Die liegt bei `signals.py` und der Freigabe.
- Keine Freigabe. Freigaben bleiben server-verifiziert über `POST /approve`.
- Kein Ersatz für `PaperBroker`. Der Paper-Pfad bleibt unberührt.
- Kein Backtest. Der Backtest nutzt simulierte Fills und ruft den Relay nicht auf.

## 3. Ist-Zustand (aus dem Code gelesen)

- `execution.py`: `LiveBroker.place_order()` prüft nur die Notional-Grenze und die Allowlist. Der Halt-Status und die Positionsanzahl werden dort nicht geprüft. Sie prüft `graph.py::execute()` vor dem Aufruf.
- `execution.py`: `LiveBroker.place_order()` sendet eine Market-Order ohne Idempotenz-Schlüssel.
- `execution.py`: `place_stop_orders()` schluckt Fehler per `except: pass` und gibt `None` zurück. Der Entry bleibt dann ohne Schutz-Stop bestehen. Das widerspricht dem fail-geschlossenen Ziel.
- `config.py`: Defaults `TRADING_MODE=paper`, `LIVE_MAX_POSITION_NOTIONAL_USD=500`, `LIVE_MAX_OPEN_POSITIONS=3`, `LIVE_SYMBOL_ALLOWLIST=BTC/USDT,ETH/USDT`. Live erfordert `BITGET_API_KEY`, `BITGET_API_SECRET`, `BITGET_API_PASSPHRASE`.
- `graph.py::execute()`: Prüft vor `broker.place_order()` den Halt-Status (`ControlPlane.is_halted()`), die Positionsanzahl (`open_count()`) und eine bestehende Position im selben Symbol (`open_position_for_symbol()`). Scheitert `place_stop_orders()`, schreibt `execute()` nur eine Warnung ins Log ("Position bleibt ohne Exchange-Stop") und legt die Position trotzdem an.
- `state.py`: Es gibt Tabellen für offene und geschlossene Positionen, P&L, Equity und Control-State. Es gibt keine Tabelle für Order-Belege, Fills oder abgelehnte Orders.
- `state.py::add_position()`: Nutzt `INSERT OR REPLACE` auf `open_positions`. Ein zweiter Aufruf mit derselben `proposal_id` überschreibt die Zeile still und meldet keinen Fehler. Das ist kein Idempotenz-Schutz.
- `execution.py`: `create_oco_order` wird ohne Prüfung der Unterstützung aufgerufen. Jede Exception löst den Fallback aus, auch ein Netzwerkfehler nach einer bereits angenommenen Order.
- `execution.py::LiveBroker.place_order()`: Ruft `self.exchange.create_market_order(symbol, side, qty)` ohne `clientOrderId`/`newClientOrderId`-Parameter auf. Ein Retry mit identischen Argumenten erzeugt eine zweite Order auf der Exchange-Seite.

## 4. Kernaufgaben

1. **Gate vor dem Senden.** Der Relay prüft erneut:
   - Control-Plane nicht gehalten (`control.py`)
   - `TRADING_MODE == live`
   - Symbol in `LIVE_SYMBOL_ALLOWLIST`
   - Notional ≤ `LIVE_MAX_POSITION_NOTIONAL_USD`
   - Offene Positionen < `LIVE_MAX_OPEN_POSITIONS`
   - Keine zweite Position im selben Symbol
2. **Fail-geschlossen.** Ein verfehlter Check sendet keine Order. Der Relay gibt einen Ablehnungsgrund zurück und schreibt ihn in den Beleg. Es gibt keine Teilausführung und keinen Bypass.
3. **Routing über CCXT.** Der Relay sendet die Order an Bitget und gibt Order-ID und Fill zurück.
4. **Idempotenz.** Jeder freigegebene Intent erhält eine feste Client-Order-ID. Ein zweiter Aufruf mit derselben ID sendet keine zweite Order.
5. **Belege.** Order-ID, Fill, Zeitstempel, Symbol, Menge und Ablehnungsgrund landen im `state.py`-Store.
6. **Schutz-Orders.** Nach einem Entry platziert der Relay Stop und Target. Scheitert das, ist das Ergebnis ein explizites Fehlersignal. Der Relay schluckt keine Fehler.
7. **Fill-Tracking.** Fills aktualisieren den Positionsstatus. `monitor.py` nutzt diesen Status für die OCO-Überwachung.
8. **Verbindung.** Rate-Limits, Timeouts und Fehlerklassen (retrybar / nicht retrybar) werden behandelt. Ein Retry darf keine Order doppelt senden. Das regelt die Idempotenz aus Punkt 4.

## 5. Einbindung

| Datei | Änderung |
|---|---|
| `tradeagent/relay.py` (neu) | Relay-Schicht mit Gate, Idempotenz, Belegen |
| `tradeagent/graph.py::execute()` | Ruft den Relay nach dem bestehenden Halt-Gate auf |
| `tradeagent/execution.py` | `LiveBroker` nutzt den Relay als Backend. Die stillen `except: pass`-Blöcke werden ersetzt. |
| `tradeagent/config.py` | Keine neuen Grenzwerte. Die bestehenden werden gelesen. |
| `tradeagent/state.py` | Neue Tabelle für Order-Belege (Intent-ID, Client-Order-ID, Order-ID, Status, Fill, Zeitstempel, Ablehnungsgrund) mit Unique-Constraint auf die Intent-ID. Die Idempotenz stützt sich auf diese Tabelle, nicht auf `add_position()`. |
| `PaperBroker` | Keine Änderung |

## 6. Abnahmekriterien

1. **Fail-geschlossen:** Für jeden Check aus Punkt 4.1 gibt es einen Test, der bei Verletzung keine Order sendet.
2. **Idempotenz:** Zwei Aufrufe mit derselben Intent-ID senden genau eine Order.
3. **Schutz-Orders:** Scheitert der Stop, ist das Ergebnis ein Fehler und kein stiller Erfolg.
4. **Belege:** Jede gesendete und jede abgelehnte Order erzeugt einen Beleg-Eintrag.
5. **Paper unberührt:** Die bestehenden Paper-Tests laufen unverändert durch.
6. **Keine Live-Tests ohne Freigabe:** Tests nutzen einen Mock für CCXT. Es wird keine echte Order an Bitget gesendet.

## 7. Offene Entscheidungen (vom Auftraggeber zu treffen)

- Verhalten, wenn der Stop nach dem Entry nicht platziert werden kann. Optionen: Position sofort schließen, Control-Plane halten und Alarm, oder nur Alarm. Bis zur Entscheidung gilt: Control-Plane halten.
- Ob OCO über CCXT bei Bitget unterstützt wird. Das ist vor der Implementierung gegen die Bitget-Dokumentation und die installierte CCXT-Version zu prüfen. Der aktuelle Code rät den Pfad und fällt bei Fehlern still zurück.
- Ob der Relay Market- oder Limit-Orders für den Entry verwendet.