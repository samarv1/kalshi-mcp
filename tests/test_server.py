import json
import sys

import httpx
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from kalshi_mcp.server import lifespan, mcp


pytestmark = pytest.mark.anyio


async def test_lifespan_closes_shared_http_client():
    async with lifespan(mcp) as client:
        assert isinstance(client.http, httpx.AsyncClient)
        assert not client.http.is_closed
    assert client.http.is_closed


# Run the real MCP subprocess with mocked HTTP responses to keep the test offline.
async def test_stdio_discovery_invocation_errors_and_shutdown(market, series, event, tmp_path):
    bootstrap = """
import json
import os
import sys
from unittest.mock import patch
import httpx
from kalshi_mcp.server import main

market = json.loads(os.environ['TEST_MARKET'])
series = json.loads(os.environ['TEST_SERIES'])
event = json.loads(os.environ['TEST_EVENT'])
def handle(request):
    if request.url.path.endswith('/MISSING'):
        return httpx.Response(404)
    if request.url.path.endswith('/series'):
        assert dict(request.url.params) == {'category': 'Economics', 'tags': 'Inflation'}
        return httpx.Response(200, json={'series': [series, {**series, 'ticker': 'SECOND'}]})
    if '/series/' in request.url.path:
        return httpx.Response(200, json={'series': series})
    if request.url.path.endswith('/events'):
        assert request.url.params['with_nested_markets'] == 'false'
        assert request.url.params['series_ticker'] == series['ticker']
        if request.url.params.get('cursor') == 'EVENT-NEXT':
            return httpx.Response(200, json={'events': [], 'cursor': ''})
        return httpx.Response(200, json={'events': [event], 'cursor': 'EVENT-NEXT'})
    if '/events/' in request.url.path:
        assert dict(request.url.params) == {'exclude_markets': 'true'}
        if request.url.path.endswith('/CONTEXTFAIL'):
            return httpx.Response(200, json={
                'event': {**event, 'event_ticker': 'CONTEXTFAIL', 'series_ticker': 'MISSING'}
            })
        return httpx.Response(200, json={'event': event})
    if request.url.path.endswith('/markets'):
        assert request.url.params['mve_filter'] == 'exclude'
        assert request.url.params['event_ticker'] == event['event_ticker']
        if request.url.params.get('cursor') == 'MARKET-NEXT':
            return httpx.Response(200, json={'markets': [], 'cursor': ''})
        return httpx.Response(200, json={'markets': [market], 'cursor': 'MARKET-NEXT'})
    return httpx.Response(200, json={'market': market})

http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
with patch('kalshi_mcp.server.httpx.AsyncClient', return_value=http):
    main()
print('http_closed=' + str(http.is_closed), file=sys.stderr)
"""
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-c", bootstrap],
        env={
            "TEST_MARKET": json.dumps(market), "TEST_SERIES": json.dumps(series),
            "TEST_EVENT": json.dumps(event),
        },
    )
    stderr_path = tmp_path / "server.stderr"
    with stderr_path.open("w") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                discovery = await session.list_tools()
                tools = {tool.name: tool for tool in discovery.tools}
                assert set(tools) == {
                    "get_market", "get_series", "list_series", "get_event", "list_events", "list_markets",
                }
                tool = tools["get_market"]
                assert tool.description == "Read a market's rules, prices, status, and available settlement data."
                assert set(tool.input_schema["properties"]) == {"ticker"}
                assert tool.annotations.read_only_hint is True
                assert tool.output_schema is not None

                result = await session.call_tool("get_market", {"ticker": market["ticker"]})
                assert not result.is_error
                assert result.structured_content["market"]["ticker"] == market["ticker"]
                assert result.structured_content["market"]["yes_bid_dollars"] == "0.420000"
                assert result.structured_content["market"]["settlement_ts"] is None

                missing = await session.call_tool("get_market", {"ticker": "MISSING"})
                assert missing.is_error
                assert "not_found" in missing.content[0].text
                invalid = await session.call_tool("get_market", {"ticker": "../bad"})
                assert invalid.is_error
                absent = await session.call_tool("get_market", {})
                assert absent.is_error

                listing = await session.call_tool(
                    "list_series", {"category": "Economics", "tags": "Inflation", "limit": 1}
                )
                assert not listing.is_error
                assert listing.structured_content["total_count"] == 2
                assert listing.structured_content["truncated"] is True
                assert listing.structured_content["next_cursor"] == series["ticker"]
                assert listing.structured_content["pagination_mode"] == "local"
                assert len(listing.structured_content["series"]) == 1
                series_ticker = listing.structured_content["series"][0]["ticker"]

                continuation = await session.call_tool(
                    "list_series", {
                        "category": "Economics", "tags": "Inflation", "limit": 1,
                        "after_ticker": listing.structured_content["next_cursor"],
                    }
                )
                assert not continuation.is_error
                assert continuation.structured_content["series"][0]["ticker"] == "SECOND"
                assert continuation.structured_content["next_cursor"] is None
                assert continuation.structured_content["truncated"] is False

                detail = await session.call_tool("get_series", {"series_ticker": series_ticker})
                assert not detail.is_error
                assert detail.structured_content["series"]["contract_url"] == series["contract_url"]
                assert detail.structured_content["series"]["fee_type"] == series["fee_type"]
                assert set(tools["get_series"].input_schema["properties"]) == {"series_ticker"}
                assert set(tools["list_series"].input_schema["properties"]) == {
                    "category", "tags", "limit", "after_ticker",
                }
                bad_limit = await session.call_tool("list_series", {"limit": 201})
                assert bad_limit.is_error

                events = await session.call_tool(
                    "list_events", {"series_ticker": series_ticker, "limit": 1}
                )
                assert not events.is_error
                assert events.structured_content["next_cursor"] == "EVENT-NEXT"
                assert events.structured_content["pagination_mode"] == "upstream"
                assert events.structured_content["markets_included"] is False
                event_ticker = events.structured_content["events"][0]["event_ticker"]

                final_page = await session.call_tool("list_events", {
                    "series_ticker": series_ticker, "cursor": events.structured_content["next_cursor"],
                    "limit": 1,
                })
                assert not final_page.is_error
                assert final_page.structured_content["events"] == []
                assert final_page.structured_content["next_cursor"] is None

                event_detail = await session.call_tool("get_event", {"event_ticker": event_ticker})
                assert not event_detail.is_error
                assert event_detail.structured_content["series"]["ticker"] == series_ticker
                assert event_detail.structured_content["series_error"] is None
                assert event_detail.structured_content["event"]["fee_type_override"] == "flat"
                assert event_detail.structured_content["series"]["fee_type"] == "quadratic"

                partial = await session.call_tool("get_event", {"event_ticker": "CONTEXTFAIL"})
                assert not partial.is_error
                assert partial.structured_content["event"]["event_ticker"] == "CONTEXTFAIL"
                assert partial.structured_content["series"] is None
                assert partial.structured_content["series_error"]["code"] == "not_found"
                missing_event = await session.call_tool("get_event", {"event_ticker": "MISSING"})
                assert missing_event.is_error
                bad_status = await session.call_tool("list_events", {"status": "active"})
                assert bad_status.is_error
                assert set(tools["get_event"].input_schema["properties"]) == {"event_ticker"}

                markets = await session.call_tool(
                    "list_markets", {"event_ticker": event_ticker, "limit": 1}
                )
                assert not markets.is_error
                assert markets.structured_content["historical_lookup_performed"] is False
                assert markets.structured_content["combo_markets_included"] is False
                assert markets.structured_content["next_cursor"] == "MARKET-NEXT"
                summary = markets.structured_content["markets"][0]
                assert summary["event_ticker"] == event_ticker
                assert "rules_primary" not in summary

                market_detail = await session.call_tool("get_market", {"ticker": summary["ticker"]})
                assert not market_detail.is_error
                assert market_detail.structured_content["market"]["rules_primary"] == market["rules_primary"]
                assert market_detail.structured_content["market"]["event_ticker"] == event_ticker

                last_markets = await session.call_tool("list_markets", {
                    "event_ticker": event_ticker, "limit": 1,
                    "cursor": markets.structured_content["next_cursor"],
                })
                assert not last_markets.is_error
                assert last_markets.structured_content["markets"] == []
                assert last_markets.structured_content["next_cursor"] is None

                invalid_filters = await session.call_tool("list_markets", {
                    "min_created_ts": 0, "status": "closed",
                })
                assert invalid_filters.is_error
                assert "Status is incompatible" in invalid_filters.content[0].text
                assert "mve_filter" not in tools["list_markets"].input_schema["properties"]
    assert "http_closed=True" in stderr_path.read_text()
