"""``python -m pydsh.runtime`` — a runtime a client can spawn.

Assembles a harness from a profile and serves it over stdin/stdout. `stdout`
carries protocol frames and nothing else; logging goes to `stderr`, which is
why the entry point configures it rather than leaving it to a default that
might not.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any, Optional

from ..boot import Harness
from .protocol import JsonRpcTransport, stdin_reader, stdout_writer
from .server import RuntimeServer

#: How long to wait after input closes before giving up on a clean stop.
SHUTDOWN_GRACE_SECONDS = 5.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m pydsh.runtime",
        description="Serve an assembled pydsh context over JSON-RPC on stdio.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="a Python module or .py file exposing PROFILE; the core profile otherwise",
    )
    parser.add_argument("--home", default=None, help="override the data root")
    parser.add_argument("--cwd", default=None, help="the working directory to resolve against")
    parser.add_argument(
        "--log-level", default="warning", help="logging level, written to stderr"
    )
    return parser


async def run(arguments: Any) -> int:
    """Serve until input closes."""
    harness = Harness(arguments.profile, home=arguments.home, cwd=arguments.cwd)
    try:
        ctx = await harness.start()
    except Exception as error:  # noqa: BLE001 - a startup failure must be visible
        logging.getLogger("pydsh.runtime").error("could not assemble: %s", error)
        return 1

    transport = JsonRpcTransport(stdin_reader(), stdout_writer())
    server = RuntimeServer(ctx, transport)
    transport.start()
    try:
        # The client closing its end is how a runtime is told to stop; there is
        # no "quit" frame, because a client that crashed cannot send one.
        while not transport.eof and not transport.closed:
            await asyncio.sleep(0.05)
    finally:
        try:
            await asyncio.wait_for(server.shutdown(), timeout=SHUTDOWN_GRACE_SECONDS)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            pass
        await transport.close("the runtime is stopping")
        await harness.close()
    return 0


def main(argv: Optional[list] = None) -> int:
    arguments = build_parser().parse_args(argv)
    # stderr, explicitly: anything on stdout is a frame, and a log line there
    # is a line the client has to parse.
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, arguments.log_level.upper(), logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
    )
    return asyncio.run(run(arguments))


if __name__ == "__main__":  # pragma: no cover - the process entry point
    raise SystemExit(main())
