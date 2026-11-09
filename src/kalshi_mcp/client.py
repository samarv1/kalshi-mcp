import asyncio
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from kalshi_mcp.models import MarketPayload, MarketResponse, Source


BASE_URL = "https://external-api.kalshi.com/trade-api/v2"
REQUEST_TIMEOUT_SECONDS = 5
TOOL_TIMEOUT_SECONDS = 30
MAX_CONCURRENT_REQUESTS = 4
RETRYABLE_STATUSES = {429, 502, 503, 504}


class KalshiError(Exception):
    """Classify upstream failures without exposing upstream response bodies."""

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


def retry_delay(response: httpx.Response) -> float:
    """Return a nonnegative delay in seconds, defaulting to 0.5 for unusable Retry-After."""
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

    # Only a live 404 permits historical lookup. Temporary failures retain their error.
    async def get_market(self, ticker: str) -> MarketResponse:
        """Fetch and validate a market, recording its source and retrieval time."""
        source: Source = "live"
        try:
            async with asyncio.timeout(TOOL_TIMEOUT_SECONDS) as deadline:
                for source, prefix in (("live", "markets"), ("historical", "historical/markets")):
                    try:
                        response = await self._request(
                            f"/{prefix}/{quote(ticker, safe='')}", source, deadline
                        )
                    except KalshiError as exc:
                        if exc.code == "not_found" and source == "live":
                            continue
                        raise

                    retrieved_at = datetime.now(UTC)
                    try:
                        payload = MarketPayload.model_validate_json(response.content)
                    except ValidationError as exc:
                        raise KalshiError("invalid_response", source) from exc
                    if payload.market.ticker != ticker:
                        raise KalshiError("invalid_response", source)
                    return MarketResponse(
                        source=source, retrieved_at=retrieved_at, market=payload.market
                    )
        except TimeoutError as exc:
            raise KalshiError("timeout", source) from exc
        raise KalshiError("not_found", source)

    async def _request(
        self, path: str, source: Source, deadline: asyncio.Timeout
    ) -> httpx.Response:
        """Retry temporary GET failures once within the caller's deadline."""
        for attempt in range(2):
            try:
                async with self._semaphore:
                    async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                        response = await self.http.get(
                            f"{BASE_URL}{path}", timeout=REQUEST_TIMEOUT_SECONDS
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
