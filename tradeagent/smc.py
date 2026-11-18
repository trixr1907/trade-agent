"""Deterministische SMC-Analyse (Smart Money Concepts).

Erzeugt keine eigenen Signale. Bewertet ein bestehendes Signal mit einem
Konfluenz-Score in [0, 1] und löst bei einem frischen CHoCH gegen die
Signalrichtung ein Veto aus.

Kein Lookahead: Jede Regel nutzt nur Bars bis einschließlich des letzten
Bars im übergebenen DataFrame. Ein Swing an Bar i gilt erst ab Bar
i + SWING_LEN als bestätigt und wird vorher von keiner Regel verwendet.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .indicators import atr
from .models import LiquiditySweep, SignalDirection, SMCResult, StructureEvent, Zone

SWING_LEN = 3            # Bars links und rechts für einen Swing
FVG_MIN_ATR = 0.1        # Mindestgröße einer FVG in ATR
OB_SEARCH = 10           # Bars vor dem Bruch, in denen der Orderblock gesucht wird
EVENT_LOOKBACK = 30      # wie alt BOS/CHoCH und Sweeps sein dürfen (Bars)
VETO_LOOKBACK = 10       # wie alt ein CHoCH sein darf, um ein Veto auszulösen
PROXIMITY_ATR = 0.5      # Abstand Preis zu Zone, damit sie als "relevant" gilt
WEIGHTS: Dict[str, float] = {"bos": 0.35, "fvg": 0.25, "orderblock": 0.25, "sweep": 0.15}

MAX_EVENTS = 5
MAX_ZONES = 3
MAX_SWEEPS = 3

_KIND_LABEL = {"bos": "BOS", "choch": "CHoCH"}


def _adj(direction: str, feminine: bool = False) -> str:
    """'bullisher'/'bearisher' (der BOS, der Orderblock) bzw. 'bullishe'/'bearishe' (die FVG)."""
    return direction + ("e" if feminine else "er")


def _fmt(value: float) -> str:
    return f"{value:.8g}"


def empty_result() -> SMCResult:
    return SMCResult(
        last_event=None,
        events=[],
        fvgs=[],
        order_blocks=[],
        liquidity_sweeps=[],
        confluence_score=0.0,
        veto=False,
        reasons=[],
    )


def find_swings(high: np.ndarray, low: np.ndarray) -> Tuple[List[int], List[int]]:
    """Indizes der Swing-Highs und Swing-Lows (streng größer/kleiner als SWING_LEN Nachbarn je Seite).

    Bars ohne SWING_LEN rechte Nachbarn im Array werden nie als Swing gemeldet.
    """
    n = len(high)
    swing_highs: List[int] = []
    swing_lows: List[int] = []
    for i in range(SWING_LEN, n - SWING_LEN):
        left_h = high[i - SWING_LEN:i]
        right_h = high[i + 1:i + SWING_LEN + 1]
        if high[i] > left_h.max() and high[i] > right_h.max():
            swing_highs.append(i)
        left_l = low[i - SWING_LEN:i]
        right_l = low[i + 1:i + SWING_LEN + 1]
        if low[i] < left_l.min() and low[i] < right_l.min():
            swing_lows.append(i)
    return swing_highs, swing_lows


def detect_structure_events(
    high: np.ndarray, low: np.ndarray, close: np.ndarray
) -> Tuple[List[StructureEvent], List[LiquiditySweep]]:
    """BOS/CHoCH und Liquidity Sweeps in einem chronologischen Durchlauf.

    Gemerkt wird jeweils das zuletzt bestätigte, noch nicht gebrochene
    Swing-High und Swing-Low. Ein Bruch verbraucht das Level. Ein Sweep
    (Docht über/unter das Level, Close zurück) verbraucht es nicht.
    """
    swing_highs, swing_lows = find_swings(high, low)
    confirm_high = {i + SWING_LEN: float(high[i]) for i in swing_highs}
    confirm_low = {i + SWING_LEN: float(low[i]) for i in swing_lows}

    active_high: Optional[float] = None
    active_low: Optional[float] = None
    prev_dir: Optional[str] = None
    events: List[StructureEvent] = []
    sweeps: List[LiquiditySweep] = []

    for k in range(len(close)):
        if k in confirm_high:
            active_high = confirm_high[k]
        if k in confirm_low:
            active_low = confirm_low[k]

        ck = float(close[k])

        if active_high is not None:
            if ck > active_high:
                kind = "choch" if prev_dir == "bearish" else "bos"
                events.append(StructureEvent(kind=kind, direction="bullish", index=k, level=active_high))
                prev_dir = "bullish"
                active_high = None
            elif float(high[k]) > active_high and ck < active_high:
                sweeps.append(LiquiditySweep(direction="bearish", index=k, level=active_high))

        if active_low is not None:
            if ck < active_low:
                kind = "choch" if prev_dir == "bullish" else "bos"
                events.append(StructureEvent(kind=kind, direction="bearish", index=k, level=active_low))
                prev_dir = "bearish"
                active_low = None
            elif float(low[k]) < active_low and ck > active_low:
                sweeps.append(LiquiditySweep(direction="bullish", index=k, level=active_low))

    return events, sweeps


def detect_fvgs(high: np.ndarray, low: np.ndarray, atr_values: np.ndarray) -> List[Zone]:
    """3-Kerzen-Lücken mit Mindestgröße FVG_MIN_ATR * ATR[i]. Bars ohne gültigen ATR werden übersprungen."""
    n = len(high)
    zones: List[Zone] = []
    for i in range(2, n):
        a = float(atr_values[i])
        if not np.isfinite(a):
            continue
        min_gap = FVG_MIN_ATR * a
        if low[i] > high[i - 2] and (low[i] - high[i - 2]) >= min_gap:
            bottom, top = float(high[i - 2]), float(low[i])
            mitigated = bool(np.any(low[i + 1:] <= bottom))
            zones.append(Zone(kind="fvg", direction="bullish", index=i, bottom=bottom, top=top, mitigated=mitigated))
        elif high[i] < low[i - 2] and (low[i - 2] - high[i]) >= min_gap:
            bottom, top = float(high[i]), float(low[i - 2])
            mitigated = bool(np.any(high[i + 1:] >= top))
            zones.append(Zone(kind="fvg", direction="bearish", index=i, bottom=bottom, top=top, mitigated=mitigated))
    return zones


def detect_order_blocks(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    events: List[StructureEvent],
) -> List[Zone]:
    """Letzte Gegenkerze in k-OB_SEARCH .. k-1 vor jedem Bruch an Bar k.

    Mitigation wird ab dem Bar nach dem Bruch geprüft, weil der Orderblock
    erst mit dem Bruch feststeht. Zone.index ist der Bar der Gegenkerze.
    """
    zones: List[Zone] = []
    seen = set()
    for ev in events:
        k = ev.index
        start = max(0, k - OB_SEARCH)
        found: Optional[int] = None
        for j in range(k - 1, start - 1, -1):
            if ev.direction == "bullish" and close[j] < open_[j]:
                found = j
                break
            if ev.direction == "bearish" and close[j] > open_[j]:
                found = j
                break
        if found is None or (ev.direction, found) in seen:
            continue
        seen.add((ev.direction, found))
        bottom, top = float(low[found]), float(high[found])
        later = close[k + 1:]
        if ev.direction == "bullish":
            mitigated = bool(np.any(later < bottom))
        else:
            mitigated = bool(np.any(later > top))
        zones.append(
            Zone(kind="orderblock", direction=ev.direction, index=found, bottom=bottom, top=top, mitigated=mitigated)
        )
    zones.sort(key=lambda z: z.index)
    return zones


def _zone_in_reach(zone: Zone, price: float, tolerance: float, sig_dir: str) -> bool:
    """BUY: Preis in oder über der Zone, höchstens tolerance über top. SELL spiegelbildlich."""
    if sig_dir == "bullish":
        return price >= zone.bottom and price - zone.top <= tolerance
    return price <= zone.top and zone.bottom - price <= tolerance


def analyze_smc(df: pd.DataFrame, direction: SignalDirection) -> SMCResult:
    """Konfluenz-Score (0..1) und CHoCH-Veto für ein Signal in `direction`."""
    n = len(df)
    if n < 2 * SWING_LEN + 3:
        return empty_result()

    open_ = np.array(df["open"], dtype=float)
    high = np.array(df["high"], dtype=float)
    low = np.array(df["low"], dtype=float)
    close = np.array(df["close"], dtype=float)
    atr_values = np.array(atr(df), dtype=float)

    events, sweeps = detect_structure_events(high, low, close)
    fvgs_all = detect_fvgs(high, low, atr_values)
    obs_all = detect_order_blocks(open_, high, low, close, events)

    sig_dir = "bullish" if direction == SignalDirection.BUY else "bearish"
    last_bar = n - 1
    price = float(close[-1])
    atr_last = float(atr_values[-1])
    tolerance = PROXIMITY_ATR * atr_last if np.isfinite(atr_last) else 0.0

    last_event = events[-1] if events else None
    score = 0.0
    reasons: List[str] = []

    if last_event is not None and last_event.direction == sig_dir and last_bar - last_event.index <= EVENT_LOOKBACK:
        score += WEIGHTS["bos"]
        reasons.append(f"SMC: {_adj(sig_dir)} {_KIND_LABEL[last_event.kind]} bei {_fmt(last_event.level)}")

    open_fvgs = [z for z in fvgs_all if not z.mitigated]
    fvg_hits = [z for z in open_fvgs if z.direction == sig_dir and _zone_in_reach(z, price, tolerance, sig_dir)]
    if fvg_hits:
        z = fvg_hits[-1]
        score += WEIGHTS["fvg"]
        reasons.append(f"SMC: {_adj(sig_dir, feminine=True)} FVG {_fmt(z.bottom)} bis {_fmt(z.top)} in Reichweite")

    open_obs = [z for z in obs_all if not z.mitigated]
    ob_hits = [z for z in open_obs if z.direction == sig_dir and _zone_in_reach(z, price, tolerance, sig_dir)]
    if ob_hits:
        z = ob_hits[-1]
        score += WEIGHTS["orderblock"]
        reasons.append(f"SMC: {_adj(sig_dir)} Orderblock {_fmt(z.bottom)} bis {_fmt(z.top)} in Reichweite")

    sweep_hits = [s for s in sweeps if s.direction == sig_dir and last_bar - s.index <= EVENT_LOOKBACK]
    if sweep_hits:
        s = sweep_hits[-1]
        score += WEIGHTS["sweep"]
        if sig_dir == "bullish":
            reasons.append(f"SMC: Liquidity Sweep unter {_fmt(s.level)} (Sell-Side)")
        else:
            reasons.append(f"SMC: Liquidity Sweep über {_fmt(s.level)} (Buy-Side)")

    veto = False
    if (
        last_event is not None
        and last_event.kind == "choch"
        and last_event.direction != sig_dir
        and last_bar - last_event.index <= VETO_LOOKBACK
    ):
        veto = True
        age = last_bar - last_event.index
        reasons.append(
            f"SMC-Veto: {_adj(last_event.direction)} CHoCH bei {_fmt(last_event.level)} "
            f"vor {age} Bars gegen die Signalrichtung"
        )

    return SMCResult(
        last_event=last_event,
        events=events[-MAX_EVENTS:],
        fvgs=open_fvgs[-MAX_ZONES:],
        order_blocks=open_obs[-MAX_ZONES:],
        liquidity_sweeps=sweeps[-MAX_SWEEPS:],
        confluence_score=round(min(score, 1.0), 4),
        veto=veto,
        reasons=reasons,
    )