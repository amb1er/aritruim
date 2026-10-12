"""Тесты HealthServer — /health и /metrics endpoints."""

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.health import HealthServer


@pytest.fixture
async def health_client():
    server = HealthServer(host="127.0.0.1", port=0)
    server.bot_mode = "paper"

    async with TestClient(TestServer(server._app)) as client:
        yield client, server


@pytest.mark.asyncio
async def test_health_ok(health_client):
    client, server = health_client
    server.circuit_breaker_open = False
    server.open_pairs = 2

    resp = await client.get("/health")
    assert resp.status == 200
    data = await resp.json()
    assert data["status"] == "ok"
    assert data["open_pairs"] == 2
    assert data["bot_mode"] == "paper"
    assert "uptime_sec" in data
    assert "timestamp" in data


@pytest.mark.asyncio
async def test_health_degraded_on_circuit_breaker(health_client):
    client, server = health_client
    server.circuit_breaker_open = True

    resp = await client.get("/health")
    assert resp.status == 503
    data = await resp.json()
    assert data["status"] == "degraded"
    assert data["circuit_breaker_open"] is True


@pytest.mark.asyncio
async def test_metrics_endpoint(health_client):
    client, server = health_client
    server.open_pairs = 3
    server.net_pnl = 0.00123
    server.total_funding_received = 0.00456
    server.total_fees_paid = 0.00333
    server.error_count = 1

    resp = await client.get("/metrics")
    assert resp.status == 200
    data = await resp.json()
    assert data["open_pairs"] == 3
    assert data["net_pnl"] == 0.00123
    assert data["funding_received"] == 0.00456
    assert data["fees_paid"] == 0.00333
    assert data["error_count"] == 1


@pytest.mark.asyncio
async def test_update_from_positions(health_client):
    client, server = health_client

    def make_pos(net_pnl, funding, fees):
        pos = MagicMock()
        pos.net_pnl = Decimal(str(net_pnl))
        pos.funding_received = Decimal(str(funding))
        pos.fees_paid = Decimal(str(fees))
        return pos

    positions = [
        make_pos(0.001, 0.003, 0.002),
        make_pos(0.002, 0.004, 0.002),
    ]
    server.update_from_positions(positions)

    assert server.open_pairs == 2
    assert abs(server.net_pnl - 0.003) < 1e-9
    assert abs(server.total_funding_received - 0.007) < 1e-9
    assert abs(server.total_fees_paid - 0.004) < 1e-9


@pytest.mark.asyncio
async def test_health_uptime_increases(health_client):
    import asyncio

    client, server = health_client

    resp1 = await client.get("/health")
    data1 = await resp1.json()
    await asyncio.sleep(0.05)
    resp2 = await client.get("/health")
    data2 = await resp2.json()

    # uptime_sec может совпадать (целые секунды) — проверяем только что не отрицательный
    assert data1["uptime_sec"] >= 0
    assert data2["uptime_sec"] >= data1["uptime_sec"]
