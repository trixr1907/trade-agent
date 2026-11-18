"""Tests für Direktive 5A: Universe, Allowlist-Modi, Batch-Scan, Config, Endpunkte."""
from __future__ import annotations

import threading
import time

import pandas as pd
import pytest

from test_auth_control import _bearer, _login, api, state_store  # noqa: F401
from tradeagent.config import Config, LiveLimits, load_config, parse_symbol_list
from tradeagent.scan_jobs import (
    STATUS_DONE,
    STATUS_FAILED,
    ScanJobBusyError,
    ScanJobManager,
    ScanProgress,
    clamp_batch_size,
    make_batches,
    run_batched_scan,
)
from tradeagent.state import StateStore
from tradeagent.universe import (
    SymbolUniverse,
    allowlist_mode,
    filter_usdt_perps,
    live_symbol_allowed,
    set_universe_for_testing,
    to_engine_symbol,
    to_perp_form,
)


# ── Hilfen ────────────────────────────────────────────────────────────────────

def _market(symbol, type_="swap", quote="USDT", settle="USDT", linear=True, active=True):
    return {
        "symbol": symbol,
        "type": type_,
        "quote": quote,
        "settle": settle,
        "linear": linear,
        "active": active,
    }


MARKETS = [
    _market("BTC/USDT:USDT"),
    _market("eth/usdt:usdt"),
    _market("BTC/USDT", type_="spot", settle=None),
    _market("BTC/USD:BTC", quote="USD", settle="BTC", linear=False),
    _market("XRP/USDC:USDC", quote="USDC", settle="USDC"),
    _market("OLD/USDT:USDT", active=False),
    _market("BTC/USDT:USDT"),
    "kein-dict",
]


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Fetcher:
    def __init__(self, markets=None, error=None) -> None:
        self.markets = markets
        self.error = error
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.markets


def _cfg(allowlist) -> Config:
    cfg = Config()
    cfg.live_limits.symbol_allowlist = list(allowlist)
    return cfg


def _wait_idle(manager: ScanJobManager, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while manager.active() is not None:
        if time.time() > deadline:
            raise AssertionError("Scan-Job wurde nicht fertig")
        time.sleep(0.01)


# ── Entdeckung / Filter ───────────────────────────────────────────────────────

def test_filter_keeps_only_active_linear_usdt_perps():
    assert filter_usdt_perps(MARKETS) == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    assert filter_usdt_perps(None) == []
    assert filter_usdt_perps([]) == []


def test_symbol_format_conversion():
    assert to_engine_symbol("btc/usdt:usdt") == "BTC/USDT"
    assert to_perp_form("btc/usdt") == "BTC/USDT:USDT"
    assert to_perp_form("ETH/USDT:USDT") == "ETH/USDT:USDT"
    assert to_perp_form("BTC/EUR") == "BTC/EUR"
    assert to_perp_form(None) == ""


def test_universe_caches_within_ttl():
    clock, fetcher = FakeClock(), Fetcher(MARKETS)
    universe = SymbolUniverse(fetcher, ttl_seconds=600, max_stale_seconds=3600, clock=clock)
    assert universe.symbols() == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    universe.symbols()
    assert fetcher.calls == 1
    clock.now += 601
    universe.symbols()
    assert fetcher.calls == 2


def test_universe_serves_stale_then_fails_closed():
    clock, fetcher = FakeClock(), Fetcher(MARKETS)
    universe = SymbolUniverse(fetcher, ttl_seconds=600, max_stale_seconds=3600, clock=clock)
    universe.symbols()

    clock.now += 700
    fetcher.error = ConnectionError("down")
    snap = universe.snapshot()
    assert snap.stale is True
    assert snap.symbols == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    assert "ConnectionError" in (snap.error or "")

    clock.now += 3600
    snap = universe.snapshot()
    assert snap.stale is True
    assert snap.symbols == []


def test_universe_without_any_success_is_empty():
    universe = SymbolUniverse(Fetcher(error=RuntimeError("boom")), clock=FakeClock())
    snap = universe.snapshot()
    assert snap.symbols == [] and snap.stale is True
    assert snap.to_dict()["fetched_at"] is None


def test_universe_empty_markets_counts_as_failure():
    snap = SymbolUniverse(Fetcher([]), clock=FakeClock()).snapshot()
    assert snap.symbols == []
    assert snap.error


# ── Allowlist-Modi ────────────────────────────────────────────────────────────

def test_explicit_allowlist_needs_no_discovery():
    cfg = _cfg(["BTC/USDT:USDT"])
    fetcher = Fetcher(MARKETS)
    universe = SymbolUniverse(fetcher, clock=FakeClock())
    assert allowlist_mode(cfg) == "explicit"
    assert live_symbol_allowed(cfg, "btc/usdt:usdt", universe=universe) is True
    assert live_symbol_allowed(cfg, "ETH/USDT:USDT", universe=universe) is False
    assert fetcher.calls == 0


def test_all_mode_uses_discovered_universe():
    cfg = _cfg([])
    universe = SymbolUniverse(Fetcher(MARKETS), clock=FakeClock())
    assert allowlist_mode(cfg) == "all"
    assert live_symbol_allowed(cfg, "ETH/USDT:USDT", universe=universe) is True
    assert live_symbol_allowed(cfg, "eth/usdt", universe=universe) is True
    assert live_symbol_allowed(cfg, "SOL/USDT:USDT", universe=universe) is False


def test_all_mode_fails_closed_on_discovery_error():
    cfg = _cfg([])
    universe = SymbolUniverse(Fetcher(error=ConnectionError("down")), clock=FakeClock())
    assert live_symbol_allowed(cfg, "BTC/USDT:USDT", universe=universe) is False

    class Broken:
        def symbols(self):
            raise RuntimeError("kaputt")

    assert live_symbol_allowed(cfg, "BTC/USDT:USDT", universe=Broken()) is False


def test_live_limits_symbol_allowed_signature():
    explicit = LiveLimits(symbol_allowlist=["BTC/USDT:USDT"])
    assert explicit.symbol_allowed(" btc/usdt:usdt ") is True
    assert explicit.symbol_allowed("BTC/USDT:USDT", universe=["ETH/USDT:USDT"]) is False

    empty = LiveLimits(symbol_allowlist=[])
    assert empty.symbol_allowed("BTC/USDT:USDT") is False
    assert empty.symbol_allowed("BTC/USDT:USDT", universe=[]) is False
    assert empty.symbol_allowed("BTC/USDT:USDT", universe="BTC/USDT:USDT") is False
    assert empty.symbol_allowed("BTC/USDT:USDT", universe=5) is False
    assert empty.symbol_allowed("BTC/USDT:USDT", universe=["btc/usdt:usdt"]) is True
    assert empty.symbol_allowed("", universe=["BTC/USDT:USDT"]) is False


# ── Config-Parsing ────────────────────────────────────────────────────────────

@pytest.fixture()
def paper_env(monkeypatch):
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("LIVE_SYMBOL_ALLOWLIST", "")
    monkeypatch.setenv("UNIVERSE_TTL_SECONDS", "600")
    monkeypatch.setenv("UNIVERSE_MAX_STALE_SECONDS", "3600")
    return monkeypatch


def test_parse_symbol_list():
    assert parse_symbol_list(" btc/usdt:usdt, ,ETH/USDT:USDT,BTC/USDT:USDT ") == [
        "BTC/USDT:USDT",
        "ETH/USDT:USDT",
    ]
    assert parse_symbol_list("") == []
    assert parse_symbol_list(None) == []


def test_load_config_allowlist_modes(paper_env):
    cfg = load_config()
    assert cfg.live_limits.symbol_allowlist == []
    assert allowlist_mode(cfg) == "all"

    paper_env.setenv("LIVE_SYMBOL_ALLOWLIST", " btc/usdt:usdt, eth/usdt:usdt ")
    cfg = load_config()
    assert cfg.live_limits.symbol_allowlist == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    assert allowlist_mode(cfg) == "explicit"


def test_load_config_universe_env(paper_env):
    paper_env.setenv("UNIVERSE_TTL_SECONDS", "120")
    paper_env.setenv("UNIVERSE_MAX_STALE_SECONDS", "900")
    cfg = load_config()
    assert cfg.universe.ttl_seconds == 120.0
    assert cfg.universe.max_stale_seconds == 900.0

    paper_env.setenv("UNIVERSE_TTL_SECONDS", "abc")
    paper_env.setenv("UNIVERSE_MAX_STALE_SECONDS", "-1")
    cfg = load_config()
    assert cfg.universe.ttl_seconds == 600.0
    assert cfg.universe.max_stale_seconds == 3600.0

    paper_env.setenv("UNIVERSE_TTL_SECONDS", "1000")
    paper_env.setenv("UNIVERSE_MAX_STALE_SECONDS", "10")
    assert load_config().universe.max_stale_seconds == 1000.0

    paper_env.setenv("UNIVERSE_TTL_SECONDS", "nan")
    assert load_config().universe.ttl_seconds == 600.0


# ── Batch-Scan ────────────────────────────────────────────────────────────────

def test_clamp_and_batches():
    assert clamp_batch_size(None) == 20
    assert clamp_batch_size("x") == 20
    assert clamp_batch_size(0) == 1
    assert clamp_batch_size(1000) == 100
    symbols = [f"S{i}" for i in range(45)]
    assert [len(b) for b in make_batches(symbols, 20)] == [20, 20, 5]
    assert make_batches([], 20) == []


def _scan_one(symbol):
    if symbol == "C":
        raise RuntimeError("timeout")
    return f"sig-{symbol}" if symbol in ("A", "D") else None


def test_run_batched_scan_progress_and_fault_tolerance():
    progress = ScanProgress(job_id="j", total=5, batch_size=2, batch_count=3)
    signals, failed = run_batched_scan(["A", "B", "C", "D", "E"], _scan_one, progress)
    assert signals == ["sig-A", "sig-D"]
    assert failed == ["C"]
    data = progress.to_dict()
    assert data["processed"] == 5
    assert data["failed"] == 1
    assert data["signals_found"] == 2
    assert data["batches_done"] == 3
    assert data["percent"] == 100.0
    assert data["current_batch"] == []
    assert data["failed_symbols"] == ["C"]


def test_manager_sync_run_and_on_finish():
    manager = ScanJobManager()
    finished = []
    progress = manager.start(
        ["A", "B", "C"],
        2,
        _scan_one,
        lambda signals, failed: {"n": len(signals), "failed": failed},
        on_finish=finished.append,
        run_async=False,
    )
    assert progress.status == STATUS_DONE
    assert progress.result == {"n": 1, "failed": ["C"]}
    assert finished == [progress]
    assert manager.active() is None
    assert manager.get(progress.job_id) is progress


def test_manager_marks_failed_when_finalize_raises():
    def finalize(signals, failed):
        raise ValueError("pipeline kaputt")

    progress = ScanJobManager().start(["A"], 1, _scan_one, finalize, run_async=False)
    assert progress.status == STATUS_FAILED
    assert "pipeline kaputt" in (progress.error or "")


def test_manager_busy_protection():
    manager = ScanJobManager()
    release = threading.Event()

    def slow(symbol):
        release.wait(5)
        return None

    first = manager.start(["A"], 1, slow, lambda s, f: {}, run_async=True)
    with pytest.raises(ScanJobBusyError) as info:
        manager.start(["B"], 1, slow, lambda s, f: {}, run_async=True)
    assert info.value.job_id == first.job_id
    release.set()
    _wait_idle(manager)
    assert manager.start(["B"], 1, _scan_one, lambda s, f: {}, run_async=False).status == STATUS_DONE


def test_manager_prunes_old_jobs():
    manager = ScanJobManager(max_kept=2)
    jobs = [manager.start(["A"], 1, _scan_one, lambda s, f: {}, run_async=False) for _ in range(3)]
    assert manager.get(jobs[0].job_id) is None
    assert manager.get(jobs[2].job_id) is jobs[2]


# ── State ─────────────────────────────────────────────────────────────────────

def test_state_scan_summary_roundtrip(tmp_path):
    store = StateStore(path=str(tmp_path / "state.db"))
    try:
        assert store.last_scan_summary() is None
        assert store.realized_pnl_by_symbol() == {}
        store.record_scan_summary(
            job_id="job-1",
            status="done",
            started_at=1_700_000_000.0,
            finished_at=1_700_000_030.0,
            total=10,
            processed=10,
            failed=2,
            signals_found=3,
            proposals_created=1,
            meta={"source": "universe", "failed_symbols": ["X/USDT:USDT"]},
        )
        summary = store.last_scan_summary()
        assert summary["job_id"] == "job-1"
        assert summary["failed"] == 2
        assert summary["signals_found"] == 3
        assert summary["finished_at"].startswith("2023-11-14T22:")
        assert summary["meta"]["failed_symbols"] == ["X/USDT:USDT"]
    finally:
        store.close()


# ── Endpunkte ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def scan_api(api, monkeypatch):  # noqa: F811
    client, app_module = api
    fetcher = Fetcher(MARKETS)
    set_universe_for_testing(SymbolUniverse(fetcher, clock=FakeClock()))
    monkeypatch.setattr(app_module.cfg.live_limits, "symbol_allowlist", [])
    monkeypatch.setattr(app_module, "scan_jobs", ScanJobManager())
    monkeypatch.setattr(app_module, "SCAN_RUN_ASYNC", False)
    shared_exchange = object()
    monkeypatch.setattr(app_module, "create_exchange", lambda cfg: shared_exchange)
    calls = []

    def fake_fetch(symbol, timeframe="1h", limit=120, cfg=None, exchange=None):
        calls.append((symbol, exchange))
        if symbol.startswith("ETH"):
            raise RuntimeError("timeout")
        return pd.DataFrame()

    monkeypatch.setattr(app_module, "fetch_ohlcv", fake_fetch)
    monkeypatch.setattr(app_module, "analyze_symbol", lambda symbol, df: None)
    headers = _bearer(_login(client))
    yield client, app_module, headers, calls, shared_exchange
    set_universe_for_testing(None)


@pytest.mark.parametrize(
    "method,path",
    [("GET", "/universe"), ("POST", "/scan"), ("GET", "/scan/abc"), ("GET", "/scan/last")],
)
def test_new_endpoints_require_session(scan_api, method, path):
    client = scan_api[0]
    resp = client.request(method, path, json={} if method == "POST" else None)
    assert resp.status_code in (401, 403)


def test_universe_endpoint(scan_api):
    client, _, headers, _, _ = scan_api
    data = client.get("/universe", headers=headers).json()
    assert data["symbols"] == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    assert data["count"] == 2
    assert data["stale"] is False
    assert data["error"] is None
    assert data["allowlist_mode"] == "all"
    assert data["open_positions"] == []
    assert data["performance"] == {}


def test_scan_subset_progress_and_persistence(scan_api):
    client, _, headers, calls, shared_exchange = scan_api
    resp = client.post("/scan", json={"symbols": ["BTC/USDT", "eth/usdt"], "batch_size": 1}, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["progress"]["total"] == 2
    assert [c[0] for c in calls] == ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    assert all(c[1] is shared_exchange for c in calls)

    status = client.get(f"/scan/{body['job_id']}", headers=headers).json()
    assert status["status"] == "done"
    assert status["batch_count"] == 2
    assert status["failed"] == 1
    assert status["failed_symbols"] == ["ETH/USDT:USDT"]
    assert status["result"]["proposals"] == []

    last = client.get("/scan/last", headers=headers).json()["last_scan"]
    assert last["job_id"] == body["job_id"]
    assert last["failed"] == 1
    assert last["meta"]["source"] == "request"


def test_scan_without_symbols_uses_universe(scan_api):
    client, _, headers, calls, _ = scan_api
    body = client.post("/scan", json={"batch_size": 0}, headers=headers).json()
    assert body["progress"]["total"] == 2
    assert body["progress"]["batch_size"] == 1
    assert len(calls) == 2


def test_scan_without_universe_returns_503(scan_api):
    client, _, headers, _, _ = scan_api
    set_universe_for_testing(SymbolUniverse(Fetcher(error=ConnectionError("down")), clock=FakeClock()))
    assert client.post("/scan", json={}, headers=headers).status_code == 503


def test_scan_unknown_job_404(scan_api):
    client, _, headers, _, _ = scan_api
    assert client.get("/scan/unbekannt", headers=headers).status_code == 404


def test_scan_busy_returns_409(scan_api):
    client, app_module, headers, _, _ = scan_api
    release = threading.Event()

    def slow(symbol):
        release.wait(5)
        return None

    running = app_module.scan_jobs.start(["A"], 1, slow, lambda s, f: {}, run_async=True)
    try:
        resp = client.post("/scan", json={"symbols": ["BTC/USDT"]}, headers=headers)
        assert resp.status_code == 409
        assert resp.json()["detail"]["job_id"] == running.job_id
    finally:
        release.set()
        _wait_idle(app_module.scan_jobs)