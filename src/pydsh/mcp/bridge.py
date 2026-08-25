"""The tool bridge — another process's tools, on our pipeline.

Every MCP tool becomes an ordinary entry in `ctx.tools`, so guards, approvers,
the spill policy and the repeat guard all apply without any of them knowing
where the tool runs. The model sees one flat list; that some of it lives in a
child process is a fact about this module and nowhere else.

Two things here are load-bearing.

**Names cannot collide.** Two servers with a tool called `search` is ordinary,
and so is a tool whose name contains characters a public name cannot carry.
Normalising those to `_` is what creates the collision risk, so any name that
was normalised or truncated carries a hash of the *original* pair.

**A sync is all or nothing.** The whole list is fetched before the registry is
touched, and a registration failure restores the previous generation rather
than leaving the model with none of that server's tools.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Callable, Optional

from .client import McpClient, McpError

logger = logging.getLogger("pydsh.mcp")

#: The longest public tool name. Providers reject longer ones.
MAX_PUBLIC_NAME_LENGTH = 64

#: Characters a public name may carry. Everything else normalises to `_`.
INVALID_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")

#: Hex characters of identity hash appended to a name that had to change.
HASH_LENGTH = 12

#: How long one tool call may take.
DEFAULT_TOOL_CALL_TIMEOUT = 60.0

#: What a call with no text content says, rather than returning nothing.
def no_text(tool_name: str) -> str:
    return f"({tool_name} returned no text content)"


def public_tool_name(server_name: str, raw_name: str) -> str:
    """The model-facing name of one server's tool. Deterministic, and unique.

    The hash is of the **original** pair, not of the normalised name:
    normalisation is what creates the collision, so hashing its output would
    hash the collision along with it.
    """
    joined = f"mcp__{server_name}__{raw_name}"
    normalized = INVALID_NAME_CHARS.sub("_", joined)
    if normalized == joined and len(normalized) <= MAX_PUBLIC_NAME_LENGTH:
        return normalized
    digest = hashlib.sha256(
        f"{server_name}\0{raw_name}".encode("utf-8")
    ).hexdigest()[:HASH_LENGTH]
    keep = MAX_PUBLIC_NAME_LENGTH - HASH_LENGTH - 1
    return f"{normalized[:keep]}_{digest}"


def extract_text(content: Any, tool_name: str) -> str:
    """Render MCP content blocks into one string.

    A non-text block becomes a stated placeholder rather than nothing. A model
    handed silence where a picture was cannot tell that anything came back, and
    will usually call the tool again.
    """
    parts: list[str] = []
    for block in content or ():
        if not isinstance(block, dict):
            parts.append("[unsupported content: not an object]")
            continue
        kind = block.get("type")
        if kind == "text":
            text = block.get("text")
            if text is not None:
                parts.append(str(text))
        elif kind in ("image", "audio"):
            parts.append(
                f"[{kind}: {block.get('mimeType', 'unknown type')}, content discarded]"
            )
        elif kind in ("resource", "resource_link"):
            parts.append("[resource: content discarded]")
        else:
            parts.append(f"[unsupported content type: {kind}]")
    return "\n".join(parts) or no_text(tool_name)


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


class McpTool:
    """One MCP tool as `ctx.tools` sees it."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict,
        client: McpClient,
        raw_name: str,
        timeout: float,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.client = client
        self.raw_name = raw_name
        self.timeout = timeout

    async def execute(self, arguments: Any, execution: Any = None) -> str:
        """Call the server. **Never raises** — the loop's contract is a return.

        An exception out of here would end a turn that had every right to
        continue, over a failure in someone else's process.
        """
        # A model can emit something that is not an object. Sent as `{}`, the
        # server answers with its own "missing argument" message, which is a
        # better error than anything this layer could invent.
        payload = arguments if isinstance(arguments, dict) else {}
        try:
            result = await self.client.call_tool(
                self.raw_name, payload, timeout=self.timeout
            )
        except McpError as error:
            return f"Error: the MCP tool {self.raw_name!r} failed: {error}"
        except Exception as error:  # noqa: BLE001 - a tool result, not a crash
            return f"Error: the MCP tool {self.raw_name!r} failed: {error}"

        content = result.get("content")
        if isinstance(content, list):
            text = extract_text(content, self.raw_name)
        else:
            rendered = result.get("toolResult")
            text = _json(rendered) if rendered is not None else no_text(self.raw_name)

        if result.get("isError") is True:
            return f"Error: {text}"
        return text


class Generation:
    """One complete set of registered tools from one sync.

    Definitions are kept alongside the disposers so a failed *next* sync can
    put this generation back. The reference keeps only disposers, which is why
    its rollback leaves the model with nothing from that server.
    """

    def __init__(self, tools: Optional[dict] = None, disposers: Optional[dict] = None) -> None:
        self.tools: dict[str, McpTool] = dict(tools or {})
        self.disposers: dict[str, Callable[[], Any]] = dict(disposers or {})

    def dispose(self) -> None:
        for dispose in self.disposers.values():
            try:
                dispose()
            except Exception as error:  # noqa: BLE001 - teardown is best-effort
                logger.warning("mcp: unregistering a tool failed: %s", error)
        self.disposers.clear()

    def __len__(self) -> int:
        return len(self.tools)

    def __contains__(self, name: str) -> bool:
        return name in self.tools


EMPTY_GENERATION = Generation()


async def fetch_tools(
    client: McpClient,
    server_name: str,
    timeout: float = DEFAULT_TOOL_CALL_TIMEOUT,
) -> dict[str, McpTool]:
    """Every page of the server's tool list, as a complete new generation.

    The registry is not touched here, and that is the whole point: a server
    that dies mid-page must leave the model's tool list exactly as it was.

    :raises McpError: the server listed one tool twice.
    """
    tools: dict[str, McpTool] = {}
    cursor: Optional[str] = None
    while True:
        page, cursor = await client.list_tools(cursor)
        for entry in page:
            raw_name = entry.get("name") or ""
            name = public_tool_name(server_name, raw_name)
            if name in tools:
                raise McpError(
                    f"mcp({server_name}): the server listed {raw_name!r} more than "
                    "once, so its tool list cannot be trusted"
                )
            tools[name] = McpTool(
                name=name,
                description=entry.get("description") or "",
                parameters=entry.get("inputSchema")
                or {"type": "object", "properties": {}},
                client=client,
                raw_name=raw_name,
                timeout=timeout,
            )
        if not cursor:
            break
    return tools


def _register_all(ctx: Any, tools: dict[str, McpTool]) -> dict:
    """Register a whole generation, rolling back if any of it fails."""
    disposers: dict[str, Callable[[], Any]] = {}
    try:
        for name, tool in tools.items():
            disposers[name] = ctx.tools.register(tool)
    except Exception:
        for dispose in disposers.values():
            try:
                dispose()
            except Exception:  # noqa: BLE001
                pass
        raise
    return disposers


async def sync_tools(
    client: McpClient,
    ctx: Any,
    server_name: str,
    previous: Generation = EMPTY_GENERATION,
    timeout: float = DEFAULT_TOOL_CALL_TIMEOUT,
    on_failure: str = "contain",
) -> Generation:
    """Bring the registry in line with the server. All of it, or none (I2).

    :param on_failure: ``contain`` logs and keeps the previous generation;
        ``throw`` re-raises, for a deployment that would rather not start.
    """
    tools = await fetch_tools(client, server_name, timeout)

    # Forced order: plugkit refuses a duplicate name, so the previous
    # generation has to come off before the new one goes on.
    previous.dispose()
    try:
        disposers = _register_all(ctx, tools)
    except Exception as error:  # noqa: BLE001
        # Put the old generation back. The reference stops at the rollback and
        # returns nothing, having already removed a working tool list over what
        # may be a transient conflict.
        restored: dict = {}
        try:
            restored = _register_all(ctx, previous.tools)
        except Exception as restore_error:  # noqa: BLE001
            logger.error(
                "mcp(%s): registration failed and the previous tools could not be "
                "restored: %s",
                server_name,
                restore_error,
            )
        logger.error("mcp(%s): tool registration failed: %s", server_name, error)
        if on_failure == "throw":
            raise
        return Generation(previous.tools, restored)

    return Generation(tools, disposers)


__all__ = [
    "public_tool_name",
    "extract_text",
    "no_text",
    "McpTool",
    "Generation",
    "EMPTY_GENERATION",
    "fetch_tools",
    "sync_tools",
    "MAX_PUBLIC_NAME_LENGTH",
    "INVALID_NAME_CHARS",
    "HASH_LENGTH",
    "DEFAULT_TOOL_CALL_TIMEOUT",
]
