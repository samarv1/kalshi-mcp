import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import ToolAnnotations

from kalshi_mcp.client import MAX_CONCURRENT_REQUESTS, KalshiClient
from kalshi_mcp.models import MarketResponse
from kalshi_mcp.tools import Ticker
from kalshi_mcp.tools import get_market as read_market


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[KalshiClient]:
    """Share one HTTP client across tool calls and close it on shutdown."""
    async with httpx.AsyncClient(
        limits=httpx.Limits(max_connections=MAX_CONCURRENT_REQUESTS),
        follow_redirects=False,
    ) as http:
        yield KalshiClient(http)


mcp = MCPServer("kalshi-mcp", lifespan=lifespan, log_level="WARNING")


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True))
async def get_market(ticker: Ticker, ctx: Context[KalshiClient]) -> MarketResponse:
    """Read a market's rules, prices, status, and available settlement data."""
    return await read_market(ticker, ctx.request_context.lifespan_context)


def main() -> None:
    """Run MCP over stdio, with application logs sent to stderr."""
    logging.basicConfig(level=logging.WARNING)
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
