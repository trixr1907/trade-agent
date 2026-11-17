"""Unit-Tests für PF-Fallback-Logik in _compute_metrics / FoldMetrics.

Ausführen:
    cd trade-agent
    .venv/Scripts/python -m pytest test_pf_unit.py -v
"""
import math
import pytest
from tradeagent.backtest import _compute_metrics, FoldMetrics, SimTrade


# ─── Hilfsfunktion ────────────────────────────────────────────────────────────

def _make_trade(pnl_r: float) -> SimTrade:
    return SimTrade(
        symbol="BTC/USDT",
        direction="buy",
        entry_price=50_000.0,
        stop_price=49_000.0,
        target_price=52_000.0,
        entry_bar=0,
        exit_bar=5,
        exit_price=52_000.0,
        gross_pnl=pnl_r * 100,
        cost_usd=0.0,
        pnl_usd=pnl_r * 100,
        pnl_r=pnl_r,
        exit_reason="target",
        score=70,
    )


def _metrics(trades):
    return _compute_metrics(
        trades,
        fold=0,
        is_bars=100,
        oos_bars=50,
        bars_per_year=252,
    )


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def win_only():
    return [_make_trade(1.0), _make_trade(0.8), _make_trade(1.5)]


@pytest.fixture
def mixed():
    # gross_profit = 1.0 + 0.8 = 1.8
    # gross_loss   = 0.5 + 0.3 = 0.8
    # PF = 1.8 / 0.8 = 2.25
    return [_make_trade(1.0), _make_trade(-0.5), _make_trade(0.8), _make_trade(-0.3)]


@pytest.fixture
def loss_only():
    return [_make_trade(-1.0), _make_trade(-0.5)]


# ─── Tests ────────────────────────────────────────────────────────────────────

class TestProfitFactorFallback:
    """Phase-3 Safety-Gate: PF-Fallback robust."""

    def test_win_only_pf_is_inf(self, win_only):
        """Nur Gewinn-Trades → profit_factor == inf."""
        m = _metrics(win_only)
        assert math.isinf(m.profit_factor), (
            f"Erwartet inf, bekam {m.profit_factor}"
        )

    def test_win_only_to_dict_maps_inf_to_none(self, win_only):
        """to_dict() muss inf → None mappen (gültiges JSON)."""
        m = _metrics(win_only)
        d = m.to_dict()
        assert d["profit_factor"] is None, (
            f"Erwartet None, bekam {d['profit_factor']}"
        )

    def test_win_only_pf_display_is_dash(self, win_only):
        """pf_display() muss '–' zurückgeben für inf."""
        m = _metrics(win_only)
        result = m.pf_display()
        assert result == "–", f"Erwartet '–', bekam {result!r}"

    def test_mixed_trades_pf_correct(self, mixed):
        """Normale Wins+Losses → PF = gross_profit / gross_loss = 2.25."""
        m = _metrics(mixed)
        assert math.isfinite(m.profit_factor), (
            f"Erwartet endlichen PF, bekam {m.profit_factor}"
        )
        assert abs(m.profit_factor - 2.25) < 1e-6, (
            f"PF sollte 2.25 sein, bekam {m.profit_factor}"
        )

    def test_mixed_pf_display_formatted(self, mixed):
        """pf_display() formatiert endlichen PF mit 2 Dezimalstellen."""
        m = _metrics(mixed)
        assert m.pf_display() == "2.25", (
            f"Erwartet '2.25', bekam {m.pf_display()!r}"
        )

    def test_no_trades_pf_is_zero(self):
        """Keine Trades → profit_factor == 0.0."""
        m = _metrics([])
        assert m.profit_factor == 0.0, (
            f"Erwartet 0.0, bekam {m.profit_factor}"
        )

    def test_loss_only_pf_is_zero(self, loss_only):
        """Nur Verlust-Trades, kein Gewinn → profit_factor == 0.0."""
        m = _metrics(loss_only)
        assert m.profit_factor == 0.0, (
            f"Erwartet 0.0, bekam {m.profit_factor}"
        )

    def test_loss_only_to_dict_no_none(self, loss_only):
        """to_dict() bei PF=0.0 darf nicht None zurückgeben."""
        m = _metrics(loss_only)
        d = m.to_dict()
        assert d["profit_factor"] == 0.0

    def test_single_win_trade_is_inf(self):
        """Genau ein Gewinn-Trade → PF == inf."""
        m = _metrics([_make_trade(2.0)])
        assert math.isinf(m.profit_factor)

    def test_single_loss_trade_is_zero(self):
        """Genau ein Verlust-Trade → PF == 0.0."""
        m = _metrics([_make_trade(-1.0)])
        assert m.profit_factor == 0.0

    def test_win_only_to_dict_is_json_serializable(self, win_only):
        """to_dict()-Output muss JSON-serialisierbar sein (kein inf/nan)."""
        import json
        m = _metrics(win_only)
        d = m.to_dict()
        # Darf keinen ValueError werfen
        serialized = json.dumps(d)
        parsed = json.loads(serialized)
        assert parsed["profit_factor"] is None