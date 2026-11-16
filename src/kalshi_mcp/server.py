import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, InputRequiredResult, Tool, ToolAnnotations

from kalshi_mcp.client import MAX_CONCURRENT_REQUESTS, KalshiClient
from kalshi_mcp.models import (
    EventListResponse,
    EventResponse,
    MarketListResponse,
    MarketResponse,
    OrderbooksResponse,
    SeriesListResponse,
    SeriesResponse,
)
from kalshi_mcp.tools import (
    Cursor,
    EventStatus,
    ListingLimit,
    MarketStatus,
    OrderbookDepth,
    OrderbookTickers,
    SeriesFilter,
    Ticker,
    TickerFilter,
    UnixTimestamp,
)
from kalshi_mcp.tools import get_event as read_event
from kalshi_mcp.tools import get_market as read_market
from kalshi_mcp.tools import get_orderbooks as read_orderbooks
from kalshi_mcp.tools import get_series as read_series
from kalshi_mcp.tools import list_events as find_events
from kalshi_mcp.tools import list_markets as find_markets
from kalshi_mcp.tools import list_series as find_series


# Share one HTTP client across tool calls and close it on shutdown.
@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[KalshiClient]:
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=MAX_CONCURRENT_REQUESTS),
        follow_redirects=False,
    ) as http:
        yield KalshiClient(http)


class KalshiMCPServer(MCPServer[KalshiClient]):
    # Publish the same closed argument contract that call_tool enforces.
    async def list_tools(self) -> list[Tool]:
        tools = await super().list_tools()
        for tool in tools:
            tool.input_schema["additionalProperties"] = False
        return tools

    # The SDK drops unknown keys before handler validation, so reject them here.
    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context[KalshiClient] | None = None,
    ) -> CallToolResult | InputRequiredResult:
        for tool in await self.list_tools():
            if tool.name == name:
                unknown = sorted(set(arguments) - set(tool.input_schema.get("properties", {})))
                if unknown:
                    raise ToolError(
                        json.dumps({
                            "code": "invalid_arguments",
                            "fields": unknown,
                            "reason": "Unknown tool arguments.",
                        })
                    )
                break
        return await super().call_tool(name, arguments, context)


mcp = KalshiMCPServer("kalshi-mcp", lifespan=lifespan, log_level="WARNING")


@mcp.tool(
    description="Read a market's rules, prices, status, and available settlement data.",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def get_market(ticker: Ticker, ctx: Context[KalshiClient]) -> MarketResponse:
    return await read_market(ticker, ctx.request_context.lifespan_context)


@mcp.tool(
    description=(
        "Find series by category and tags, sorted by ticker. "
        "Pass next_cursor as after_ticker with the same filters to continue. "
        "Each call fetches a fresh full list."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def list_series(
    ctx: Context[KalshiClient],
    category: SeriesFilter | None = None,
    tags: SeriesFilter | None = None,
    limit: ListingLimit = 50,
    after_ticker: Ticker | None = None,
) -> SeriesListResponse:
    return await find_series(
        ctx.request_context.lifespan_context, category, tags, limit, after_ticker
    )


@mcp.tool(
    description="Read a series' contract references, settlement sources, and default fee metadata.",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def get_series(series_ticker: Ticker, ctx: Context[KalshiClient]) -> SeriesResponse:
    return await read_series(series_ticker, ctx.request_context.lifespan_context)


@mcp.tool(
    description=(
        "Find events using series, ticker, status, and time filters. "
        "Pass next_cursor as cursor with the same filters to continue. "
        "Child markets are excluded."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def list_events(
    ctx: Context[KalshiClient],
    series_ticker: Ticker | None = None,
    tickers: TickerFilter | None = None,
    status: EventStatus | None = None,
    min_close_ts: UnixTimestamp | None = None,
    min_updated_ts: UnixTimestamp | None = None,
    limit: ListingLimit = 50,
    cursor: Cursor | None = None,
) -> EventListResponse:
    return await find_events(
        ctx.request_context.lifespan_context,
        series_ticker=series_ticker,
        tickers=tickers,
        status=status,
        min_close_ts=min_close_ts,
        min_updated_ts=min_updated_ts,
        limit=limit,
        cursor=cursor,
    )


@mcp.tool(
    description=(
        "Read an event with parent-series context. Child markets are excluded. "
        "Parent-series lookup failures are reported separately."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def get_event(event_ticker: Ticker, ctx: Context[KalshiClient]) -> EventResponse:
    return await read_event(event_ticker, ctx.request_context.lifespan_context)


@mcp.tool(
    description=(
        "List market summaries from the live endpoint. "
        "Older settled markets may require historical lookup with get_market. "
        "Pass next_cursor as cursor with the same filters to continue. "
        "Time filters use Unix seconds. Updated-time filters track metadata changes. "
        "Combo markets are excluded."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def list_markets(
    ctx: Context[KalshiClient],
    event_ticker: Ticker | None = None,
    series_ticker: Ticker | None = None,
    tickers: TickerFilter | None = None,
    status: MarketStatus | None = None,
    min_created_ts: UnixTimestamp | None = None,
    max_created_ts: UnixTimestamp | None = None,
    min_close_ts: UnixTimestamp | None = None,
    max_close_ts: UnixTimestamp | None = None,
    min_settled_ts: UnixTimestamp | None = None,
    max_settled_ts: UnixTimestamp | None = None,
    min_updated_ts: UnixTimestamp | None = None,
    max_updated_ts: UnixTimestamp | None = None,
    limit: ListingLimit = 50,
    cursor: Cursor | None = None,
) -> MarketListResponse:
    return await find_markets(
        ctx.request_context.lifespan_context,
        event_ticker=event_ticker,
        series_ticker=series_ticker,
        tickers=tickers,
        status=status,
        min_created_ts=min_created_ts,
        max_created_ts=max_created_ts,
        min_close_ts=min_close_ts,
        max_close_ts=max_close_ts,
        min_settled_ts=min_settled_ts,
        max_settled_ts=max_settled_ts,
        min_updated_ts=min_updated_ts,
        max_updated_ts=max_updated_ts,
        limit=limit,
        cursor=cursor,
    )


@mcp.tool(
    description=(
        "Read YES and NO bids for up to 10 distinct market tickers. "
        "Return one book or error per ticker in input order. "
        "Depth defaults to 20 levels per side, with a maximum of 100. "
        "Books do not guarantee fills."
    ),
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def get_orderbooks(
    tickers: OrderbookTickers, ctx: Context[KalshiClient], depth: OrderbookDepth = 20
) -> OrderbooksResponse:
    return await read_orderbooks(ctx.request_context.lifespan_context, tickers, depth)


# Keep logs on stderr because stdout carries MCP protocol messages.
def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
