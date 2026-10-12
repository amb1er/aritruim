"""
Hyperliquid DEX адаптер.

REST API: https://api.hyperliquid.xyz/info  (POST, JSON body)
WS:       wss://api.hyperliquid.xyz/ws
SDK:      hyperliquid-python-sdk

Rate limits: ~1200 req/min на REST (неофициально).
Funding interval: 1 час (не 8ч как у CEX).

Торговля через SDK:
  from hyperliquid.exchange import Exchange
  exchange.market_open(coin, is_buy, sz, slippage)
  exchange.market_close(coin, slippage=slippage)
  exchange.cancel(coin, oid)

Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api
"""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import aiohttp
import structlog
import websockets

from src.adapters.base import ExchangeAdapter
from src.config import Settings
from src.models import (
    Balance,
    FeeRate,
    FundingRate,
    Orderbook,
    OrderbookLevel,
    OrderRequest,
    OrderResult,
    OrderSide,
    Position,
)

log = structlog.get_logger(__name__)

FUNDING_INTERVAL_HOURS = 1
# Максимальный slippage при закрытии позиции по рынку (0.5%)
_CLOSE_SLIPPAGE = 0.005
# Таймаут ожидания подтверждения ордера (секунды)
_ORDER_TIMEOUT_SEC = 15
_WS_OPEN_TIMEOUT_SEC = 10  # таймаут на установку WS соединения
_WS_PING_TIMEOUT_SEC = 20  # таймаут ожидания pong после ping


class HyperliquidAdapter(ExchangeAdapter):
    funding_model = "hourly_history"
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._adapter_name = "hyperliquid"
        self._dex = ""
        self._session: aiohttp.ClientSession | None = None
        self._rest_url = settings.hyperliquid_rest_url
        self._ws_url = settings.hyperliquid_ws_url
        if settings.hyperliquid_testnet:
            self._rest_url = self._rest_url.replace(
                "api.hyperliquid.xyz", "api.hyperliquid-testnet.xyz"
            )
            self._ws_url = self._ws_url.replace(
                "api.hyperliquid.xyz", "api.hyperliquid-testnet.xyz"
            )
        # SDK exchange инициализируется лениво при первом ордере
        self._exchange = None
        self._ws_task: asyncio.Task | None = None

    @property
    def name(self) -> str:
        return self._adapter_name

    def _api_symbol(self, symbol: str) -> str:
        if not self._dex or symbol.startswith(f"{self._dex}:"):
            return symbol
        return f"{self._dex}:{symbol}"

    def _public_symbol(self, symbol: str) -> str:
        return symbol.removeprefix(f"{self._dex}:") if self._dex else symbol

    def _dex_payload(self, request_type: str) -> dict:
        payload = {"type": request_type}
        if self._dex:
            payload["dex"] = self._dex
        return payload

    # ── Жизненный цикл ────────────────────────────────────────────────────────

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"Content-Type": "application/json"},
            connector=aiohttp.TCPConnector(resolver=aiohttp.ThreadedResolver()),
        )
        log.info("hyperliquid.connected", url=self._rest_url)

    async def disconnect(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None
        if self._session:
            await self._session.close()
            self._session = None
        log.info("hyperliquid.disconnected")

    def _get_exchange(self):
        """
        Лениво инициализирует hyperliquid-python-sdk Exchange.
        SDK синхронный — используем в executor через asyncio.to_thread().
        Docs: https://github.com/hyperliquid-dex/hyperliquid-python-sdk
        """
        if self._exchange is None:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            from hyperliquid.utils import constants

            base_url = (
                constants.TESTNET_API_URL
                if self._settings.hyperliquid_testnet
                else constants.MAINNET_API_URL
            )
            self._exchange = Exchange(
                wallet=Account.from_key(self._settings.hyperliquid_private_key),
                base_url=base_url,
                account_address=self._settings.hyperliquid_wallet_address or None,
                perp_dexs=[self._dex] if self._dex else None,
                timeout=10,
            )
        return self._exchange

    # ── Внутренний HTTP запрос ────────────────────────────────────────────────

    async def _post(self, payload: dict) -> dict | list:
        assert self._session, "Вызови connect() перед использованием адаптера"
        async with self._session.post(self._rest_url, json=payload) as resp:
            resp.raise_for_status()
            return await resp.json()

    # ── Рыночные данные ───────────────────────────────────────────────────────

    async def get_symbols(self) -> list[str]:
        """
        Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals
        """
        data = await self._post(self._dex_payload("meta"))
        universe: list[dict] = data.get("universe", [])
        return [
            self._public_symbol(asset["name"])
            for asset in universe
            if not asset.get("isDelisted", False)
        ]

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        rates = await self.get_funding_rates()
        for rate in rates:
            if rate.symbol == symbol:
                return rate
        raise ValueError(f"Symbol {symbol} not found on Hyperliquid")

    async def get_funding_rates(self) -> list[FundingRate]:
        """
        Batch через metaAndAssetCtxs.
        assetCtx.funding — ставка per hour (строка).
        """
        data = await self._post(self._dex_payload("metaAndAssetCtxs"))
        meta, asset_ctxs = data[0], data[1]
        universe: list[dict] = meta.get("universe", [])
        now = datetime.now(UTC)

        result: list[FundingRate] = []
        for asset_info, ctx in zip(universe, asset_ctxs, strict=False):
            if asset_info.get("isDelisted", False):
                continue
            result.append(
                FundingRate(
                    exchange=self.name,
                    symbol=self._public_symbol(asset_info["name"]),
                    rate=Decimal(ctx.get("funding", "0")),
                    interval_hours=FUNDING_INTERVAL_HOURS,
                    next_funding_ts=now,
                    timestamp=now,
                )
            )
        log.debug("hyperliquid.funding_rates_fetched", count=len(result))
        return result

    async def get_orderbook(self, symbol: str, depth: int = 20) -> Orderbook:
        """
        Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#l-2-snapshot
        """
        data = await self._post({"type": "l2Book", "coin": self._api_symbol(symbol)})
        levels: list = data.get("levels", [[], []])

        def parse_levels(raw: list[dict]) -> list[OrderbookLevel]:
            return [
                OrderbookLevel(price=Decimal(lvl["px"]), size=Decimal(lvl["sz"]))
                for lvl in raw[:depth]
            ]

        return Orderbook(
            exchange=self.name,
            symbol=symbol,
            bids=parse_levels(levels[0]),
            asks=parse_levels(levels[1]),
        )

    async def get_mark_price(self, symbol: str) -> Decimal:
        """
        Mid-price из стакана как прокси mark price.
        В M4 заменить на type=allMids для точного mark price.
        """
        ob = await self.get_orderbook(symbol, depth=1)
        if ob.bids and ob.asks:
            return (ob.bids[0].price + ob.asks[0].price) / Decimal("2")
        if ob.bids:
            return ob.bids[0].price
        if ob.asks:
            return ob.asks[0].price
        raise ValueError(f"Empty orderbook for {symbol} on {self.name}")

    async def get_fee_rate(self, symbol: str) -> FeeRate:
        """
        Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#user-fees
        """
        if not self._settings.hyperliquid_wallet_address:
            return FeeRate(self.name, symbol, Decimal("0.00015"), Decimal("0.00045"))
        data = await self._post(
            {
                "type": "userFees",
                "user": self._settings.hyperliquid_wallet_address,
            }
        )
        return FeeRate(
            exchange=self.name,
            symbol=symbol,
            maker=Decimal(str(data.get("userAddRate", "0.00015"))),
            taker=Decimal(str(data.get("userCrossRate", "0.00045"))),
        )

    async def get_funding_price(self, symbol: str) -> Decimal:
        meta, contexts = await self._post(self._dex_payload("metaAndAssetCtxs"))
        for asset, ctx in zip(meta["universe"], contexts, strict=True):
            if self._public_symbol(asset["name"]) == symbol:
                return Decimal(ctx["oraclePx"])
        raise ValueError(f"No funding oracle price for {symbol}")

    async def get_funding_history(self, symbol: str, lookback_hours: int = 24) -> list[FundingRate]:
        """
        История funding rates для spike detection.
        Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#funding-history
        """
        import time

        start_time = int((time.time() - lookback_hours * 3600) * 1000)
        data = await self._post(
            {
                "type": "fundingHistory",
                "coin": self._api_symbol(symbol),
                "startTime": start_time,
            }
        )
        history: list[dict] = data if isinstance(data, list) else []
        return [
            FundingRate(
                exchange=self.name,
                symbol=symbol,
                rate=Decimal(str(item.get("fundingRate", "0"))),
                interval_hours=FUNDING_INTERVAL_HOURS,
                next_funding_ts=datetime.fromtimestamp(item.get("time", 0) / 1000, tz=UTC),
                timestamp=datetime.fromtimestamp(item.get("time", 0) / 1000, tz=UTC),
            )
            for item in history
        ]

    # ── Аккаунт ───────────────────────────────────────────────────────────────

    async def get_balance(self) -> Balance:
        if not self._settings.hyperliquid_wallet_address:
            return Balance(self.name, Decimal("0"), Decimal("0"))
        data = await self._post(
            {
                "type": "clearinghouseState",
                "user": self._settings.hyperliquid_wallet_address,
                "dex": self._dex,
            }
        )
        margin_summary = data.get("marginSummary", {})
        total = Decimal(str(margin_summary.get("accountValue", "0")))
        available = Decimal(str(data.get("withdrawable", "0")))
        return Balance(self.name, total, available)

    async def get_positions(self) -> list[Position]:
        if not self._settings.hyperliquid_wallet_address:
            return []
        data = await self._post(
            {
                "type": "clearinghouseState",
                "user": self._settings.hyperliquid_wallet_address,
                "dex": self._dex,
            }
        )
        positions = []
        for p in data.get("assetPositions", []):
            pos = p.get("position", {})
            size = Decimal(str(pos.get("szi", "0")))
            if size == 0:
                continue
            positions.append(
                Position(
                    exchange=self.name,
                    symbol=self._public_symbol(pos.get("coin", "")),
                    side=OrderSide.LONG if size > 0 else OrderSide.SHORT,
                    size=abs(size),
                    entry_price=Decimal(str(pos.get("entryPx", "0"))),
                    unrealized_pnl=Decimal(str(pos.get("unrealizedPnl", "0"))),
                )
            )
        return positions

    # ── Торговля ──────────────────────────────────────────────────────────────

    async def place_order(self, order: OrderRequest) -> OrderResult:
        """
        Market ордер через hyperliquid-python-sdk.
        SDK синхронный — оборачиваем в asyncio.to_thread().

        SDK возвращает:
          {"status": "ok", "response": {"type": "order", "data": {"statuses": [...]}}}
        Каждый статус: {"filled": {"totalSz": "0.01", "avgPx": "100000", "oid": 123}}

        Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint
        """
        exchange = self._get_exchange()
        is_buy = order.side == OrderSide.LONG

        # sz = notional / price; берём mid из стакана
        ob = await self.get_orderbook(order.symbol, depth=1)
        if not ob.asks and not ob.bids:
            return self._error_result(order, "Empty orderbook")
        mid = (
            (ob.bids[0].price + ob.asks[0].price) / Decimal("2")
            if ob.bids and ob.asks
            else (ob.asks[0].price if ob.asks else ob.bids[0].price)
        )
        # SDK requires float; Decimal division is done first to preserve precision
        sz = float(order.quantity if order.quantity is not None else order.notional_usd / mid)

        from hyperliquid.utils.types import Cloid

        cloid = Cloid.from_str("0x" + order.client_id) if order.client_id else None
        try:
            if order.is_reduce_only:
                pending = asyncio.to_thread(
                    exchange.market_close,
                    self._api_symbol(order.symbol),
                    sz=sz,
                    slippage=float(self._settings.max_slippage),
                    cloid=cloid,
                )
            else:
                pending = asyncio.to_thread(
                    exchange.market_open,
                    self._api_symbol(order.symbol),
                    is_buy,
                    sz,
                    slippage=float(self._settings.max_slippage),
                    cloid=cloid,
                )
            result = await asyncio.wait_for(pending, timeout=_ORDER_TIMEOUT_SEC)
        except TimeoutError:
            return self._error_result(order, f"Order timeout after {_ORDER_TIMEOUT_SEC}s")
        except Exception as e:
            return self._error_result(order, str(e))

        return self._parse_sdk_result(order, result)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        Отмена ордера через SDK.
        Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint#cancel-order
        """
        exchange = self._get_exchange()
        try:
            result = await asyncio.to_thread(
                exchange.cancel, self._api_symbol(symbol), int(order_id)
            )
            return result.get("status") == "ok"
        except Exception as e:
            log.error("hyperliquid.cancel_failed", symbol=symbol, order_id=order_id, error=str(e))
            return False

    async def close_position(self, symbol: str, side: str) -> OrderResult:
        """
        Закрытие позиции по рынку через market_close.
        Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint#market-close
        """
        exchange = self._get_exchange()
        fake_req = OrderRequest(
            exchange=self.name,
            symbol=symbol,
            side=OrderSide.SHORT if side == "long" else OrderSide.LONG,
            notional_usd=Decimal("0"),
            leverage=Decimal("1"),
            is_reduce_only=True,
        )
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    exchange.market_close,
                    self._api_symbol(symbol),
                    slippage=_CLOSE_SLIPPAGE,
                ),
                timeout=_ORDER_TIMEOUT_SEC,
            )
        except TimeoutError:
            return self._error_result(fake_req, f"Close timeout after {_ORDER_TIMEOUT_SEC}s")
        except Exception as e:
            return self._error_result(fake_req, str(e))

        return self._parse_sdk_result(fake_req, result)

    # ── WebSocket ─────────────────────────────────────────────────────────────

    async def subscribe_funding(self, callback: Callable[[FundingRate], None]) -> None:
        """
        Подписка на funding updates через WS.
        Hyperliquid шлёт allMids каждую секунду; funding обновляется раз в час.
        Используем polling через REST для надёжности (subscribe_funding — WS фасад).

        WS формат подписки:
          {"method": "subscribe", "subscription": {"type": "activeAssetCtx", "coin": "BTC"}}
        Docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket
        """
        if self._ws_task and not self._ws_task.done():
            log.warning("hyperliquid.ws_already_subscribed")
            return
        self._ws_task = asyncio.create_task(self._ws_funding_loop(callback), name="hl_ws_funding")

    async def _ws_funding_loop(self, callback: Callable[[FundingRate], None]) -> None:
        while True:
            try:
                async with websockets.connect(
                    self._ws_url,
                    open_timeout=_WS_OPEN_TIMEOUT_SEC,
                    ping_timeout=_WS_PING_TIMEOUT_SEC,
                ) as ws:
                    symbols = await self.get_symbols()
                    for symbol in symbols:
                        await ws.send(
                            json.dumps(
                                {
                                    "method": "subscribe",
                                    "subscription": {
                                        "type": "activeAssetCtx",
                                        "coin": self._api_symbol(symbol),
                                    },
                                }
                            )
                        )
                    log.info(
                        "hyperliquid.ws_funding_subscribed",
                        exchange=self.name,
                        symbols=len(symbols),
                    )

                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            await self._handle_ws_funding_msg(msg, callback)
                        except Exception as e:
                            log.warning("hyperliquid.ws_parse_error", error=str(e))
            except Exception as e:
                log.warning("hyperliquid.ws_disconnected", error=str(e))
                await asyncio.sleep(5)  # reconnect delay

    async def _handle_ws_funding_msg(
        self, msg: dict, callback: Callable[[FundingRate], None]
    ) -> None:
        channel = msg.get("channel", "")
        if channel != "activeAssetCtx":
            return
        data = msg.get("data", {})
        coin = self._public_symbol(data.get("coin", ""))
        ctx = data.get("ctx", {})
        funding = ctx.get("funding")
        if not coin or funding is None:
            return
        now = datetime.now(UTC)
        callback(
            FundingRate(
                exchange=self.name,
                symbol=coin,
                rate=Decimal(str(funding)),
                interval_hours=FUNDING_INTERVAL_HOURS,
                next_funding_ts=now,
                timestamp=now,
            )
        )

    # ── Утилиты ───────────────────────────────────────────────────────────────

    def _parse_sdk_result(self, order: OrderRequest, sdk_response: dict) -> OrderResult:
        """
        Парсит ответ SDK в OrderResult.
        SDK response: {"status": "ok", "response": {"type": "order", "data": {"statuses": [...]}}}
        """
        if not isinstance(sdk_response, dict):
            return self._error_result(order, "Unknown order response")
        if sdk_response.get("status") != "ok":
            error = str(sdk_response.get("response", "unknown error"))
            return self._error_result(order, error)

        statuses = sdk_response.get("response", {}).get("data", {}).get("statuses", [{}])
        status = statuses[0] if statuses else {}
        filled = status.get("filled", {})

        if not filled:
            # Ордер мог быть resting (не market) — для нас это ошибка
            return self._error_result(order, f"Order not filled: {status}")

        total_sz = Decimal(str(filled.get("totalSz", "0")))
        avg_px = Decimal(str(filled.get("avgPx", "0")))
        oid = str(filled.get("oid", ""))
        if total_sz <= 0 or avg_px <= 0 or not oid:
            return self._error_result(order, "Unknown fill details")

        # Use default taker fee; real fee is available via get_fee_rate() if wallet is set
        fee_rate = Decimal("0.00045")
        fee_paid = total_sz * avg_px * fee_rate

        return OrderResult(
            exchange=self.name,
            symbol=order.symbol,
            order_id=oid,
            side=order.side,
            filled_size=total_sz,
            avg_price=avg_px,
            fee_paid=fee_paid,
            success=True,
        )

    @staticmethod
    def _error_result(order: OrderRequest, error: str) -> OrderResult:
        return OrderResult(
            exchange=order.exchange,
            symbol=order.symbol,
            order_id="",
            side=order.side,
            filled_size=Decimal("0"),
            avg_price=Decimal("0"),
            fee_paid=Decimal("0"),
            success=False,
            error=error,
            uncertain=any(
                w in error.lower() for w in ("timeout", "connection", "unknown", "resting")
            ),
        )
