import json

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from kalshi_mcp.client import KalshiClient
from kalshi_mcp.tools import get_market


pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("ticker", ["", " ", "../markets", "X?status=open", "X/Y", "X\n", "X" * 201, 123, None])
async def test_invalid_ticker_never_sends_request(ticker):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        with pytest.raises(ToolError, match="invalid_arguments"):
            await get_market(ticker, KalshiClient(http))
    assert requests == []


async def test_tool_response_shape_and_null_values(market):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"market": market}))
    ) as http:
        result = await get_market(market["ticker"], KalshiClient(http))
    data = result.model_dump(mode="json")
    assert set(data) == {"source", "retrieved_at", "market"}
    assert data["market"]["rules_primary"] == market["rules_primary"]
    assert data["market"]["rules_secondary"] == market["rules_secondary"]
    assert data["market"]["settlement_value_dollars"] is None
    assert data["market"]["settlement_ts"] is None
    assert data["market"]["yes_bid_dollars"] == "0.420000"


async def test_tool_errors_preserve_failure_kind():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))
    ) as http:
        with pytest.raises(ToolError) as error:
            await get_market("MISSING", KalshiClient(http))
    assert json.loads(str(error.value)) == {
        "code": "not_found", "source": "historical", "status_code": 404,
    }
