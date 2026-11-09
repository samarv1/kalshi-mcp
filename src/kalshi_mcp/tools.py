import json
from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from kalshi_mcp.client import KalshiClient, KalshiError
from kalshi_mcp.models import MarketResponse, SeriesListResponse, SeriesResponse


Ticker = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$"),
]
SeriesFilter = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
ListingLimit = Annotated[int, Field(strict=True, ge=1, le=200)]


class GetMarketArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: Ticker


class GetSeriesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    series_ticker: Ticker


class ListSeriesArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: SeriesFilter | None = None
    tags: SeriesFilter | None = None
    limit: ListingLimit = 50
    after_ticker: Ticker | None = None


def _invalid_arguments(exc: ValidationError) -> ToolError:
    return ToolError(
        json.dumps({"code": "invalid_arguments", "field": exc.errors()[0]["loc"][0]})
    )


def _upstream_error(exc: KalshiError) -> ToolError:
    return ToolError(
        json.dumps({"code": exc.code, "source": exc.source, "status_code": exc.status_code})
    )


# Validate tool input and translate upstream failures into MCP tool errors.
async def get_market(ticker: str, client: KalshiClient) -> MarketResponse:
    try:
        arguments = GetMarketArguments(ticker=ticker)
    except ValidationError as exc:
        raise _invalid_arguments(exc) from exc

    try:
        return await client.get_market(arguments.ticker)
    except KalshiError as exc:
        raise _upstream_error(exc) from exc


# Validate the series ticker and translate upstream failures into MCP tool errors.
async def get_series(series_ticker: str, client: KalshiClient) -> SeriesResponse:
    try:
        arguments = GetSeriesArguments(series_ticker=series_ticker)
    except ValidationError as exc:
        raise _invalid_arguments(exc) from exc
    try:
        return await client.get_series(arguments.series_ticker)
    except KalshiError as exc:
        raise _upstream_error(exc) from exc


# Sort and page locally because Kalshi's series endpoint is unpaginated.
# Comparing tickers allows continuation even if the previous page's last series disappears.
async def list_series(
    client: KalshiClient,
    category: str | None = None,
    tags: str | None = None,
    limit: int = 50,
    after_ticker: str | None = None,
) -> SeriesListResponse:
    try:
        arguments = ListSeriesArguments(
            category=category, tags=tags, limit=limit, after_ticker=after_ticker
        )
    except ValidationError as exc:
        raise _invalid_arguments(exc) from exc
    try:
        result = await client.list_series(arguments.category, arguments.tags)
    except KalshiError as exc:
        raise _upstream_error(exc) from exc
    remaining = sorted(result.series, key=lambda series: series.ticker)
    if arguments.after_ticker is not None:
        remaining = [series for series in remaining if series.ticker > arguments.after_ticker]
    page = remaining[:arguments.limit]
    has_more = len(remaining) > arguments.limit
    return result.model_copy(
        update={
            "series": page,
            "truncated": has_more,
            "next_cursor": page[-1].ticker if has_more else None,
        }
    )
