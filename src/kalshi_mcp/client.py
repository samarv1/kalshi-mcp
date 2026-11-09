import asyncio
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ValidationError

from kalshi_mcp.models import (
    EventListPayload,
    EventListResponse,
    EventPayload,
    EventResponse,
    MarketPayload,
    MarketResponse,
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

    # Validate the endpoint's JSON envelope and record receipt time before validation.
    async def _read_payload[Payload: BaseModel](
        self,
        path: str,
        source: Source,
        deadline: asyncio.Timeout,
        payload_type: type[Payload],
        params: dict[str, str] | None = None,
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
        params: dict[str, str] | None = None,
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
