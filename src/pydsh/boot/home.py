"""Where this deployment keeps things — resolved once, in one place.

Every service that needs a durable path asks here rather than reading the
environment itself. That is the whole value: two services resolving "home"
independently will eventually disagree, usually in a way nobody notices until
half a deployment's files are in one directory and half in another.

Resolution is explicit config, then ``PYDSH_HOME``, then ``~/.pydsh``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

#: The directory under the operating system's home, when nothing overrides.
HOME_DIR_NAME = ".pydsh"

#: The variable that overrides it. The same prefix the MCP scrub withholds from
#: child processes — two spellings of "this project's own variables" would mean
#: the scrub was missing some.
HOME_ENV = "PYDSH_HOME"

#: What a path under home looks like when shown to someone.
HOME_DISPLAY = f"~/{HOME_DIR_NAME}"


class HomePathError(ValueError):
    """A path that would not be inside home."""


def expand_home_path(path: str) -> str:
    """Expand a leading ``~``, and nothing else."""
    if path == "~":
        return os.path.expanduser("~")
    if path.startswith(("~/", "~\\")):
        return os.path.join(os.path.expanduser("~"), path[2:])
    return path


def resolve_home(configured: Optional[str] = None, env: Optional[Any] = None) -> str:
    """This deployment's data root, as an absolute path.

    :param configured: an explicit override, which wins over everything.
    :param env: the mapping to read ``PYDSH_HOME`` from; the process
        environment by default.
    """
    env = os.environ if env is None else env
    selected = configured
    if selected is None or (isinstance(selected, str) and not selected.strip()):
        from_env = env.get(HOME_ENV)
        # A blank override is not an override. Treating `PYDSH_HOME=""` as a
        # value resolves home to the working directory, which scatters a
        # deployment's files into wherever it happened to start.
        if from_env is not None and from_env.strip():
            selected = from_env
        else:
            selected = os.path.join(os.path.expanduser("~"), HOME_DIR_NAME)
    return os.path.abspath(expand_home_path(selected))


def home_path(*segments: str, home: Optional[str] = None) -> str:
    """A path beneath home.

    :raises HomePathError: a segment that would land outside it. Resolved on
        both sides, so a symlink cannot be the way out either.
    """
    root = Path(resolve_home(home)).resolve()
    target = root.joinpath(*segments)
    try:
        resolved = target.resolve()
    except OSError as error:  # pragma: no cover - depends on the filesystem
        raise HomePathError(f"cannot resolve {target}") from error
    if resolved != root and root not in resolved.parents:
        raise HomePathError(
            f"{os.path.join(*segments)!r} would land outside the home directory"
        )
    return str(resolved)


def home_display(path: str, home: Optional[str] = None) -> str:
    """A path as it should be *shown* — symbolic, never an absolute one.

    A status line, a log entry and an error message all end up somewhere they
    can be read by someone who should not learn a machine's directory layout.
    """
    root = resolve_home(home)
    absolute = os.path.abspath(expand_home_path(path))
    if absolute == root:
        return HOME_DISPLAY
    if absolute.startswith(root + os.sep):
        return f"{HOME_DISPLAY}/{absolute[len(root) + 1:].replace(os.sep, '/')}"
    user_home = os.path.expanduser("~")
    if absolute.startswith(user_home + os.sep):
        return f"~/{absolute[len(user_home) + 1:].replace(os.sep, '/')}"
    return absolute


__all__ = [
    "resolve_home",
    "home_path",
    "home_display",
    "expand_home_path",
    "HomePathError",
    "HOME_DIR_NAME",
    "HOME_ENV",
    "HOME_DISPLAY",
]
