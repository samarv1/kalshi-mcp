import json
from typing import Annotated

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError

from kalshi_mcp.client import KalshiClient, KalshiError
from kalshi_mcp.models import MarketResponse


Ticker = Annotated[
    str,
    StringConstraints(strict=True, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$"),
]


class GetMarketArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: Ticker


# Validate tool input and translate upstream failures into MCP tool errors.
async def get_market(ticker: str, client: KalshiClient) -> MarketResponse:
    try:
        arguments = GetMarketArguments(ticker=ticker)
    except ValidationError as exc:
        raise ToolError(json.dumps({"code": "invalid_arguments", "field": "ticker"})) from exc

    try:
        return await client.get_market(arguments.ticker)
    except KalshiError as exc:
        raise ToolError(
            json.dumps(
                {"code": exc.code, "source": exc.source, "status_code": exc.status_code}
            )
        ) from exc
