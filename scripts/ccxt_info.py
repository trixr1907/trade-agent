"""Gibt die für den Bitget-Demo-Lauf relevanten Fähigkeiten der installierten CCXT-Version aus. Kein Netz."""
from __future__ import annotations

import ccxt


def main() -> None:
    exchange = ccxt.bitget()
    print("ccxt", ccxt.__version__)
    for key in (
        "createOcoOrder",
        "createOrderWithTakeProfitAndStopLoss",
        "createStopLossOrder",
        "createTakeProfitOrder",
        "sandbox",
    ):
        print(f"has.{key}", exchange.has.get(key))
    print("urls.keys", sorted(exchange.urls.keys()))
    print("urls.test", exchange.urls.get("test"))
    print("options.sandboxMode", exchange.options.get("sandboxMode"))
    print("hasattr enable_demo_trading", hasattr(exchange, "enable_demo_trading"))


if __name__ == "__main__":
    main()