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


class Market(BaseModel):
    """Validate exchange fields while preserving decimal strings and missing optional values."""

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


class MarketResponse(BaseModel):
    """Market data with the serving endpoint and local retrieval time."""

    source: Source
    retrieved_at: datetime
    market: Market
