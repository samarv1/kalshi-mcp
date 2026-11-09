import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def market():
    return {
        "ticker": "EXAMPLE-26NOV09-YES",
        "event_ticker": "EXAMPLE-26NOV09",
        "market_type": "binary",
        "status": "active",
        "yes_sub_title": "Example outcome",
        "rules_primary": "The market resolves according to the example outcome.",
        "rules_secondary": "Additional example contract terms.",
        "yes_bid_dollars": "0.420000",
        "yes_ask_dollars": "0.4500",
        "volume_fp": "123.50",
        "updated_time": "2026-11-08T12:00:00Z",
    }
