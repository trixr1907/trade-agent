"""FastAPI Web-Dashboard für den Trading-Signal-Agent.

Endpunkte:
- GET  /               -> Dashboard
- POST /scan           -> Signale scannen & Anträge erstellen
- GET  /proposals      -> Liste der Anträge
- POST /approve        -> Freigabe (Server-Trust, fail-closed)
- POST /reject         -> Ablehnen
- POST /execute        -> Ausführung (nur nach Freigabe)
- GET  /health         -> Gesundheits-Status
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List

from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..backtest import CostModel, WalkForwardConfig, run_walk_forward
from ..config import load_config
from ..data import fetch_ohlcv
from ..graph import (
    ProposalStore,
    approve,
    execute,
    reject,
    run_pipeline,
)
from ..signals import analyze_symbol, signals_above_threshold

app = FastAPI(title="Crypto Trading Signal Agent", version="0.1.0")

cfg = load_config()
store = ProposalStore()

# Configurable scan symbols
SCAN_SYMBOLS: List[str] = ["BTC/USDT", "ETH/USDT"]


class ScanRequest(BaseModel):
    symbols: List[str] | None = None
    timeframe: str = "1h"
    limit: int = 120


class ApproveRequest(BaseModel):
    proposal_id: str
    approved_by: str = "operator"


class RejectRequest(BaseModel):
    proposal_id: str
    reason: str = ""


class ExecuteRequest(BaseModel):
    proposal_id: str


class BacktestRequest(BaseModel):
    symbols: List[str] = ["BTC/USDT", "ETH/USDT"]
    timeframe: str = "1h"
    limit: int = 500
    n_folds: int = 5
    is_ratio: float = 0.70
    min_signal_score: int = 50
    risk_usd: Optional[float] = None
    taker_fee: float = 0.001       # 0.1 % Bitget Taker
    slippage_bps: float = 3.0      # 3 Basispunkte


@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    html = Path(__file__).parent.joinpath("web").joinpath("dashboard.html")
    return HTMLResponse(html.read_text(encoding="utf-8"))


@app.post("/scan")
def scan(req: ScanRequest):
    symbols = req.symbols or SCAN_SYMBOLS
    signals = []
    for symbol in symbols:
        try:
            df = fetch_ohlcv(symbol, timeframe=req.timeframe, limit=req.limit, cfg=cfg.exchange)
            sig = analyze_symbol(symbol, df)
            if sig is not None:
                signals.append(sig)
        except Exception as exc:  # Fail-safe: ein Symbol-Fehler stoppt nicht alle
            signals.append(None)
            print(f"[scan] {symbol} Fehler: {exc}")

    valid = signals_above_threshold(
        [s for s in signals if s is not None], cfg.risk.min_signal_score
    )
    proposals = run_pipeline(valid, cfg, store)

    return {
        "signals": [s.to_dict() for s in valid],
        "proposals": [p.to_dict() for p in proposals],
        "mode": cfg.trading.mode,
        "error": "some symbols failed" if None in signals else None,
    }


@app.get("/proposals")
def list_proposals():
    return {"proposals": [p.to_dict() for p in store.list()]}


@app.post("/approve")
def approve_endpoint(req: ApproveRequest):
    proposal = approve(store, req.proposal_id, req.approved_by)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Antrag nicht gefunden")
    if proposal.state.value != "approved":
        raise HTTPException(status_code=409, detail="Antrag nicht freigabefähig")
    return {"proposal": proposal.to_dict()}


@app.post("/reject")
def reject_endpoint(req: RejectRequest):
    proposal = reject(store, req.proposal_id, req.reason)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Antrag nicht gefunden")
    return {"proposal": proposal.to_dict()}


@app.post("/execute")
def execute_endpoint(req: ExecuteRequest):
    result = execute(store, req.proposal_id, cfg)
    if result is None:
        proposal = store.get(req.proposal_id)
        if proposal is None:
            raise HTTPException(status_code=404, detail="Antrag nicht gefunden")
        raise HTTPException(status_code=409, detail="Antrag nicht freigegeben")
    return result.__dict__


@app.post("/backtest")
def backtest_endpoint(req: BacktestRequest):
    """Führt Walk-Forward-Backtest für ein oder mehrere Symbole aus."""
    wf_cfg = WalkForwardConfig(
        is_ratio=req.is_ratio,
        n_folds=req.n_folds,
        min_signal_score=req.min_signal_score,
        risk_per_trade_usd=req.risk_usd,
        bars_per_year=252 * (24 if "h" in req.timeframe else 1),
    )
    cost_model = CostModel(
        taker_fee=req.taker_fee,
        slippage_bps=req.slippage_bps,
    )
    out: dict = {}
    for symbol in req.symbols:
        try:
            df = fetch_ohlcv(symbol, timeframe=req.timeframe, limit=req.limit, cfg=cfg.exchange)
        except Exception as e:
            out[symbol] = {"error": str(e)}
            continue
        try:
            result = run_walk_forward(
                df=df,
                symbol=symbol,
                timeframe=req.timeframe,
                config=wf_cfg,
                risk_usd=req.risk_usd or 20.0,
                cost_model=cost_model,
            )
            out[symbol] = result.to_dict()
        except ValueError as e:
            out[symbol] = {"error": str(e)}
    return out


@app.get("/health")
def health():
    return {
        "status": "ok",
        "mode": cfg.trading.mode,
        "exchange": cfg.exchange.exchange,
        "llm": cfg.llm.enabled,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("tradeagent.api.app:app", host=cfg.host, port=cfg.port, reload=False)
