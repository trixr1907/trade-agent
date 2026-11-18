import httpx, json
from smoke_login import authed_client

base = "http://localhost:8000"


def main():
    with authed_client(base) as client:
        print("=== BACKTEST ENDPOINT TEST ===")
        r = client.post(
            f"{base}/backtest",
            json={
                "symbols": ["BTC/USDT"],
                "timeframe": "1h",
                "limit": 500,
                "n_folds": 3,
                "is_ratio": 0.70,
                "min_signal_score": 50,
            },
            timeout=120.0,
        )
        print("Status:", r.status_code)
        if r.status_code != 200:
            print("Fehler:", r.text)
        else:
            data = r.json()
            errors = data.get("errors", [])
            if errors:
                print("Errors:", json.dumps(errors, indent=2))
            for res in data.get("results", []):
                print(f"\nSymbol  : {res['symbol']} @ {res['timeframe']}")
                print(f"Bars    : {res['total_bars']}")
                print(f"Folds   : {res['n_folds']}")
                print(f"Trades  : {res['oos_n_trades']}")
                print(f"Win-Rate: {res['oos_win_rate']*100:.1f}%")
                print(f"Sharpe  : {res['oos_sharpe']:.3f}")
                print(f"Sortino : {res['oos_sortino']:.3f}")
                print(f"MaxDD   : {res['oos_max_drawdown_pct']:.1f}%")
                print(f"Profit F: {res['oos_profit_factor']:.2f}")
                print(f"DSR     : {res['dsr']:.4f} ({'✅ SIGNIFIKANT' if res['dsr_significant'] else '❌ nicht signifikant'})")
                print()
                for fold in res.get("folds", []):
                    print(
                        f"  Fold {fold['fold']}: {fold['n_trades']} Trades | "
                        f"Win {fold['win_rate']*100:.0f}% | "
                        f"Sharpe {fold['sharpe']:.2f} | "
                        f"AvgR {fold['avg_r']:.3f}"
                    )


if __name__ == "__main__":
    main()
