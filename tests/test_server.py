import json
import sys

import httpx
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from kalshi_mcp.server import lifespan, mcp


pytestmark = pytest.mark.anyio


async def test_lifespan_closes_shared_http_client():
    async with lifespan(mcp) as client:
        assert isinstance(client.http, httpx.AsyncClient)
        assert not client.http.is_closed
    assert client.http.is_closed


# Run the real MCP subprocess with mocked HTTP responses to keep the test offline.
async def test_stdio_discovery_invocation_errors_and_shutdown(market, tmp_path):
    bootstrap = """
import json
import os
import sys
from unittest.mock import patch
import httpx
from kalshi_mcp.server import main

market = json.loads(os.environ['TEST_MARKET'])
def handle(request):
    if request.url.path.endswith('/MISSING'):
        return httpx.Response(404)
    return httpx.Response(200, json={'market': market})

http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
with patch('kalshi_mcp.server.httpx.AsyncClient', return_value=http):
    main()
print('http_closed=' + str(http.is_closed), file=sys.stderr)
"""
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-c", bootstrap],
        env={"TEST_MARKET": json.dumps(market)},
    )
    stderr_path = tmp_path / "server.stderr"
    with stderr_path.open("w") as stderr:
        async with stdio_client(parameters, errlog=stderr) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                discovery = await session.list_tools()
                assert [tool.name for tool in discovery.tools] == ["get_market"]
                tool = discovery.tools[0]
                assert tool.description == "Read a market's rules, prices, status, and available settlement data."
                assert set(tool.input_schema["properties"]) == {"ticker"}
                assert tool.annotations.read_only_hint is True
                assert tool.output_schema is not None

                result = await session.call_tool("get_market", {"ticker": market["ticker"]})
                assert not result.is_error
                assert result.structured_content["market"]["ticker"] == market["ticker"]
                assert result.structured_content["market"]["yes_bid_dollars"] == "0.420000"
                assert result.structured_content["market"]["settlement_ts"] is None

                missing = await session.call_tool("get_market", {"ticker": "MISSING"})
                assert missing.is_error
                assert "not_found" in missing.content[0].text
                invalid = await session.call_tool("get_market", {"ticker": "../bad"})
                assert invalid.is_error
                absent = await session.call_tool("get_market", {})
                assert absent.is_error
    assert "http_closed=True" in stderr_path.read_text()
