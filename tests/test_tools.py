import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from kalshi_mcp.client import KalshiClient
from kalshi_mcp.tools import get_event, get_market, get_series, list_events, list_series


pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("ticker", ["", " ", "../markets", "X?status=open", "X/Y", "X\n", "X" * 201, 123, None])
async def test_invalid_ticker_never_sends_request(ticker):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(ToolError, match="invalid_arguments"):
            await get_market(ticker, KalshiClient(http))
    assert requests == []


async def test_tool_response_shape_and_null_values(market):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"market": market}))
    ) as http:
        result = await get_market(market["ticker"], KalshiClient(http))
    data = result.model_dump(mode="json")
    assert set(data) == {"source", "retrieved_at", "market"}
    assert data["market"]["rules_primary"] == market["rules_primary"]
    assert data["market"]["rules_secondary"] == market["rules_secondary"]
    assert data["market"]["settlement_value_dollars"] is None
    assert data["market"]["settlement_ts"] is None
    assert data["market"]["yes_bid_dollars"] == "0.420000"


async def test_tool_errors_preserve_failure_kind():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))
    ) as http:
        with pytest.raises(ToolError) as error:
            await get_market("MISSING", KalshiClient(http))
    assert json.loads(str(error.value)) == {
        "code": "not_found", "source": "historical", "status_code": 404,
    }


@pytest.mark.parametrize("arguments", [
    {"limit": 0}, {"limit": 201}, {"limit": True}, {"limit": "50"},
    {"category": ""}, {"category": " "}, {"tags": ""}, {"tags": ["Inflation"]},
    {"after_ticker": ""}, {"after_ticker": "../bad"}, {"after_ticker": 123},
])
async def test_invalid_series_filters_never_send_a_request(arguments):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(ToolError, match="invalid_arguments"):
            await list_series(KalshiClient(http), **arguments)
    assert requests == []


@pytest.mark.parametrize("count,limit", [(3, 1), (2, 2), (0, 50), (201, 200)])
async def test_series_listing_summaries_and_coverage(series, count, limit):
    rows = [{**series, "ticker": f"EXAMPLE{i}"} for i in range(count)]
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"series": rows})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await list_series(KalshiClient(http), category="Economics", limit=limit)
    data = result.model_dump(mode="json")
    assert len(data["series"]) == min(count, limit)
    assert data["total_count"] == count
    assert data["truncated"] is (count > limit)
    assert data["pagination_supported"] is True
    assert data["pagination_mode"] == "local"
    expected_cursor = data["series"][-1]["ticker"] if count > limit else None
    assert data["next_cursor"] == expected_cursor
    assert dict(requests[0].url.params) == {"category": "Economics"}
    assert len(requests) == 1
    if count:
        assert set(data["series"][0]) == {"ticker", "title", "frequency", "category", "categories", "tags"}


async def test_series_listing_defaults_to_fifty(series):
    rows = [{**series, "ticker": f"EXAMPLE{i}"} for i in range(51)]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"series": rows}))
    ) as http:
        result = await list_series(KalshiClient(http))
    assert len(result.series) == 50
    assert result.truncated


async def test_series_pagination_sorts_and_reaches_the_final_page(series):
    rows = [{**series, "ticker": ticker} for ticker in ("GAMMA", "ALPHA", "BETA")]
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"series": rows})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = KalshiClient(http)
        first = await list_series(client, category="Economics", tags="Inflation", limit=2)
        last = await list_series(
            client, category="Economics", tags="Inflation", limit=2,
            after_ticker=first.next_cursor,
        )
    assert [item.ticker for item in first.series] == ["ALPHA", "BETA"]
    assert first.next_cursor == "BETA"
    assert first.truncated
    assert [item.ticker for item in last.series] == ["GAMMA"]
    assert last.next_cursor is None
    assert not last.truncated
    assert first.total_count == last.total_count == 3
    assert len(requests) == 2
    assert all(dict(request.url.params) == {"category": "Economics", "tags": "Inflation"} for request in requests)


@pytest.mark.parametrize("after_ticker,expected", [
    ("BETA", ["GAMMA"]), ("BETWEEN", ["GAMMA"]), ("ZETA", []),
])
async def test_series_continuation_handles_missing_and_exhausted_markers(series, after_ticker, expected):
    rows = [{**series, "ticker": ticker} for ticker in ("ALPHA", "GAMMA")]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"series": rows}))
    ) as http:
        result = await list_series(KalshiClient(http), after_ticker=after_ticker)
    assert [item.ticker for item in result.series] == expected
    assert result.total_count == 2
    assert result.next_cursor is None
    assert not result.truncated


async def test_series_continuation_uses_a_fresh_response(series):
    snapshots = [("ALPHA", "BETA", "GAMMA"), ("AARDVARK", "ALPHA", "GAMMA", "ZETA")]
    requests = []

    def handle(request):
        tickers = snapshots[len(requests)]
        requests.append(request)
        rows = [{**series, "ticker": ticker} for ticker in tickers]
        return httpx.Response(200, json={"series": rows})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = KalshiClient(http)
        first = await list_series(client, limit=2)
        second = await list_series(client, limit=2, after_ticker=first.next_cursor)
    assert first.next_cursor == "BETA"
    assert [item.ticker for item in second.series] == ["GAMMA", "ZETA"]
    assert second.total_count == 4
    assert second.next_cursor is None


async def test_series_detail_tool_wraps_metadata(series):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"series": series}))
    ) as http:
        result = await get_series(series["ticker"], KalshiClient(http))
    data = result.model_dump(mode="json")
    assert set(data) == {"source", "retrieved_at", "series"}
    assert data["series"]["contract_terms_url"] == series["contract_terms_url"]


async def test_invalid_series_ticker_never_sends_a_request():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(ToolError, match="invalid_arguments"):
            await get_series("../bad", KalshiClient(http))
    assert requests == []


@pytest.mark.parametrize("operation", ["get_series", "list_series"])
async def test_series_request_failures_remain_tool_errors(operation):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as http:
        client = KalshiClient(http)
        with pytest.raises(ToolError, match="upstream_http_error"):
            if operation == "get_series":
                await get_series("EXAMPLESERIES", client)
            else:
                await list_series(client)


@pytest.mark.parametrize("arguments", [
    {"series_ticker": "../bad"}, {"tickers": []}, {"tickers": ["../bad"]},
    {"tickers": ["EXAMPLE"] * 101}, {"tickers": "EXAMPLE"},
    {"status": "active"}, {"limit": 0}, {"limit": 201}, {"limit": True},
    {"min_close_ts": -1}, {"min_close_ts": True}, {"min_updated_ts": "123"},
    {"cursor": 123},
])
async def test_invalid_event_filters_never_send_a_request(arguments):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(ToolError, match="invalid_arguments"):
            await list_events(KalshiClient(http), **arguments)
    assert requests == []


@pytest.mark.parametrize("status", ["unopened", "open", "closed", "settled"])
async def test_documented_event_status_filters_are_forwarded(status):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"events": [], "cursor": ""})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await list_events(KalshiClient(http), status=status)
    assert requests[0].url.params["status"] == status
    assert result.events == []


async def test_event_listing_limit_and_ticker_filter_bounds():
    tickers = [f"EXAMPLE{i}" for i in range(100)]
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"events": [], "cursor": ""})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        await list_events(KalshiClient(http), tickers=tickers, limit=200, cursor="")
    assert requests[0].url.params["limit"] == "200"
    assert requests[0].url.params["tickers"] == ",".join(tickers)
    assert requests[0].url.params["cursor"] == ""


async def test_event_detail_missing_metadata_is_null(event, series):
    for field in ("fee_type_override", "fee_multiplier_override", "settlement_sources", "product_metadata"):
        del event[field]

    def handle(request):
        value = {"event": event} if "/events/" in request.url.path else {"series": series}
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await get_event(event["event_ticker"], KalshiClient(http))
    data = result.model_dump(mode="json")
    assert data["event"]["settlement_sources"] is None
    assert data["event"]["fee_type_override"] is None
    assert data["event"]["fee_multiplier_override"] is None
    assert data["series"]["fee_type"] == "quadratic"
    assert data["series_error"] is None


async def test_invalid_event_ticker_never_sends_a_request():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(ToolError, match="invalid_arguments"):
            await get_event("../bad", KalshiClient(http))
    assert requests == []


@pytest.mark.parametrize("operation", ["get_event", "list_events"])
async def test_event_request_failures_remain_tool_errors(operation):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(500))
    ) as http:
        client = KalshiClient(http)
        with pytest.raises(ToolError, match="upstream_http_error"):
            if operation == "get_event":
                await get_event("EXAMPLE", client)
            else:
                await list_events(client)
