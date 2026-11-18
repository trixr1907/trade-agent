"""Walk-Forward-Backtest-Engine mit Deflated Sharpe Ratio (DSR) und Cost-Modell.

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
    is_ratio: float = 0.70
    n_folds: int = 5
    min_bars_per_fold: int = 60
    min_signal_score: int = 50
    risk_per_trade_usd: Optional[float] = None
    atr_stop_mult: float = 2.5
    bars_per_year: int = 252
    dsr_trials: int = 20


# ─── Cost-Modell ──────────────────────────────────────────────────────────────

@dataclass
class CostModel:
    """Transaktionskosten: Taker-Gebühr + Slippage.

    taker_fee:    Anteil der Positionsgrösse (z.B. 0.001 = 0.1 % Bitget Taker).
    slippage_bps: Slippage in Basispunkten, wird auf Entry- und Exit-Preis
                  angewendet (gegen den Trader).
    """
    taker_fee: float = 0.001      # 0.1 % Taker-Fee (Bitget Standard)
    slippage_bps: float = 3.0     # 3 Basispunkte Slippage

    @classmethod
    def zero(cls) -> "CostModel":
        """Kein Kosten-Modell (Gross-Modus)."""
        return cls(taker_fee=0.0, slippage_bps=0.0)


def _exec_price(price: float, direction: SignalDirection, is_entry: bool, slippage_bps: float) -> float:
    """Wendet Slippage auf einen Ausführungspreis an (immer gegen den Trader).

    BUY  Entry → Preis steigt  (teurer)
    BUY  Exit  → Preis fällt  (billiger)
    SELL Entry → Preis fällt  (billiger short)
    SELL Exit  → Preis steigt (teurer Cover)
    """
    factor = slippage_bps / 10_000
    if direction == SignalDirection.BUY:
        return price * (1 + factor) if is_entry else price * (1 - factor)
    else:
        return price * (1 - factor) if is_entry else price * (1 + factor)


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
    gross_pnl: float = 0.0
    cost_usd: float = 0.0
    pnl_usd: float = 0.0        # Net PnL (nach Kosten)
    pnl_r: float = 0.0          # Net Return in Risk-Einheiten (R)
    exit_reason: str = ""       # "target" | "stop" | "eod"
    score: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _simulate_trade(
    signal,
    df: pd.DataFrame,
    entry_bar_idx: int,
    risk_usd: float,
    cost_model: Optional[CostModel] = None,
) -> SimTrade:
    """Simuliert einen Trade nach Signal ab entry_bar_idx im DF."""
    if cost_model is None:
        cost_model = CostModel.zero()

    direction = signal.direction
    symbol = signal.symbol

    # Entry-Preis mit Slippage
    raw_entry = signal.price
    entry_price = _exec_price(raw_entry, direction, is_entry=True, slippage_bps=cost_model.slippage_bps)
    stop_price = signal.stop_price
    target_price = signal.target_price

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

    raw_exit_price: Optional[float] = None
    exit_reason = "eod"

    # Folgebars durchlaufen
    for i in range(entry_bar_idx + 1, len(df)):
        bar = df.iloc[i]
        high = bar["high"]
        low = bar["low"]

        if direction == SignalDirection.BUY:
            if low <= stop_price:
                raw_exit_price = stop_price
                trade.exit_bar = i
                trade.exit_reason = "stop"
                break
            if high >= target_price:
                raw_exit_price = target_price
                trade.exit_bar = i
                trade.exit_reason = "target"
                break
        else:  # SELL
            if high >= stop_price:
                raw_exit_price = stop_price
                trade.exit_bar = i
                trade.exit_reason = "stop"
                break
            if low <= target_price:
                raw_exit_price = target_price
                trade.exit_bar = i
                trade.exit_reason = "target"
                break

    # Kein Exit gefunden → End-of-Data
    if raw_exit_price is None:
        raw_exit_price = float(df["close"].iloc[-1])
        trade.exit_bar = len(df) - 1
        trade.exit_reason = "eod"

    # Exit-Preis mit Slippage
    exit_price = _exec_price(raw_exit_price, direction, is_entry=False, slippage_bps=cost_model.slippage_bps)
    trade.exit_price = exit_price

    # Gross PnL (ohne Gebühren)
    if direction == SignalDirection.BUY:
        gross = (exit_price - entry_price) * qty
    else:
        gross = (entry_price - exit_price) * qty
    trade.gross_pnl = gross

    # Taker-Gebühr: beide Seiten (Entry-Notional + Exit-Notional)
    entry_notional = entry_price * qty
    exit_notional = exit_price * qty
    cost = (entry_notional + exit_notional) * cost_model.taker_fee
    trade.cost_usd = cost

    # Net PnL und R
    trade.pnl_usd = gross - cost
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
    cost_usd: float = 0.0
    trades: List[SimTrade] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["trades"] = [t.to_dict() for t in self.trades]
        # inf ist kein gültiges JSON — None mappen; Dashboard zeigt None als "–"
        if d.get("profit_factor") is not None and math.isinf(d["profit_factor"]):
            d["profit_factor"] = None
        return d

    def pf_display(self) -> str:
        """Gibt profit_factor als lesbaren String zurück (inf → '–')."""
        return _pf_display(self.profit_factor)


def _pf_display(pf: float) -> str:
    """Formatiert profit_factor für Dashboard/Summary. inf → '–'."""
    if math.isinf(pf):
        return "–"
    return f"{pf:.2f}"


def _compute_metrics(
    trades: List[SimTrade],
    fold: int,
    is_bars: int,
    oos_bars: int,
    bars_per_year: int,
) -> FoldMetrics:
    if not trades:
        return FoldMetrics(
            fold=fold, is_bars=is_bars, oos_bars=oos_bars,
            n_trades=0, win_rate=0.0, avg_r=0.0, total_r=0.0,
            sharpe=0.0, sortino=0.0, max_drawdown_pct=0.0,
            profit_factor=0.0, cost_usd=0.0, trades=[],
        )

    pnl_r = np.array([t.pnl_r for t in trades])
    total_cost = float(sum(t.cost_usd for t in trades))
    win_rate = float(np.sum(pnl_r > 0) / len(pnl_r))
    avg_r = float(np.mean(pnl_r))
    total_r = float(np.sum(pnl_r))

    wins = pnl_r[pnl_r > 0]
    losses = pnl_r[pnl_r <= 0]
    gross_profit = float(np.sum(wins)) if len(wins) > 0 else 0.0
    gross_loss = float(abs(np.sum(losses))) if len(losses) > 0 else 0.0

    if gross_loss > 0:
        profit_factor = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = float("inf")  # Nur Gewinne, keine Verlust-Trades
    else:
        profit_factor = 0.0           # Kein Gewinn, kein Verlust

    if len(pnl_r) < 2:
        return FoldMetrics(
            fold=fold, is_bars=is_bars, oos_bars=oos_bars,
            n_trades=len(trades), win_rate=win_rate, avg_r=avg_r,
            total_r=total_r, sharpe=0.0, sortino=0.0,
            max_drawdown_pct=0.0, profit_factor=profit_factor,
            cost_usd=total_cost, trades=trades,
        )

    std = float(np.std(pnl_r, ddof=1))
    std = max(std, 1e-6)
    ann_factor = math.sqrt(max(bars_per_year / max(oos_bars, 1) * len(pnl_r), 1.0))
    ann_factor = min(ann_factor, 100.0)
    sharpe = float(np.clip(avg_r / std * ann_factor, -50.0, 50.0))

    downside = losses if len(losses) > 0 else np.array([0.0])
    downside_std = float(np.std(downside, ddof=1)) if len(downside) > 1 else max(std, 1e-6)
    downside_std = max(downside_std, 1e-6)
    sortino = float(np.clip(avg_r / downside_std * ann_factor, -50.0, 50.0))

    cum = np.cumsum(pnl_r)
    running_max = np.maximum.accumulate(cum)
    dd = running_max - cum
    max_dd_r = float(np.max(dd)) if len(dd) > 0 else 0.0
    abs_peak = float(np.max(np.abs(running_max)))
    max_dd_pct = (max_dd_r / abs_peak * 100) if abs_peak > 1e-6 else 0.0
    max_dd_pct = min(max_dd_pct, 100.0)

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
        cost_usd=total_cost,
        trades=trades,
    )


# ─── Deflated Sharpe Ratio ─────────────────────────────────────────────────────

def _deflated_sharpe_ratio(
    sharpe_obs: float,
    sharpe_values: List[float],
    n: int,
    trials: int,
) -> float:
    """DSR nach Bailey & López de Prado (2014). Gibt p-Wert (0–1) zurück."""
    if n < 4 or not sharpe_values:
        return 0.0

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
    excess_kurt = kurt

    denom = math.sqrt(
        max(1e-9, 1 - skew * sharpe_obs + (excess_kurt / 4) * sharpe_obs ** 2)
    )
    z = (sharpe_obs - sr_star) * math.sqrt(n - 1) / denom
    return _cdf_normal(z)


def _ppf_normal(p: float) -> float:
    """Inverse Normalverteilung (Beasley-Springer-Moro)."""
    if p <= 0:
        return -8.0
    if p >= 1:
        return 8.0
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
    oos_sharpe: float
    oos_sortino: float
    oos_win_rate: float
    oos_total_r: float
    oos_max_drawdown_pct: float
    oos_n_trades: int
    oos_cost_usd: float
    # Cost-Modell-Parameter (für Nachvollziehbarkeit im Report)
    cost_taker_fee: float
    cost_slippage_bps: float
    dsr: float
    dsr_significant: bool
    oos_profit_factor: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["folds"] = [f.to_dict() for f in self.folds]
        return d

    def summary(self) -> str:
        lines = [
            f"Symbol  : {self.symbol} @ {self.timeframe}",
            f"Bars    : {self.total_bars}",
            f"Folds   : {self.n_folds}",
            f"Trades  : {self.oos_n_trades}",
            f"Win-Rate: {self.oos_win_rate:.1%}",
            f"Sharpe  : {self.oos_sharpe:.3f}",
            f"Sortino : {self.oos_sortino:.3f}",
            f"MaxDD   : {self.oos_max_drawdown_pct:.1f}%",
            f"Profit F: {_pf_display(self.oos_profit_factor)}",
            f"Kosten  : ${self.oos_cost_usd:.2f}  "
            f"(Fee {self.cost_taker_fee*100:.2f}% + {self.cost_slippage_bps:.0f} bps Slippage)",
            f"DSR     : {self.dsr:.4f} "
            f"({'✅ signifikant' if self.dsr_significant else '❌ nicht signifikant'})",
        ]
        for f in self.folds:
            lines.append(
                f"  Fold {f.fold}: {f.n_trades} Trades | Win {f.win_rate:.0%} "
                f"| Sharpe {f.sharpe:.2f} | AvgR {f.avg_r:.3f} "
                f"| Cost ${f.cost_usd:.2f}"
            )
        return "\n".join(lines)


def run_walk_forward(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    config: Optional[WalkForwardConfig] = None,
    risk_usd: float = 20.0,
    cost_model: Optional[CostModel] = None,
) -> BacktestResult:
    """Führt einen Walk-Forward-Backtest auf den übergebenen OHLCV-Daten durch.

    Args:
        df:         OHLCV DataFrame (Spalten: open, high, low, close, volume)
        symbol:     Handelspaar, z.B. "BTC/USDT"
        timeframe:  Zeitrahmen, z.B. "1h"
        config:     WalkForwardConfig (optional, Default wird verwendet)
        risk_usd:   Risiko pro Trade in USD
        cost_model: CostModel (optional; Default = Bitget Taker 0.1% + 3 bps)
    """
    if config is None:
        config = WalkForwardConfig()
    if cost_model is None:
        cost_model = CostModel()

    total_bars = len(df)
    fold_size = total_bars // config.n_folds
    if fold_size < config.min_bars_per_fold:
        raise ValueError(
            f"Zu wenige Bars für {config.n_folds} Folds "
            f"(min {config.min_bars_per_fold} pro Fold, got {fold_size})."
        )

    effective_risk = config.risk_per_trade_usd if config.risk_per_trade_usd else risk_usd
    fold_metrics: List[FoldMetrics] = []
    all_oos_trades: List[SimTrade] = []

    for fold_idx in range(config.n_folds):
        start = fold_idx * fold_size
        end = start + fold_size if fold_idx < config.n_folds - 1 else total_bars
        fold_df = df.iloc[start:end].reset_index(drop=True)
        n_bars = len(fold_df)

        is_end = int(n_bars * config.is_ratio)
        is_df = fold_df.iloc[:is_end]
        oos_df = fold_df.iloc[is_end:].reset_index(drop=True)

        if len(oos_df) < 10:
            continue  # Zu wenige OOS-Bars

        # Signale im OOS-Fenster generieren
        oos_trades: List[SimTrade] = []
        for bar_idx in range(len(oos_df) - 1):
            window = oos_df.iloc[: bar_idx + 1]
            if len(window) < 20:
                continue  # Mindestfenster für Indikatoren
            try:
                sig = analyze_symbol(symbol, window)
            except Exception:
                continue

            if sig is None or sig.score < config.min_signal_score:
                continue

            trade = _simulate_trade(
                signal=sig,
                df=oos_df,
                entry_bar_idx=bar_idx,
                risk_usd=effective_risk,
                cost_model=cost_model,
            )
            oos_trades.append(trade)

        metrics = _compute_metrics(
            trades=oos_trades,
            fold=fold_idx + 1,
            is_bars=len(is_df),
            oos_bars=len(oos_df),
            bars_per_year=config.bars_per_year,
        )
        fold_metrics.append(metrics)
        all_oos_trades.extend(oos_trades)

    # ── Aggregierte OOS-Metriken ───────────────────────────────────────────────
    if not all_oos_trades:
        all_pnl_r = np.array([0.0])
    else:
        all_pnl_r = np.array([t.pnl_r for t in all_oos_trades])

    total_cost_usd = float(sum(t.cost_usd for t in all_oos_trades))

    oos_win_rate = float(np.sum(all_pnl_r > 0) / len(all_pnl_r)) if len(all_pnl_r) > 0 else 0.0
    oos_total_r = float(np.sum(all_pnl_r))

    wins_all = all_pnl_r[all_pnl_r > 0]
    losses_all = all_pnl_r[all_pnl_r <= 0]
    gp = float(np.sum(wins_all)) if len(wins_all) > 0 else 0.0
    gl = float(abs(np.sum(losses_all))) if len(losses_all) > 0 else 1e-9
    if gl > 0:
        oos_profit_factor = gp / gl
    elif gp > 0:
        oos_profit_factor = float("inf")
    else:
        oos_profit_factor = 0.0

    if len(all_pnl_r) >= 2:
        std_all = max(float(np.std(all_pnl_r, ddof=1)), 1e-6)
        avg_all = float(np.mean(all_pnl_r))
        total_oos_bars = sum(f.oos_bars for f in fold_metrics) or 1
        ann = min(math.sqrt(max(config.bars_per_year / total_oos_bars * len(all_pnl_r), 1.0)), 100.0)
        oos_sharpe = float(np.clip(avg_all / std_all * ann, -50.0, 50.0))

        down_all = losses_all if len(losses_all) > 0 else np.array([0.0])
        ds_std = float(np.std(down_all, ddof=1)) if len(down_all) > 1 else std_all
        ds_std = max(ds_std, 1e-6)
        oos_sortino = float(np.clip(avg_all / ds_std * ann, -50.0, 50.0))
    else:
        oos_sharpe = 0.0
        oos_sortino = 0.0

    cum_all = np.cumsum(all_pnl_r)
    rm_all = np.maximum.accumulate(cum_all)
    dd_all = rm_all - cum_all
    max_dd_r = float(np.max(dd_all)) if len(dd_all) > 0 else 0.0
    abs_peak_all = float(np.max(np.abs(rm_all)))
    oos_max_dd_pct = min((max_dd_r / abs_peak_all * 100) if abs_peak_all > 1e-6 else 0.0, 100.0)

    # DSR
    fold_sharpes = [f.sharpe for f in fold_metrics if f.n_trades > 0]
    dsr = _deflated_sharpe_ratio(
        sharpe_obs=oos_sharpe,
        sharpe_values=fold_sharpes,
        n=len(all_oos_trades),
        trials=config.dsr_trials,
    )

    return BacktestResult(
        id=str(uuid.uuid4()),
        symbol=symbol,
        timeframe=timeframe,
        total_bars=total_bars,
        n_folds=len(fold_metrics),
        folds=fold_metrics,
        oos_sharpe=oos_sharpe,
        oos_sortino=oos_sortino,
        oos_win_rate=oos_win_rate,
        oos_total_r=oos_total_r,
        oos_max_drawdown_pct=oos_max_dd_pct,
        oos_profit_factor=oos_profit_factor,
        oos_n_trades=len(all_oos_trades),
        oos_cost_usd=total_cost_usd,
        cost_taker_fee=cost_model.taker_fee,
        cost_slippage_bps=cost_model.slippage_bps,
        dsr=dsr,
        dsr_significant=dsr > 0.95,
    )