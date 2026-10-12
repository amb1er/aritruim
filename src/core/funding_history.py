"""Time-bucketed, signed pair history. No future samples or scan-count warmup."""

from decimal import Decimal

from src.models import as_utc

ZERO = Decimal("0")


class FundingHistory:
    def __init__(self, settings):
        self.cfg = settings
        self.samples = {}

    def observe(self, rates, now):
        bucket = int(now.timestamp()) // self.cfg.funding_history_bucket_sec
        cutoff = bucket - self.cfg.funding_history_window_hours * 3600 // (
            self.cfg.funding_history_bucket_sec
        )
        self.samples = {k: v for k, v in self.samples.items() if k[2] >= cutoff}
        rows = []
        for exchange, symbols in rates.items():
            for symbol, rate in symbols.items():
                age = (now - as_utc(rate.timestamp)).total_seconds()
                if (
                    rate.interval_hours <= 0
                    or not rate.rate.is_finite()
                    or age < -5
                    or age > self.cfg.max_funding_age_sec
                ):
                    continue
                value = rate.rate / Decimal(rate.interval_hours)
                # Keep the most recent observation in each fixed-duration bucket.
                self.samples[exchange, symbol, bucket] = value
                rows.append((exchange, symbol, bucket, str(value)))
        return rows

    def forecast(self, symbol, long, short, current, now):
        if self.cfg.funding_history_min_hours == 0:
            return current, self.cfg.expected_hold_hours, ZERO, ""
        end = int(now.timestamp()) // self.cfg.funding_history_bucket_sec
        values = []
        direction_changed = False
        # Only an uninterrupted suffix counts. Outages cannot masquerade as history.
        for bucket in range(
            end,
            end
            - self.cfg.funding_history_window_hours * 3600 // self.cfg.funding_history_bucket_sec
            - 1,
            -1,
        ):
            a = self.samples.get((long, symbol, bucket))
            b = self.samples.get((short, symbol, bucket))
            if a is None or b is None:
                break
            if b - a <= 0:
                direction_changed = True
                break
            values.append(b - a)
        hours = Decimal(max(0, len(values) - 1) * self.cfg.funding_history_bucket_sec) / 3600
        # History establishes confidence; it is not the future holding period.
        # Capping the latter by elapsed warmup silently overrides EXPECTED_HOLD_HOURS
        # and rejects carry that can repay costs within the configured holding plan.
        horizon = min(self.cfg.expected_hold_hours, Decimal(self.cfg.max_hold_hours))
        if hours < self.cfg.funding_history_min_hours:
            reason = "funding_direction_unstable" if direction_changed else "history_warmup"
            return ZERO, horizon, hours, reason
        # Old low-rate regimes must not suppress a sustained recent opportunity
        # for an entire day. Keep the signed warmup, but estimate the rate using
        # only the recent window, still capped by the current quote and discounted.
        recent_count = (
            self.cfg.funding_forecast_window_hours * 3600
            // self.cfg.funding_history_bucket_sec
            + 1
        )
        ordered = sorted(values[:recent_count])
        low_quartile = ordered[(len(ordered) - 1) // 4]
        forecast = min(current, low_quartile) * self.cfg.funding_forecast_discount
        return forecast, horizon, hours, ""
