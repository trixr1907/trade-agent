"""Zentrale Konfiguration — geladen aus Env (.env).

Grundprinzip: **keine Secrets im Quellcode**. Alles läuft über Env-Variablen.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Iterable, List, Optional


_TRUE_VALUES = ("true", "1", "yes")
_FALSE_VALUES = ("false", "0", "no")


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _get_int(name: str, default: int) -> int:
    try:
        return int(_get(name, str(default)))
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


def _get_positive_float(name: str, default: float) -> float:
    """Endliche Zahl > 0, sonst Default."""
    value = _get_float(name, default)
    return value if math.isfinite(value) and value > 0 else default


def normalize_symbol(symbol: object) -> str:
    """Vergleichsform eines Symbols: Großschreibung, ohne Leerzeichen."""
    if symbol is None:
        return ""
    return str(symbol).strip().upper().replace(" ", "")


def parse_symbol_list(raw: Optional[str]) -> List[str]:
    """Kommagetrennte Symbolliste -> normalisiert, ohne Leereinträge und Duplikate."""
    result: List[str] = []
    for part in (raw or "").split(","):
        symbol = normalize_symbol(part)
        if symbol and symbol not in result:
            result.append(symbol)
    return result


DEFAULT_UNIVERSE_TTL_SECONDS = 600.0
DEFAULT_UNIVERSE_MAX_STALE_SECONDS = 3600.0


@dataclass
class ExchangeConfig:
    exchange: str = "bitget"
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    # Paper-Trading immer aktiv, unabhängig von API-Keys.
    enable_ccxt: bool = True
    # Live-Produkt: USDT-Perpetuals (swap). Der Relay setzt beides pro Symbol vor dem Entry.
    margin_mode: str = "isolated"
    leverage: int = 3
    # Bitget Demo-Trading (Header PAPTRADING: 1). Nutzt ausschließlich BITGET_DEMO_API_*-Keys.
    demo: bool = False

    @property
    def has_live_credentials(self) -> bool:
        return bool(self.api_key and self.api_secret and self.api_passphrase)


@dataclass
class TradingMode:
    # "paper" (Standard) oder "live"
    mode: str = "paper"

    @property
    def is_live(self) -> bool:
        return self.mode.lower() == "live"


@dataclass
class LiveLimits:
    max_position_notional_usd: float = 500.0
    max_open_positions: int = 3
    # Perp-Format (BTC/USDT:USDT). Der Relay mappt Engine-Symbole vor der Prüfung.
    symbol_allowlist: List[str] = field(
        default_factory=lambda: ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    )

    def symbol_allowed(self, symbol: str, universe: Optional[Iterable[str]] = None) -> bool:
        """Allowlist-Prüfung, Groß/Klein-unabhängig.

        - Explizite Allowlist ohne universe: nur Listen-Mitglieder (bisheriges Verhalten).
        - Explizite Allowlist mit universe: zusätzlich Mitglied der Universe.
        - Leere Allowlist: Symbol muss in universe stehen. Fehlt universe, ist es
          leer oder kein gültiges Iterable, wird abgelehnt (fail-closed).
        """
        s = normalize_symbol(symbol)
        if not s:
            return False
        if self.symbol_allowlist:
            if not any(s == normalize_symbol(allowed) for allowed in self.symbol_allowlist):
                return False
            if universe is None:
                return True
        elif universe is None:
            return False
        if isinstance(universe, (str, bytes)):
            return False
        try:
            members = {normalize_symbol(member) for member in universe}
        except TypeError:
            return False
        return s in members


@dataclass
class UniverseConfig:
    # Cache-Dauer der entdeckten USDT-Perp-Universe.
    ttl_seconds: float = DEFAULT_UNIVERSE_TTL_SECONDS
    # Wie lange ein alter Stand bei Entdeckungsfehlern noch gilt; danach fail-closed.
    max_stale_seconds: float = DEFAULT_UNIVERSE_MAX_STALE_SECONDS


@dataclass
class RiskConfig:
    max_drawdown_pct: float = 0.15
    kelly_fraction: float = 0.25
    atr_stop_mult: float = 2.5
    min_signal_score: int = 60


@dataclass
class LLMConfig:
    api_key: str = ""
    model: str = ""

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.model)


@dataclass
class Config:
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    trading: TradingMode = field(default_factory=TradingMode)
    live_limits: LiveLimits = field(default_factory=LiveLimits)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    host: str = "0.0.0.0"
    port: int = 8000
    # Direktive 5C: Monitor-Intervall und optionaler Alarm-Webhook (leer = aus).
    monitor_interval_seconds: float = 60.0
    alert_webhook_url: str = ""


def load_config() -> Config:
    """Lädt die Konfiguration aus der Umgebung (.env wird geladen)."""
    # python-dotenv ist optional; versteckt Fehler bei fehlender Dependency.
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:
        pass

    cfg = Config()
    cfg.exchange.exchange = _get("EXCHANGE", "bitget") or "bitget"
    # Demo und Live sind getrennt: Im Demo-Modus werden die Live-Keys nie geladen.
    raw_demo = (_get("BITGET_DEMO", "false") or "false").lower()
    cfg.exchange.demo = raw_demo in _TRUE_VALUES
    key_prefix = "BITGET_DEMO_API" if cfg.exchange.demo else "BITGET_API"
    cfg.exchange.api_key = _get(f"{key_prefix}_KEY")
    cfg.exchange.api_secret = _get(f"{key_prefix}_SECRET")
    cfg.exchange.api_passphrase = _get(f"{key_prefix}_PASSPHRASE")
    cfg.exchange.enable_ccxt = True
    cfg.exchange.margin_mode = (_get("BITGET_MARGIN_MODE", "isolated") or "isolated").lower()
    raw_leverage = _get("BITGET_LEVERAGE", "3") or "3"
    cfg.exchange.leverage = _get_int("BITGET_LEVERAGE", 3)

    cfg.trading.mode = (_get("TRADING_MODE", "paper") or "paper").lower()

    cfg.live_limits.max_position_notional_usd = _get_float("LIVE_MAX_POSITION_NOTIONAL_USD", 500.0)
    cfg.live_limits.max_open_positions = _get_int("LIVE_MAX_OPEN_POSITIONS", 3)
    # Leer / nicht gesetzt -> Modus "all" (alle entdeckten USDT-Perps, fail-closed).
    cfg.live_limits.symbol_allowlist = parse_symbol_list(_get("LIVE_SYMBOL_ALLOWLIST", ""))
    cfg.universe.ttl_seconds = _get_positive_float("UNIVERSE_TTL_SECONDS", DEFAULT_UNIVERSE_TTL_SECONDS)
    cfg.universe.max_stale_seconds = max(
        _get_positive_float("UNIVERSE_MAX_STALE_SECONDS", DEFAULT_UNIVERSE_MAX_STALE_SECONDS),
        cfg.universe.ttl_seconds,
    )

    cfg.risk.max_drawdown_pct = _get_float("RISK_MAX_DRAWDOWN_PCT", 0.15)
    cfg.risk.kelly_fraction = _get_float("RISK_KELLY_FRACTION", 0.25)
    cfg.risk.atr_stop_mult = _get_float("RISK_ATR_STOP_MULT", 2.5)
    cfg.risk.min_signal_score = _get_int("RISK_MIN_SCORE", 60)

    cfg.llm.api_key = _get("LLM_API_KEY")
    cfg.llm.model = _get("LLM_MODEL")

    cfg.host = _get("HOST", "0.0.0.0")
    cfg.port = _get_int("PORT", 8000)

    cfg.monitor_interval_seconds = _get_positive_float("MONITOR_INTERVAL_SECONDS", 60.0)
    cfg.alert_webhook_url = _get("ALERT_WEBHOOK_URL").strip()

    # Hard-Guard: Live-Modus ohne API-Keys und ohne Allowlist-Konfiguration
    # wird sofort abgelehnt — fail-closed.
    if cfg.trading.is_live:
        if raw_demo not in _TRUE_VALUES + _FALSE_VALUES:
            raise RuntimeError(
                f"BITGET_DEMO={raw_demo!r} ungültig (erlaubt: true/false). "
                "Fail-closed: unklar, ob Demo oder Live gemeint ist."
            )
        if not cfg.exchange.has_live_credentials:
            raise RuntimeError(
                f"TRADING_MODE=live erfordert {key_prefix}_KEY/SECRET/PASSPHRASE. "
                "Fail-closed: Live-Modus ohne Credentials nicht möglich."
            )
        if cfg.exchange.demo and cfg.exchange.api_key == _get("BITGET_API_KEY"):
            raise RuntimeError(
                "BITGET_DEMO_API_KEY ist identisch mit BITGET_API_KEY. "
                "Fail-closed: Demo-Lauf nur mit eigenen Demo-Keys."
            )
        if cfg.exchange.margin_mode not in ("isolated", "cross"):
            raise RuntimeError(
                f"BITGET_MARGIN_MODE={cfg.exchange.margin_mode!r} ungültig (erlaubt: isolated, cross). "
                "Fail-closed: Live-Modus ohne gültigen Margin-Modus nicht möglich."
            )
        if not raw_leverage.isdigit() or cfg.exchange.leverage < 1:
            raise RuntimeError(
                f"BITGET_LEVERAGE={raw_leverage!r} ungültig (ganze Zahl >= 1 erwartet). "
                "Fail-closed: Live-Modus ohne gültigen Hebel nicht möglich."
            )

    return cfg
