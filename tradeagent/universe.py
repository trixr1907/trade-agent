"""Dynamische USDT-Perp-Universe (Bitget) und zentrale Live-Allowlist-Prüfung.

Direktive 5A. Die Symbolmenge kommt aus CCXT fetch_markets(), gefiltert auf
USDT-Perpetuals: type == "swap", quote == "USDT", settle == "USDT", linear,
nicht inaktiv. Es gibt keine hartkodierte Liste; ein Cache mit TTL
(UNIVERSE_TTL_SECONDS) hält die Menge bei Bitget-Änderungen aktuell.

Allowlist (LIVE_SYMBOL_ALLOWLIST, Perp-Format BTC/USDT:USDT, kommagetrennt):
- leer / nicht gesetzt -> Modus "all": alle entdeckten USDT-Perps erlaubt
- explizite Liste      -> Modus "explicit": nur diese Symbole erlaubt

Fail-closed: Schlägt die Entdeckung fehl und ist der letzte erfolgreiche Stand
älter als UNIVERSE_MAX_STALE_SECONDS (oder gibt es keinen), ist die Universe
leer. Im Modus "all" wird dann jedes Symbol abgelehnt.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

from .config import (
    DEFAULT_UNIVERSE_MAX_STALE_SECONDS,
    DEFAULT_UNIVERSE_TTL_SECONDS,
    Config,
    ExchangeConfig,
    normalize_symbol,
)
from .data import create_exchange

logger = logging.getLogger(__name__)

PERP_QUOTE = "USDT"
PERP_SUFFIX = ":" + PERP_QUOTE
DISCOVERY_TIMEOUT_MS = 10000

MODE_ALL = "all"
MODE_EXPLICIT = "explicit"

MarketsFetcher = Callable[[], Iterable[Dict[str, Any]]]


# ── Reine Hilfsfunktionen ─────────────────────────────────────────────────────

def is_usdt_perp(market: Any) -> bool:
    """True für einen aktiven, linearen USDT-Perpetual im CCXT-Market-Format."""
    if not isinstance(market, dict):
        return False
    if market.get("type") != "swap":
        return False
    if normalize_symbol(market.get("quote")) != PERP_QUOTE:
        return False
    settle = market.get("settle")
    if settle is not None and normalize_symbol(settle) != PERP_QUOTE:
        return False
    if market.get("linear") is False:
        return False
    if market.get("active") is False:
        return False
    symbol = market.get("symbol")
    if not isinstance(symbol, str):
        return False
    return normalize_symbol(symbol).endswith(PERP_SUFFIX)


def filter_usdt_perps(markets: Optional[Iterable[Any]]) -> List[str]:
    """Sortierte, eindeutige Perp-Symbole (BTC/USDT:USDT) aus fetch_markets()."""
    if not markets:
        return []
    found = set()
    for market in markets:
        if is_usdt_perp(market):
            found.add(normalize_symbol(market["symbol"]))
    return sorted(found)


def to_engine_symbol(perp_symbol: str) -> str:
    """BTC/USDT:USDT -> BTC/USDT (Engine-Format). Gegenstück zu relay.to_perp_symbol."""
    return normalize_symbol(perp_symbol).split(":", 1)[0]


def to_perp_form(symbol: object) -> str:
    """BTC/USDT -> BTC/USDT:USDT; Perp-Symbole bleiben unverändert (normalisiert)."""
    s = normalize_symbol(symbol)
    if not s or ":" in s:
        return s
    if s.endswith("/" + PERP_QUOTE):
        return s + PERP_SUFFIX
    return s


# ── Universe mit Cache ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UniverseSnapshot:
    symbols: List[str]
    fetched_at: float  # Epoch-Sekunden des letzten erfolgreichen Abrufs, 0.0 = nie
    error: Optional[str] = None
    stale: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbols": list(self.symbols),
            "count": len(self.symbols),
            "fetched_at": self.fetched_at or None,
            "stale": self.stale,
            "error": self.error,
        }


class SymbolUniverse:
    """Thread-sicherer TTL-Cache der entdeckten USDT-Perps."""

    def __init__(
        self,
        fetch_markets: MarketsFetcher,
        ttl_seconds: float = DEFAULT_UNIVERSE_TTL_SECONDS,
        max_stale_seconds: float = DEFAULT_UNIVERSE_MAX_STALE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._fetch = fetch_markets
        self._ttl = ttl_seconds
        self._max_stale = max(max_stale_seconds, ttl_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._symbols: List[str] = []
        self._fetched_at = 0.0

    def snapshot(self, force_refresh: bool = False) -> UniverseSnapshot:
        with self._lock:
            now = self._clock()
            age = (now - self._fetched_at) if self._fetched_at else None
            if not force_refresh and age is not None and age < self._ttl:
                return UniverseSnapshot(list(self._symbols), self._fetched_at)

            try:
                symbols = filter_usdt_perps(self._fetch())
                if not symbols:
                    raise RuntimeError("fetch_markets lieferte keine USDT-Perpetuals")
            except Exception as exc:  # Netz, Exchange-Fehler, leere Antwort
                error = type(exc).__name__ + ": " + str(exc)
                logger.warning("[universe] Entdeckung fehlgeschlagen: %s", error)
                if age is not None and age < self._max_stale:
                    return UniverseSnapshot(list(self._symbols), self._fetched_at, error, stale=True)
                # Fail-closed: kein verlässlicher Stand -> leere Universe.
                return UniverseSnapshot([], self._fetched_at, error, stale=True)

            self._symbols = symbols
            self._fetched_at = now
            return UniverseSnapshot(list(symbols), now)

    def symbols(self, force_refresh: bool = False) -> List[str]:
        return self.snapshot(force_refresh).symbols


def _exchange_fetcher(cfg_exchange: ExchangeConfig) -> MarketsFetcher:
    def fetch() -> Iterable[Dict[str, Any]]:
        exchange = create_exchange(cfg_exchange)
        exchange.timeout = DISCOVERY_TIMEOUT_MS
        return exchange.fetch_markets()

    return fetch


_default_universe: Optional[SymbolUniverse] = None
_default_lock = threading.Lock()


def get_universe(cfg: Config) -> SymbolUniverse:
    """Prozessweite Universe-Instanz (ein Cache für API, Relay und Scan)."""
    global _default_universe
    with _default_lock:
        if _default_universe is None:
            _default_universe = SymbolUniverse(
                _exchange_fetcher(cfg.exchange),
                ttl_seconds=cfg.universe.ttl_seconds,
                max_stale_seconds=cfg.universe.max_stale_seconds,
            )
        return _default_universe


def set_universe_for_testing(universe: Optional[SymbolUniverse]) -> None:
    global _default_universe
    with _default_lock:
        _default_universe = universe


# ── Allowlist ─────────────────────────────────────────────────────────────────

def allowlist_mode(cfg: Config) -> str:
    """'explicit' bei gesetzter LIVE_SYMBOL_ALLOWLIST, sonst 'all'."""
    return MODE_EXPLICIT if cfg.live_limits.symbol_allowlist else MODE_ALL


def live_symbol_allowed(
    cfg: Config,
    symbol: str,
    universe: Optional[SymbolUniverse] = None,
) -> bool:
    """Zentrale Allowlist-Prüfung für den Live-Pfad.

    Explizite Liste: nur diese Symbole (Vergleich wie konfiguriert), ohne Netzabfrage.
    Leere Liste: Symbol (Engine- oder Perp-Format) muss als USDT-Perp in der
    entdeckten Universe stehen. Jeder Fehler bei der Entdeckung führt zur
    Ablehnung (fail-closed).
    """
    limits = cfg.live_limits
    if limits.symbol_allowlist:
        return limits.symbol_allowed(symbol)
    try:
        source = universe if universe is not None else get_universe(cfg)
        symbols = source.symbols()
    except Exception as exc:
        logger.warning("[universe] Allowlist-Prüfung fail-closed: %s", exc)
        return False
    return limits.symbol_allowed(to_perp_form(symbol), universe=symbols)