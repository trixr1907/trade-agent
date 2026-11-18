"""Demo-Testlauf gegen Bitget Demo-Trading (Header PAPTRADING: 1, fiktive Bilanzen).

Die Demo-Werte stehen in .env.demo (Standard, siehe --env-file): TRADING_MODE=live, BITGET_DEMO=true
und BITGET_DEMO_API_KEY/SECRET/PASSPHRASE. Sie gehen vor die Werte aus .env, die .env bleibt auf paper.
Ohne gültige Demo-Konfiguration bricht das Skript vor jedem privaten API-Call ab.

Modi:
  ohne Flag:   nur Prüfungen (Config, Sandbox-Modus, Demo-Kontostand, Alarm-Gate lokal).
  --place:     Mindest-Position BTC/USDT:USDT über BitgetRelay.place_entry, dann
               - rohe Antworten der TP/SL-Trigger-Orders (planType, Preisfelder),
               - LiveBroker.fetch_order für Stop und Target (offen, nicht gefüllt),
               - LiveBroker.cancel_order für das Target und Abfrage aus der Trigger-Historie,
               - Position per reduceOnly-Market schliessen, prüfen ob Bitget den Stop selbst storniert.
  --stop-fill: Mindest-Position mit engem Stop, warten bis Bitget ihn auslöst, dabei monitor.sync_once
               pollen. Prüft, ob der Monitor den Stop als gefüllt erkennt, woher der Fill-Preis kommt
               und ob das Target storniert wird.

Aufräumen im finally: alle TP/SL-Orders auf dem Symbol stornieren und Restpositionen schliessen.
Das ist sicher, weil der Lauf nur startet, wenn vorher keine Position und keine TP/SL-Order offen war.
Der State läuft auf einer temporären SQLite-DB, trade-agent-state.db bleibt unberührt.
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from tradeagent.config import Config, load_config  # noqa: E402
from tradeagent.control import ControlPlane  # noqa: E402
from tradeagent.execution import LiveBroker  # noqa: E402
from tradeagent.monitor import _is_filled, sync_once  # noqa: E402
from tradeagent.relay import AlarmsOpenError, BitgetRelay, resume_trading, to_perp_symbol  # noqa: E402
from tradeagent.state import OpenPosition, StateStore  # noqa: E402

ACTOR = "demo-run"
SYMBOL = "BTC/USDT"
DEFAULT_ENV_FILE = os.path.join(PROJECT_ROOT, ".env.demo")
TPSL_FILTER = {"planType": "profit_loss"}
# Wartezeit, die der Monitor nach dem Verschwinden der Position auf Bitget noch bekommt.
MONITOR_GRACE_S = 30.0
UNIFIED_FIELDS = (
    "id",
    "clientOrderId",
    "status",
    "type",
    "side",
    "amount",
    "filled",
    "price",
    "average",
    "triggerPrice",
    "stopLossPrice",
    "takeProfitPrice",
    "reduceOnly",
)

Check = Tuple[str, bool, str]


def dump(title: str, obj: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def unified(order: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if order is None:
        return None
    view = {key: order.get(key) for key in UNIFIED_FIELDS}
    view["info"] = order.get("info")
    return view


def as_dict(result: Any) -> Dict[str, Any]:
    if dataclasses.is_dataclass(result):
        return dataclasses.asdict(result)
    return dict(vars(result))


def num(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def load_env_file(path: str) -> Optional[str]:
    """Lädt die Demo-Datei mit Vorrang vor .env. Gibt eine Fehlermeldung zurück oder None."""
    if not os.path.isfile(path):
        return f"{path} fehlt. Vorlage .env.demo im Projektordner anlegen und mit den Demo-Werten füllen."
    try:
        from dotenv import load_dotenv
    except ImportError:
        return "python-dotenv ist nicht installiert (pip install python-dotenv)."
    load_dotenv(path, override=True)
    return None


def check_config(cfg: Config, env_file: str) -> Optional[str]:
    name = os.path.basename(env_file)
    if not cfg.trading.is_live:
        return f"TRADING_MODE=live fehlt in {name} (der Relay arbeitet nur im Live-Pfad)."
    if not cfg.exchange.demo:
        return f"BITGET_DEMO=true fehlt in {name}. Ohne Demo-Schalter sendet dieses Skript nichts."
    if not cfg.exchange.has_live_credentials:
        return f"BITGET_DEMO_API_KEY/SECRET/PASSPHRASE fehlen in {name}."
    return None


def open_tpsl(exchange: Any, perp: str) -> List[Dict[str, Any]]:
    orders = exchange.fetch_open_orders(perp, None, None, dict(TPSL_FILTER))
    return [o for o in orders if isinstance(o, dict)] if isinstance(orders, list) else []


def open_positions(exchange: Any, perp: str) -> List[Dict[str, Any]]:
    positions = exchange.fetch_positions([perp])
    result = []
    for pos in positions or []:
        contracts = num(pos.get("contracts")) or 0.0
        if pos.get("symbol") == perp and contracts > 0:
            result.append(pos)
    return result


def close_positions(exchange: Any, perp: str, margin_mode: str) -> List[Dict[str, Any]]:
    responses = []
    for pos in open_positions(exchange, perp):
        close_side = "sell" if pos.get("side") == "long" else "buy"
        order = exchange.create_order(
            perp,
            "market",
            close_side,
            float(pos["contracts"]),
            None,
            {"reduceOnly": True, "marginMode": margin_mode},
        )
        responses.append(unified(order) or {})
    return responses


def alarm_gate_check(state: StateStore) -> Check:
    """Lokal: Resume muss bei offenem Alarm scheitern und nach dem Ack gelingen."""
    alarm_id = state.raise_alarm("demo", "Demo-Lauf: Prüfung Alarm-Gate", ACTOR)
    plane = ControlPlane(state)
    if not plane.is_halted():
        plane.halt(ACTOR, "Demo-Lauf: Prüfung Alarm-Gate")
    try:
        resume_trading(state, ACTOR, "Demo-Lauf")
        return ("Alarm-Gate", False, "Resume ging trotz offenem Alarm durch")
    except AlarmsOpenError:
        pass
    if not state.acknowledge_alarm(alarm_id, ACTOR):
        return ("Alarm-Gate", False, "Ack des Alarms fehlgeschlagen")
    resume_trading(state, ACTOR, "Demo-Lauf")
    if ControlPlane(state).is_halted():
        return ("Alarm-Gate", False, "nach Ack weiterhin HALTED")
    return ("Alarm-Gate", True, "409-Fall blockiert, nach Ack fortgesetzt")


def order_size(exchange: Any, perp: str, price: float) -> float:
    market = exchange.market(perp)
    limits = market.get("limits") or {}
    min_amount = float((limits.get("amount") or {}).get("min") or 0)
    min_cost = float((limits.get("cost") or {}).get("min") or 0)
    wanted = max(min_amount, (min_cost * 1.2) / price if min_cost else 0.0)
    if wanted <= 0:
        raise RuntimeError(f"Keine Mindestmenge für {perp} in den Marktdaten.")
    qty = float(exchange.amount_to_precision(perp, wanted))
    if qty < wanted:
        step = float((market.get("precision") or {}).get("amount") or 0)
        qty = float(exchange.amount_to_precision(perp, wanted + step))
    return qty


def open_demo_position(
    cfg: Config,
    state: StateStore,
    relay: BitgetRelay,
    perp: str,
    stop_pct: float,
    target_pct: float,
    checks: List[Check],
) -> Optional[Dict[str, Any]]:
    """Long-Mindestposition über den Relay. None, wenn die Mindestgröße über dem Limit liegt."""
    exchange = relay.exchange
    last = float(exchange.fetch_ticker(perp)["last"])
    qty = order_size(exchange, perp, last)
    notional = qty * last
    if notional > cfg.live_limits.max_position_notional_usd:
        checks.append(("Positionsgröße", False, f"Mindestgröße {notional:.2f} USD über LIVE_MAX_POSITION_NOTIONAL_USD"))
        return None
    stop = float(exchange.price_to_precision(perp, last * (1 - stop_pct / 100)))
    target = float(exchange.price_to_precision(perp, last * (1 + target_pct / 100)))
    intent_id = f"demo-{uuid.uuid4().hex[:12]}"
    dump("Entry-Parameter", {"intent_id": intent_id, "qty": qty, "price": last, "stop": stop, "target": target})

    result = relay.place_entry(intent_id, SYMBOL, "buy", qty, last, stop, target)
    dump("place_entry-Ergebnis", as_dict(result))
    dump("Belege", state.list_receipts(intent_id))
    dump("Offene Alarme", state.open_alarms())
    raw = result.raw if isinstance(result.raw, dict) else {}
    opened: Dict[str, Any] = {
        "intent_id": intent_id,
        "result": result,
        "stop": stop,
        "target": target,
        "stop_id": raw.get("stop_order_id"),
        "target_id": raw.get("target_order_id"),
    }
    opened["ok"] = result.status in ("filled", "submitted") and bool(opened["stop_id"]) and bool(opened["target_id"])
    checks.append(
        (
            "Entry + Stop + Target",
            opened["ok"],
            f"Status {result.status}, Stop {opened['stop_id']}, Target {opened['target_id']}",
        )
    )
    return opened


def run_order_flow(
    cfg: Config, state: StateStore, relay: BitgetRelay, broker: LiveBroker, perp: str, args: argparse.Namespace
) -> List[Check]:
    checks: List[Check] = []
    exchange = relay.exchange
    try:
        opened = open_demo_position(cfg, state, relay, perp, args.stop_pct, args.target_pct, checks)
        if opened is None or not opened["ok"]:
            return checks
        stop_id = str(opened["stop_id"])
        target_id = str(opened["target_id"])

        time.sleep(args.wait)
        orders = open_tpsl(exchange, perp)
        dump("Offene TP/SL-Orders (roh)", [unified(o) for o in orders])
        by_id = {str(o.get("id")): o for o in orders}
        for role, oid in (("Stop", stop_id), ("Target", target_id)):
            found = by_id.get(oid)
            plan_type = ((found or {}).get("info") or {}).get("planType")
            checks.append((f"{role} offen gelistet", found is not None, f"planType={plan_type!r}"))

        for role, oid in (("Stop", stop_id), ("Target", target_id)):
            order = broker.fetch_order(SYMBOL, oid)
            dump(f"LiveBroker.fetch_order {role}", unified(order))
            ok = order is not None and not _is_filled(order)
            checks.append((f"fetch_order {role} (offen)", ok, f"status={(order or {}).get('status')!r}"))

        cancelled = broker.cancel_order(SYMBOL, target_id)
        checks.append(("cancel_order Target", bool(cancelled), ""))
        time.sleep(args.wait)
        target_after = broker.fetch_order(SYMBOL, target_id)
        dump("Target nach Cancel (Trigger-Historie)", unified(target_after))
        ok = target_after is not None and not _is_filled(target_after)
        checks.append(("Target in Historie, nicht gefüllt", ok, f"status={(target_after or {}).get('status')!r}"))

        dump("Schliessen der Position (reduceOnly)", close_positions(exchange, perp, cfg.exchange.margin_mode))
        time.sleep(args.wait)
        remaining = open_positions(exchange, perp)
        checks.append(("Position geschlossen", not remaining, f"{len(remaining)} offen"))

        stop_still_open = any(str(o.get("id")) == stop_id for o in open_tpsl(exchange, perp))
        stop_after = broker.fetch_order(SYMBOL, stop_id)
        dump("Stop nach Schliessen der Position", unified(stop_after))
        checks.append(
            (
                "OCO: Bitget storniert Stop beim Schliessen selbst",
                not stop_still_open,
                "ja" if not stop_still_open else "nein, monitor.py muss stornieren",
            )
        )
    except Exception as exc:  # Demo-Lauf: Fehler protokollieren, Aufräumen läuft im finally
        checks.append(("Ablauf", False, f"{type(exc).__name__}: {exc}"))
    finally:
        checks.extend(cleanup(cfg, exchange, broker, perp))
    return checks


def run_stop_fill_flow(
    cfg: Config, state: StateStore, relay: BitgetRelay, broker: LiveBroker, perp: str, args: argparse.Namespace
) -> List[Check]:
    checks: List[Check] = []
    exchange = relay.exchange
    try:
        opened = open_demo_position(cfg, state, relay, perp, args.stop_pct, args.target_pct, checks)
        if opened is None or not opened["ok"]:
            return checks
        stop_id = str(opened["stop_id"])
        target_id = str(opened["target_id"])
        result = opened["result"]

        # Wie graph.execute(): Position mit Order-IDs in den State, damit sync_once sie prüft.
        pos = OpenPosition(
            proposal_id=opened["intent_id"],
            symbol=SYMBOL,
            side="buy",
            qty=result.qty,
            entry_price=result.price,
            notional_usd=result.notional_usd,
            stop_price=opened["stop"],
            target_price=opened["target"],
            opened_at=datetime.now(timezone.utc).isoformat(),
        )
        pos.stop_order_id = stop_id
        pos.target_order_id = target_id
        state.add_position(pos)

        closes: List[Dict[str, Any]] = []

        def on_close(closed_pos: OpenPosition, fill_price: float, pnl: float) -> None:
            closes.append({"proposal_id": closed_pos.proposal_id, "fill_price": fill_price, "pnl": pnl})

        print(f"\nWarte bis zu {args.timeout:.0f} s, bis der Stop bei {opened['stop']} auslöst (Abbruch mit Strg+C).")
        deadline = time.monotonic() + args.timeout
        closed_ids: List[str] = []
        gone_since: Optional[float] = None
        while time.monotonic() < deadline:
            closed_ids = sync_once(state, cfg, on_close=on_close)
            if closed_ids:
                break
            if not open_positions(exchange, perp):
                if gone_since is None:
                    gone_since = time.monotonic()
                    print("Position auf Bitget geschlossen, warte auf den Monitor ...")
                elif time.monotonic() - gone_since > MONITOR_GRACE_S:
                    break
            time.sleep(args.poll)

        stop_order = broker.fetch_order(SYMBOL, stop_id)
        dump("Stop-Order nach dem Warten (roh)", unified(stop_order))
        stop_status = (stop_order or {}).get("status")

        if not closed_ids:
            if gone_since is not None:
                checks.append(
                    (
                        "Monitor erkennt ausgelösten Stop",
                        False,
                        f"Position auf Bitget zu, Monitor sieht status={stop_status!r}. _is_filled/fetch_order anpassen.",
                    )
                )
            else:
                checks.append(
                    (
                        "Stop ausgelöst",
                        False,
                        f"nicht innerhalb von {args.timeout:.0f} s. Mit kleinerem --stop-pct oder längerem --timeout wiederholen.",
                    )
                )
            return checks

        stop_filled = stop_order is not None and _is_filled(stop_order)
        checks.append(
            (
                "Monitor erkennt ausgelösten Stop als gefüllt",
                opened["intent_id"] in closed_ids and stop_filled,
                f"status={stop_status!r}" + ("" if stop_filled else ", geschlossen wurde über das Target"),
            )
        )
        dump("on_close (verbuchter Fill und P&L)", closes)

        average = num((stop_order or {}).get("average"))
        booked = closes[0]["fill_price"] if closes else None
        checks.append(
            (
                "Fill-Preis aus der Order (nicht Fallback)",
                average is not None,
                f"average={average}, verbucht={booked}, Stop={opened['stop']}",
            )
        )
        checks.append(
            (
                "Position im State geschlossen",
                state.open_position_for_symbol(SYMBOL) is None,
                "",
            )
        )

        time.sleep(args.wait)
        remaining = open_positions(exchange, perp)
        checks.append(("Position auf Bitget geschlossen", not remaining, f"{len(remaining)} offen"))
        target_open = any(str(o.get("id")) == target_id for o in open_tpsl(exchange, perp))
        target_after = broker.fetch_order(SYMBOL, target_id)
        dump("Target nach dem Stop-Fill", unified(target_after))
        checks.append(
            (
                "Target storniert (manuelles OCO)",
                not target_open,
                f"status={(target_after or {}).get('status')!r}",
            )
        )
    except Exception as exc:  # Demo-Lauf: Fehler protokollieren, Aufräumen läuft im finally
        checks.append(("Ablauf", False, f"{type(exc).__name__}: {exc}"))
    finally:
        checks.extend(cleanup(cfg, exchange, broker, perp))
    return checks


def cleanup(cfg: Config, exchange: Any, broker: LiveBroker, perp: str) -> List[Check]:
    """Storniert alle TP/SL-Orders und schliesst Restpositionen auf perp.

    Alle Orders dort gehören zu diesem Lauf, weil main() vorher ein leeres Symbol verlangt.
    """
    checks: List[Check] = []
    try:
        for order in open_tpsl(exchange, perp):
            broker.cancel_order(SYMBOL, str(order.get("id")))
        close_positions(exchange, perp, cfg.exchange.margin_mode)
        time.sleep(1)
        left_orders = open_tpsl(exchange, perp)
        left_positions = open_positions(exchange, perp)
        ok = not left_orders and not left_positions
        checks.append(("Aufräumen", ok, f"{len(left_orders)} Orders, {len(left_positions)} Positionen übrig"))
    except Exception as exc:
        checks.append(("Aufräumen", False, f"{type(exc).__name__}: {exc}. Demo-Konto manuell prüfen."))
    return checks


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bitget Demo-Testlauf für den Relay.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--place", action="store_true", help="Orders im Demo-Konto senden und wieder schliessen.")
    mode.add_argument(
        "--stop-fill",
        action="store_true",
        help="Position mit engem Stop eröffnen, warten bis er auslöst, Monitor-Erkennung prüfen.",
    )
    parser.add_argument(
        "--stop-pct",
        type=float,
        default=None,
        help="Stop-Abstand in Prozent. Standard 3.0 (0.5 bis 20), mit --stop-fill 0.15 (0.05 bis 20).",
    )
    parser.add_argument("--target-pct", type=float, default=3.0, help="Target-Abstand in Prozent (0.5 bis 20).")
    parser.add_argument("--wait", type=float, default=2.0, help="Wartezeit in Sekunden zwischen den Schritten (0 bis 30).")
    parser.add_argument("--timeout", type=float, default=900.0, help="--stop-fill: maximale Wartezeit in Sekunden (30 bis 3600).")
    parser.add_argument("--poll", type=float, default=5.0, help="--stop-fill: Abstand der Monitor-Zyklen in Sekunden (1 bis 60).")
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE, help="Datei mit den Demo-Werten (Standard: .env.demo).")
    args = parser.parse_args()

    min_stop = 0.05 if args.stop_fill else 0.5
    if args.stop_pct is None:
        args.stop_pct = 0.15 if args.stop_fill else 3.0
    if not min_stop <= args.stop_pct <= 20:
        parser.error(f"--stop-pct muss zwischen {min_stop} und 20 liegen.")
    if not 0.5 <= args.target_pct <= 20:
        parser.error("--target-pct muss zwischen 0.5 und 20 liegen.")
    if not 0 <= args.wait <= 30:
        parser.error("--wait muss zwischen 0 und 30 liegen.")
    if not 30 <= args.timeout <= 3600:
        parser.error("--timeout muss zwischen 30 und 3600 liegen.")
    if not 1 <= args.poll <= 60:
        parser.error("--poll muss zwischen 1 und 60 liegen.")
    return args


def main() -> int:
    args = parse_args()
    env_problem = load_env_file(args.env_file)
    if env_problem:
        print(f"ABBRUCH: {env_problem}", file=sys.stderr)
        return 2
    try:
        cfg = load_config()
    except RuntimeError as exc:
        print(f"ABBRUCH: {exc}", file=sys.stderr)
        return 2
    problem = check_config(cfg, args.env_file)
    if problem:
        print(f"ABBRUCH: {problem}", file=sys.stderr)
        return 2

    perp = to_perp_symbol(SYMBOL)
    from tradeagent.universe import live_symbol_allowed

    if not live_symbol_allowed(cfg, perp):
        print(
            f"ABBRUCH: {perp} ist nicht erlaubt (LIVE_SYMBOL_ALLOWLIST bzw. entdeckte USDT-Perp-Universe).",
            file=sys.stderr,
        )
        return 2

    tmp_dir = tempfile.mkdtemp(prefix="demo-run-")
    state = StateStore(path=os.path.join(tmp_dir, "demo-state.db"))
    checks: List[Check] = []
    try:
        relay = BitgetRelay(cfg, state)
        broker = LiveBroker(cfg)
        if relay.exchange.options.get("sandboxMode") is not True or broker.exchange.options.get("sandboxMode") is not True:
            print("ABBRUCH: Sandbox-Modus ist nicht auf beiden Exchanges aktiv.", file=sys.stderr)
            return 2
        checks.append(("Sandbox-Modus (PAPTRADING: 1)", True, "Relay und LiveBroker"))

        relay.exchange.load_markets()
        balance = relay.exchange.fetch_balance()
        usdt = balance.get("USDT") or {}
        checks.append(("Demo-Auth (fetch_balance)", True, f"USDT frei={usdt.get('free')}, gesamt={usdt.get('total')}"))

        pre_positions = open_positions(relay.exchange, perp)
        pre_orders = open_tpsl(relay.exchange, perp)
        if pre_positions or pre_orders:
            print(
                f"ABBRUCH: Im Demo-Konto sind schon {len(pre_positions)} Positionen und "
                f"{len(pre_orders)} TP/SL-Orders auf {perp} offen. Bitte zuerst schliessen.",
                file=sys.stderr,
            )
            return 2

        checks.append(alarm_gate_check(state))

        if args.place:
            checks.extend(run_order_flow(cfg, state, relay, broker, perp, args))
        elif args.stop_fill:
            checks.extend(run_stop_fill_flow(cfg, state, relay, broker, perp, args))
        else:
            print("\nNur Prüfungen. Mit --place oder --stop-fill werden Orders im Demo-Konto gesendet.")
    except Exception as exc:
        checks.append(("Vorprüfung", False, f"{type(exc).__name__}: {exc}"))
    finally:
        state.close()

    print("\n=== Ergebnis ===")
    for name, ok, detail in checks:
        print(f"[{'OK' if ok else 'FEHLER'}] {name}" + (f": {detail}" if detail else ""))
    return 0 if checks and all(ok for _, ok, _ in checks) else 1


if __name__ == "__main__":
    sys.exit(main())