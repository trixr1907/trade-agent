"""Walk-Forward-Backtest-Engine mit Deflated Sharpe Ratio (DSR).

Verwendung:
    from tradeagent.backtest import run_walk_forward

Ablauf:
  1. OHLCV-Daten in IS (In-Sample) + OOS (Out-of-Sample) Fenster aufteilen
  2. Pro Fenster: Signale erzeugen, Trades simulieren
  3. Metriken berechnen: Sharpe, Sortino, Max-Drawdown, Win-Rate
  4. Deflated Sharpe Ratio (DSR) nach Bailey/López de Prado
  5. Ergebnis-Summary zurückgeben

Deterministisch — keine LLM-Einwirkung.
"""
from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from .indicators import compute_snapshot
from .models import SignalDirection
from .signals import analyze_symbol


# ─── Konfiguration ────────────────────────────────────────────────────────────

@dataclass
class WalkForwardConfig:
    """Parameter für die Walk-Forward-Aufteilung."""
    # Anteil IS / OOS  (z.B. 0.7 = 70 % IS, 30 % OOS)
    is_ratio: float = 0.70
    # Anzahl der Fenster (Folds)
    n_folds: int = 5
    # Mindest-Bars pro Fenster (IS + OOS)
    min_bars_per_fold: int = 60
    # Mindest-Score für Signale
    min_signal_score: int = 50
    # Risiko pro Trade (USD) — wird aus RiskConfig übernommen falls None
    risk_per_trade_usd: Optional[float] = None
    # Stop/Target-Multiplikatoren (ATR)
    atr_stop_mult: float = 2.5
    # Annualisierungsfaktor (252 Handelstage, bei 1h → 252*24)
    bars_per_year: int = 252
    # DSR: Trials (wie viele alternative Strategien wurden implizit getestet)
    dsr_trials: int = 20


# ─── Trade-Simulation ─────────────────────────────────────────────────────────

@dataclass
class SimTrade:
    """Einzelner simulierter Trade."""
    symbol: str
    direction: str
    entry_price: float
    stop_price: float
    target_price: float
    entry_bar: int
    exit_bar: Optional[int] = None
    exit_price: Optional[float] = None
    pnl_usd: float = 0.0
    pnl_r: float = 0.0        # Return in Risk-Einheiten (R)
    exit_reason: str = ""     # "target" | "stop" | "eod"
    score: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _simulate_trade(
    signal,
    df: pd.DataFrame,
    entry_bar_idx: int,
    risk_usd: float,
) -> SimTrade:
    """Simuliert einen Trade nach Signal ab entry_bar_idx im DF."""
    direction = signal.direction
    entry_price = signal.price
    stop_price = signal.stop_price
    target_price = signal.target_price
    symbol = signal.symbol

    if direction == SignalDirection.BUY:
        risk_per_unit = entry_price - stop_price
    else:
        risk_per_unit = stop_price - entry_price

    if risk_per_unit <= 0:
        risk_per_unit = entry_price * 0.01  # Fallback 1%

    qty = risk_usd / risk_per_unit

    trade = SimTrade(
        symbol=symbol,
        direction=direction.value,
        entry_price=entry_price,
        stop_price=stop_price,
        target_price=target_price,
        entry_bar=entry_bar_idx,
        score=signal.score,
    )

    # Folgebars durchlaufen
    for i in range(entry_bar_idx + 1, len(df)):
        bar = df.iloc[i]
        high = bar["high"]
        low = bar["low"]

        if direction == SignalDirection.BUY:
            if low <= stop_price:
                trade.exit_bar = i
                trade.exit_price = stop_price
                trade.pnl_usd = (stop_price - entry_price) * qty
                trade.exit_reason = "stop"
                break
            if high >= target_price:
                trade.exit_bar = i
                trade.exit_price = target_price
                trade.pnl_usd = (target_price - entry_price) * qty
                trade.exit_reason = "target"
                break
        else:  # SELL
            if high >= stop_price:
                trade.exit_bar = i
                trade.exit_price = stop_price
                trade.pnl_usd = (entry_price - stop_price) * qty
                trade.exit_reason = "stop"
                break
            if low <= target_price:
                trade.exit_bar = i
                trade.exit_price = target_price
                trade.pnl_usd = (entry_price - target_price) * qty
                trade.exit_reason = "target"
                break

    # Kein Exit gefunden → End-of-Data
    if trade.exit_bar is None:
        last_price = float(df["close"].iloc[-1])
        trade.exit_bar = len(df) - 1
        trade.exit_price = last_price
        if direction == SignalDirection.BUY:
            trade.pnl_usd = (last_price - entry_price) * qty
        else:
            trade.pnl_usd = (entry_price - last_price) * qty
        trade.exit_reason = "eod"

    trade.pnl_r = trade.pnl_usd / risk_usd if risk_usd > 0 else 0.0
    return trade


# ─── Metriken ─────────────────────────────────────────────────────────────────

@dataclass
class FoldMetrics:
    fold: int
    is_bars: int
    oos_bars: int
    n_trades: int
    win_rate: float
    avg_r: float
    total_r: float
    sharpe: float
    sortino: float
    max_drawdown_pct: float
    profit_factor: float
    trades: List[SimTrade] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["trades"] = [t.to_dict() for t in self.trades]
        return d


def _compute_metrics(trades: List[SimTrade], fold: int, is_bars: int, oos_bars: int, bars_per_year: int) -> FoldMetrics:
    if not trades:
        return FoldMetrics(
            fold=fold, is_bars=is_bars, oos_bars=oos_bars,
            n_trades=0, win_rate=0.0, avg_r=0.0, total_r=0.0,
            sharpe=0.0, sortino=0.0, max_drawdown_pct=0.0, profit_factor=0.0,
            trades=[],
        )

    pnl_r = np.array([t.pnl_r for t in trades])
    win_rate = float(np.sum(pnl_r > 0) / len(pnl_r)) if len(pnl_r) > 0 else 0.0
    avg_r = float(np.mean(pnl_r))
    total_r = float(np.sum(pnl_r))

    # Profit Factor (wird vor dem Early-Return benötigt)
    wins = pnl_r[pnl_r > 0]
    losses = pnl_r[pnl_r <= 0]
    gross_profit = float(np.sum(wins)) if len(wins) > 0 else 0.0
    gross_loss = float(abs(np.sum(losses))) if len(losses) > 0 else 1e-9
    profit_factor = gross_profit / gross_loss

    # Sharpe (annualisiert auf bars_per_year-Basis)
    # Mindestens 2 Trades nötig für sinnvollen Sharpe
    if len(pnl_r) < 2:
        return FoldMetrics(
            fold=fold, is_bars=is_bars, oos_bars=oos_bars,
            n_trades=len(trades), win_rate=win_rate, avg_r=avg_r,
            total_r=total_r, sharpe=0.0, sortino=0.0,
            max_drawdown_pct=0.0, profit_factor=profit_factor,
            trades=trades,
        )
    std = float(np.std(pnl_r, ddof=1))
    std = max(std, 1e-6)  # Clamp gegen Division durch 0
    ann_factor = math.sqrt(max(bars_per_year / max(oos_bars, 1) * len(pnl_r), 1.0))
    ann_factor = min(ann_factor, 100.0)  # Cap gegen astronomische Werte
    sharpe = float(np.clip(avg_r / std * ann_factor, -50.0, 50.0))

    # Sortino (nur downside deviation)
    downside = losses if len(losses) > 0 else np.array([0.0])
    downside_std = float(np.std(downside, ddof=1)) if len(downside) > 1 else max(std, 1e-6)
    downside_std = max(downside_std, 1e-6)
    sortino = float(np.clip(avg_r / downside_std * ann_factor, -50.0, 50.0))

    # Max Drawdown (relativ zum kumulierten Peak, absolut in R)
    cum = np.cumsum(pnl_r)
    running_max = np.maximum.accumulate(cum)
    dd = running_max - cum
    max_dd_r = float(np.max(dd)) if len(dd) > 0 else 0.0
    # Normierung: relativ zum Maximum des absoluten kumulierten Returns
    # Wenn kein einziger Bar profitabel war, bleibt DD = 0 (kein Peak)
    abs_peak = float(np.max(np.abs(running_max)))
    max_dd_pct = (max_dd_r / abs_peak * 100) if abs_peak > 1e-6 else 0.0
    max_dd_pct = min(max_dd_pct, 100.0)  # Cap bei 100% (kein Over-100%-DD möglich)

    return FoldMetrics(
        fold=fold,
        is_bars=is_bars,
        oos_bars=oos_bars,
        n_trades=len(trades),
        win_rate=win_rate,
        avg_r=avg_r,
        total_r=total_r,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown_pct=max_dd_pct,
        profit_factor=profit_factor,
        trades=trades,
    )


# ─── Deflated Sharpe Ratio ─────────────────────────────────────────────────────

def _deflated_sharpe_ratio(sharpe_obs: float, sharpe_values: List[float], n: int, trials: int) -> float:
    """
    DSR nach Bailey & López de Prado (2014).
    Korrigiert den beobachteten Sharpe für Multiple-Testing-Bias.

    DSR = Φ( (SR_obs - SR*) * sqrt(n-1) / sqrt(1 - γ3*SR_obs + (γ4-1)/4 * SR_obs²) )

    SR* = Erwarteter Maximum-Sharpe unter H0 (Gauß-Annäherung).
    γ3 / γ4 = Skewness / Kurtosis der R-Verteilung.

    Gibt den p-Wert (0–1) zurück: > 0.95 = signifikant.
    """
    if n < 4 or not sharpe_values:
        return 0.0

    # Erwarteter Max-Sharpe unter H0 (Euler-Mascheroni-Annäherung)
    euler_gamma = 0.5772156649
    if trials > 1:
        sr_star = (
            (1 - euler_gamma) * _ppf_normal(1 - 1 / trials)
            + euler_gamma * _ppf_normal(1 - 1 / (trials * math.e))
        )
    else:
        sr_star = 0.0

    r_arr = np.array(sharpe_values)
    skew = float(pd.Series(r_arr).skew()) if len(r_arr) > 2 else 0.0
    kurt = float(pd.Series(r_arr).kurt()) if len(r_arr) > 3 else 3.0
    excess_kurt = kurt  # pandas.kurt() gibt Excess-Kurtosis zurück

    denom = math.sqrt(
        max(1e-9, 1 - skew * sharpe_obs + (excess_kurt / 4) * sharpe_obs ** 2)
    )
    z = (sharpe_obs - sr_star) * math.sqrt(n - 1) / denom
    return _cdf_normal(z)


def _ppf_normal(p: float) -> float:
    """Inverse Normalverteilung (Rational Approximation, Abramowitz & Stegun)."""
    if p <= 0:
        return -8.0
    if p >= 1:
        return 8.0
    # Beasley-Springer-Moro Annäherung
    a = [0, -3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [0, -5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01, -1.328068155288572e+01]
    c = [0, -7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [0, 7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00]
    p_low = 0.02425
    p_high = 1 - p_low
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[1]*q+c[2])*q+c[3])*q+c[4])*q+c[5])*q+c[6]) / \
               ((((d[1]*q+d[2])*q+d[3])*q+d[4])*q+1)
    elif p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[1]*r+a[2])*r+a[3])*r+a[4])*r+a[5])*r+a[6])*q / \
               (((((b[1]*r+b[2])*r+b[3])*r+b[4])*r+b[5])*r+1)
    else:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[1]*q+c[2])*q+c[3])*q+c[4])*q+c[5])*q+c[6]) / \
                ((((d[1]*q+d[2])*q+d[3])*q+d[4])*q+1)


def _cdf_normal(x: float) -> float:
    """Kumulative Standardnormalverteilung."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ─── Walk-Forward-Engine ───────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    id: str
    symbol: str
    timeframe: str
    total_bars: int
    n_folds: int
    folds: List[FoldMetrics]
    # Aggregierte OOS-Metriken
    oos_sharpe: float
    oos_sortino: float
    oos_win_rate: float
    oos_total_r: float
    oos_max_drawdown_pct: float
    oos_profit_factor: float
    oos_n_trades: int
    # DSR
    dsr: float                    # 0–1 (p-Wert)
    dsr_significant: bool         # True wenn DSR > 0.95
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["folds"] = [f.to_dict() for f in self.folds]
        return d

    def summary(self) -> str:
        lines = [
            f"=== Backtest: {self.symbol} @ {self.timeframe} ===",
            f"Bars gesamt : {self.total_bars}",
            f"Folds       : {self.n_folds}",
            f"OOS Trades  : {self.oos_n_trades}",
            f"Win-Rate    : {self.oos_win_rate*100:.1f}%",
            f"Avg R       : {self.oos_total_r/max(self.oos_n_trades,1):.3f}R",
            f"Sharpe (OOS): {self.oos_sharpe:.3f}",
            f"Sortino     : {self.oos_sortino:.3f}",
            f"Max DD      : {self.oos_max_drawdown_pct:.1f}%",
            f"Profit F.   : {self.oos_profit_factor:.2f}",
            f"DSR         : {self.dsr:.4f} ({'✅ SIGNIFIKANT' if self.dsr_significant else '❌ nicht signifikant'})",
        ]
        return "\n".join(lines)


def run_walk_forward(
    symbol: str,
    df: pd.DataFrame,
    cfg: WalkForwardConfig,
) -> BacktestResult:
    """
    Führt einen Walk-Forward-Backtest auf dem gegebenen OHLCV-DataFrame aus.

    Args:
        symbol: Handelssymbol (z.B. "BTC/USDT")
        df: OHLCV-DataFrame (Spalten: open/high/low/close/volume)
        cfg: WalkForwardConfig-Parameter

    Returns:
        BacktestResult mit allen Fold-Metriken + DSR
    """
    total_bars = len(df)
    risk_usd = cfg.risk_per_trade_usd or 20.0

    bars_per_fold = total_bars // cfg.n_folds
    if bars_per_fold < cfg.min_bars_per_fold:
        raise ValueError(
            f"Zu wenig Bars ({total_bars}) für {cfg.n_folds} Folds "
            f"(min {cfg.min_bars_per_fold} pro Fold)."
        )

    folds: List[FoldMetrics] = []
    all_oos_trades: List[SimTrade] = []
    all_oos_sharpe_values: List[float] = []

    for fold_idx in range(cfg.n_folds):
        start = fold_idx * bars_per_fold
        end = start + bars_per_fold if fold_idx < cfg.n_folds - 1 else total_bars

        is_end = start + int((end - start) * cfg.is_ratio)
        is_df = df.iloc[start:is_end].copy()
        oos_df = df.iloc[is_end:end].copy()

        if len(oos_df) < 10:
            continue

        # Signale auf dem OOS-Fenster erzeugen
        # (IS wird nur zur Warm-up-Bestimmung genutzt — Indikatoren brauchen
        #  genügend History, daher geben wir IS+OOS als Context und simulieren
        #  nur auf OOS-Bars.)
        context_df = df.iloc[start:end].copy()
        is_bars_count = len(is_df)
        oos_trades: List[SimTrade] = []

        # Jeden OOS-Bar als möglichen Einstiegspunkt prüfen
        active_trade: Optional[SimTrade] = None
        last_signal_bar = -99  # Cooldown: kein Signal sofort nach Signal

        for oos_bar_i, bar_pos in enumerate(range(is_bars_count, len(context_df))):
            # Cooldown: mindestens 3 Bars zwischen Signalen
            if active_trade is not None:
                if active_trade.exit_bar is None or bar_pos <= active_trade.exit_bar:
                    continue

            if bar_pos - last_signal_bar < 3:
                continue

            # Snapshot aus den letzten [bar_pos] Bars berechnen
            window = context_df.iloc[max(0, bar_pos - 50):bar_pos + 1]
            if len(window) < 35:
                continue

            sig = analyze_symbol(symbol, window)
            if sig is None or sig.score < cfg.min_signal_score:
                continue

            last_signal_bar = bar_pos
            trade = _simulate_trade(sig, context_df, bar_pos, risk_usd)
            if trade.exit_bar is not None and trade.exit_bar > bar_pos:
                oos_trades.append(trade)
                active_trade = trade

        metrics = _compute_metrics(
            oos_trades, fold_idx + 1, len(is_df), len(oos_df), cfg.bars_per_year
        )
        folds.append(metrics)
        all_oos_trades.extend(oos_trades)
        if metrics.n_trades > 0:
            all_oos_sharpe_values.append(metrics.sharpe)

    # Aggregierte OOS-Metriken
    all_pnl_r = [t.pnl_r for t in all_oos_trades]
    agg_metrics = _compute_metrics(
        all_oos_trades, 0, 0,
        total_bars - int(total_bars * cfg.is_ratio),
        cfg.bars_per_year,
    )

    # DSR
    dsr = _deflated_sharpe_ratio(
        agg_metrics.sharpe,
        all_oos_sharpe_values,
        len(all_oos_trades),
        cfg.dsr_trials,
    )

    return BacktestResult(
        id=str(uuid.uuid4())[:8],
        symbol=symbol,
        timeframe="unknown",   # wird vom Aufrufer gesetzt
        total_bars=total_bars,
        n_folds=len(folds),
        folds=folds,
        oos_sharpe=agg_metrics.sharpe,
        oos_sortino=agg_metrics.sortino,
        oos_win_rate=agg_metrics.win_rate,
        oos_total_r=agg_metrics.total_r,
        oos_max_drawdown_pct=agg_metrics.max_drawdown_pct,
        oos_profit_factor=agg_metrics.profit_factor,
        oos_n_trades=agg_metrics.n_trades,
        dsr=dsr,
        dsr_significant=dsr > 0.95,
    )