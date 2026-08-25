"""MCP — someone else's tools, on our pipeline.

A server publishes tools; this connects to it, lists what it offers, and
registers each on ``ctx.tools`` so the loop calls it exactly like a built-in.
Guards, approvers, the spill policy and the repeat guard all apply without any
of them knowing the tool runs in another process — the model sees one flat
list, and where each entry lives is a fact about this package alone.

Mount it, then connect::

    await root.plugin(McpClientPlugin, {"servers": {
        "files": {"command": "mcp-server-filesystem", "args": ["/srv"]},
    }})
    await root.mcp.start()

Each server gets a supervised connection: a dropped one is retried with
bounded backoff, and after enough consecutive failures its tools are taken
away rather than left to fail a turn at a time.
"""

from .bridge import (
    DEFAULT_TOOL_CALL_TIMEOUT,
    HASH_LENGTH,
    MAX_PUBLIC_NAME_LENGTH,
    Generation,
    McpTool,
    extract_text,
    fetch_tools,
    public_tool_name,
    sync_tools,
)
from .client import (
    CLIENT_NAME,
    DEFAULT_REQUEST_TIMEOUT,
    OWN_ENV_PREFIX,
    PROTOCOL_VERSION,
    SENSITIVE_ENV_PATTERN,
    McpClient,
    McpError,
    StdioTransport,
    StreamableHttpTransport,
    Transport,
    scrubbed_parent_env,
)
from .connection import (
    RECONNECT_DEFAULTS,
    STABILITY_WINDOW_SECONDS,
    TOOLS_CHANGED,
    TRANSPORT_KINDS,
    Connection,
    McpClientPlugin,
    McpConfigError,
    build_client,
    resolve_reconnect_policy,
    resolve_server,
)

__all__ = [
    # the plugin
    "McpClientPlugin",
    "Connection",
    "McpConfigError",
    "resolve_reconnect_policy",
    "resolve_server",
    "build_client",
    "TOOLS_CHANGED",
    "RECONNECT_DEFAULTS",
    "STABILITY_WINDOW_SECONDS",
    "TRANSPORT_KINDS",
    # the protocol
    "McpClient",
    "McpError",
    "Transport",
    "StdioTransport",
    "StreamableHttpTransport",
    "scrubbed_parent_env",
    "PROTOCOL_VERSION",
    "CLIENT_NAME",
    "SENSITIVE_ENV_PATTERN",
    "OWN_ENV_PREFIX",
    "DEFAULT_REQUEST_TIMEOUT",
    # the bridge
    "public_tool_name",
    "extract_text",
    "fetch_tools",
    "sync_tools",
    "McpTool",
    "Generation",
    "MAX_PUBLIC_NAME_LENGTH",
    "HASH_LENGTH",
    "DEFAULT_TOOL_CALL_TIMEOUT",
]
