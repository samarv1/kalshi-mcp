import asyncio
import json
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from kalshi_mcp.models import (
    EventListPayload,
    EventListResponse,
    EventPayload,
    EventResponse,
    MarketListPayload,
    MarketListResponse,
    MarketPayload,
    MarketResponse,
    OrderbookEntry,
    OrderbookError,
    OrderbookFailure,
    OrderbookSuccess,
    OrderbooksPayload,
    OrderbooksResponse,
    SeriesContextError,
    SeriesListPayload,
    SeriesListResponse,
    SeriesPayload,
    SeriesResponse,
    Source,
)


BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
REQUEST_TIMEOUT_SECONDS = 5
TOOL_TIMEOUT_SECONDS = 30
MAX_CONCURRENT_REQUESTS = 4
RETRYABLE_STATUSES = {429, 502, 503, 504}


# Classify upstream failures without exposing upstream response bodies.
class KalshiError(Exception):
    def __init__(
        self,
        code: str,
        source: Source | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.source = source
        self.status_code = status_code


# Retry-After accepts seconds or an HTTP date. Return a nonnegative delay in seconds,
# defaulting to 0.5 when the header is missing or invalid.
def retry_delay(response: httpx.Response) -> float:
    value = response.headers.get("Retry-After")
    if value is None:
        return 0.5
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
            seconds = (retry_at - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0.5
    if not math.isfinite(seconds):
        return 0.5
    return max(0.0, seconds)


class KalshiClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    # Unknown tickers also have empty books, so verify existence before the batch read.
    # One outer deadline covers all lookups, retries, and the final batch request.
    async def get_orderbooks(self, tickers: list[str], depth: int = 20) -> OrderbooksResponse:
        results: dict[str, OrderbookSuccess | OrderbookFailure] = {}

        def failure(ticker: str, exc: KalshiError) -> None:
            results[ticker] = OrderbookFailure(
                ticker=ticker,
                error=OrderbookError(
                    code=exc.code, source=exc.source, status_code=exc.status_code
                ),
            )

        async def verify(ticker: str) -> None:
            try:
                market = await self.get_market(ticker)
                if market.source == "historical":
                    failure(ticker, KalshiError("orderbook_unavailable", "historical"))
            except KalshiError as exc:
                failure(ticker, exc)

        try:
            async with asyncio.timeout(TOOL_TIMEOUT_SECONDS) as deadline:
                async with asyncio.TaskGroup() as group:
                    for ticker in tickers:
                        group.create_task(verify(ticker))
                eligible = [ticker for ticker in tickers if ticker not in results]
                if eligible:
                    payload, retrieved_at = await self._read_payload(
                        "/markets/orderbooks", "live", deadline, OrderbooksPayload,
                        {"tickers": eligible},
                    )
                    entries: dict[str, list[Any]] = {ticker: [] for ticker in eligible}
                    for entry in payload.orderbooks:
                        if not isinstance(entry, dict) or not isinstance(entry.get("ticker"), str):
                            raise KalshiError("invalid_response", "live")
                        if entry["ticker"] not in entries:
                            raise KalshiError("invalid_response", "live")
                        entries[entry["ticker"]].append(entry)
                    for ticker, matches in entries.items():
                        if not matches:
                            failure(ticker, KalshiError("missing_result", "live"))
                            continue
                        try:
                            if len(matches) != 1:
                                raise KalshiError("invalid_response", "live")
                            entry = OrderbookEntry.model_validate_json(json.dumps(matches[0]))
                        except (ValidationError, KalshiError):
                            failure(ticker, KalshiError("invalid_response", "live"))
                            continue
                        book = entry.orderbook_fp
                        results[ticker] = OrderbookSuccess(
                            ticker=ticker, retrieved_at=retrieved_at,
                            orderbook_fp=book.model_copy(update={
                                "yes_dollars": book.yes_dollars[-depth:],
                                "no_dollars": book.no_dollars[-depth:],
                            }),
                            yes_truncated=len(book.yes_dollars) > depth,
                            no_truncated=len(book.no_dollars) > depth,
                        )
        except (TimeoutError, KalshiError) as exc:
            error = exc if isinstance(exc, KalshiError) else KalshiError("timeout", "live")
            for ticker in tickers:
                if ticker not in results:
                    failure(ticker, error)
        return OrderbooksResponse(depth=depth, results=[results[ticker] for ticker in tickers])

    # Fetch and validate live or historical market data under one deadline.
    # Only a live 404 permits historical lookup. Temporary failures retain their error.
    async def get_market(self, ticker: str) -> MarketResponse:
        source: Source = "live"
        try:
            async with asyncio.timeout(TOOL_TIMEOUT_SECONDS) as deadline:
                for source, prefix in (("live", "markets"), ("historical", "historical/markets")):
                    try:
                        payload, retrieved_at = await self._read_payload(
                            f"/{prefix}/{quote(ticker, safe='')}", source, deadline, MarketPayload
                        )
                    except KalshiError as exc:
                        if exc.code == "not_found" and source == "live":
                            continue
                        raise

                    if payload.market.ticker != ticker:
                        raise KalshiError("invalid_response", source)
                    return MarketResponse(
                        source=source, retrieved_at=retrieved_at, market=payload.market
                    )
        except TimeoutError as exc:
            raise KalshiError("timeout", source) from exc
        raise KalshiError("not_found", source)

    # Fetch and validate a series' contract and settlement metadata.
    async def get_series(self, series_ticker: str) -> SeriesResponse:
        try:
            async with asyncio.timeout(TOOL_TIMEOUT_SECONDS) as deadline:
                return await self._read_series(series_ticker, deadline)
        except TimeoutError as exc:
            raise KalshiError("timeout", "live") from exc

    # Reuse the caller's deadline when series metadata is part of an event lookup.
    async def _read_series(
        self, series_ticker: str, deadline: asyncio.Timeout
    ) -> SeriesResponse:
        payload, retrieved_at = await self._read_payload(
            f"/series/{quote(series_ticker, safe='')}", "live", deadline, SeriesPayload
        )
        if payload.series.ticker != series_ticker:
            raise KalshiError("invalid_response", "live")
        return SeriesResponse(source="live", retrieved_at=retrieved_at, series=payload.series)

    # Forward the supported filters exactly, including their case. The API has no cursor.
    async def list_series(
        self, category: str | None = None, tags: str | None = None
    ) -> SeriesListResponse:
        params = {
            key: value
            for key, value in {"category": category, "tags": tags}.items()
            if value is not None
        }
        try:
            async with asyncio.timeout(TOOL_TIMEOUT_SECONDS) as deadline:
                payload, retrieved_at = await self._read_payload(
                    "/series", "live", deadline, SeriesListPayload, params
                )
                return SeriesListResponse(
                    source="live",
                    retrieved_at=retrieved_at,
                    series=payload.series,
                    total_count=len(payload.series),
                )
        except TimeoutError as exc:
            raise KalshiError("timeout", "live") from exc

    # Keep the event available if its parent lookup fails, sharing one total deadline.
    async def get_event(self, event_ticker: str) -> EventResponse:
        result: EventResponse | None = None
        try:
            async with asyncio.timeout(TOOL_TIMEOUT_SECONDS) as deadline:
                payload, retrieved_at = await self._read_payload(
                    f"/events/{quote(event_ticker, safe='')}",
                    "live",
                    deadline,
                    EventPayload,
                    {"exclude_markets": "true"},
                )
                if payload.event.event_ticker != event_ticker:
                    raise KalshiError("invalid_response", "live")
                result = EventResponse(
                    source="live", retrieved_at=retrieved_at, event=payload.event
                )
                try:
                    parent = await self._read_series(payload.event.series_ticker, deadline)
                except KalshiError as exc:
                    return result.model_copy(
                        update={
                            "series_error": SeriesContextError(
                                code=exc.code, status_code=exc.status_code
                            )
                        }
                    )
                return result.model_copy(
                    update={
                        "series": parent.series,
                        "series_retrieved_at": parent.retrieved_at,
                    }
                )
        except TimeoutError as exc:
            if result is None:
                raise KalshiError("timeout", "live") from exc
            return result.model_copy(update={"series_error": SeriesContextError(code="timeout")})

    # Return exactly one upstream page and its cursor, excluding child markets.
    async def list_events(
        self,
        *,
        series_ticker: str | None = None,
        tickers: list[str] | None = None,
        status: str | None = None,
        min_close_ts: int | None = None,
        min_updated_ts: int | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> EventListResponse:
        filters = {
            "series_ticker": series_ticker,
            "status": status,
            "min_close_ts": min_close_ts,
            "min_updated_ts": min_updated_ts,
            "limit": limit,
            "cursor": cursor,
        }
        params = {key: str(value) for key, value in filters.items() if value is not None}
        params["with_nested_markets"] = "false"
        if tickers is not None:
            params["tickers"] = ",".join(tickers)
        try:
            async with asyncio.timeout(TOOL_TIMEOUT_SECONDS) as deadline:
                payload, retrieved_at = await self._read_payload(
                    "/events", "live", deadline, EventListPayload, params
                )
                if len(payload.events) > limit:
                    raise KalshiError("invalid_response", "live")
                return EventListResponse(
                    source="live",
                    retrieved_at=retrieved_at,
                    events=payload.events,
                    next_cursor=payload.cursor or None,
                    truncated=bool(payload.cursor),
                )
        except TimeoutError as exc:
            raise KalshiError("timeout", "live") from exc

    # Query only the live listing and exclude combos, matching the server's MVP scope.
    async def list_markets(
        self,
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
        filters = {
            "event_ticker": event_ticker,
            "series_ticker": series_ticker,
            "status": status,
            "min_created_ts": min_created_ts,
            "max_created_ts": max_created_ts,
            "min_close_ts": min_close_ts,
            "max_close_ts": max_close_ts,
            "min_settled_ts": min_settled_ts,
            "max_settled_ts": max_settled_ts,
            "min_updated_ts": min_updated_ts,
            "max_updated_ts": max_updated_ts,
            "limit": limit,
            "cursor": cursor,
        }
        params = {key: str(value) for key, value in filters.items() if value is not None}
        params["mve_filter"] = "exclude"
        if tickers is not None:
            params["tickers"] = ",".join(tickers)
        try:
            async with asyncio.timeout(TOOL_TIMEOUT_SECONDS) as deadline:
                payload, retrieved_at = await self._read_payload(
                    "/markets", "live", deadline, MarketListPayload, params
                )
                if len(payload.markets) > limit:
                    raise KalshiError("invalid_response", "live")
                return MarketListResponse(
                    source="live",
                    retrieved_at=retrieved_at,
                    markets=payload.markets,
                    next_cursor=payload.cursor or None,
                    truncated=bool(payload.cursor),
                )
        except TimeoutError as exc:
            raise KalshiError("timeout", "live") from exc

    # Validate the endpoint's JSON envelope and record receipt time before validation.
    async def _read_payload[Payload: BaseModel](
        self,
        path: str,
        source: Source,
        deadline: asyncio.Timeout,
        payload_type: type[Payload],
        params: dict[str, str | list[str]] | None = None,
    ) -> tuple[Payload, datetime]:
        response = await self._request(path, source, deadline, params)
        retrieved_at = datetime.now(UTC)
        try:
            return payload_type.model_validate_json(response.content), retrieved_at
        except ValidationError as exc:
            raise KalshiError("invalid_response", source) from exc

    # Retry temporary GET failures once within the caller's deadline.
    # Acquire capacity per attempt so retry waits do not block other requests.
    async def _request(
        self,
        path: str,
        source: Source,
        deadline: asyncio.Timeout,
        params: dict[str, str | list[str]] | None = None,
    ) -> httpx.Response:
        for attempt in range(2):
            try:
                async with self._semaphore:
                    async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                        response = await self.http.get(
                            f"{BASE_URL}{path}", params=params, timeout=REQUEST_TIMEOUT_SECONDS
                        )
            except (TimeoutError, httpx.TimeoutException) as exc:
                if attempt == 1:
                    raise KalshiError("timeout", source) from exc
                await asyncio.sleep(0.5)
                continue
            except httpx.NetworkError as exc:
                if attempt == 1:
                    raise KalshiError("connection_error", source) from exc
                await asyncio.sleep(0.5)
                continue
            except httpx.RequestError as exc:
                raise KalshiError("request_error", source) from exc

            if response.status_code == 200:
                return response
            if response.status_code == 404:
                raise KalshiError("not_found", source, 404)
            if response.status_code in (401, 403):
                raise KalshiError("authentication_required", source, response.status_code)

            if response.status_code in RETRYABLE_STATUSES and attempt == 0:
                delay = retry_delay(response)
                remaining = deadline.when() - asyncio.get_running_loop().time()
                if delay < remaining:
                    await asyncio.sleep(delay)
                    continue

            code = "rate_limited" if response.status_code == 429 else "upstream_http_error"
            raise KalshiError(code, source, response.status_code)
        raise KalshiError("request_error", source)
