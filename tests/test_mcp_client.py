"""The MCP client and its transports — Requirements 1 and 2.

The stdio tests spawn a **real child process** speaking real JSON-RPC, because
that is the only way to test what actually goes wrong: a server that logs to
stdout, one that never answers, one that has to be killed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Optional

import pytest

from pydsh.mcp import (
    OWN_ENV_PREFIX,
    PROTOCOL_VERSION,
    McpClient,
    McpError,
    StdioTransport,
    Transport,
    scrubbed_parent_env,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# A transport with no process and no socket
# --------------------------------------------------------------------------- #
class FakeTransport(Transport):
    """Answers requests from a script, and can push notifications."""

    def __init__(self, responses: Optional[dict] = None) -> None:
        self.responses = dict(responses or {})
        self.sent: list = []
        self.started = False
        self.closed = False
        self._on_message = None

    async def start(self, on_message) -> None:
        self.started = True
        self._on_message = on_message

    async def send(self, payload: dict) -> None:
        self.sent.append(payload)
        if "id" not in payload:
            return  # a notification wants no answer
        answer = self.responses.get(payload["method"])
        if answer is None:
            return  # nothing comes back: the caller will time out
        if callable(answer):
            answer = answer(payload)
        self.deliver({"jsonrpc": "2.0", "id": payload["id"], **answer})

    def deliver(self, payload: dict) -> None:
        self._on_message(payload)

    async def close(self) -> None:
        self.closed = True


HANDSHAKE = {
    "initialize": {
        "result": {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": "fake", "version": "1"},
        }
    }
}


# --------------------------------------------------------------------------- #
# R1 — the client
# --------------------------------------------------------------------------- #
async def test_connect_handshakes_and_confirms():
    """R1.1 — `initialized` is what tells a server it may start sending."""
    transport = FakeTransport(HANDSHAKE)
    client = McpClient(transport)
    await client.connect()

    assert transport.started
    assert client.server_info == {"name": "fake", "version": "1"}
    assert [m["method"] for m in transport.sent] == [
        "initialize",
        "notifications/initialized",
    ]
    assert transport.sent[0]["params"]["protocolVersion"] == PROTOCOL_VERSION
    assert "id" not in transport.sent[1]


async def test_a_response_matches_its_request():
    """R1.2."""
    transport = FakeTransport({**HANDSHAKE, "ping": {"result": {"pong": True}}})
    client = McpClient(transport)
    await client.connect()
    assert await client.request("ping") == {"pong": True}


async def test_an_error_response_carries_its_code():
    """R1.3."""
    transport = FakeTransport(
        {**HANDSHAKE, "boom": {"error": {"code": -32601, "message": "no such method"}}}
    )
    client = McpClient(transport)
    await client.connect()

    with pytest.raises(McpError) as caught:
        await client.request("boom")
    assert caught.value.code == -32601
    assert "no such method" in str(caught.value)


async def test_a_string_id_matches():
    """R1.4 — the reference coerces with int(), which raises on one."""
    transport = FakeTransport(HANDSHAKE)
    client = McpClient(transport)
    await client.connect()

    pending = asyncio.ensure_future(client.request("slow", timeout=2))
    await asyncio.sleep(0)
    sent_id = transport.sent[-1]["id"]
    # A server is entitled to echo the id in another JSON type.
    transport.deliver({"jsonrpc": "2.0", "id": sent_id, "result": {"ok": True}})
    assert await pending == {"ok": True}


async def test_a_request_that_is_never_answered_times_out():
    transport = FakeTransport(HANDSHAKE)
    client = McpClient(transport)
    await client.connect()

    with pytest.raises(McpError) as caught:
        await client.request("silence", timeout=0.05)
    assert "timed out" in str(caught.value)


async def test_notifications_reach_their_handler():
    """R1.5."""
    transport = FakeTransport(HANDSHAKE)
    client = McpClient(transport)
    seen: list = []

    async def handler(params):
        seen.append(params)

    client.on_notification("notifications/tools/list_changed", handler)
    await client.connect()

    transport.deliver(
        {"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {"x": 1}}
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert seen == [{"x": 1}]


async def test_an_unhandled_notification_is_ignored():
    transport = FakeTransport(HANDSHAKE)
    client = McpClient(transport)
    await client.connect()
    transport.deliver({"jsonrpc": "2.0", "method": "notifications/whatever"})
    assert not client.closed


async def test_close_fails_in_flight_requests_with_a_reason():
    """R1.6 — a bare cancellation looks like the *caller's* task was cancelled."""
    transport = FakeTransport(HANDSHAKE)
    client = McpClient(transport)
    await client.connect()

    pending = asyncio.ensure_future(client.request("slow", timeout=5))
    await asyncio.sleep(0)
    await client.close("the server went away")

    with pytest.raises(McpError) as caught:
        await pending
    assert "went away" in str(caught.value)
    assert transport.closed


async def test_close_is_idempotent():
    transport = FakeTransport(HANDSHAKE)
    client = McpClient(transport)
    await client.connect()
    await client.close()
    await client.close()
    assert client.closed


async def test_a_closed_client_refuses_work():
    transport = FakeTransport(HANDSHAKE)
    client = McpClient(transport)
    await client.connect()
    await client.close()

    with pytest.raises(McpError):
        await client.request("anything")
    with pytest.raises(McpError):
        await client.notify("anything")


async def test_list_tools_follows_pagination():
    """R1.7."""
    pages = {
        None: {"tools": [{"name": "a"}], "nextCursor": "p2"},
        "p2": {"tools": [{"name": "b"}], "nextCursor": None},
    }

    def answer(payload):
        cursor = (payload.get("params") or {}).get("cursor")
        return {"result": pages[cursor]}

    transport = FakeTransport({**HANDSHAKE, "tools/list": answer})
    client = McpClient(transport)
    await client.connect()

    first, cursor = await client.list_tools()
    assert [t["name"] for t in first] == ["a"] and cursor == "p2"
    second, cursor = await client.list_tools(cursor)
    assert [t["name"] for t in second] == ["b"] and cursor is None


async def test_call_tool_sends_the_raw_name_and_arguments():
    transport = FakeTransport({**HANDSHAKE, "tools/call": {"result": {"content": []}}})
    client = McpClient(transport)
    await client.connect()

    await client.call_tool("read", {"path": "/x"})
    call = transport.sent[-1]
    assert call["params"] == {"name": "read", "arguments": {"path": "/x"}}


# --------------------------------------------------------------------------- #
# R2.3 — the environment scrub
# --------------------------------------------------------------------------- #
async def test_the_scrub_removes_credential_shaped_names(monkeypatch):
    """I3 — the reference's version removes nothing at all."""
    monkeypatch.setenv("SOME_API_KEY", "secret")
    monkeypatch.setenv("MY_PASSWORD", "secret")
    monkeypatch.setenv("A_TOKEN", "secret")
    monkeypatch.setenv("CLIENT_SECRET", "secret")
    monkeypatch.setenv("HARMLESS", "fine")

    scrubbed = scrubbed_parent_env()
    assert "HARMLESS" in scrubbed
    for name in ("SOME_API_KEY", "MY_PASSWORD", "A_TOKEN", "CLIENT_SECRET"):
        assert name not in scrubbed


async def test_the_scrub_removes_our_own_variables(monkeypatch):
    monkeypatch.setenv(f"{OWN_ENV_PREFIX}HOME", "/somewhere")
    assert f"{OWN_ENV_PREFIX}HOME" not in scrubbed_parent_env()


async def test_the_child_environment_is_the_scrub_plus_config(monkeypatch):
    """I3 — the scrub is the base, never an overlay over a full copy."""
    monkeypatch.setenv("SOME_API_KEY", "secret")
    transport = StdioTransport("true", env={"EXTRA": "yes"})

    environment = transport.child_env()
    assert "SOME_API_KEY" not in environment
    assert environment["EXTRA"] == "yes"


# --------------------------------------------------------------------------- #
# R2.2, R2.4 — a real child process
# --------------------------------------------------------------------------- #
SERVER = r'''
import json, sys, os
print("this server logs to stdout, which is not JSON", flush=True)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18",
                  "serverInfo": {"name": "echo", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "echoes",
                             "inputSchema": {"type": "object",
                                             "properties": {"text": {"type": "string"}}}}]}
    elif method == "tools/call":
        args = message["params"].get("arguments") or {}
        result = {"content": [{"type": "text", "text": args.get("text", "")}]}
    elif method == "env":
        result = {"content": [{"type": "text", "text": os.environ.get("SOME_API_KEY", "")}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                          "error": {"code": -32601, "message": "unknown"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
'''


def echo_server(tmp_path, extra: str = "") -> StdioTransport:
    script = tmp_path / "server.py"
    script.write_text(SERVER + extra)
    return StdioTransport(sys.executable, ["-u", str(script)])


async def test_a_real_child_process_speaks_json_rpc(tmp_path):
    """R2.2 — including a server that logs to stdout before answering."""
    client = McpClient(echo_server(tmp_path))
    try:
        await client.connect()
        assert client.server_info["name"] == "echo"

        tools, cursor = await client.list_tools()
        assert [t["name"] for t in tools] == ["echo"] and cursor is None

        result = await client.call_tool("echo", {"text": "hello there"})
        assert result["content"][0]["text"] == "hello there"
    finally:
        await client.close()


async def test_a_real_child_does_not_see_scrubbed_variables(tmp_path, monkeypatch):
    """I3, end to end: the child reports what it actually got."""
    monkeypatch.setenv("SOME_API_KEY", "must-not-leak")
    client = McpClient(echo_server(tmp_path))
    try:
        await client.connect()
        result = await client.request("env")
        assert result["content"][0]["text"] == ""
    finally:
        await client.close()


async def test_closing_stops_the_child(tmp_path):
    """R2.4."""
    transport = echo_server(tmp_path)
    client = McpClient(transport)
    await client.connect()
    process = transport._process
    assert process.returncode is None

    await client.close()
    assert process.returncode is not None


async def test_a_child_that_ignores_stdin_close_is_killed(tmp_path):
    """R2.4 — the escalation, and it reaches the process group (I4)."""
    script = tmp_path / "stubborn.py"
    script.write_text(
        "import signal, time, sys\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "sys.stdin.close()\n"
        "while True: time.sleep(0.05)\n"
    )
    transport = StdioTransport(sys.executable, ["-u", str(script)])
    await transport.start(lambda payload: None)
    process = transport._process
    await asyncio.sleep(0.1)

    await transport.close()
    assert process.returncode is not None


async def test_the_child_runs_in_its_own_process_group(tmp_path):
    transport = echo_server(tmp_path)
    await transport.start(lambda payload: None)
    try:
        assert os.getpgid(transport._process.pid) != os.getpgid(os.getpid())
    finally:
        await transport.close()


async def test_sending_to_a_stopped_child_is_an_mcp_error(tmp_path):
    transport = echo_server(tmp_path)
    client = McpClient(transport)
    await client.connect()
    await transport.close()

    with pytest.raises(McpError):
        await client.request("tools/list", timeout=1)
