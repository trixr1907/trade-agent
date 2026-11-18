#!/usr/bin/env python3
"""CLI-Skript für Walk-Forward-Backtest.

Verwendung:
    .venv/bin/python run_backtest.py
    .venv/bin/python run_backtest.py --symbol ETH/USDT --timeframe 4h --limit 500
    .venv/bin/python run_backtest.py --symbols BTC/USDT ETH/USDT SOL/USDT
"""
from __future__ import annotations

import argparse
import json
import sys

from tradeagent.backtest import WalkForwardConfig, run_walk_forward
from tradeagent.config import load_config
from tradeagent.data import fetch_ohlcv


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Walk-Forward-Backtest für trade-agent")
    p.add_argument(
        "--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT"],
        help="Symbole (Standard: BTC/USDT ETH/USDT)",
    )
    p.add_argument("--timeframe", default="1h", help="Timeframe (Standard: 1h)")
    p.add_argument("--limit", type=int, default=500, help="OHLCV-Bars (Standard: 500)")
    p.add_argument("--folds", type=int, default=5, help="Walk-Forward-Folds (Standard: 5)")
    p.add_argument("--is-ratio", type=float, default=0.70, help="IS-Anteil (Standard: 0.70)")
    p.add_argument("--min-score", type=int, default=50, help="Min. Signal-Score (Standard: 50)")
    p.add_argument("--risk-usd", type=float, default=None, help="Risiko pro Trade in USD")
    p.add_argument("--json", action="store_true", help="Ausgabe als JSON")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    cfg = load_config()
    wf_cfg = WalkForwardConfig(
        is_ratio=args.is_ratio,
        n_folds=args.folds,
        min_signal_score=args.min_score,
        risk_per_trade_usd=args.risk_usd,
        bars_per_year=252 * (24 if "h" in args.timeframe else 1),
    )

    all_results = []
    for symbol in args.symbols:
        print(f"\n[INFO] Lade {symbol} @ {args.timeframe} ({args.limit} Bars) …", flush=True)
        try:
            df = fetch_ohlcv(symbol, timeframe=args.timeframe, limit=args.limit, cfg=cfg.exchange)
        except Exception as e:
            print(f"[ERROR] Daten für {symbol} nicht ladbar: {e}", file=sys.stderr)
            continue

        print(f"[INFO] {len(df)} Bars geladen. Starte Walk-Forward …", flush=True)
        try:
            result = run_walk_forward(df, symbol, args.timeframe, config=wf_cfg)
        except ValueError as e:
            print(f"[ERROR] {e}", file=sys.stderr)
            continue

        result.timeframe = args.timeframe

        if args.json:
            all_results.append(result.to_dict())
        else:
            print()
            print(result.summary())
            fold_trades = [t for f in result.folds for t in f.trades]
            avg_score = (
                sum(t.score for t in fold_trades) / len(fold_trades)
                if fold_trades else 0.0
            )
            print(f"Avg-Score: {avg_score:.1f} (aus {len(fold_trades)} Trades)")
            print(f"Profit F : {result.oos_profit_factor:.4f} (4 Dezimalen)")
            print()
            print(f"{'Fold':<5} {'IS-Bars':<9} {'OOS-Bars':<10} {'Trades':<8} "
                  f"{'Win%':<8} {'Avg R':<8} {'Sharpe':<8} {'MaxDD%':<8}")
            print("─" * 68)
            for f in result.folds:
                print(
                    f"{f.fold:<5} {f.is_bars:<9} {f.oos_bars:<10} {f.n_trades:<8} "
                    f"{f.win_rate*100:<8.1f} {f.avg_r:<8.3f} {f.sharpe:<8.3f} "
                    f"{f.max_drawdown_pct:<8.1f}"
                )

    if args.json and all_results:
        print(json.dumps(all_results, indent=2, default=str))


if __name__ == "__main__":
    main()