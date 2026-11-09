import json
from typing import Annotated, Literal

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from kalshi_mcp.client import KalshiClient, KalshiError
from kalshi_mcp.models import (
    EventListResponse,
    EventResponse,
    MarketResponse,
    SeriesListResponse,
    SeriesResponse,
    Ticker,
)


SeriesFilter = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
ListingLimit = Annotated[int, Field(strict=True, ge=1, le=200)]
EventTickers = Annotated[list[Ticker], Field(min_length=1, max_length=100)]
EventStatus = Literal["unopened", "open", "closed", "settled"]
UnixTimestamp = Annotated[int, Field(strict=True, ge=0)]
Cursor = Annotated[str, StringConstraints(strict=True)]


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


class GetEventArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_ticker: Ticker


class ListEventsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    series_ticker: Ticker | None = None
    tickers: EventTickers | None = None
    status: EventStatus | None = None
    min_close_ts: UnixTimestamp | None = None
    min_updated_ts: UnixTimestamp | None = None
    limit: ListingLimit = 50
    cursor: Cursor | None = None


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


# Validate the event ticker and expose parent-series failures within the event response.
async def get_event(event_ticker: str, client: KalshiClient) -> EventResponse:
    try:
        arguments = GetEventArguments(event_ticker=event_ticker)
    except ValidationError as exc:
        raise _invalid_arguments(exc) from exc
    try:
        return await client.get_event(arguments.event_ticker)
    except KalshiError as exc:
        raise _upstream_error(exc) from exc


# Timestamps are Unix seconds. Opaque cursors are forwarded without interpretation.
async def list_events(
    client: KalshiClient,
    *,
    series_ticker: str | None = None,
    tickers: list[str] | None = None,
    status: str | None = None,
    min_close_ts: int | None = None,
    min_updated_ts: int | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> EventListResponse:
    try:
        arguments = ListEventsArguments(
            series_ticker=series_ticker,
            tickers=tickers,
            status=status,
            min_close_ts=min_close_ts,
            min_updated_ts=min_updated_ts,
            limit=limit,
            cursor=cursor,
        )
    except ValidationError as exc:
        raise _invalid_arguments(exc) from exc
    try:
        return await client.list_events(**arguments.model_dump())
    except KalshiError as exc:
        raise _upstream_error(exc) from exc
