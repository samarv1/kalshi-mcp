from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints, field_validator


Source = Literal["live", "historical"]
Ticker = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$"),
]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
DecimalString = Annotated[str, StringConstraints(pattern=r"^[0-9]+(?:\.[0-9]+)?$")]


class Orderbook(BaseModel):
    model_config = ConfigDict(strict=True)

    yes_dollars: list[tuple[DecimalString, DecimalString]]
    no_dollars: list[tuple[DecimalString, DecimalString]]

    # Kalshi returns ascending bid levels. Preserve strings while checking price order.
    @field_validator("yes_dollars", "no_dollars")
    @classmethod
    def validate_levels(
        cls, levels: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        previous = Decimal("-1")
        for price, quantity in levels:
            value = Decimal(price)
            if not previous < value <= 1 or Decimal(quantity) <= 0:
                raise ValueError("Invalid order-book levels.")
            previous = value
        return levels


class OrderbookEntry(BaseModel):
    model_config = ConfigDict(strict=True)

    ticker: str
    orderbook_fp: Orderbook


class OrderbooksPayload(BaseModel):
    model_config = ConfigDict(strict=True)

    orderbooks: list[Any]


class OrderbookSuccess(BaseModel):
    ticker: str
    source: Literal["live"] = "live"
    retrieved_at: datetime
    orderbook_fp: Orderbook
    yes_truncated: bool
    no_truncated: bool
    error: None = None


class OrderbookError(BaseModel):
    code: str
    source: Source | None = None
    status_code: int | None = None


class OrderbookFailure(BaseModel):
    ticker: str
    orderbook_fp: None = None
    error: OrderbookError


class OrderbooksResponse(BaseModel):
    depth: int
    results: list[OrderbookSuccess | OrderbookFailure]


class PriceRange(BaseModel):
    model_config = ConfigDict(strict=True)

    start: DecimalString
    end: DecimalString
    step: DecimalString


# Dollar fields use USD strings and *_fp fields use contract-count strings.
# Preserve their precision and keep unavailable optional values distinct from zero.
class MarketSummary(BaseModel):
    model_config = ConfigDict(strict=True)

    ticker: NonEmptyString
    event_ticker: NonEmptyString
    market_type: Literal["binary", "scalar"]
    status: NonEmptyString
    title: str | None = None
    yes_sub_title: str | None = None
    no_sub_title: str | None = None
    yes_bid_dollars: DecimalString | None = None
    yes_ask_dollars: DecimalString | None = None
    no_bid_dollars: DecimalString | None = None
    no_ask_dollars: DecimalString | None = None
    last_price_dollars: DecimalString | None = None
    volume_fp: DecimalString | None = None
    volume_24h_fp: DecimalString | None = None
    open_interest_fp: DecimalString | None = None
    updated_time: AwareDatetime | None = None
    close_time: AwareDatetime | None = None


class Market(MarketSummary):
    rules_primary: str
    rules_secondary: str
    subtitle: str | None = None
    yes_bid_size_fp: DecimalString | None = None
    yes_ask_size_fp: DecimalString | None = None
    notional_value_dollars: DecimalString | None = None
    created_time: AwareDatetime | None = None
    open_time: AwareDatetime | None = None
    expected_expiration_time: AwareDatetime | None = None
    latest_expiration_time: AwareDatetime | None = None
    occurrence_datetime: AwareDatetime | None = None
    can_close_early: bool | None = None
    early_close_condition: str | None = None
    strike_type: str | None = None
    floor_strike: float | None = None
    cap_strike: float | None = None
    functional_strike: str | None = None
    custom_strike: dict[str, Any] | None = None
    result: Literal["yes", "no", "scalar", ""] | None = None
    expiration_value: str | None = None
    settlement_value_dollars: DecimalString | None = None
    settlement_ts: AwareDatetime | None = None
    settlement_timer_seconds: int | None = None
    settlement_bounds_type: str | None = None
    settlement_floor_dollars: DecimalString | None = None
    price_level_structure: str | None = None
    price_ranges: list[PriceRange] | None = None
    fee_waiver_expiration_time: AwareDatetime | None = None
    is_provisional: bool | None = None


class MarketPayload(BaseModel):
    model_config = ConfigDict(strict=True)

    market: Market


# Market data with the serving endpoint and local retrieval time.
class MarketResponse(BaseModel):
    source: Source
    retrieved_at: datetime
    market: Market


class MarketListPayload(BaseModel):
    model_config = ConfigDict(strict=True)

    markets: list[MarketSummary]
    cursor: str


# Coverage identifies the queried endpoints independently of market settlement status.
class MarketListResponse(BaseModel):
    source: Literal["live"]
    retrieved_at: datetime
    markets: list[MarketSummary]
    next_cursor: str | None
    truncated: bool
    pagination_supported: Literal[True] = True
    pagination_mode: Literal["upstream"] = "upstream"
    historical_lookup_performed: Literal[False] = False
    combo_markets_included: Literal[False] = False


class SettlementSource(BaseModel):
    model_config = ConfigDict(strict=True)

    name: str | None = None
    url: str | None = None


class SeriesSummary(BaseModel):
    model_config = ConfigDict(strict=True)

    ticker: NonEmptyString
    title: str
    frequency: str
    category: str | None = None
    categories: list[str] | None = None
    tags: list[str] | None = None


# Fee metadata describes series defaults. Event-specific overrides can take precedence.
class Series(SeriesSummary):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)

    contract_url: str | None = None
    contract_terms_url: str | None = None
    settlement_sources: list[SettlementSource] | None = None
    fee_type: str | None = None
    fee_multiplier: float | None = None
    additional_prohibitions: list[str] | None = None
    last_updated_ts: AwareDatetime | None = None


class SeriesPayload(BaseModel):
    model_config = ConfigDict(strict=True)

    series: Series


class SeriesListPayload(BaseModel):
    model_config = ConfigDict(strict=True)

    series: list[SeriesSummary]


class SeriesResponse(BaseModel):
    source: Literal["live"]
    retrieved_at: datetime
    series: Series


# Each page is sliced locally from a fresh full upstream response.
class SeriesListResponse(BaseModel):
    source: Literal["live"]
    retrieved_at: datetime
    series: list[SeriesSummary]
    total_count: int
    truncated: bool = False
    pagination_supported: Literal[True] = True
    pagination_mode: Literal["local"] = "local"
    next_cursor: str | None = None


class EventSummary(BaseModel):
    model_config = ConfigDict(strict=True)

    event_ticker: NonEmptyString
    series_ticker: Ticker
    title: str
    sub_title: str | None = None
    strike_date: AwareDatetime | None = None
    strike_period: str | None = None


# Preserve fee overrides separately from series defaults.
# mutually_exclusive=true means at most one child market can resolve YES.
class Event(EventSummary):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)

    mutually_exclusive: bool | None = None
    collateral_return_type: str | None = None
    settlement_sources: list[SettlementSource] | None = None
    fee_type_override: str | None = None
    fee_multiplier_override: float | None = None
    last_updated_ts: AwareDatetime | None = None
    product_metadata: dict[str, Any] | None = None


class EventPayload(BaseModel):
    model_config = ConfigDict(strict=True)

    event: Event


class EventListPayload(BaseModel):
    model_config = ConfigDict(strict=True)

    events: list[EventSummary]
    cursor: str


class SeriesContextError(BaseModel):
    code: str
    source: Literal["live"] = "live"
    status_code: int | None = None


# Event and parent-series timestamps describe separate reads within one tool call.
class EventResponse(BaseModel):
    source: Literal["live"]
    retrieved_at: datetime
    event: Event
    series: Series | None = None
    series_retrieved_at: datetime | None = None
    series_error: SeriesContextError | None = None
    markets_included: Literal[False] = False


class EventListResponse(BaseModel):
    source: Literal["live"]
    retrieved_at: datetime
    events: list[EventSummary]
    next_cursor: str | None
    truncated: bool
    pagination_supported: Literal[True] = True
    pagination_mode: Literal["upstream"] = "upstream"
    markets_included: Literal[False] = False
