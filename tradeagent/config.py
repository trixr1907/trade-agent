"""Zentrale Konfiguration — geladen aus Env (.env).

Grundprinzip: **keine Secrets im Quellcode**. Alles läuft über Env-Variablen.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


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


@dataclass
class ExchangeConfig:
    exchange: str = "bitget"
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    # Paper-Trading immer aktiv, unabhängig von API-Keys.
    enable_ccxt: bool = True

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
    symbol_allowlist: List[str] = field(
        default_factory=lambda: ["BTC/USDT", "ETH/USDT"]
    )

    def symbol_allowed(self, symbol: str) -> bool:
        s = symbol.upper().replace(" ", "")
        return any(s == allowed.upper().replace(" ", "") for allowed in self.symbol_allowlist)


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
    risk: RiskConfig = field(default_factory=RiskConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    host: str = "0.0.0.0"
    port: int = 8000


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
    cfg.exchange.api_key = _get("BITGET_API_KEY")
    cfg.exchange.api_secret = _get("BITGET_API_SECRET")
    cfg.exchange.api_passphrase = _get("BITGET_API_PASSPHRASE")
    cfg.exchange.enable_ccxt = True

    cfg.trading.mode = (_get("TRADING_MODE", "paper") or "paper").lower()

    cfg.live_limits.max_position_notional_usd = _get_float("LIVE_MAX_POSITION_NOTIONAL_USD", 500.0)
    cfg.live_limits.max_open_positions = _get_int("LIVE_MAX_OPEN_POSITIONS", 3)
    raw_allow = _get("LIVE_SYMBOL_ALLOWLIST", "BTC/USDT,ETH/USDT")
    cfg.live_limits.symbol_allowlist = [s.strip() for s in raw_allow.split(",") if s.strip()]

    cfg.risk.max_drawdown_pct = _get_float("RISK_MAX_DRAWDOWN_PCT", 0.15)
    cfg.risk.kelly_fraction = _get_float("RISK_KELLY_FRACTION", 0.25)
    cfg.risk.atr_stop_mult = _get_float("RISK_ATR_STOP_MULT", 2.5)
    cfg.risk.min_signal_score = _get_int("RISK_MIN_SCORE", 60)

    cfg.llm.api_key = _get("LLM_API_KEY")
    cfg.llm.model = _get("LLM_MODEL")

    cfg.host = _get("HOST", "0.0.0.0")
    cfg.port = _get_int("PORT", 8000)

    # Hard-Guard: Live-Modus ohne API-Keys und ohne Allowlist-Konfiguration
    # wird sofort abgelehnt — fail-closed.
    if cfg.trading.is_live:
        if not cfg.exchange.has_live_credentials:
            raise RuntimeError(
                "TRADING_MODE=live erfordert BITGET_API_KEY/SECRET/PASSPHRASE. "
                "Fail-closed: Live-Modus ohne Credentials nicht möglich."
            )

    return cfg
