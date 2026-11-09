from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints


Source = Literal["live", "historical"]
NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
DecimalString = Annotated[str, StringConstraints(pattern=r"^[0-9]+(?:\.[0-9]+)?$")]


class PriceRange(BaseModel):
    model_config = ConfigDict(strict=True)

    start: DecimalString
    end: DecimalString
    step: DecimalString


# Dollar fields use USD strings and *_fp fields use contract-count strings.
# Preserve their precision and keep unavailable optional values distinct from zero.
class Market(BaseModel):
    model_config = ConfigDict(strict=True)

    ticker: NonEmptyString
    event_ticker: NonEmptyString
    market_type: Literal["binary", "scalar"]
    status: NonEmptyString
    rules_primary: str
    rules_secondary: str
    title: str | None = None
    subtitle: str | None = None
    yes_sub_title: str | None = None
    no_sub_title: str | None = None
    yes_bid_dollars: DecimalString | None = None
    yes_ask_dollars: DecimalString | None = None
    no_bid_dollars: DecimalString | None = None
    no_ask_dollars: DecimalString | None = None
    yes_bid_size_fp: DecimalString | None = None
    yes_ask_size_fp: DecimalString | None = None
    last_price_dollars: DecimalString | None = None
    notional_value_dollars: DecimalString | None = None
    volume_fp: DecimalString | None = None
    volume_24h_fp: DecimalString | None = None
    open_interest_fp: DecimalString | None = None
    created_time: AwareDatetime | None = None
    updated_time: AwareDatetime | None = None
    open_time: AwareDatetime | None = None
    close_time: AwareDatetime | None = None
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
