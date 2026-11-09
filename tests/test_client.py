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


async def test_series_details_keep_contract_and_settlement_context(series):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"series": series})

    before = datetime.now(UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await KalshiClient(http).get_series(series["ticker"])
    assert str(requests[0].url) == f"{BASE_URL}/series/{series['ticker']}"
    assert result.source == "live"
    assert before <= result.retrieved_at <= datetime.now(UTC)
    assert result.series.contract_url == series["contract_url"]
    assert result.series.contract_terms_url == series["contract_terms_url"]
    assert result.series.settlement_sources[0].name == "Example Agency"
    assert result.series.settlement_sources[0].url == "https://example.com/data"
    assert result.series.additional_prohibitions == ["Example restriction"]
    assert result.series.fee_type == "quadratic"
    assert result.series.fee_multiplier == 1.0
    assert result.series.last_updated_ts < result.retrieved_at
    assert result.series.categories == ["Economics", "Climate"]


async def test_series_missing_optional_metadata_is_null(series):
    for key in ("contract_url", "contract_terms_url", "fee_type", "fee_multiplier", "settlement_sources"):
        del series[key]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"series": series}))
    ) as http:
        result = await KalshiClient(http).get_series(series["ticker"])
    data = result.model_dump(mode="json")["series"]
    assert all(data[key] is None for key in (
        "contract_url", "contract_terms_url", "fee_type", "fee_multiplier", "settlement_sources",
    ))


async def test_missing_series_has_no_historical_fallback():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(KalshiError, match="not_found") as error:
            await KalshiClient(http).get_series("MISSING")
    assert paths == ["/trade-api/v2/series/MISSING"]
    assert error.value.source == "live"


async def test_series_listing_forwards_only_supported_filters(series):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"series": [series]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await KalshiClient(http).list_series("Economics", "Inflation,Jobs & Wages")
    assert requests[0].url.path == "/trade-api/v2/series"
    assert dict(requests[0].url.params) == {"category": "Economics", "tags": "Inflation,Jobs & Wages"}
    assert result.total_count == 1
    assert result.series[0].ticker == series["ticker"]
    assert result.series[0].categories == ["Economics", "Climate"]


async def test_empty_series_listing_is_successful():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"series": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await KalshiClient(http).list_series()
    assert dict(requests[0].url.params) == {}
    assert result.series == []
    assert result.total_count == 0


@pytest.mark.parametrize("body", [{}, {"series": None}, {"series": {}}, {"series": [{}]}])
async def test_malformed_series_listing_is_an_error(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as http:
        with pytest.raises(KalshiError, match="invalid_response"):
            await KalshiClient(http).list_series()


@pytest.mark.parametrize("mutation", ["wrong_ticker", "bad_source", "bad_fee"])
async def test_malformed_series_details_are_an_error(series, mutation):
    if mutation == "wrong_ticker":
        series["ticker"] = "OTHER"
    elif mutation == "bad_source":
        series["settlement_sources"] = "Example Agency"
    else:
        series["fee_multiplier"] = "free"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"series": series}))
    ) as http:
        with pytest.raises(KalshiError, match="invalid_response"):
            await KalshiClient(http).get_series("EXAMPLESERIES")


@pytest.mark.parametrize("operation", ["get_series", "list_series"])
async def test_series_tools_use_existing_retry_budget(operation, series):
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        value = series if operation == "get_series" else [series]
        return httpx.Response(200, json={"series": value})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = KalshiClient(http)
        if operation == "get_series":
            result = await client.get_series(series["ticker"])
        else:
            result = await client.list_series()
    assert result.source == "live"
    assert len(requests) == 2
    assert requests[0].url == requests[1].url


@pytest.mark.parametrize("operation", ["get_series", "list_series"])
async def test_series_tools_keep_the_tool_deadline(operation, monkeypatch):
    monkeypatch.setattr(client_module, "TOOL_TIMEOUT_SECONDS", 0.02)

    async def handle(request):
        await asyncio.sleep(10)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = KalshiClient(http)
        with pytest.raises(KalshiError, match="timeout"):
            if operation == "get_series":
                await client.get_series("EXAMPLESERIES")
            else:
                await client.list_series()


async def test_event_detail_retrieves_bounded_parent_context(event, series):
    requests = []

    def handle(request):
        requests.append(request)
        if "/events/" in request.url.path:
            return httpx.Response(200, json={"event": event})
        return httpx.Response(200, json={"series": series})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await KalshiClient(http).get_event(event["event_ticker"])
    assert len(requests) == 2
    assert requests[0].url.path == f"/trade-api/v2/events/{event['event_ticker']}"
    assert dict(requests[0].url.params) == {"exclude_markets": "true"}
    assert requests[1].url.path == f"/trade-api/v2/series/{series['ticker']}"
    assert result.event.event_ticker == event["event_ticker"]
    assert result.event.fee_type_override == "flat"
    assert result.event.fee_multiplier_override == 0.5
    assert result.event.settlement_sources[0].name == "Event Agency"
    assert result.event.mutually_exclusive is True
    assert result.event.collateral_return_type == "MECNET"
    assert result.event.product_metadata == {"report": "monthly"}
    assert result.series.contract_terms_url == series["contract_terms_url"]
    assert result.series.fee_type == "quadratic"
    assert result.series.fee_multiplier == 1.0
    assert result.retrieved_at <= result.series_retrieved_at <= datetime.now(UTC)
    assert result.series_error is None
    assert result.markets_included is False


@pytest.mark.parametrize("failure", [404, 500, 503, 429, "invalid_response", "wrong_ticker", "timeout"])
async def test_parent_series_failures_preserve_the_event(event, series, failure):
    requests = []

    def handle(request):
        requests.append(request)
        if "/events/" in request.url.path:
            return httpx.Response(200, json={"event": event})
        if failure == "timeout":
            raise httpx.ReadTimeout("temporary failure", request=request)
        if failure == "invalid_response":
            return httpx.Response(200, json={"series": {}})
        if failure == "wrong_ticker":
            return httpx.Response(200, json={"series": {**series, "ticker": "OTHER"}})
        return httpx.Response(failure, headers={"Retry-After": "3600" if failure == 429 else "0"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await KalshiClient(http).get_event(event["event_ticker"])
    assert result.event.event_ticker == event["event_ticker"]
    assert result.series is None
    assert result.series_retrieved_at is None
    expected_code = {
        404: "not_found", 500: "upstream_http_error", 503: "upstream_http_error",
        429: "rate_limited", "invalid_response": "invalid_response",
        "wrong_ticker": "invalid_response", "timeout": "timeout",
    }[failure]
    assert result.series_error.code == expected_code
    assert len(requests) == (3 if failure in {503, "timeout"} else 2)


@pytest.mark.parametrize("status", [404, 500])
async def test_event_request_failure_does_not_request_parent_series(status):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(KalshiError) as error:
            await KalshiClient(http).get_event("MISSING")
    assert error.value.status_code == status
    assert len(requests) == 1


@pytest.mark.parametrize("mutation", ["wrong_ticker", "missing_series", "unsafe_series", "bad_fee"])
async def test_malformed_event_detail_stops_before_parent_lookup(event, mutation):
    if mutation == "wrong_ticker":
        event["event_ticker"] = "OTHER"
    elif mutation == "missing_series":
        del event["series_ticker"]
    elif mutation == "unsafe_series":
        event["series_ticker"] = ".."
    else:
        event["fee_multiplier_override"] = "free"
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"event": event})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(KalshiError, match="invalid_response"):
            await KalshiClient(http).get_event("EXAMPLESERIES-26NOV09")
    assert len(requests) == 1


async def test_parent_lookup_shares_event_deadline(event, monkeypatch):
    monkeypatch.setattr(client_module, "TOOL_TIMEOUT_SECONDS", 0.03)
    parent_cancelled = asyncio.Event()

    async def handle(request):
        if "/events/" in request.url.path:
            return httpx.Response(200, json={"event": event})
        try:
            await asyncio.sleep(10)
        finally:
            parent_cancelled.set()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await KalshiClient(http).get_event(event["event_ticker"])
    assert result.event.event_ticker == event["event_ticker"]
    assert result.series_error.code == "timeout"
    assert parent_cancelled.is_set()


@pytest.mark.parametrize("operation", ["get_event", "list_events"])
async def test_event_request_timeout_is_an_error(operation, monkeypatch):
    monkeypatch.setattr(client_module, "TOOL_TIMEOUT_SECONDS", 0.02)

    async def handle(request):
        await asyncio.sleep(10)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = KalshiClient(http)
        with pytest.raises(KalshiError, match="timeout"):
            if operation == "get_event":
                await client.get_event("EXAMPLE")
            else:
                await client.list_events()


async def test_event_listing_forwards_filters_and_one_cursor_page(event):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"events": [event], "cursor": "next+/="})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await KalshiClient(http).list_events(
            series_ticker=event["series_ticker"], tickers=[event["event_ticker"], "OTHER"],
            status="open", min_close_ts=0, min_updated_ts=123, limit=1, cursor="previous+/=",
        )
    assert len(requests) == 1
    assert dict(requests[0].url.params) == {
        "series_ticker": event["series_ticker"], "tickers": f"{event['event_ticker']},OTHER",
        "status": "open", "min_close_ts": "0", "min_updated_ts": "123", "limit": "1",
        "cursor": "previous+/=", "with_nested_markets": "false",
    }
    assert result.next_cursor == "next+/="
    assert result.truncated
    assert result.pagination_mode == "upstream"
    assert result.markets_included is False
    assert result.events[0].event_ticker == event["event_ticker"]
    assert "settlement_sources" not in result.events[0].model_dump()


@pytest.mark.parametrize("cursor", ["", "next"])
async def test_empty_event_page_preserves_continuation(cursor):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"events": [], "cursor": cursor})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await KalshiClient(http).list_events()
    assert result.events == []
    assert result.next_cursor == (cursor or None)
    assert result.truncated is bool(cursor)
    assert dict(requests[0].url.params) == {"limit": "50", "with_nested_markets": "false"}


@pytest.mark.parametrize("body", [{}, {"events": [], "cursor": None}, {"events": [{}], "cursor": ""}])
async def test_malformed_event_listing_is_an_error(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as http:
        with pytest.raises(KalshiError, match="invalid_response"):
            await KalshiClient(http).list_events()


async def test_event_page_exceeding_requested_limit_is_an_error(event):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"events": [event, {**event, "event_ticker": "OTHER"}], "cursor": "next"}
        ))
    ) as http:
        with pytest.raises(KalshiError, match="invalid_response"):
            await KalshiClient(http).list_events(limit=1)


async def test_market_listing_preserves_summaries_and_forwards_identifiers(market):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"markets": [market], "cursor": "next+/="})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await KalshiClient(http).list_markets(
            event_ticker=market["event_ticker"], series_ticker="EXAMPLESERIES",
            tickers=[market["ticker"], "OTHER"], status="open", limit=200, cursor="previous+/=",
        )
    assert len(requests) == 1
    assert requests[0].url.path == "/trade-api/v2/markets"
    assert dict(requests[0].url.params) == {
        "event_ticker": market["event_ticker"], "series_ticker": "EXAMPLESERIES",
        "tickers": f"{market['ticker']},OTHER", "status": "open", "limit": "200",
        "cursor": "previous+/=", "mve_filter": "exclude",
    }
    assert result.next_cursor == "next+/="
    assert result.truncated
    assert result.pagination_mode == "upstream"
    assert result.historical_lookup_performed is False
    assert result.combo_markets_included is False
    summary = result.markets[0].model_dump(mode="json")
    assert summary["status"] == "active"
    assert summary["yes_bid_dollars"] == "0.420000"
    assert summary["volume_fp"] == "123.50"
    assert summary["yes_ask_dollars"] == "0.4500"
    assert summary["no_bid_dollars"] is None
    assert "rules_primary" not in summary
    assert "settlement_value_dollars" not in summary


@pytest.mark.parametrize("group", ["created", "close", "settled", "updated"])
async def test_all_market_timestamp_parameters_are_forwarded(group):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"markets": [], "cursor": ""})

    filters = {f"min_{group}_ts": 0, f"max_{group}_ts": 123}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        await KalshiClient(http).list_markets(**filters)
    assert requests[0].url.params[f"min_{group}_ts"] == "0"
    assert requests[0].url.params[f"max_{group}_ts"] == "123"
    assert requests[0].url.params["mve_filter"] == "exclude"


@pytest.mark.parametrize("cursor", ["", "next"])
async def test_empty_market_page_retains_continuation(cursor):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"markets": [], "cursor": cursor})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await KalshiClient(http).list_markets()
    assert result.markets == []
    assert result.next_cursor == (cursor or None)
    assert result.truncated is bool(cursor)
    assert dict(requests[0].url.params) == {"limit": "50", "mve_filter": "exclude"}


@pytest.mark.parametrize("body", [
    {}, {"markets": None, "cursor": ""}, {"markets": [{}], "cursor": ""},
    {"markets": [], "cursor": None},
])
async def test_malformed_market_listing_is_an_error(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as http:
        with pytest.raises(KalshiError, match="invalid_response"):
            await KalshiClient(http).list_markets()


async def test_market_listing_does_not_require_detail_rules(market):
    del market["rules_primary"]
    del market["rules_secondary"]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"markets": [market], "cursor": ""}
        ))
    ) as http:
        result = await KalshiClient(http).list_markets()
    assert result.markets[0].ticker == market["ticker"]


async def test_market_page_exceeding_requested_limit_is_an_error(market):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(
            200, json={"markets": [market, {**market, "ticker": "OTHER"}], "cursor": "next"}
        ))
    ) as http:
        with pytest.raises(KalshiError, match="invalid_response"):
            await KalshiClient(http).list_markets(limit=1)


async def test_market_listing_failure_does_not_trigger_historical_scanning():
    paths = []

    def handle(request):
        paths.append(request.url.path)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(KalshiError, match="upstream_http_error"):
            await KalshiClient(http).list_markets()
    assert paths == ["/trade-api/v2/markets"]


async def test_market_listing_obeys_tool_deadline(monkeypatch):
    monkeypatch.setattr(client_module, "TOOL_TIMEOUT_SECONDS", 0.02)

    async def handle(request):
        await asyncio.sleep(10)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(KalshiError, match="timeout"):
            await KalshiClient(http).list_markets()
