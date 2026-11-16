import json
from typing import Annotated, Literal, Self

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from kalshi_mcp.client import KalshiClient, KalshiError
from kalshi_mcp.models import (
    EventListResponse,
    EventResponse,
    MarketListResponse,
    MarketResponse,
    OrderbooksResponse,
    SeriesListResponse,
    SeriesResponse,
    Ticker,
)


SeriesFilter = Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r"\S")]
ListingLimit = Annotated[int, Field(strict=True, ge=1, le=200)]
TickerFilter = Annotated[list[Ticker], Field(min_length=1, max_length=100)]
EventStatus = Literal["unopened", "open", "closed", "settled"]
MarketStatus = Literal["unopened", "open", "paused", "closed", "settled"]
UnixTimestamp = Annotated[int, Field(strict=True, ge=0)]
Cursor = Annotated[str, StringConstraints(strict=True)]
OrderbookTickers = Annotated[list[Ticker], Field(min_length=1, max_length=10)]
OrderbookDepth = Annotated[int, Field(strict=True, ge=1, le=100)]


class GetOrderbooksArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    tickers: OrderbookTickers
    depth: OrderbookDepth = 20

    @model_validator(mode="after")
    def distinct_tickers(self) -> Self:
        if len(set(self.tickers)) != len(self.tickers):
            raise ValueError("Market tickers must be distinct.")
        return self


async def get_orderbooks(
    client: KalshiClient, tickers: list[str], depth: int = 20
) -> OrderbooksResponse:
    try:
        arguments = GetOrderbooksArguments(tickers=tickers, depth=depth)
    except ValidationError as exc:
        raise _invalid_arguments(exc) from exc
    return await client.get_orderbooks(arguments.tickers, arguments.depth)


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
    tickers: TickerFilter | None = None
    status: EventStatus | None = None
    min_close_ts: UnixTimestamp | None = None
    min_updated_ts: UnixTimestamp | None = None
    limit: ListingLimit = 50
    cursor: Cursor | None = None


class ListMarketsArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    event_ticker: Ticker | None = None
    series_ticker: Ticker | None = None
    tickers: TickerFilter | None = None
    status: MarketStatus | None = None
    min_created_ts: UnixTimestamp | None = None
    max_created_ts: UnixTimestamp | None = None
    min_close_ts: UnixTimestamp | None = None
    max_close_ts: UnixTimestamp | None = None
    min_settled_ts: UnixTimestamp | None = None
    max_settled_ts: UnixTimestamp | None = None
    min_updated_ts: UnixTimestamp | None = None
    max_updated_ts: UnixTimestamp | None = None
    limit: ListingLimit = 50
    cursor: Cursor | None = None

    # Kalshi permits one timestamp group per request, with group-specific status filters.
    @model_validator(mode="after")
    def validate_timestamp_filters(self) -> Self:
        ranges = {
            "created": (self.min_created_ts, self.max_created_ts),
            "close": (self.min_close_ts, self.max_close_ts),
            "settled": (self.min_settled_ts, self.max_settled_ts),
            "updated": (self.min_updated_ts, self.max_updated_ts),
        }
        selected = [
            group for group, (minimum, maximum) in ranges.items()
            if minimum is not None or maximum is not None
        ]
        if len(selected) > 1:
            raise ValueError("Use only one timestamp group per request.")
        if not selected:
            return self
        group = selected[0]
        minimum, maximum = ranges[group]
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError("Minimum timestamp must not exceed maximum timestamp.")
        allowed_statuses = {
            "created": {None, "unopened", "open"},
            "close": {None, "closed"},
            "settled": {None, "settled"},
            "updated": {None},
        }
        if self.status not in allowed_statuses[group]:
            raise ValueError("Status is incompatible with the selected timestamp group.")
        if group == "updated" and (self.event_ticker is not None or self.tickers is not None):
            raise ValueError("Updated-time filters allow only series_ticker, limit, and cursor.")
        return self


def _invalid_arguments(exc: ValidationError) -> ToolError:
    error = exc.errors()[0]
    location = error["loc"]
    details = {"code": "invalid_arguments", "field": location[0] if location else "filters"}
    if not location:
        details["reason"] = error["msg"]
    return ToolError(json.dumps(details))


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


# Enforce Kalshi's filter combinations before requesting one live market page.
async def list_markets(
    client: KalshiClient,
    *,
    event_ticker: str | None = None,
    series_ticker: str | None = None,
    tickers: list[str] | None = None,
    status: str | None = None,
    min_created_ts: int | None = None,
    max_created_ts: int | None = None,
    min_close_ts: int | None = None,
    max_close_ts: int | None = None,
    min_settled_ts: int | None = None,
    max_settled_ts: int | None = None,
    min_updated_ts: int | None = None,
    max_updated_ts: int | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> MarketListResponse:
    try:
        arguments = ListMarketsArguments(
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
    except ValidationError as exc:
        raise _invalid_arguments(exc) from exc
    try:
        return await client.list_markets(**arguments.model_dump())
    except KalshiError as exc:
        raise _upstream_error(exc) from exc
