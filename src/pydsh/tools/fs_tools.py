"""``read`` / ``write`` / ``edit`` — the file system, as a model sees it.

Thin on purpose. Every decision worth making was made in :mod:`pydsh.capability.fs`
— containment against symlinks, budgets that bound the read's cost, an
ambiguous edit refused rather than guessed. A tool that re-checked any of them
would have forked the rule, and the two copies would diverge.

What a tool *does* own is the translation: a schema the model can be shown, and
turning a Python exception into a sentence the model can act on. An exception
reaching the pipeline arrives as a failure with less information than the
message would have carried.
"""

from __future__ import annotations

from typing import Any

from plugkit import Service

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The file to read."},
        "offset": {
            "type": "integer",
            "description": "First line to return, 1-based. Defaults to 1.",
        },
        "limit": {
            "type": "integer",
            "description": "How many lines to return.",
        },
    },
    "required": ["path"],
}

WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The file to write."},
        "content": {"type": "string", "description": "The complete new contents."},
    },
    "required": ["path", "content"],
}

EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "The file to edit."},
        "old_string": {
            "type": "string",
            "description": (
                "The exact text to replace. Must appear exactly once unless "
                "replace_all is set — include surrounding context to make it "
                "unique."
            ),
        },
        "new_string": {"type": "string", "description": "What to put there."},
        "replace_all": {
            "type": "boolean",
            "description": "Replace every occurrence instead of requiring one.",
        },
    },
    "required": ["path", "old_string", "new_string"],
}


def _failure(error: Exception) -> str:
    """A seam's exception as a sentence the model can act on."""
    return f"Error: {error}"


class _Tool:
    """A plain tool object — plugkit accepts anything with these attributes."""

    def __init__(self, name: str, description: str, parameters: dict, execute) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.execute = execute


class FsTools(Service):
    """Registers the file-system tools. Removed when this plugin unloads."""

    provide = "fs_tools"
    inject = ["tools", "fs"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        self._fs = ctx.fs
        for tool in (
            _Tool("read", "Read a UTF-8 file as numbered lines.", READ_SCHEMA, self._read),
            _Tool("write", "Write a UTF-8 file, replacing its contents.", WRITE_SCHEMA, self._write),
            _Tool("edit", "Replace exact text in a file.", EDIT_SCHEMA, self._edit),
        ):
            dispose = ctx.tools.register(tool)
            ctx.effect(lambda d=dispose: d)

    async def _read(self, arguments: dict, execution: Any = None) -> str:
        try:
            result = self._fs.read_text(
                arguments["path"],
                offset=int(arguments.get("offset", 1)),
                limit=int(arguments.get("limit", 2000)),
            )
        except Exception as error:  # noqa: BLE001 - the model is the caller
            return _failure(error)

        body = "\n".join(f"{number:>6}\t{text}" for number, text in result["lines"])
        if result["truncated"]:
            body += (
                f"\n\n[truncated — the file has {result['total_lines']} lines; "
                "read again with a different offset to see more]"
            )
        return body or "[the file is empty]"

    async def _write(self, arguments: dict, execution: Any = None) -> str:
        try:
            result = self._fs.write_text(arguments["path"], arguments["content"])
        except Exception as error:  # noqa: BLE001
            return _failure(error)
        return f"Wrote {result['bytes']} bytes to {result['path']}."

    async def _edit(self, arguments: dict, execution: Any = None) -> str:
        try:
            result = self._fs.edit_text(
                arguments["path"],
                arguments["old_string"],
                arguments["new_string"],
                replace_all=bool(arguments.get("replace_all", False)),
            )
        except Exception as error:  # noqa: BLE001
            # An ambiguous edit arrives here with its count in the message,
            # which is exactly what the model needs to widen its context.
            return _failure(error)
        return f"Made {result['replacements']} replacement(s) in {result['path']}."


__all__ = ["FsTools", "READ_SCHEMA", "WRITE_SCHEMA", "EDIT_SCHEMA"]
