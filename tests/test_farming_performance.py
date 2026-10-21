from datetime import timedelta
from decimal import Decimal

from src.farming.performance import PerformanceGuard
from src.models import utc_now


def guard() -> PerformanceGuard:
    return PerformanceGuard(
        min_combo_trades=2,
        min_route_trades=4,
        block_seconds=86400,
        tail_loss_return=Decimal("0.002"),
    )


def test_exact_profitable_pair_overrides_negative_route_history():
    history = guard()
    now = utc_now()
    for symbol in ("BAD1", "BAD2", "BAD3", "BAD4"):
        history.add_trade(symbol, "txflow_account_0", "nado_account_0", Decimal("100"),
                          Decimal("-0.10"), now)
    for _ in range(3):
        history.add_trade("EIGEN", "txflow_account_0", "nado_account_0", Decimal("100"),
                          Decimal("0.10"), now)

    decision = history.decide("EIGEN", "txflow_account_0", "nado_account_0", now)

    assert decision.allowed
    assert decision.reason == "profitable_combo"


def test_negative_symbol_route_is_temporarily_blocked_then_retested():
    history = guard()
    now = utc_now()
    for net in (Decimal("-0.20"), Decimal("0.05")):
        history.add_trade("SPCX", "txflow", "nado", Decimal("100"), net, now)

    blocked = history.decide("SPCX", "txflow_account_0", "nado_account_0", now)
    probe = history.decide(
        "SPCX", "txflow_account_0", "nado_account_0", now + timedelta(days=1, seconds=1)
    )

    assert not blocked.allowed
    assert blocked.reason == "negative_symbol_route_history"
    assert probe.allowed


def test_single_tail_loss_gets_cooldown_without_permanent_blacklist():
    history = guard()
    now = utc_now()
    history.add_trade("PONS", "nado", "txflow", Decimal("150"), Decimal("-0.67"), now)

    blocked = history.decide("PONS", "nado", "txflow", now)
    probe = history.decide("PONS", "nado", "txflow", now + timedelta(days=2))

    assert not blocked.allowed
    assert blocked.reason == "recent_tail_loss"
    assert probe.allowed


def test_high_win_rate_does_not_hide_negative_route_expectancy():
    history = guard()
    now = utc_now()
    for index in range(7):
        history.add_trade(
            f"WIN{index}", "txflow", "nado", Decimal("100"), Decimal("0.10"), now
        )
    for index in range(3):
        history.add_trade(
            f"LOSS{index}", "txflow", "nado", Decimal("100"), Decimal("-0.40"), now
        )

    decision = history.decide("NEW", "txflow", "nado", now)

    assert decision.route.win_rate == Decimal("0.7")
    assert decision.route.profit_factor < 1
    assert decision.route.average_return < 0
    assert not decision.allowed
    assert decision.reason == "negative_route_expectancy"
