"""
SQLite persistence через aiosqlite.

Схема:
  positions   — открытые арбитражные пары
  trades      — история всех ордеров
  funding_log — funding payments по каждой ноге
  pnl_summary — дневной P&L снэпшот
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import aiosqlite
import structlog

from src.models import ArbitragePosition, BotMode, OrderResult, OrderSide, PositionStatus, as_utc

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id              TEXT PRIMARY KEY,
    symbol          TEXT NOT NULL,
    long_exchange   TEXT NOT NULL,
    short_exchange  TEXT NOT NULL,
    notional_usd    TEXT NOT NULL,
    leverage        TEXT NOT NULL,
    status          TEXT NOT NULL,
    entry_spread    TEXT NOT NULL,
    funding_received TEXT NOT NULL DEFAULT '0',
    fees_paid       TEXT NOT NULL DEFAULT '0',
    net_pnl         TEXT NOT NULL DEFAULT '0',
    long_order_json TEXT,
    short_order_json TEXT,
    opened_at       TEXT NOT NULL,
    closed_at       TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    order_id        TEXT NOT NULL,
    side            TEXT NOT NULL,
    filled_size     TEXT NOT NULL,
    avg_price       TEXT NOT NULL,
    fee_paid        TEXT NOT NULL,
    success         INTEGER NOT NULL,
    error           TEXT,
    timestamp       TEXT NOT NULL,
    FOREIGN KEY (position_id) REFERENCES positions(id)
);

CREATE TABLE IF NOT EXISTS funding_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id     TEXT NOT NULL,
    exchange        TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL,
    funding_rate    TEXT NOT NULL,
    payment         TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    FOREIGN KEY (position_id) REFERENCES positions(id)
);

CREATE TABLE IF NOT EXISTS pnl_summary (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT NOT NULL,
    open_pairs      INTEGER NOT NULL,
    closed_pairs    INTEGER NOT NULL,
    funding_received TEXT NOT NULL,
    fees_paid       TEXT NOT NULL,
    net_pnl         TEXT NOT NULL,
    timestamp       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
CREATE INDEX IF NOT EXISTS idx_trades_position ON trades(position_id);
CREATE INDEX IF NOT EXISTS idx_funding_position ON funding_log(position_id);
CREATE TABLE IF NOT EXISTS funding_observations (
    exchange TEXT NOT NULL, symbol TEXT NOT NULL, bucket INTEGER NOT NULL,
    hourly_rate TEXT NOT NULL,
    PRIMARY KEY(exchange, symbol, bucket)
);
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _order_to_json(order: OrderResult | None) -> str | None:
    if order is None:
        return None
    return json.dumps(
        {
            "exchange": order.exchange,
            "symbol": order.symbol,
            "order_id": order.order_id,
            "side": order.side.value,
            "filled_size": str(order.filled_size),
            "avg_price": str(order.avg_price),
            "fee_paid": str(order.fee_paid),
            "success": order.success,
            "error": order.error,
            "timestamp": order.timestamp.isoformat(),
            "uncertain": order.uncertain,
        }
    )


def _order_from_json(raw: str | None) -> OrderResult | None:
    if raw is None:
        return None
    d = json.loads(raw)
    return OrderResult(
        exchange=d["exchange"],
        symbol=d["symbol"],
        order_id=d["order_id"],
        side=OrderSide(d["side"]),
        filled_size=Decimal(d["filled_size"]),
        avg_price=Decimal(d["avg_price"]),
        fee_paid=Decimal(d["fee_paid"]),
        success=d["success"],
        error=d.get("error", ""),
        timestamp=as_utc(datetime.fromisoformat(d["timestamp"])),
        uncertain=d.get("uncertain", False),
    )


class Database:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._write_lock = asyncio.Lock()
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        # Additive migrations preserve existing databases.
        for table, column, declaration in (
            ("positions", "state_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("positions", "mode", "TEXT NOT NULL DEFAULT 'paper'"),
            ("trades", "realized_pnl", "TEXT NOT NULL DEFAULT '0'"),
        ):
            async with self._conn.execute(f"PRAGMA table_info({table})") as cursor:
                columns = {row[1] for row in await cursor.fetchall()}
            if column not in columns:
                await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        await self._conn.commit()
        log.info("db.connected", path=str(self._path))

    async def disconnect(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
        log.info("db.disconnected")

    async def __aenter__(self) -> Database:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.disconnect()

    def _conn_or_raise(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database not connected. Call connect() first.")
        return self._conn

    # ── Positions ──────────────────────────────────────────────────────────────

    async def save_position(self, pos: ArbitragePosition, *, commit: bool = True) -> None:
        if commit:
            async with self._write_lock:
                try:
                    await self.save_position(pos, commit=False)
                    await self._conn_or_raise().commit()
                except BaseException:
                    await self._conn_or_raise().rollback()
                    raise
            return
        conn = self._conn_or_raise()
        await conn.execute(
            """
            INSERT OR REPLACE INTO positions
              (id, symbol, long_exchange, short_exchange, notional_usd, leverage,
               status, entry_spread, funding_received, fees_paid, net_pnl,
               long_order_json, short_order_json, opened_at, closed_at, state_json, mode)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                pos.id,
                pos.symbol,
                pos.long_exchange,
                pos.short_exchange,
                str(pos.notional_usd),
                str(pos.leverage),
                pos.status.value,
                str(pos.entry_spread),
                str(pos.funding_received),
                str(pos.fees_paid),
                str(pos.net_pnl),
                _order_to_json(pos.long_order),
                _order_to_json(pos.short_order),
                pos.opened_at.isoformat(),
                pos.closed_at.isoformat() if pos.closed_at else None,
                json.dumps(
                    {
                        "realized_price_pnl": str(pos.realized_price_pnl),
                        "unrealized_price_pnl": str(pos.unrealized_price_pnl),
                        "long_remaining": str(pos.long_remaining)
                        if pos.long_remaining is not None
                        else None,
                        "short_remaining": str(pos.short_remaining)
                        if pos.short_remaining is not None
                        else None,
                        "last_funding_at": pos.last_funding_at.isoformat()
                        if pos.last_funding_at
                        else None,
                        "needs_reconciliation": pos.needs_reconciliation,
                        "close_reason": pos.close_reason,
                        "close_pnl_estimate": str(pos.close_pnl_estimate)
                        if pos.close_pnl_estimate is not None
                        else None,
                        "strategy_version": pos.strategy_version,
                        "funding_state": pos.funding_state,
                        "negative_funding_since": pos.negative_funding_since.isoformat()
                        if pos.negative_funding_since
                        else None,
                        "peak_close_pnl": str(pos.peak_close_pnl)
                        if pos.peak_close_pnl is not None
                        else None,
                    }
                ),
                pos.mode.value,
            ),
        )
        if commit:
            await conn.commit()

    async def get_open_positions(self) -> list[ArbitragePosition]:
        conn = self._conn_or_raise()
        async with conn.execute("SELECT * FROM positions WHERE status != 'closed'") as cur:
            rows = await cur.fetchall()
        return [self._row_to_position(r) for r in rows]

    async def get_last_closed_at(self, symbol: str, mode: BotMode) -> datetime | None:
        async with self._conn_or_raise().execute(
            "SELECT closed_at FROM positions WHERE symbol=? AND mode=? "
            "AND status='closed' AND closed_at IS NOT NULL",
            (symbol, mode.value),
        ) as cur:
            dates = [as_utc(datetime.fromisoformat(row[0])) for row in await cur.fetchall()]
        return max(dates, default=None)

    async def get_all_positions(self) -> list[ArbitragePosition]:
        conn = self._conn_or_raise()
        async with conn.execute("SELECT * FROM positions ORDER BY opened_at DESC") as cur:
            rows = await cur.fetchall()
        return [self._row_to_position(r) for r in rows]

    def _row_to_position(self, row: aiosqlite.Row) -> ArbitragePosition:
        state = json.loads(row["state_json"])
        return ArbitragePosition(
            id=row["id"],
            symbol=row["symbol"],
            long_exchange=row["long_exchange"],
            short_exchange=row["short_exchange"],
            notional_usd=Decimal(row["notional_usd"]),
            leverage=Decimal(row["leverage"]),
            status=PositionStatus(row["status"]),
            mode=BotMode(row["mode"]),
            realized_price_pnl=Decimal(state.get("realized_price_pnl", "0")),
            unrealized_price_pnl=Decimal(state.get("unrealized_price_pnl", "0")),
            long_remaining=Decimal(state["long_remaining"])
            if state.get("long_remaining") is not None
            else None,
            short_remaining=Decimal(state["short_remaining"])
            if state.get("short_remaining") is not None
            else None,
            last_funding_at=datetime.fromisoformat(state["last_funding_at"])
            if state.get("last_funding_at")
            else None,
            close_reason=state.get("close_reason", ""),
            close_pnl_estimate=Decimal(state["close_pnl_estimate"])
            if state.get("close_pnl_estimate") is not None
            else None,
            strategy_version=state.get("strategy_version", "legacy"),
            funding_state=state.get("funding_state", {}),
            negative_funding_since=as_utc(datetime.fromisoformat(state["negative_funding_since"]))
            if state.get("negative_funding_since")
            else None,
            peak_close_pnl=Decimal(state["peak_close_pnl"])
            if state.get("peak_close_pnl") is not None
            else None,
            needs_reconciliation=state.get("needs_reconciliation", False),
            entry_spread=Decimal(row["entry_spread"]),
            funding_received=Decimal(row["funding_received"]),
            fees_paid=Decimal(row["fees_paid"]),
            net_pnl=Decimal(row["net_pnl"]),
            long_order=_order_from_json(row["long_order_json"]),
            short_order=_order_from_json(row["short_order_json"]),
            opened_at=datetime.fromisoformat(row["opened_at"]),
            closed_at=(datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None),
        )

    # ── Trades ────────────────────────────────────────────────────────────────

    async def load_funding_observations(self):
        async with self._conn_or_raise().execute(
            "SELECT exchange, symbol, bucket, hourly_rate FROM funding_observations"
        ) as cur:
            return {(r[0], r[1], r[2]): Decimal(r[3]) for r in await cur.fetchall()}

    async def save_funding_observations(self, rows, settings):
        cutoff = int(datetime.now(UTC).timestamp()) // settings.funding_history_bucket_sec
        cutoff -= (
            settings.funding_history_window_hours * 3600 // settings.funding_history_bucket_sec
        )
        async with self._write_lock:
            conn = self._conn_or_raise()
            try:
                await conn.executemany(
                    "INSERT OR REPLACE INTO funding_observations VALUES (?, ?, ?, ?)", rows
                )
                await conn.execute("DELETE FROM funding_observations WHERE bucket < ?", (cutoff,))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def pair_loss_cooldown_until(self, symbol, long, short, mode, settings, symbols=None):
        from datetime import timedelta

        now = datetime.now(UTC)
        symbols = sorted(symbols or {symbol})
        placeholders = ",".join("?" for _ in symbols)
        async with self._conn_or_raise().execute(
            f"SELECT net_pnl, closed_at FROM positions WHERE symbol IN ({placeholders}) "
            "AND long_exchange=? "
            "AND short_exchange=? AND mode=? AND status='closed' AND closed_at IS NOT NULL",
            (*symbols, long, short, mode.value),
        ) as cur:
            losses = [
                as_utc(datetime.fromisoformat(r[1]))
                for r in await cur.fetchall()
                if Decimal(r[0]) < 0
                and as_utc(datetime.fromisoformat(r[1]))
                >= now - timedelta(hours=settings.pair_loss_lookback_hours)
            ]
        if len(losses) >= settings.pair_loss_limit:
            return max(losses) + timedelta(seconds=settings.pair_loss_cooldown_sec)
        return None

    async def save_trade(
        self,
        position_id: str,
        order: OrderResult,
        realized_pnl: Decimal = Decimal("0"),
        *,
        commit: bool = True,
    ) -> None:
        if commit:
            async with self._write_lock:
                try:
                    await self.save_trade(position_id, order, realized_pnl, commit=False)
                    await self._conn_or_raise().commit()
                except BaseException:
                    await self._conn_or_raise().rollback()
                    raise
            return
        conn = self._conn_or_raise()
        await conn.execute(
            """
            INSERT INTO trades
              (position_id, exchange, symbol, order_id, side,
               filled_size, avg_price, fee_paid, success, error, timestamp, realized_pnl)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                position_id,
                order.exchange,
                order.symbol,
                order.order_id,
                order.side.value,
                str(order.filled_size),
                str(order.avg_price),
                str(order.fee_paid),
                int(order.success),
                order.error,
                order.timestamp.isoformat(),
                str(realized_pnl),
            ),
        )
        if commit:
            await conn.commit()

    # ── Funding log ───────────────────────────────────────────────────────────

    async def log_funding(
        self,
        position_id: str,
        exchange: str,
        symbol: str,
        side: str,
        funding_rate: Decimal,
        payment: Decimal,
        *,
        commit: bool = True,
    ) -> None:
        if commit:
            async with self._write_lock:
                try:
                    await self.log_funding(
                        position_id, exchange, symbol, side, funding_rate, payment, commit=False
                    )
                    await self._conn_or_raise().commit()
                except BaseException:
                    await self._conn_or_raise().rollback()
                    raise
            return
        conn = self._conn_or_raise()
        await conn.execute(
            """
            INSERT INTO funding_log
              (position_id, exchange, symbol, side, funding_rate, payment, timestamp)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                position_id,
                exchange,
                symbol,
                side,
                str(funding_rate),
                str(payment),
                _now_iso(),
            ),
        )
        if commit:
            await conn.commit()

    async def get_total_funding(self, position_id: str) -> Decimal:
        conn = self._conn_or_raise()
        async with conn.execute(
            "SELECT payment FROM funding_log WHERE position_id=?", (position_id,)
        ) as cur:
            return sum((Decimal(row[0]) for row in await cur.fetchall()), Decimal("0"))

    async def record_fill(
        self, pos: ArbitragePosition, order: OrderResult, realized_pnl: Decimal = Decimal("0")
    ) -> None:
        async with self._write_lock:
            try:
                await self.save_trade(pos.id, order, realized_pnl, commit=False)
                await self.save_position(pos, commit=False)
                await self._conn_or_raise().commit()
            except BaseException:
                await self._conn_or_raise().rollback()
                raise

    async def record_funding(self, pos: ArbitragePosition, payments: list) -> None:
        async with self._write_lock:
            try:
                for exchange, side, rate, payment in payments:
                    await self.log_funding(
                        pos.id, exchange, pos.symbol, side, rate, payment, commit=False
                    )
                await self.save_position(pos, commit=False)
                await self._conn_or_raise().commit()
            except BaseException:
                await self._conn_or_raise().rollback()
                raise

    async def save_pnl_snapshot(
        self,
        open_pairs: int,
        closed_pairs: int,
        funding_received: Decimal,
        fees_paid: Decimal,
        net_pnl: Decimal,
    ) -> None:
        async with self._write_lock:
            conn = self._conn_or_raise()
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            await conn.execute(
                """
                INSERT INTO pnl_summary
                  (date, open_pairs, closed_pairs, funding_received, fees_paid, net_pnl, timestamp)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    today,
                    open_pairs,
                    closed_pairs,
                    str(funding_received),
                    str(fees_paid),
                    str(net_pnl),
                    _now_iso(),
                ),
            )
            await conn.commit()

    async def get_ledger_totals(
        self, *, date: str | None = None, exchange: str | None = None, mode: BotMode | None = None
    ) -> dict:
        """Sum cash events with Decimal, never cumulative snapshots or SQLite REAL."""
        totals = {
            "funding_received": Decimal("0"),
            "fees_paid": Decimal("0"),
            "realized_price_pnl": Decimal("0"),
        }
        for table, fields in (
            ("funding_log", ("payment",)),
            ("trades", ("fee_paid", "realized_pnl")),
        ):
            clauses, params = [], []
            if date:
                clauses.append("substr(e.timestamp,1,10)=?")
                params.append(date)
            if exchange:
                clauses.append("e.exchange=?")
                params.append(exchange)
            if mode:
                clauses.append("p.mode=?")
                params.append(mode.value)
            where = " WHERE " + " AND ".join(clauses) if clauses else ""
            query = (
                f"SELECT {','.join('e.' + f for f in fields)} FROM {table} e "
                "JOIN positions p ON p.id=e.position_id" + where
            )
            async with self._conn_or_raise().execute(query, params) as cur:
                for row in await cur.fetchall():
                    if table == "funding_log":
                        totals["funding_received"] += Decimal(row[0])
                    else:
                        totals["fees_paid"] += Decimal(row[0])
                        totals["realized_price_pnl"] += Decimal(row[1])
        totals["net_pnl"] = (
            totals["funding_received"] + totals["realized_price_pnl"] - totals["fees_paid"]
        )
        return totals

    async def get_daily_pnl(self, date: str | None = None, mode: BotMode | None = None) -> dict:
        date = date or datetime.now(UTC).strftime("%Y-%m-%d")
        return {"date": date, **await self.get_ledger_totals(date=date, mode=mode)}
