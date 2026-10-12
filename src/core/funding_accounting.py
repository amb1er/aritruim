"""Paper funding: exchange indices/history, with explicitly scheduled estimates.

Historical rate payments still use an observed mark near settlement, not a
confirmed account cash flow. Never apply today's rate over an unobserved gap.
"""

from datetime import datetime, timedelta
from decimal import Decimal

from src.models import as_utc

ZERO = Decimal("0")


def paid_hours_within(rate, model, horizon, now):
    """Hours of quoted carry actually payable inside the forecast horizon."""
    if model == "spot":
        return ZERO
    if model == "index":
        return horizon
    end = now + timedelta(seconds=float(horizon * 3600))
    if model == "hourly_history":
        return Decimal(int(end.timestamp()) // 3600 - int(now.timestamp()) // 3600)
    if model in ("period_history", "scheduled_estimate"):
        if not rate.next_funding_ts or rate.interval_hours <= 0:
            raise ValueError("unknown settlement schedule")
        next_event = as_utc(rate.next_funding_ts)
        if next_event <= now:
            raise ValueError("stale settlement schedule")
        if next_event > end:
            return ZERO
        periods = 1 + int((end - next_event).total_seconds() // (rate.interval_hours * 3600))
        return Decimal(periods * rate.interval_hours)
    raise ValueError("unverified settlement schedule")


async def observe_leg(adapter, symbol, side, quantity, state, now, max_age_sec=180):
    model = getattr(adapter, "funding_model", "unverified")
    if model == "spot":
        return ZERO, ZERO, {"model": "spot"}
    if model == "index":
        indices = await adapter.get_funding_indices(symbol)
        current = indices[0 if side == "long" else 1]
        if not current.is_finite():
            raise ValueError("invalid funding index")
        previous = Decimal(state["index"]) if "index" in state else current
        payment = quantity * (current - previous) * (-1 if side == "long" else 1)
        return ZERO, payment, {"index": str(current), "model": "index"}
    if model not in ("hourly_history", "period_history", "scheduled_estimate"):
        raise ValueError(f"Unverified paper funding model for {adapter.name}")
    mark = await adapter.get_funding_price(symbol)
    if not mark.is_finite() or mark <= 0:
        raise ValueError("invalid funding mark")
    updated = dict(state)
    updated.update(model=model, mark=str(mark), observed_at=now.isoformat())
    observations = [
        (as_utc(datetime.fromisoformat(ts)), Decimal(value)) for ts, value in state.get("marks", [])
    ]
    observations.append((now, mark))
    observations = [(ts, value) for ts, value in observations if (now - ts).total_seconds() <= 7200]
    updated["marks"] = [(ts.isoformat(), str(value)) for ts, value in observations[-240:]]
    if not state:
        updated["cursor"] = now.isoformat()
    cursor = as_utc(datetime.fromisoformat(updated["cursor"]))
    sign = -1 if side == "long" else 1
    payment, total_rate = ZERO, ZERO
    if model == "scheduled_estimate":
        # The API provides a future schedule and an indicative rate, not a cash
        # event. Credit only at the boundary previously observed while holding.
        if state.get("next"):
            event = as_utc(datetime.fromisoformat(state["next"]))
            if cursor < event <= now:
                previous_at = as_utc(datetime.fromisoformat(state["observed_at"]))
                if abs((event - previous_at).total_seconds()) > max_age_sec:
                    raise ValueError("missing mark/rate near funding settlement")
                total_rate = Decimal(state["rate"])
                payment = quantity * Decimal(state["mark"]) * total_rate * sign
                updated["cursor"] = event.isoformat()
        rate = await adapter.get_funding_rate(symbol)
        if (
            not rate.next_funding_ts
            or as_utc(rate.next_funding_ts) <= now
            or not rate.rate.is_finite()
        ):
            raise ValueError("missing future funding schedule")
        updated.update(next=as_utc(rate.next_funding_ts).isoformat(), rate=str(rate.rate))
    elif state:
        # Fetch applied rates only. The cursor advances only after a returned
        # event, so publication delay cannot silently drop or duplicate it.
        lookback = min(168, max(2, int((now - cursor).total_seconds() / 3600) + 2))
        history = await adapter.get_funding_history(symbol, lookback)
        events = {as_utc(r.timestamp): r for r in history if cursor < as_utc(r.timestamp) <= now}
        for event, rate in sorted(events.items()):
            if not rate.rate.is_finite():
                raise ValueError("invalid applied funding rate")
            if state.get("observed_at"):
                observations.append(
                    (as_utc(datetime.fromisoformat(state["observed_at"])), Decimal(state["mark"]))
                )
            observed_at, observed_mark = min(
                observations, key=lambda x: abs((x[0] - event).total_seconds())
            )
            if abs((observed_at - event).total_seconds()) > max_age_sec:
                raise ValueError("historical funding mark unavailable; reconciliation required")
            # History rates represent the actual payment period, NOT the
            # normalized 8h/24h indicative rates used by some tickers.
            payment += quantity * observed_mark * rate.rate * sign
            total_rate += rate.rate
            updated["cursor"] = event.isoformat()
        if model == "hourly_history":
            boundary = now.replace(minute=0, second=0, microsecond=0)
            if as_utc(datetime.fromisoformat(updated["cursor"])) < boundary:
                raise ValueError("applied hourly funding not published yet")
        elif state.get("next"):
            due = as_utc(datetime.fromisoformat(state["next"]))
            if due <= now and as_utc(datetime.fromisoformat(updated["cursor"])) < due:
                raise ValueError("applied period funding not published yet")
    if model == "period_history":
        rate = await adapter.get_funding_rate(symbol)
        if not rate.next_funding_ts or as_utc(rate.next_funding_ts) <= now:
            raise ValueError("missing future funding schedule")
        updated["next"] = as_utc(rate.next_funding_ts).isoformat()
    return total_rate, payment, updated
