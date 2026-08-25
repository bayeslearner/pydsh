"""Tool presentation order — Requirement 4."""

from __future__ import annotations

import pytest

from pydsh.prompt import TOOL_ORDER_REST, ToolOrderError, order_tools


def tools(*names: str) -> list[dict]:
    return [{"name": name, "description": "", "parameters": {}} for name in names]


def names(ordered: list[dict]) -> list[str]:
    return [tool["name"] for tool in ordered]


def test_no_configured_order_sorts_by_name():
    """R4.2 — predictable beats registration-dependent when nobody has said."""
    assert names(order_tools(tools("write", "bash", "read"))) == [
        "bash",
        "read",
        "write",
    ]


def test_a_configured_order_leads_with_the_named_tools():
    """R4.4"""
    ordered = order_tools(
        tools("write", "bash", "read"), ["read", "write", TOOL_ORDER_REST]
    )
    assert names(ordered) == ["read", "write", "bash"]


def test_the_rest_marker_places_the_unlisted_tools():
    """R4.4 — the marker is a position, not a suffix."""
    ordered = order_tools(
        tools("write", "bash", "read", "todo"), ["todo", TOOL_ORDER_REST, "write"]
    )
    assert names(ordered) == ["todo", "bash", "read", "write"]


def test_the_remainder_is_name_sorted():
    ordered = order_tools(tools("z", "a", "m", "keep"), ["keep", TOOL_ORDER_REST])
    assert names(ordered) == ["keep", "a", "m", "z"]


def test_an_unknown_tool_in_the_order_raises():
    """R4.5 — silence would leave a deployment believing it placed a tool."""
    with pytest.raises(ToolOrderError) as caught:
        order_tools(tools("bash"), ["ghost", TOOL_ORDER_REST])
    assert "ghost" in str(caught.value)
    assert "bash" in str(caught.value)  # what it could have meant


def test_a_duplicate_entry_shows_the_tool_once_at_its_first_position():
    """A repeat is a config typo, not a request to show a tool twice — the
    model would read two identical schemas and could call either."""
    ordered = order_tools(tools("bash", "read"), ["bash", TOOL_ORDER_REST, "bash"])
    assert names(ordered) == ["bash", "read"]


def test_an_order_with_only_the_rest_marker_is_name_sorted():
    ordered = order_tools(tools("z", "a"), [TOOL_ORDER_REST])
    assert names(ordered) == ["a", "z"]


def test_no_tools_at_all_is_not_an_error():
    assert order_tools([], ["bash", TOOL_ORDER_REST]) == []


def test_ordering_does_not_mutate_the_input():
    original = tools("b", "a")
    order_tools(original, ["a", TOOL_ORDER_REST])
    assert names(original) == ["b", "a"]
