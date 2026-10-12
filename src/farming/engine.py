"""Persistent paper execution loop for the four-venue farming strategy."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal

import aiosqlite
import structlog

from src.farming.optimizer import FarmingOptimizer, FarmingRoute, VenueQuote
from src.models import FundingRate, utc_now

log = structlog.get_logger(__name__)
D = Decimal


@dataclass
class PaperPosition:
    id: str
    symbol: str
    long_venue: str
    short_venue: str
    quantity: Decimal
    notional_usd: Decimal
    long_entry: Decimal
    short_entry: Decimal
    entry_fees: Decimal
    opened_at: datetime
    expected_cash_pnl: Decimal


class FarmingEngine:
    def __init__(self, adapters: dict, settings) -> None:
        self.adapters = adapters
        self.settings = settings
        self.db: aiosqlite.Connection | None = None
        self.positions: dict[str, PaperPosition] = {}
        self.confirmations: dict[tuple[str, str, str], int] = {}
        self.cooldowns: dict[tuple[str, str, str], datetime] = {}
        self.optimizer = FarmingOptimizer(
            notional_usd=settings.farming_notional_usd,
            hold_hours=settings.farming_expected_hold_hours,
            basis_buffer_pct=settings.basis_risk_buffer_pct,
            min_exit_depth_multiplier=settings.exit_liquidity_multiplier,
            max_cash_loss_usd=settings.farming_max_cash_loss_usd,
        )

    async def connect(self) -> None:
        self.settings.farming_sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.settings.farming_sqlite_path)
        await self.db.executescript("""
        CREATE TABLE IF NOT EXISTS farming_positions (
          id TEXT PRIMARY KEY, symbol TEXT NOT NULL, long_venue TEXT NOT NULL,
          short_venue TEXT NOT NULL, quantity TEXT NOT NULL, notional_usd TEXT NOT NULL,
          long_entry TEXT NOT NULL, short_entry TEXT NOT NULL, entry_fees TEXT NOT NULL,
          opened_at TEXT NOT NULL, expected_cash_pnl TEXT NOT NULL,
          closed_at TEXT, close_reason TEXT, net_pnl TEXT
        );
        CREATE TABLE IF NOT EXISTS farming_scans (
          timestamp TEXT NOT NULL, candidates INTEGER NOT NULL, viable INTEGER NOT NULL,
          best_route TEXT
        );
        """)
        await self.db.commit()
        cursor = await self.db.execute(
            "SELECT id,symbol,long_venue,short_venue,quantity,notional_usd,long_entry,"
            "short_entry,entry_fees,opened_at,expected_cash_pnl FROM farming_positions "
            "WHERE closed_at IS NULL"
        )
        for row in await cursor.fetchall():
            p = PaperPosition(row[0], row[1], row[2], row[3], D(row[4]), D(row[5]),
                              D(row[6]), D(row[7]), D(row[8]),
                              datetime.fromisoformat(row[9]), D(row[10]))
            self.positions[p.id] = p

    async def disconnect(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None

    async def _rates(self) -> dict[str, dict[str, FundingRate]]:
        names = list(self.adapters)
        results = await asyncio.gather(
            *(self.adapters[n].get_funding_rates() for n in names), return_exceptions=True
        )
        by_symbol: dict[str, dict[str, FundingRate]] = {}
        for name, result in zip(names, results, strict=True):
            if isinstance(result, BaseException):
                log.warning("farming.venue_rates_failed", venue=name, error=str(result))
                continue
            for rate in result:
                by_symbol.setdefault(rate.symbol, {})[name] = rate
        return by_symbol

    @staticmethod
    def _per_hour(rate: FundingRate) -> Decimal:
        return rate.rate / D(rate.interval_hours)

    def _forecast_rate(self, rate: FundingRate) -> Decimal:
        """Bound one transient funding print before extrapolating it for hours."""
        value = self._per_hour(rate)
        cap = self.settings.farming_funding_cap_per_hour
        return max(-cap, min(cap, value))

    async def _quote(self, symbol: str, venue: str, rate: FundingRate) -> VenueQuote | None:
        adapter = self.adapters[venue]
        try:
            book, fee = await asyncio.gather(
                adapter.get_orderbook(symbol, 20), adapter.get_fee_rate(symbol)
            )
            if not book.bids or not book.asks:
                return None
            bid_depth = sum(x.price * x.size for x in book.bids)
            ask_depth = sum(x.price * x.size for x in book.asks)
            return VenueQuote(venue, symbol, self._forecast_rate(rate), book.bids[0].price,
                              book.asks[0].price, fee.taker, bid_depth, ask_depth)
        except Exception as exc:
            log.info("farming.quote_failed", venue=venue, symbol=symbol, error=str(exc))
            return None

    async def scan(self) -> list[FarmingRoute]:
        rates = await self._rates()
        raw = []
        for symbol, venues in rates.items():
            if len(venues) < 2:
                continue
            hourly = [self._per_hour(x) for x in venues.values()]
            raw.append((max(hourly) - min(hourly), symbol, venues))
        raw.sort(reverse=True, key=lambda x: x[0])
        # Fetch books only for the strongest signals. This keeps public APIs healthy.
        tasks = [self._quote(symbol, venue, rate) for _, symbol, venues in raw[:20]
                 for venue, rate in venues.items()]
        quotes = [x for x in await asyncio.gather(*tasks) if x is not None]
        routes = self.optimizer.rank(quotes)
        assert self.db is not None
        best = asdict(routes[0]) if routes else None
        await self.db.execute(
            "INSERT INTO farming_scans VALUES (?,?,?,?)",
            (utc_now().isoformat(), len(raw), len(routes),
             json.dumps(best, default=str, ensure_ascii=False) if best else None),
        )
        await self.db.commit()
        log.info("farming.scan_complete", shared_symbols=len(raw), viable_routes=len(routes),
                 best=(f"{routes[0].symbol}:{routes[0].long_venue}->{routes[0].short_venue}"
                       if routes else None),
                 expected_cash_pnl=(str(routes[0].expected_cash_pnl_usd) if routes else None))
        return routes

    async def _open(self, route: FarmingRoute) -> None:
        key = (route.symbol, route.long_venue, route.short_venue)
        now = utc_now()
        cooldown = self.cooldowns.get(key)
        if cooldown and (now - cooldown).total_seconds() < self.settings.farming_reentry_cooldown_sec:
            return
        self.confirmations[key] = self.confirmations.get(key, 0) + 1
        if self.confirmations[key] < self.settings.farming_entry_confirmation_scans:
            return
        if route.expected_cash_pnl_usd < self.settings.farming_min_expected_cash_pnl_usd:
            return
        if any(p.symbol == route.symbol for p in self.positions.values()):
            return
        long_book, short_book = await asyncio.gather(
            self.adapters[route.long_venue].get_orderbook(route.symbol, 20),
            self.adapters[route.short_venue].get_orderbook(route.symbol, 20),
        )
        long_px = long_book.execution_price("long", route.quantity)
        short_px = short_book.execution_price("short", route.quantity)
        if long_px is None or short_px is None:
            return
        long_fee, short_fee = await asyncio.gather(
            self.adapters[route.long_venue].get_fee_rate(route.symbol),
            self.adapters[route.short_venue].get_fee_rate(route.symbol),
        )
        entry_fees = route.quantity * (long_px * long_fee.taker + short_px * short_fee.taker)
        p = PaperPosition(uuid.uuid4().hex, route.symbol, route.long_venue, route.short_venue,
                          route.quantity, route.notional_usd, long_px, short_px, entry_fees,
                          now, route.expected_cash_pnl_usd)
        self.positions[p.id] = p
        assert self.db is not None
        await self.db.execute("INSERT INTO farming_positions VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)",
                              (p.id,p.symbol,p.long_venue,p.short_venue,str(p.quantity),
                               str(p.notional_usd),str(p.long_entry),str(p.short_entry),
                               str(p.entry_fees),p.opened_at.isoformat(),str(p.expected_cash_pnl)))
        await self.db.commit()
        log.warning("farming.paper_open", symbol=p.symbol, long=p.long_venue,
                    short=p.short_venue, notional=str(p.notional_usd), quantity=str(p.quantity),
                    expected_cash_pnl=str(p.expected_cash_pnl))

    async def _monitor(self, rates: dict[str, dict[str, FundingRate]]) -> None:
        for p in list(self.positions.values()):
            try:
                lb, sb, lf, sf = await asyncio.gather(
                    self.adapters[p.long_venue].get_orderbook(p.symbol, 20),
                    self.adapters[p.short_venue].get_orderbook(p.symbol, 20),
                    self.adapters[p.long_venue].get_fee_rate(p.symbol),
                    self.adapters[p.short_venue].get_fee_rate(p.symbol),
                )
                long_exit = lb.execution_price("short", p.quantity)
                short_exit = sb.execution_price("long", p.quantity)
                if long_exit is None or short_exit is None:
                    continue
                hours = D(str((utc_now() - p.opened_at).total_seconds())) / D("3600")
                rr = rates.get(p.symbol, {})
                lr, sr = rr.get(p.long_venue), rr.get(p.short_venue)
                carry = D("0")
                current_spread = D("0")
                if lr and sr:
                    current_spread = self._per_hour(sr) - self._per_hour(lr)
                    carry = p.notional_usd * current_spread * hours
                price_pnl = p.quantity * ((long_exit - p.long_entry) + (p.short_entry - short_exit))
                exit_fees = p.quantity * (long_exit * lf.taker + short_exit * sf.taker)
                net = price_pnl + carry - p.entry_fees - exit_fees
                reason = None
                if net >= self.settings.farming_take_profit_usd:
                    reason = "take_profit"
                elif net <= -self.settings.farming_stop_loss_usd:
                    reason = "stop_loss"
                elif hours >= self.settings.farming_max_hold_hours:
                    reason = "max_hold"
                elif current_spread <= 0 and hours >= D("1"):
                    reason = "funding_reversed"
                if reason:
                    await self._close(p, net, reason)
                else:
                    log.info("farming.paper_mark", symbol=p.symbol, net_pnl=str(net),
                             elapsed_hours=str(hours.quantize(D('0.001'))),
                             funding_spread_hourly=str(current_spread))
            except Exception as exc:
                log.warning("farming.monitor_failed", symbol=p.symbol, error=str(exc))

    async def _close(self, p: PaperPosition, net: Decimal, reason: str) -> None:
        assert self.db is not None
        await self.db.execute(
            "UPDATE farming_positions SET closed_at=?,close_reason=?,net_pnl=? WHERE id=?",
            (utc_now().isoformat(), reason, str(net), p.id),
        )
        await self.db.commit()
        self.positions.pop(p.id, None)
        self.cooldowns[(p.symbol, p.long_venue, p.short_venue)] = utc_now()
        log.warning("farming.paper_close", symbol=p.symbol, long=p.long_venue,
                    short=p.short_venue, net_pnl=str(net), reason=reason)

    async def run(self) -> None:
        while True:
            started = asyncio.get_running_loop().time()
            rates = await self._rates()
            await self._monitor(rates)
            if len(self.positions) < self.settings.farming_max_open_pairs:
                for route in await self.scan():
                    await self._open(route)
                    if len(self.positions) >= self.settings.farming_max_open_pairs:
                        break
            delay = max(1, self.settings.farming_scan_interval_sec -
                        (asyncio.get_running_loop().time() - started))
            await asyncio.sleep(delay)
