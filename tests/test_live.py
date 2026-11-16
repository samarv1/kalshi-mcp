import asyncio
import json
import os
import sysconfig
from pathlib import Path

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from kalshi_mcp.models import (
    EventListResponse,
    EventResponse,
    MarketListResponse,
    MarketResponse,
    OrderbooksResponse,
    SeriesListResponse,
    SeriesResponse,
)


pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        os.environ.get("KALSHI_LIVE_TEST") != "1",
        reason="Set KALSHI_LIVE_TEST=1 to call Kalshi's public API.",
    ),
]


# Run bounded public reads through the installed entry point; never scan extra pages.
async def test_live_stdio_workflow():
    executable = "kalshi-mcp.exe" if os.name == "nt" else "kalshi-mcp"
    command = str(Path(sysconfig.get_path("scripts")) / executable)
    parameters = StdioServerParameters(command=command)
    async with asyncio.timeout(180):
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                discovery = await session.list_tools()
                assert {tool.name for tool in discovery.tools} == {
                    "list_series", "get_series", "list_events", "get_event",
                    "list_markets", "get_market", "get_orderbooks",
                }

                async def call(name, arguments, model):
                    result = await session.call_tool(name, arguments)
                    assert not result.is_error, (name, result.content)
                    assert result.structured_content is not None, name
                    return model.model_validate_json(json.dumps(result.structured_content))

                markets = await call(
                    "list_markets", {"status": "open", "limit": 1}, MarketListResponse
                )
                assert markets.markets, "No open market returned on the first page."
                ticker = markets.markets[0].ticker
                market = await call("get_market", {"ticker": ticker}, MarketResponse)
                assert market.market.ticker == ticker
                event_ticker = market.market.event_ticker
                event = await call("get_event", {"event_ticker": event_ticker}, EventResponse)
                assert event.event.event_ticker == event_ticker
                assert event.series_error is None and event.series is not None
                series_ticker = event.event.series_ticker
                series = await call(
                    "get_series", {"series_ticker": series_ticker}, SeriesResponse
                )
                assert series.series.ticker == series_ticker
                events = await call(
                    "list_events", {"series_ticker": series_ticker, "limit": 1}, EventListResponse
                )
                assert events.events
                assert events.events[0].series_ticker == series_ticker
                series_page = await call("list_series", {"limit": 1}, SeriesListResponse)
                assert series_page.series
                books = await call(
                    "get_orderbooks", {"tickers": [ticker], "depth": 2}, OrderbooksResponse
                )
                assert len(books.results) == 1
                assert books.results[0].ticker == ticker
                assert books.results[0].error is None, books.results[0]

                settled_page = await call(
                    "list_markets", {"status": "settled", "limit": 1}, MarketListResponse
                )
                assert settled_page.markets, "No settled market returned on the first page."
                settled_ticker = settled_page.markets[0].ticker
                settled = await call("get_market", {"ticker": settled_ticker}, MarketResponse)
                assert settled.market.ticker == settled_ticker
                assert settled.market.result in {"yes", "no", "scalar"}
                print({
                    "market": ticker,
                    "event": event_ticker,
                    "series": series_ticker,
                    "settled_market": settled_ticker,
                    "settled_source": settled.source,
                    "settlement_value_dollars": settled.market.settlement_value_dollars,
                    "settlement_ts": settled.market.settlement_ts,
                })
