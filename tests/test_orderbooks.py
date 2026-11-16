import asyncio
from datetime import UTC, datetime

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

import kalshi_mcp.client as client_module
from kalshi_mcp.client import KalshiClient
from kalshi_mcp.tools import get_orderbooks


pytestmark = pytest.mark.anyio


def book(ticker, yes=None, no=None):
    return {"ticker": ticker, "orderbook_fp": {
        "yes_dollars": yes if yes is not None else [["0.420000", "13.50"]],
        "no_dollars": no if no is not None else [],
    }}


async def test_books_keep_input_order_precision_and_best_levels(market):
    requests = []

    def handle(request):
        requests.append(request)
        if request.url.path.endswith("/orderbooks"):
            assert request.url.params.get_list("tickers") == ["A", "B"]
            assert "depth" not in request.url.params
            return httpx.Response(200, json={"orderbooks": [
                book("B", []), book("A", [["0.10", "1.00"], ["0.420000", "13.50"]]),
            ]})
        return httpx.Response(200, json={"market": {
            **market, "ticker": request.url.path.rsplit("/", 1)[1],
        }})

    before = datetime.now(UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await get_orderbooks(KalshiClient(http), ["A", "B"], 1)
    first, empty = result.results
    assert [item.ticker for item in result.results] == ["A", "B"]
    assert first.orderbook_fp.yes_dollars == [("0.420000", "13.50")]
    assert first.yes_truncated and not first.no_truncated
    assert before <= first.retrieved_at <= datetime.now(UTC)
    assert empty.orderbook_fp.yes_dollars == []
    assert empty.error is None and not empty.yes_truncated
    assert len(requests) == 3


async def test_lookup_failures_and_archived_markets_are_excluded(market):
    paths = []

    def handle(request):
        paths.append(request.url.path)
        ticker = request.url.path.rsplit("/", 1)[1]
        if ticker == "orderbooks":
            assert request.url.params.get_list("tickers") == ["GOOD"]
            return httpx.Response(200, json={"orderbooks": [book("GOOD")]})
        if ticker == "MISSING" or (ticker == "ARCHIVED" and "/historical/" not in request.url.path):
            return httpx.Response(404)
        if ticker == "FAILED":
            return httpx.Response(500)
        return httpx.Response(200, json={"market": {**market, "ticker": ticker}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await get_orderbooks(KalshiClient(http), ["MISSING", "GOOD", "ARCHIVED", "FAILED"])
    assert [item.error.code if item.error else None for item in result.results] == [
        "not_found", None, "orderbook_unavailable", "upstream_http_error",
    ]
    assert result.results[2].error.source == "historical"
    assert not any("historical" in path and path.endswith("FAILED") for path in paths)


@pytest.mark.parametrize("bad", [
    {"yes_dollars": None, "no_dollars": []},
    {"yes_dollars": [[0.42, "1.0"]], "no_dollars": []},
    {"yes_dollars": [["0.42", "0"]], "no_dollars": []},
    {"yes_dollars": [["1.01", "1"]], "no_dollars": []},
    {"yes_dollars": [["0.5", "1"], ["0.4", "1"]], "no_dollars": []},
    {"yes_dollars": [["0.5", "1"], ["0.50", "1"]], "no_dollars": []},
    {"yes_dollars": [["0.5"]], "no_dollars": []},
    {"yes_dollars": []},
])
async def test_bad_entry_preserves_other_books(market, bad):
    def handle(request):
        ticker = request.url.path.rsplit("/", 1)[1]
        if ticker == "orderbooks":
            return httpx.Response(200, json={"orderbooks": [
                book("GOOD"), {"ticker": "BAD", "orderbook_fp": bad},
            ]})
        return httpx.Response(200, json={"market": {**market, "ticker": ticker}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await get_orderbooks(KalshiClient(http), ["BAD", "GOOD", "OMITTED"])
    assert result.results[0].error.code == "invalid_response"
    assert result.results[1].error is None
    assert result.results[2].error.code == "missing_result"


@pytest.mark.parametrize("payload", [{}, {"orderbooks": None}, {"orderbooks": [None]},
                                     {"orderbooks": [book("UNREQUESTED")]}])
async def test_unassignable_batch_response_fails_explicitly(market, payload):
    def handle(request):
        if request.url.path.endswith("/orderbooks"):
            return httpx.Response(200, json=payload)
        return httpx.Response(200, json={"market": {**market, "ticker": "A"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await get_orderbooks(KalshiClient(http), ["A"])
    assert result.results[0].error.code == "invalid_response"


@pytest.mark.parametrize("arguments", [
    {"tickers": []}, {"tickers": ["A"] * 11}, {"tickers": ["A", "A"]},
    {"tickers": ["../bad"]}, {"tickers": "A"}, {"tickers": [1]},
    *[{"tickers": ["A"], "depth": value} for value in [0, 101, True, "20", None]],
])
async def test_invalid_arguments_send_no_requests(arguments):
    def handle(request):
        pytest.fail("Invalid input sent an HTTP request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(ToolError, match="invalid_arguments"):
            await get_orderbooks(KalshiClient(http), **arguments)


@pytest.mark.parametrize("status", [429, 502, 503, 504])
@pytest.mark.parametrize("recover", [True, False])
async def test_batch_retries_once(market, status, recover):
    attempts = 0

    def handle(request):
        nonlocal attempts
        if request.url.path.endswith("/orderbooks"):
            attempts += 1
            if recover and attempts == 2:
                return httpx.Response(200, json={"orderbooks": [book("A")]})
            return httpx.Response(status, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"market": {**market, "ticker": "A"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await get_orderbooks(KalshiClient(http), ["A"])
    assert attempts == 2
    if recover:
        assert result.results[0].error is None
    else:
        assert result.results[0].error.code == ("rate_limited" if status == 429 else "upstream_http_error")


async def test_deadline_cancels_lookups_and_preserves_completed_failures(monkeypatch):
    monkeypatch.setattr(client_module, "TOOL_TIMEOUT_SECONDS", 0.03)
    active = 0

    async def handle(request):
        nonlocal active
        if request.url.path.endswith("/FAILED"):
            return httpx.Response(500)
        active += 1
        try:
            await asyncio.sleep(10)
        finally:
            active -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await get_orderbooks(KalshiClient(http), ["FAILED", "SLOW"])
    assert result.results[0].error.code == "upstream_http_error"
    assert result.results[1].error.code == "timeout"
    assert active == 0


async def test_concurrent_tools_share_four_request_limit(market):
    active = peak = 0

    async def handle(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.005)
            if request.url.path.endswith("/orderbooks"):
                return httpx.Response(200, json={"orderbooks": [
                    book(ticker) for ticker in request.url.params.get_list("tickers")
                ]})
            return httpx.Response(200, json={"market": {
                **market, "ticker": request.url.path.rsplit("/", 1)[1],
            }})
        finally:
            active -= 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = KalshiClient(http)
        results = await asyncio.gather(
            get_orderbooks(client, [f"A{i}" for i in range(10)]),
            get_orderbooks(client, [f"B{i}" for i in range(10)], 100),
            client.get_market("OTHER"),
        )
    assert peak == 4 and active == 0
    assert all(item.error is None for result in results[:2] for item in result.results)


async def test_duplicate_entry_fails_only_affected_ticker(market):
    def handle(request):
        ticker = request.url.path.rsplit("/", 1)[1]
        if ticker == "orderbooks":
            return httpx.Response(200, json={"orderbooks": [book("A"), book("A"), book("B")]})
        return httpx.Response(200, json={"market": {**market, "ticker": ticker}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await get_orderbooks(KalshiClient(http), ["A", "B"])
    assert result.results[0].error.code == "invalid_response"
    assert result.results[1].error is None


@pytest.mark.parametrize("mode", ["timeout", "rate_limit", "cancel"])
async def test_batch_deadline_and_cancellation(market, monkeypatch, mode):
    monkeypatch.setattr(client_module, "TOOL_TIMEOUT_SECONDS", 0.03)
    started = asyncio.Event()
    active = attempts = 0

    async def handle(request):
        nonlocal active, attempts
        if request.url.path.endswith("/orderbooks"):
            attempts += 1
            started.set()
            if mode == "rate_limit":
                return httpx.Response(429, headers={"Retry-After": "60"})
            active += 1
            try:
                await asyncio.sleep(10)
            finally:
                active -= 1
        return httpx.Response(200, json={"market": {**market, "ticker": "A"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        task = asyncio.create_task(get_orderbooks(KalshiClient(http), ["A"]))
        if mode == "cancel":
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.results[0].error.code == ("timeout" if mode == "timeout" else "rate_limited")
    assert attempts == 1 and active == 0


async def test_no_batch_when_all_lookups_fail():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await get_orderbooks(KalshiClient(http), ["A", "B"])
    assert all(item.error.code == "not_found" for item in result.results)
    assert len(paths) == 4
    assert not any(path.endswith("/orderbooks") for path in paths)
