import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest

import kalshi_mcp.client as client_module
from kalshi_mcp.client import BASE_URL, KalshiClient, KalshiError, retry_delay


pytestmark = pytest.mark.anyio


async def test_current_market_preserves_values_and_retrieval_time(market):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"market": market})

    before = datetime.now(UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await KalshiClient(http).get_market(market["ticker"])
    assert result.source == "live"
    assert before <= result.retrieved_at <= datetime.now(UTC)
    assert result.market.updated_time < result.retrieved_at
    assert result.market.yes_bid_dollars == "0.420000"
    assert result.market.volume_fp == "123.50"
    assert result.market.settlement_value_dollars is None
    assert result.market.settlement_ts is None
    assert str(requests[0].url) == f"{BASE_URL}/markets/{market['ticker']}"
    assert len(requests) == 1


async def test_historical_lookup_after_live_404(market):
    paths = []
    market.update(
        status="finalized", result="no", settlement_value_dollars="0.0000",
        settlement_ts="2026-11-08T13:00:00Z",
    )

    def handle(request):
        paths.append(request.url.path)
        if "/historical/markets/" in request.url.path:
            return httpx.Response(200, json={"market": market})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await KalshiClient(http).get_market(market["ticker"])
    assert result.source == "historical"
    assert result.market.settlement_value_dollars == "0.0000"
    assert result.market.settlement_ts == datetime(2026, 11, 8, 13, tzinfo=UTC)
    assert paths == [
        f"/trade-api/v2/markets/{market['ticker']}",
        f"/trade-api/v2/historical/markets/{market['ticker']}",
    ]


async def test_missing_market_checks_both_routes():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(KalshiError) as error:
            await KalshiClient(http).get_market("MISSING")
    assert error.value.code == "not_found"
    assert error.value.source == "historical"
    assert len(requests) == 2


@pytest.mark.parametrize("status", [400, 401, 403, 500, 502, 503, 504, 429])
async def test_http_failures_never_trigger_historical_lookup(status):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status, headers={"Retry-After": "0"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(KalshiError) as error:
            await KalshiClient(http).get_market("EXAMPLE")
    assert error.value.status_code == status
    assert error.value.source == "live"
    assert error.value.code != "not_found"
    assert len(requests) == (2 if status in {429, 502, 503, 504} else 1)
    assert all("historical" not in request.url.path for request in requests)


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ConnectError])
async def test_transport_failure_retries_once_without_historical_lookup(failure):
    requests = []

    def handle(request):
        requests.append(request)
        raise failure("temporary failure", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(KalshiError) as error:
            await KalshiClient(http).get_market("EXAMPLE")
    assert error.value.code == ("timeout" if failure is httpx.ReadTimeout else "connection_error")
    assert len(requests) == 2
    assert all("historical" not in request.url.path for request in requests)


async def test_temporary_http_failure_can_recover(market):
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"market": market})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await KalshiClient(http).get_market(market["ticker"])
    assert result.source == "live"
    assert len(requests) == 2


@pytest.mark.parametrize("mutation", ["missing_rules", "numeric_price", "bad_price", "wrong_ticker", "bad_date"])
async def test_invalid_market_is_an_error(market, mutation):
    if mutation == "missing_rules":
        del market["rules_primary"]
    elif mutation == "numeric_price":
        market["yes_bid_dollars"] = 0.42
    elif mutation == "bad_price":
        market["yes_bid_dollars"] = "NaN"
    elif mutation == "wrong_ticker":
        market["ticker"] = "OTHER"
    else:
        market["updated_time"] = "yesterday"

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"market": market}))
    ) as http:
        with pytest.raises(KalshiError, match="invalid_response"):
            await KalshiClient(http).get_market("EXAMPLE-26NOV09-YES")


@pytest.mark.parametrize("body", [b"not json", b"{}", b'{"market":null}'])
async def test_malformed_envelope_is_an_error(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    ) as http:
        with pytest.raises(KalshiError, match="invalid_response"):
            await KalshiClient(http).get_market("EXAMPLE")


async def test_retry_after_larger_than_budget_returns_rate_limit():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(429, headers={"Retry-After": "3600"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(KalshiError, match="rate_limited"):
            await KalshiClient(http).get_market("EXAMPLE")
    assert len(requests) == 1


async def test_retry_after_http_date():
    retry_at = datetime.now(UTC) + timedelta(seconds=60)
    response = httpx.Response(429, headers={"Retry-After": format_datetime(retry_at)})
    assert 58 <= retry_delay(response) <= 60


async def test_tool_deadline_cancels_request(monkeypatch):
    monkeypatch.setattr(client_module, "TOOL_TIMEOUT_SECONDS", 0.02)
    cancelled = asyncio.Event()

    async def handle(request):
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(KalshiError, match="timeout"):
            await KalshiClient(http).get_market("EXAMPLE")
    assert cancelled.is_set()


async def test_concurrency_bound(market):
    active = 0
    peak = 0

    async def handle(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            return httpx.Response(200, json={"market": market})
        finally:
            active -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = KalshiClient(http)
        await asyncio.gather(*(client.get_market(market["ticker"]) for _ in range(8)))
    assert peak == 4
