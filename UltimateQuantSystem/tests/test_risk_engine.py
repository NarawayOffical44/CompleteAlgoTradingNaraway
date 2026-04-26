"""Tests for RiskEngine — run with: python -m pytest tests/"""

import sys
sys.path.insert(0, "..")

from risk.engine import RiskEngine, RiskMode
from config import RiskConfig


def make_engine(capital=10000):
    cfg = RiskConfig(
        max_portfolio_risk_pct=6.0,
        max_trade_risk_pct=1.0,
        daily_loss_limit_pct=3.0,
        drawdown_warning_pct=8.0,
        drawdown_kill_pct=12.0,
    )
    return RiskEngine(starting_capital=capital, config=cfg)


def test_approve_within_limits():
    e = make_engine(10000)
    ok, reason = e.approve_trade("agent1", 90)   # 0.9% — within 1%
    assert ok, reason


def test_reject_over_per_trade_limit():
    e = make_engine(10000)
    ok, reason = e.approve_trade("agent1", 150)  # 1.5% — over 1%
    assert not ok


def test_reject_over_portfolio_limit():
    e = make_engine(10000)
    e.register_open("agent1", "t1", 200)
    e.register_open("agent1", "t2", 200)
    e.register_open("agent1", "t3", 150)
    # Total open risk = 550 = 5.5%, adding 100 more = 6.5% > 6%
    ok, reason = e.approve_trade("agent1", 100)
    assert not ok


def test_kill_switch_on_drawdown():
    e = make_engine(10000)
    e.register_open("agent1", "t1", 100)
    e.register_close("agent1", "t1", pnl=-1300)  # -13% drawdown
    assert e.state.mode == RiskMode.STOPPED


def test_reduced_mode_on_warning():
    e = make_engine(10000)
    # Simulate: capital already at 9200 from previous days (daily limit not breached today)
    # Today's loss is 2% of 9200 = 184 → total drawdown = 9.84% > 8% warning
    # But daily_loss = 2% < 3% limit → should be REDUCED not STOPPED
    e.state.capital              = 9200
    e.state.daily_start_capital  = 9200
    e.register_open("agent1", "t1", 100)
    e.register_close("agent1", "t1", pnl=-184)   # 2% daily loss, 9.84% total drawdown
    assert e.state.mode == RiskMode.REDUCED


def test_correlation_alert():
    e = make_engine(10000)
    # Perfectly correlated agents with varying values (avoids std=0 edge case)
    for i in range(15):
        e.state.agent_pnl_history.setdefault("a1", []).append(float(i))
        e.state.agent_pnl_history.setdefault("a2", []).append(float(i))   # identical → corr=1.0
    alerts = e.check_agent_correlation()
    assert "a1↔a2" in alerts


def test_stopped_blocks_all_trades():
    e = make_engine(10000)
    e.state.mode = RiskMode.STOPPED
    ok, reason = e.approve_trade("agent1", 10)
    assert not ok
    assert "KILL SWITCH" in reason


def test_manual_reset():
    e = make_engine(10000)
    e.state.mode = RiskMode.STOPPED
    e.manual_reset("manual review complete")
    assert e.state.mode == RiskMode.NORMAL


if __name__ == "__main__":
    tests = [test_approve_within_limits, test_reject_over_per_trade_limit,
             test_reject_over_portfolio_limit, test_kill_switch_on_drawdown,
             test_reduced_mode_on_warning, test_correlation_alert,
             test_stopped_blocks_all_trades, test_manual_reset]
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:
            print(f"  ERROR {t.__name__}: {e}")
