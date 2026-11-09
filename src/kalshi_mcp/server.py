import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from kalshi_mcp.client import MAX_CONCURRENT_REQUESTS, KalshiClient
from kalshi_mcp.models import MarketResponse, SeriesListResponse, SeriesResponse
from kalshi_mcp.tools import ListingLimit, SeriesFilter, Ticker
from kalshi_mcp.tools import get_market as read_market
from kalshi_mcp.tools import get_series as read_series
from kalshi_mcp.tools import list_series as find_series


# Share one HTTP client across tool calls and close it on shutdown.
@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[KalshiClient]:
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=MAX_CONCURRENT_REQUESTS),
        follow_redirects=False,
    ) as http:
        yield KalshiClient(http)


mcp = MCPServer("kalshi-mcp", lifespan=lifespan, log_level="WARNING")


# Expose get_market through MCP using the client supplied by the server lifespan.
@mcp.tool(
    description="Read a market's rules, prices, status, and available settlement data.",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def get_market(ticker: Ticker, ctx: Context[KalshiClient]) -> MarketResponse:
    return await read_market(ticker, ctx.request_context.lifespan_context)


# Expose series discovery through MCP using the shared Kalshi client.
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


# Expose series details through MCP using the shared Kalshi client.
@mcp.tool(
    description="Read a series' contract references, settlement sources, and default fee metadata.",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
)
async def get_series(series_ticker: Ticker, ctx: Context[KalshiClient]) -> SeriesResponse:
    return await read_series(series_ticker, ctx.request_context.lifespan_context)


# Keep logs on stderr because stdout carries MCP protocol messages.
def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
