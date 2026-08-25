"""The MCP tool bridge and supervisor — Requirements 3 and 4, properties 1–3.

The bridge's job is that the model cannot tell the difference. So the tests
that matter are about what happens when the other process misbehaves: a
duplicate listing, a registration conflict, a server that dies.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

import pytest

from plugkit import Context, PointsService, ToolsService

from pydsh.mcp import (
    HASH_LENGTH,
    MAX_PUBLIC_NAME_LENGTH,
    Connection,
    Generation,
    McpClient,
    McpClientPlugin,
    McpConfigError,
    McpError,
    extract_text,
    fetch_tools,
    public_tool_name,
    resolve_reconnect_policy,
    resolve_server,
    sync_tools,
)
from pydsh.mcp.bridge import McpTool

pytestmark = pytest.mark.asyncio


class FakeClient:
    """A client with a scripted tool list and call behaviour."""

    def __init__(self, pages=None, call=None) -> None:
        self.pages = pages if pages is not None else [([{"name": "echo"}], None)]
        self.call = call
        self.calls: list = []
        self.closed = False
        self.handlers: dict = {}
        self._page = 0

    def on_notification(self, method, handler):
        self.handlers[method] = handler

    async def connect(self):
        return {}

    async def list_tools(self, cursor=None):
        page = self.pages[self._page]
        self._page = min(self._page + 1, len(self.pages) - 1)
        if callable(page):
            return page(cursor)
        return page

    async def call_tool(self, name, arguments=None, timeout=None):
        self.calls.append((name, arguments, timeout))
        if callable(self.call):
            return self.call(name, arguments)
        if isinstance(self.call, Exception):
            raise self.call
        return self.call or {"content": [{"type": "text", "text": "ok"}]}

    async def close(self, reason: str = ""):
        self.closed = True


async def tools_context() -> Context:
    root = Context()
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    return root


# --------------------------------------------------------------------------- #
# R3.1, R3.2 — naming (property 1)
# --------------------------------------------------------------------------- #
async def test_an_ordinary_name_is_readable():
    assert public_tool_name("files", "read") == "mcp__files__read"


async def test_the_server_name_keeps_two_servers_apart():
    assert public_tool_name("a", "search") != public_tool_name("b", "search")


async def test_names_that_normalise_the_same_stay_distinct():
    """Property 1 (I1) — normalisation is what creates the collision.

    `a/b` and `a_b` both normalise to `mcp__s__a_b`. The one that had to change
    carries a hash of its *original* pair, so the two cannot collapse.
    """
    changed = public_tool_name("s", "a/b")
    unchanged = public_tool_name("s", "a_b")
    assert changed != unchanged
    assert unchanged == "mcp__s__a_b"
    assert changed.startswith("mcp__s__a_b_")
    assert len(changed) == len("mcp__s__a_b_") + HASH_LENGTH


async def test_a_long_name_is_truncated_and_hashed():
    """R3.2."""
    name = public_tool_name("server", "x" * 200)
    assert len(name) == MAX_PUBLIC_NAME_LENGTH
    assert name[-(HASH_LENGTH + 1)] == "_"


async def test_two_long_names_sharing_a_prefix_stay_distinct():
    """Property 1 — truncation is the other way two names collapse."""
    prefix = "y" * 200
    assert public_tool_name("s", prefix + "a") != public_tool_name("s", prefix + "b")


async def test_naming_is_deterministic():
    assert public_tool_name("s", "a/b") == public_tool_name("s", "a/b")


async def test_a_name_that_survives_normalisation_carries_no_hash():
    """An unchanged name should be readable, not gratuitously hashed."""
    assert public_tool_name("files", "read-file_2") == "mcp__files__read-file_2"


# --------------------------------------------------------------------------- #
# R3.3 — rendering results
# --------------------------------------------------------------------------- #
async def test_text_blocks_join():
    assert extract_text([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}], "t") == "a\nb"


async def test_a_non_text_block_becomes_a_stated_placeholder():
    """A model handed silence cannot tell that anything came back."""
    rendered = extract_text([{"type": "image", "mimeType": "image/png"}], "t")
    assert "image/png" in rendered and "discarded" in rendered

    assert "resource" in extract_text([{"type": "resource"}], "t")
    assert "audio" in extract_text([{"type": "audio"}], "t")
    assert "unsupported" in extract_text([{"type": "hologram"}], "t")
    assert "not an object" in extract_text(["a bare string"], "t")


async def test_no_content_says_so():
    assert "no text content" in extract_text([], "search")


# --------------------------------------------------------------------------- #
# R3.4–R3.6 — syncing (property 2)
# --------------------------------------------------------------------------- #
async def test_a_sync_registers_every_page():
    """R3.4."""
    root = await tools_context()
    client = FakeClient(
        pages=[([{"name": "a"}], "p2"), ([{"name": "b"}], None)]
    )
    generation = await sync_tools(client, root, "srv")
    assert sorted(generation.tools) == ["mcp__srv__a", "mcp__srv__b"]
    assert root.tools.names() == ["mcp__srv__a", "mcp__srv__b"]


async def test_a_duplicate_listing_is_refused():
    """R3.5 — a tool list that contradicts itself cannot be trusted."""
    root = await tools_context()
    client = FakeClient(pages=[([{"name": "a"}, {"name": "a"}], None)])
    with pytest.raises(McpError) as caught:
        await fetch_tools(client, "srv")
    assert "more than once" in str(caught.value)


async def test_a_failed_fetch_leaves_the_registry_untouched():
    """R3.4 — the reason the fetch comes first."""
    root = await tools_context()
    first = await sync_tools(FakeClient(pages=[([{"name": "a"}], None)]), root, "srv")
    assert root.tools.names() == ["mcp__srv__a"]

    class Dying(FakeClient):
        async def list_tools(self, cursor=None):
            raise McpError("the server died mid-page")

    with pytest.raises(McpError):
        await sync_tools(Dying(), root, "srv", first)
    assert root.tools.names() == ["mcp__srv__a"]


async def test_a_second_sync_replaces_the_generation():
    root = await tools_context()
    first = await sync_tools(FakeClient(pages=[([{"name": "a"}], None)]), root, "srv")
    second = await sync_tools(
        FakeClient(pages=[([{"name": "b"}], None)]), root, "srv", first
    )
    assert root.tools.names() == ["mcp__srv__b"]
    assert sorted(second.tools) == ["mcp__srv__b"]


async def test_a_failed_registration_restores_the_previous_generation():
    """Property 2 (R3.6, I2) — the reference leaves the model with nothing."""
    root = await tools_context()
    previous = await sync_tools(FakeClient(pages=[([{"name": "a"}], None)]), root, "srv")
    assert root.tools.names() == ["mcp__srv__a"]

    # Something else takes the name the new generation wants, so registering
    # it raises partway through.
    class Squatter:
        name = "mcp__srv__c"
        description = "in the way"
        parameters: dict = {}

        async def execute(self, arguments, execution=None):
            return ""

    root.tools.register(Squatter())

    result = await sync_tools(
        FakeClient(pages=[([{"name": "b"}, {"name": "c"}], None)]),
        root,
        "srv",
        previous,
    )
    assert "mcp__srv__a" in root.tools.names(), "a transient conflict cost a working tool"
    assert sorted(result.tools) == ["mcp__srv__a"]
    assert "mcp__srv__b" not in root.tools.names()


async def test_a_failed_registration_can_be_configured_to_raise():
    root = await tools_context()

    class Squatter:
        name = "mcp__srv__a"
        description = "in the way"
        parameters: dict = {}

        async def execute(self, arguments, execution=None):
            return ""

    root.tools.register(Squatter())
    with pytest.raises(Exception):
        await sync_tools(
            FakeClient(pages=[([{"name": "a"}], None)]),
            root,
            "srv",
            on_failure="throw",
        )


# --------------------------------------------------------------------------- #
# R3.7, R3.8 — calling (property 3)
# --------------------------------------------------------------------------- #
async def test_a_call_goes_out_under_the_raw_name():
    root = await tools_context()
    client = FakeClient(pages=[([{"name": "read-file"}], None)])
    await sync_tools(client, root, "srv", timeout=12)

    result = await root.tools.execute("mcp__srv__read-file", {"path": "/x"})
    assert result.ok and result.value == "ok"
    assert client.calls == [("read-file", {"path": "/x"}, 12)]


async def test_an_error_result_comes_back_as_a_failure():
    """R3.7."""
    root = await tools_context()
    client = FakeClient(
        call={"content": [{"type": "text", "text": "no such file"}], "isError": True}
    )
    await sync_tools(client, root, "srv")

    result = await root.tools.execute("mcp__srv__echo", {})
    assert "no such file" in str(result.value)
    assert str(result.value).startswith("Error:")


async def test_a_transport_failure_is_a_result_not_an_exception():
    """Property 3 (I5) — an exception here would end a turn that could go on."""
    root = await tools_context()
    await sync_tools(FakeClient(call=McpError("the socket died")), root, "srv")

    result = await root.tools.execute("mcp__srv__echo", {})
    assert result.ok  # the pipeline completed
    assert "socket died" in str(result.value)


async def test_an_unexpected_exception_is_also_a_result():
    root = await tools_context()

    def explode(name, arguments):
        raise RuntimeError("something else entirely")

    await sync_tools(FakeClient(call=explode), root, "srv")
    result = await root.tools.execute("mcp__srv__echo", {})
    assert "something else entirely" in str(result.value)


async def test_arguments_that_are_not_an_object_are_sent_as_empty():
    """R3.8 — the server's own "missing argument" beats one invented here."""
    client = FakeClient()
    tool = McpTool("mcp__s__t", "", {}, client, "t", 5)
    await tool.execute("not an object")
    assert client.calls[-1][1] == {}


async def test_a_result_with_no_content_list_falls_back_to_tool_result():
    client = FakeClient(call={"toolResult": {"rows": 3}})
    tool = McpTool("mcp__s__t", "", {}, client, "t", 5)
    assert "rows" in await tool.execute({})


async def test_a_tool_carries_the_servers_schema():
    root = await tools_context()
    schema = {"type": "object", "properties": {"path": {"type": "string"}}}
    await sync_tools(
        FakeClient(pages=[([{"name": "read", "description": "reads", "inputSchema": schema}], None)]),
        root,
        "srv",
    )
    tool = root.tools.get("mcp__srv__read")
    assert tool.description == "reads"
    assert tool.parameters == schema


async def test_a_tool_with_no_schema_gets_an_empty_object():
    root = await tools_context()
    await sync_tools(FakeClient(pages=[([{"name": "n"}], None)]), root, "srv")
    assert root.tools.get("mcp__srv__n").parameters == {
        "type": "object",
        "properties": {},
    }


# --------------------------------------------------------------------------- #
# R4.2 — configuration
# --------------------------------------------------------------------------- #
async def test_reconnect_defaults_resolve():
    policy = resolve_reconnect_policy(None)
    assert policy["enabled"] is True and policy["max_attempts"] == 10


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"wibble": 1}, "not a reconnect option"),
        ({"initial_delay": 0}, "positive"),
        ({"max_delay": -1}, "positive"),
        ({"initial_delay": 10, "max_delay": 1}, "must not exceed"),
        ({"max_attempts": 0}, "positive integer"),
        ({"max_attempts": 1.5}, "positive integer"),
    ],
)
async def test_an_impossible_reconnect_policy_is_refused(config, expected):
    with pytest.raises(McpConfigError) as caught:
        resolve_reconnect_policy(config)
    assert expected in str(caught.value)


@pytest.mark.parametrize(
    "config,expected",
    [
        ({"transport": "carrier-pigeon"}, "transports are"),
        ({}, "needs a `command`"),
        ({"transport": "http"}, "needs a `url`"),
        ({"command": "x", "tool_call_timeout": 0}, "positive number"),
    ],
)
async def test_an_unusable_server_is_refused_at_mount(config, expected):
    with pytest.raises(McpConfigError) as caught:
        resolve_server("srv", config)
    assert expected in str(caught.value)


async def test_a_server_resolves_its_defaults():
    server = resolve_server("files", {"command": "run-me", "args": ["--x"]})
    assert server["transport"] == "stdio"
    assert server["args"] == ["--x"]
    assert server["reconnect"]["max_attempts"] == 10


# --------------------------------------------------------------------------- #
# R4 — the supervisor
# --------------------------------------------------------------------------- #
async def plugin_with(servers: dict, make_client) -> Context:
    root = Context()
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    await root.plugin(
        McpClientPlugin, {"servers": servers, "make_client": make_client}
    )
    return root


async def test_the_plugin_connects_and_registers():
    """R4.1."""
    client = FakeClient(pages=[([{"name": "echo"}], None)])
    root = await plugin_with({"srv": {"command": "x"}}, lambda server: client)
    await root.mcp.start()

    assert root.tools.names() == ["mcp__srv__echo"]
    assert root.mcp.tools_of("srv") == ["mcp__srv__echo"]


async def test_start_is_idempotent():
    client = FakeClient()
    root = await plugin_with({"srv": {"command": "x"}}, lambda server: client)
    await root.mcp.start()
    await root.mcp.start()
    assert len(root.mcp.connections) == 1


async def test_list_changed_triggers_a_resync():
    """R4.6."""
    pages = [([{"name": "a"}], None), ([{"name": "b"}], None)]
    calls = {"n": 0}

    class Changing(FakeClient):
        async def list_tools(self, cursor=None):
            page = pages[min(calls["n"], len(pages) - 1)]
            return page

    client = Changing()
    root = await plugin_with({"srv": {"command": "x"}}, lambda server: client)
    await root.mcp.start()
    assert root.tools.names() == ["mcp__srv__a"]

    calls["n"] = 1
    await client.handlers["notifications/tools/list_changed"]({})
    assert root.tools.names() == ["mcp__srv__b"]


async def test_a_first_connection_failure_does_not_raise_by_default():
    def make(server):
        raise McpError("cannot reach it")

    root = await plugin_with({"srv": {"command": "x", "reconnect": {"enabled": False}}}, make)
    await root.mcp.start()
    assert root.tools.names() == []


async def test_a_first_connection_failure_can_be_fatal():
    def make(server):
        raise McpError("cannot reach it")

    root = await plugin_with({"srv": {"command": "x", "fail_on_startup_error": True}}, make)
    with pytest.raises(McpError):
        await root.mcp.start()


async def test_backoff_grows_and_is_capped():
    """R4.3."""
    server = resolve_server(
        "srv", {"command": "x", "reconnect": {"initial_delay": 1, "max_delay": 8}}
    )
    connection = Connection(None, server)
    delays = []
    for attempt in range(1, 7):
        connection.attempts = attempt
        delays.append(connection.backoff())
    assert delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


async def test_giving_up_takes_the_tools_away():
    """R4.5 — offering what cannot run costs a turn to discover."""
    client = FakeClient(pages=[([{"name": "echo"}], None)])
    root = await plugin_with({"srv": {"command": "x"}}, lambda server: client)
    await root.mcp.start()
    assert root.tools.names() == ["mcp__srv__echo"]

    connection = root.mcp.connections["srv"]
    connection.attempts = 99
    await connection._give_up()

    assert connection.gave_up
    assert root.tools.names() == []


async def test_a_stable_connection_forgives_its_failures():
    """R4.4 — failures an hour apart are not consecutive."""
    import pydsh.mcp.connection as connection_module

    server = resolve_server("srv", {"command": "x"})
    connection = Connection(None, server)
    connection.attempts = 5
    connection._connected_at = 0.0  # long ago, on the monotonic clock

    original = connection_module.time.monotonic
    connection_module.time.monotonic = lambda: connection_module.STABILITY_WINDOW_SECONDS + 1
    try:
        assert connection._forgiven()
    finally:
        connection_module.time.monotonic = original


async def test_disposal_closes_the_client_and_unregisters():
    """R4.8."""
    client = FakeClient(pages=[([{"name": "echo"}], None)])
    root = await plugin_with({"srv": {"command": "x"}}, lambda server: client)
    await root.mcp.start()

    await root.mcp.shutdown()
    assert client.closed
    assert root.tools.names() == []
    assert root.mcp.connections == {}


async def test_shutdown_is_idempotent():
    client = FakeClient()
    root = await plugin_with({"srv": {"command": "x"}}, lambda server: client)
    await root.mcp.start()
    await root.mcp.shutdown()
    await root.mcp.shutdown()


async def test_syncs_are_serialised():
    """R4.7 — two swaps at once each dispose what the other registered."""
    order: list = []

    class Slow(FakeClient):
        async def list_tools(self, cursor=None):
            order.append("start")
            await asyncio.sleep(0.01)
            order.append("end")
            return [{"name": "a"}], None

    client = Slow()
    root = await plugin_with({"srv": {"command": "x"}}, lambda server: client)
    await root.mcp.start()
    order.clear()

    connection = root.mcp.connections["srv"]
    await asyncio.gather(connection.sync(), connection.sync(), connection.sync())
    # Never two fetches in flight: every start is followed by its own end.
    assert order == ["start", "end"] * 3
    assert root.tools.names() == ["mcp__srv__a"]


async def test_disposing_a_generation_twice_is_harmless():
    """Disposal runs on several paths — give-up, re-sync, shutdown."""
    root = await tools_context()
    generation = await sync_tools(FakeClient(pages=[([{"name": "a"}], None)]), root, "srv")
    assert root.tools.names() == ["mcp__srv__a"]

    generation.dispose()
    generation.dispose()
    assert root.tools.names() == []


async def test_a_disposer_that_throws_does_not_stop_the_rest():
    """Teardown is best-effort: one bad disposer must not strand the others."""
    root = await tools_context()
    generation = await sync_tools(
        FakeClient(pages=[([{"name": "a"}, {"name": "b"}], None)]), root, "srv"
    )

    def explode():
        raise RuntimeError("disposal went wrong")

    generation.disposers["mcp__srv__a"] = explode
    generation.dispose()
    assert "mcp__srv__b" not in root.tools.names()
