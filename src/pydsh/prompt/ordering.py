"""Presentation order for tool schemas.

Models weight what they are shown first, so which tool leads is a deployment
decision rather than an accident of registration. A configured order names the
tools that matter and puts :data:`TOOL_ORDER_REST` where everything else goes —
so adding a tool later does not silently append it to the end of the list, it
lands in the slot the deployment chose for unlisted tools.
"""

from __future__ import annotations

from typing import Optional

#: Where tools not named in a configured order are inserted.
TOOL_ORDER_REST = "<unlisted-tools>"


class ToolOrderError(ValueError):
    """A configured tool order does not describe the registered tools."""


def order_tools(
    tools: list[dict], tool_order: Optional[list[str]] = None
) -> list[dict]:
    """Put tool schemas in presentation order.

    With no configured order, name-sorted — stable and predictable, which is
    what matters when nobody has expressed a preference.

    :raises ToolOrderError: the order names a tool that is not registered.
        Silently ignoring it would leave a deployment believing it had placed a
        tool that is not there.
    """
    if tool_order is None:
        return sorted(tools, key=lambda tool: tool["name"])

    if not tools:
        # Nothing registered at all is not a typo, it is a composition without
        # tools — or one assembling before they mount. Validating the order
        # against an empty registry would make a perfectly good deployment
        # raise on every assembly. With *some* tools present, an unnamed entry
        # really is a mistake, and the check below catches it.
        return []

    by_name = {tool["name"]: tool for tool in tools}
    unknown = [
        name for name in tool_order if name != TOOL_ORDER_REST and name not in by_name
    ]
    if unknown:
        known = ", ".join(sorted(by_name)) or "(none registered)"
        raise ToolOrderError(
            f"tool order names unregistered tool(s): {', '.join(unknown)}; "
            f"registered: {known}"
        )

    listed = {name for name in tool_order if name != TOOL_ORDER_REST}
    rest = sorted(
        (tool for tool in tools if tool["name"] not in listed),
        key=lambda tool: tool["name"],
    )

    ordered: list[dict] = []
    emitted: set[str] = set()
    for name in tool_order:
        if name == TOOL_ORDER_REST:
            ordered.extend(rest)
            continue
        # A duplicate entry is a config typo, not a request to show the tool
        # twice — the model would read two identical schemas and could call
        # either. Keep the first position and ignore the repeat.
        if name not in emitted:
            emitted.add(name)
            ordered.append(by_name[name])
    return ordered


__all__ = ["order_tools", "TOOL_ORDER_REST", "ToolOrderError"]
