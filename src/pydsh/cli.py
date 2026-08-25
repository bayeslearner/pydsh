"""``pydsh`` — the command line.

Four subcommands over the boot layer: run a prompt, list sessions, serve a
runtime on stdio, serve a gateway on a socket.

One rule runs through all of them: **an expected failure prints one line and
exits non-zero**. A missing profile, an unroutable provider, a port already in
use — a person who typed something wrong should read a sentence, not a stack.
An *unexpected* failure still raises, because a traceback is exactly what that
case needs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Optional

from .boot import Harness, ProfileError
from .boot.harness import HarnessError
from .boot.home import HomePathError, resolve_home
from .llm.errors import LlmError

#: What the process returns when a command could not do what was asked.
EXIT_FAILURE = 1

#: Failures a person caused and can fix: a bad profile, a path outside home, a
#: provider nobody configured, a profile missing what a command needs, a file
#: that is not there, a port that is taken.
#:
#: `ValueError` is broad, and knowingly so — every coded refusal in this package
#: derives from it, and the alternative is enumerating them here and finding out
#: at a user's expense which one was missed. Anything outside this tuple is a
#: bug, and a bug deserves its traceback.
EXPECTED = (
    ProfileError,
    HomePathError,
    HarnessError,
    LlmError,
    FileNotFoundError,
    OSError,
    ValueError,
)


def _shared(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--profile", help="a module or .py file exposing PROFILE")
    parser.add_argument("--home", help="override the data root")
    parser.add_argument("--log-level", help="written to stderr")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pydsh", description="Run and serve a pydsh harness."
    )
    _shared(parser)
    parser.set_defaults(profile=None, home=None, log_level="warning", json=False)

    # The same options again on every subcommand, so both `pydsh --json chat hi`
    # and `pydsh chat hi --json` work. SUPPRESS on the copies is what makes that
    # possible: without it a subparser writes its own default over whatever was
    # given before the subcommand, and the earlier flag silently does nothing.
    shared = _shared(
        argparse.ArgumentParser(add_help=False, argument_default=argparse.SUPPRESS)
    )

    commands = parser.add_subparsers(dest="command", parser_class=argparse.ArgumentParser)

    chat = commands.add_parser("chat", parents=[shared], help="run one prompt and print the answer")
    chat.add_argument("prompt", nargs="*", help="the prompt; read from stdin if absent")
    chat.add_argument("--session", default="cli", help="the session to run in")
    chat.add_argument("--provider", default="", help="the provider route")
    chat.add_argument("--model", default="", help="the model")

    sessions = commands.add_parser("sessions", parents=[shared], help="list the sessions a store holds")
    sessions.add_argument("--session", default=None, help="show one session's events")

    runtime = commands.add_parser("runtime", parents=[shared], help="serve JSON-RPC on stdio")

    gateway = commands.add_parser("gateway", parents=[shared], help="serve JSON-RPC over WebSocket")
    gateway.add_argument("--host", default=None)
    gateway.add_argument("--port", type=int, default=None)
    gateway.add_argument("--max-connections", type=int, default=None)

    return parser


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
async def chat(arguments: Any) -> int:
    from .agent import AgentOptions

    prompt = " ".join(arguments.prompt).strip() or sys.stdin.read().strip()
    if not prompt:
        _fail("nothing to say: give a prompt, or pipe one in")
        return EXIT_FAILURE

    options = AgentOptions(provider=arguments.provider, model=arguments.model)
    async with Harness(arguments.profile, home=arguments.home, options=options) as harness:
        result = await harness.session(arguments.session).run(prompt)
    _emit(
        arguments,
        result.final_response,
        {
            "session_id": result.session_id,
            "final_response": result.final_response,
            "event_count": len(result.events),
        },
    )
    return 0


async def sessions(arguments: Any) -> int:
    async with Harness(arguments.profile, home=arguments.home) as harness:
        ctx = harness.ctx
        store = getattr(ctx, "sessions", None)
        if store is None:
            _fail("this profile mounts no session store")
            return EXIT_FAILURE

        if arguments.session:
            session = store.get(arguments.session)
            if session is None:
                _fail(f"no session {arguments.session!r} is loaded")
                return EXIT_FAILURE
            rows = [
                {"seq": event.seq, "type": event.type, "time": event.time}
                for event in session.events
            ]
            _emit(
                arguments,
                "\n".join(f"{row['seq']:>5}  {row['type']}" for row in rows) or "(no events)",
                {"session_id": session.id, "events": rows},
            )
            return 0

        names = [s.id for s in store.list()]
        _emit(arguments, "\n".join(names) or "(no sessions)", {"sessions": names})
        return 0


async def runtime(arguments: Any) -> int:
    from .runtime.__main__ import run as serve_runtime

    return await serve_runtime(
        argparse.Namespace(
            profile=arguments.profile,
            home=arguments.home,
            cwd=None,
            log_level=arguments.log_level,
        )
    )


async def gateway(arguments: Any) -> int:
    from .gateway import DEFAULT_HOST, DEFAULT_MAX_CONNECTIONS, DEFAULT_PORT
    from .gateway import serve as serve_gateway

    host = arguments.host or DEFAULT_HOST
    port = arguments.port or DEFAULT_PORT
    limit = arguments.max_connections or DEFAULT_MAX_CONNECTIONS

    async with Harness(arguments.profile, home=arguments.home) as harness:
        try:
            served, server = await serve_gateway(
                harness.ctx, host, port, max_connections=limit
            )
        except RuntimeError as error:  # the extra is missing
            _fail(str(error))
            return EXIT_FAILURE
        except OSError as error:  # the port is taken
            _fail(f"cannot listen on {host}:{port}: {error}")
            return EXIT_FAILURE

        print(f"pydsh gateway listening on ws://{host}:{port} (no authentication)", file=sys.stderr)
        try:
            await asyncio.Future()  # until interrupted
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            await served.close()
            server.close()
            await server.wait_closed()
    return 0


COMMANDS = {
    "chat": chat,
    "sessions": sessions,
    "runtime": runtime,
    "gateway": gateway,
}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def _fail(message: str) -> None:
    print(f"pydsh: {message}", file=sys.stderr)


def _emit(arguments: Any, text: str, payload: dict) -> None:
    if getattr(arguments, "json", False):
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(text)


def main(argv: Optional[list] = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    if not arguments.command:
        parser.print_help(sys.stderr)
        return EXIT_FAILURE

    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, str(arguments.log_level).upper(), logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
    )
    # Resolved up front so a bad `--home` is one line here rather than an
    # exception from whichever service happened to need a path first.
    try:
        resolve_home(arguments.home)
    except EXPECTED as error:
        _fail(str(error))
        return EXIT_FAILURE

    try:
        return asyncio.run(COMMANDS[arguments.command](arguments))
    except KeyboardInterrupt:
        return EXIT_FAILURE
    except EXPECTED as error:
        # One line, no traceback (I5). Anything not in EXPECTED still raises —
        # a bug is exactly the case where a stack is what someone needs.
        _fail(str(error))
        return EXIT_FAILURE


if __name__ == "__main__":  # pragma: no cover - the process entry point
    raise SystemExit(main())


__all__ = ["main", "build_parser", "COMMANDS", "EXIT_FAILURE", "EXPECTED"]
