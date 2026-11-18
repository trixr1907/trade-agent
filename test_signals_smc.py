"""Tests für die deterministische SMC-Analyse (tradeagent/smc.py)
und ihre Integration in analyze_symbol()."""
from __future__ import annotations

import json
from typing import List

import numpy as np
import pandas as pd
import pytest

import tradeagent.signals as signals_mod
from tradeagent import smc
from tradeagent.indicators import compute_snapshot
from tradeagent.models import LiquiditySweep, SignalDirection, SMCResult, StructureEvent
from tradeagent.signals import _score_buy, _score_sell, analyze_symbol

BUY = SignalDirection.BUY
SELL = SignalDirection.SELL


# ─── Hilfen ─────────────────────────────────────────────────────────────────

def bars_from_closes(closes: List[float]) -> pd.DataFrame:
    """OHLC aus Closes: Open = vorheriger Close, langer Docht nur auf der Close-Seite."""
    rows = []
    prev = closes[0]
    for c in closes:
        o = prev
        h = max(o, c) + (0.5 if c > o else 0.1)
        l = min(o, c) - (0.5 if c < o else 0.1)
        rows.append((float(o), float(h), float(l), float(c)))
        prev = c
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["volume"] = 1000.0
    return df


def zigzag_closes(n_legs: int, start: float = 100.0) -> List[float]:
    """Aufwärtstrend: je Leg 5 Bars +2, dann 3 Bars -2."""
    closes = [start]
    for _ in range(n_legs):
        for _ in range(5):
            closes.append(closes[-1] + 2.0)
        for _ in range(3):
            closes.append(closes[-1] - 2.0)
    return closes


def uptrend_df() -> pd.DataFrame:
    # 33 Bars. Bullishe BOS an Bar 12, 20, 28.
    return bars_from_closes(zigzag_closes(4))


def choch_df() -> pd.DataFrame:
    # Uptrend + 6 Bars je -3. Bearisher CHoCH an Bar 34 unter Swing-Low 111.5 (Bar 24).
    closes = zigzag_closes(4)
    for _ in range(6):
        closes.append(closes[-1] - 3.0)
    return bars_from_closes(closes)


def arrays(df: pd.DataFrame):
    return tuple(np.array(df[c], dtype=float) for c in ("open", "high", "low", "close"))


# ─── Swings ─────────────────────────────────────────────────────────────────

def test_swing_confirmed_only_after_swing_len_bars():
    df = bars_from_closes([100.0] * 20)
    _, high, low, _ = arrays(df)
    high[10] = 105.0
    # Bar 10 braucht SWING_LEN rechte Bars (11..13).
    assert 10 not in smc.find_swings(high[:13], low[:13])[0]
    assert 10 in smc.find_swings(high[:14], low[:14])[0]
    assert smc.find_swings(high, low) == ([10], [])


def test_swing_at_end_without_right_bars_is_not_used():
    df = bars_from_closes([100.0] * 12)
    _, high, low, _ = arrays(df)
    high[10] = 105.0
    swing_highs, _ = smc.find_swings(high, low)
    assert 10 not in swing_highs


# ─── BOS / CHoCH ────────────────────────────────────────────────────────────

def test_bullish_bos_in_uptrend():
    _, high, low, close = arrays(uptrend_df())
    events, sweeps = smc.detect_structure_events(high, low, close)
    assert [(e.kind, e.direction, e.index) for e in events] == [
        ("bos", "bullish", 12),
        ("bos", "bullish", 20),
        ("bos", "bullish", 28),
    ]
    assert events[0].level == pytest.approx(110.5)
    assert sweeps == []


def test_choch_after_bullish_breaks():
    _, high, low, close = arrays(choch_df())
    events, _ = smc.detect_structure_events(high, low, close)
    last = events[-1]
    assert (last.kind, last.direction, last.index) == ("choch", "bearish", 34)
    assert last.level == pytest.approx(111.5)
    assert all(e.kind == "bos" and e.direction == "bullish" for e in events[:-1])


def test_bos_component_in_uptrend_for_buy():
    res = smc.analyze_smc(uptrend_df(), BUY)
    assert res.veto is False
    assert res.last_event is not None and res.last_event.index == 28
    assert res.confluence_score >= smc.WEIGHTS["bos"]
    assert any("bullisher BOS" in r for r in res.reasons)


def test_choch_veto_for_buy_and_confluence_for_sell():
    df = choch_df()
    res_buy = smc.analyze_smc(df, BUY)
    assert res_buy.veto is True
    assert any("Veto" in r for r in res_buy.reasons)

    res_sell = smc.analyze_smc(df, SELL)
    assert res_sell.veto is False
    assert res_sell.confluence_score >= smc.WEIGHTS["bos"]
    assert any("CHoCH" in r for r in res_sell.reasons)


# ─── FVG ────────────────────────────────────────────────────────────────────

def test_bullish_fvg_zone():
    high = np.array([10.0, 11.0, 13.0])
    low = np.array([9.0, 10.0, 12.0])
    zones = smc.detect_fvgs(high, low, np.ones(3))
    assert len(zones) == 1
    z = zones[0]
    assert (z.kind, z.direction, z.index) == ("fvg", "bullish", 2)
    assert (z.bottom, z.top) == (10.0, 12.0)
    assert z.mitigated is False


def test_bearish_fvg_zone():
    high = np.array([13.0, 12.0, 10.0])
    low = np.array([12.0, 11.0, 9.0])
    zones = smc.detect_fvgs(high, low, np.ones(3))
    assert len(zones) == 1
    z = zones[0]
    assert (z.kind, z.direction, z.index) == ("fvg", "bearish", 2)
    assert (z.bottom, z.top) == (10.0, 12.0)
    assert z.mitigated is False


def test_fvg_below_min_atr_is_ignored():
    high = np.array([10.0, 11.0, 10.5])
    low = np.array([9.0, 10.0, 10.05])   # Lücke 0.05
    assert smc.detect_fvgs(high, low, np.ones(3)) == []            # 0.05 < 0.1 * 1.0
    assert len(smc.detect_fvgs(high, low, np.full(3, 0.4))) == 1   # 0.05 >= 0.1 * 0.4


def test_fvg_mitigation():
    bull = smc.detect_fvgs(np.array([10.0, 11.0, 13.0, 12.5]), np.array([9.0, 10.0, 12.0, 9.5]), np.ones(4))
    assert len(bull) == 1 and bull[0].mitigated is True     # späteres low 9.5 <= bottom 10

    bear = smc.detect_fvgs(np.array([13.0, 12.0, 10.0, 12.5]), np.array([12.0, 11.0, 9.0, 10.5]), np.ones(4))
    assert len(bear) == 1 and bear[0].mitigated is True     # späteres high 12.5 >= top 12


# ─── Orderblocks ────────────────────────────────────────────────────────────

def test_orderblock_is_last_opposite_candle_before_break():
    open_, high, low, close = arrays(uptrend_df())
    events, _ = smc.detect_structure_events(high, low, close)
    obs = smc.detect_order_blocks(open_, high, low, close, events)
    first = obs[0]
    assert (first.kind, first.direction, first.index) == ("orderblock", "bullish", 8)
    assert first.bottom == pytest.approx(103.5)
    assert first.top == pytest.approx(106.1)
    assert first.mitigated is False
    assert [z.index for z in smc.analyze_smc(uptrend_df(), BUY).order_blocks] == [8, 16, 24]


def test_bearish_orderblock_and_mitigation():
    open_, high, low, close = arrays(choch_df())
    events, _ = smc.detect_structure_events(high, low, close)
    obs = smc.detect_order_blocks(open_, high, low, close, events)
    bearish = [z for z in obs if z.direction == "bearish"]
    assert len(bearish) == 1
    assert bearish[0].index == 29
    assert (bearish[0].bottom, bearish[0].top) == (pytest.approx(119.9), pytest.approx(122.5))
    # Bullisher OB an Bar 24 (bottom 111.5) ist nach dem Close 110 an Bar 34 mitigiert.
    ob24 = [z for z in obs if z.direction == "bullish" and z.index == 24]
    assert len(ob24) == 1 and ob24[0].mitigated is True


def test_no_orderblock_without_opposite_candle():
    open_ = np.full(12, 100.0)
    close = open_ + 1.0
    high = close + 0.5
    low = open_ - 0.5
    events = [StructureEvent(kind="bos", direction="bullish", index=11, level=101.0)]
    assert smc.detect_order_blocks(open_, high, low, close, events) == []


# ─── Liquidity Sweep ────────────────────────────────────────────────────────

def test_liquidity_sweep_is_not_a_break():
    flat = (100.0, 101.0, 99.0, 100.0)
    rows = [flat] * 3 + [(100.0, 101.0, 97.0, 100.0)] + [flat] * 3 + [(100.0, 101.0, 96.0, 100.0)] + [flat]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    df["volume"] = 1000.0

    res = smc.analyze_smc(df, BUY)
    assert res.liquidity_sweeps == [LiquiditySweep(direction="bullish", index=7, level=97.0)]
    assert res.events == []
    assert res.confluence_score == pytest.approx(smc.WEIGHTS["sweep"])
    assert any("Sweep" in r for r in res.reasons)


# ─── Lookahead und Randfälle ────────────────────────────────────────────────

def test_no_lookahead():
    df = choch_df()
    _, high, low, close = arrays(df)
    full_events, full_sweeps = smc.detect_structure_events(high, low, close)
    future = bars_from_closes([500.0, 20.0, 500.0, 20.0, 500.0])

    for n in range(2 * smc.SWING_LEN + 3, len(df) + 1):
        events, sweeps = smc.detect_structure_events(high[:n], low[:n], close[:n])
        assert events == [e for e in full_events if e.index < n]
        assert sweeps == [s for s in full_sweeps if s.index < n]

        alt = pd.concat([df.iloc[:n], future], ignore_index=True)
        for direction in (BUY, SELL):
            assert smc.analyze_smc(alt.iloc[:n], direction) == smc.analyze_smc(df.iloc[:n], direction)


def test_too_few_bars_returns_empty_result():
    df = uptrend_df()
    for n in range(0, 2 * smc.SWING_LEN + 3):
        res = smc.analyze_smc(df.iloc[:n], BUY)
        assert res == smc.empty_result()
        assert res.confluence_score == 0.0 and res.veto is False


# ─── Konfluenz-Integration in analyze_symbol() ──────────────────────────────

def signal_df() -> pd.DataFrame:
    return bars_from_closes(zigzag_closes(25))   # 201 Bars Aufwärtstrend


def fake_smc(confluence: float, veto: bool = False) -> SMCResult:
    return SMCResult(
        last_event=None,
        events=[],
        fvgs=[],
        order_blocks=[],
        liquidity_sweeps=[],
        confluence_score=confluence,
        veto=veto,
        reasons=["SMC-Veto: Test"] if veto else [],
    )


def raw_score(df: pd.DataFrame, direction: SignalDirection) -> int:
    snap = compute_snapshot(df)
    score = _score_buy(snap)[0] if direction == BUY else _score_sell(snap)[0]
    return min(score, 100)


def test_full_confluence_keeps_score(monkeypatch):
    df = signal_df()
    monkeypatch.setattr(signals_mod, "analyze_smc", lambda d, direction: fake_smc(1.0))
    sig = analyze_symbol("BTC/USDT", df)
    assert sig is not None
    raw = raw_score(df, sig.direction)
    assert raw > 0
    assert sig.score == raw


def test_zero_confluence_scales_score_to_70_percent(monkeypatch):
    df = signal_df()
    monkeypatch.setattr(signals_mod, "analyze_smc", lambda d, direction: fake_smc(0.0))
    sig = analyze_symbol("BTC/USDT", df)
    assert sig is not None
    assert sig.score == round(raw_score(df, sig.direction) * 0.7)


def test_veto_sets_score_to_zero(monkeypatch):
    df = signal_df()
    monkeypatch.setattr(signals_mod, "analyze_smc", lambda d, direction: fake_smc(1.0, veto=True))
    sig = analyze_symbol("BTC/USDT", df)
    assert sig is not None
    assert sig.score == 0
    assert sig.smc is not None and sig.smc.veto is True
    assert any("Veto" in r for r in sig.reasons)


def test_signal_to_dict_serializes_smc():
    sig = analyze_symbol("BTC/USDT", signal_df())
    assert sig is not None and sig.smc is not None
    data = json.loads(json.dumps(sig.to_dict()))
    assert "confluence_score" in data["smc"]
    assert isinstance(data["smc"]["events"], list)
    assert isinstance(data["smc"]["veto"], bool)