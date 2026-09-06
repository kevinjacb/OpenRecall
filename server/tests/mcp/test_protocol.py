import pytest
from openrecall_server.mcp.protocol import (
    PROTOCOL_VERSION, ToolRegistry, ToolSpec, dispatch,
)


def _registry():
    reg = ToolRegistry()

    async def echo(args: dict) -> dict:
        return {"echoed": args["value"]}

    reg.register(ToolSpec(
        name="echo",
        description="Echo a value back.",
        input_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
        },
        handler=echo,
    ))
    return reg


async def test_initialize_reports_protocol_and_tools_capability():
    out = await dispatch(_registry(), {"jsonrpc": "2.0", "id": 1,
                                       "method": "initialize", "params": {}})
    assert out["id"] == 1
    assert out["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert "tools" in out["result"]["capabilities"]


async def test_tools_list_returns_registered_specs():
    out = await dispatch(_registry(), {"jsonrpc": "2.0", "id": 2,
                                       "method": "tools/list"})
    names = [t["name"] for t in out["result"]["tools"]]
    assert names == ["echo"]
    assert out["result"]["tools"][0]["inputSchema"]["required"] == ["value"]


async def test_tools_call_invokes_handler():
    out = await dispatch(_registry(), {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "echo", "arguments": {"value": "hi"}},
    })
    assert out["result"]["structuredContent"] == {"echoed": "hi"}
    assert out["result"]["isError"] is False


async def test_unknown_method_is_32601():
    out = await dispatch(_registry(), {"jsonrpc": "2.0", "id": 4,
                                       "method": "nope"})
    assert out["error"]["code"] == -32601


async def test_unknown_tool_is_32602():
    out = await dispatch(_registry(), {
        "jsonrpc": "2.0", "id": 5, "method": "tools/call",
        "params": {"name": "ghost", "arguments": {}},
    })
    assert out["error"]["code"] == -32602


async def test_handler_exception_becomes_tool_error_not_transport_error():
    reg = ToolRegistry()

    async def boom(args: dict) -> dict:
        raise RuntimeError("kaboom")

    reg.register(ToolSpec(name="boom", description="", input_schema={},
                          handler=boom))
    out = await dispatch(reg, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                               "params": {"name": "boom", "arguments": {}}})
    assert "error" not in out
    assert out["result"]["isError"] is True
    assert "kaboom" in out["result"]["content"][0]["text"]


async def test_notification_returns_none():
    out = await dispatch(_registry(), {"jsonrpc": "2.0", "method": "ping"})
    assert out is None
