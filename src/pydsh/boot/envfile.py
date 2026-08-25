"""The layered environment — what a deployment configures, and what it cannot.

Three layers, nearest first: the inherited environment, the working
directory's ``.env``, and home's. An inherited value always wins, because a
shell override that a checked-in file could overwrite is not an override.

And one refusal. A `.env` may configure the application; it may **not** set a
variable that decides how the process starts. Those variables determined where
this file was looked for — a file setting one is asking to be loaded from
somewhere other than where it was found.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional

from .home import resolve_home

#: Variable prefixes a file may never set. `PYDSH_` decides where code and data
#: come from; the others decide how the interpreter and linker behave.
BOOTSTRAP_PREFIXES = ("PYDSH_", "XDG_", "DYLD_", "LD_")

#: The file each layer is read from.
ENV_FILE_NAME = ".env"

#: `${VAR}` or `$VAR`.
ENV_PATTERN = re.compile(
    r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)"
)


class EnvFileError(ValueError):
    """A `.env` file that cannot be honoured."""


def parse_env(text: str) -> dict[str, str]:
    """Read `KEY=VALUE` text: blanks and `#` comments skipped, quotes stripped."""
    values: dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _assert_no_bootstrap(values: dict, source: str) -> None:
    for name in values:
        if name.upper().startswith(BOOTSTRAP_PREFIXES):
            raise EnvFileError(
                f"{source} sets {name!r}, which only the launching environment "
                "may provide — it decides where this process loads its code and "
                "data from, including this very file. Export it instead."
            )


def _read(path: Path) -> dict[str, str]:
    try:
        return parse_env(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return {}
    except OSError as error:
        raise EnvFileError(f"{path} could not be read: {error}") from error


def load_layered_env(
    cwd: Optional[str] = None,
    home: Optional[str] = None,
    inherited: Optional[Any] = None,
) -> dict[str, str]:
    """Merge the environment with the `.env` files beneath it.

    :raises EnvFileError: a file setting a bootstrap variable.
    """
    merged = dict(os.environ if inherited is None else inherited)
    layers = [
        (Path(cwd or os.getcwd()) / ENV_FILE_NAME),
        (Path(resolve_home(home)) / ENV_FILE_NAME),
    ]
    for path in layers:
        values = _read(path)
        if not values:
            continue
        _assert_no_bootstrap(values, str(path))
        for name, value in values.items():
            # `setdefault`, not assignment: whatever is already here came from
            # the inherited environment or a nearer file, and both outrank this.
            merged.setdefault(name, value)
    return merged


def interpolate_env(value: Any, env: Optional[Any] = None) -> Any:
    """Substitute `${VAR}` through a config value, recursively.

    An unset variable's reference is left **as written**. Replacing it with an
    empty string turns a missing secret into a configured-looking blank, which
    fails somewhere else entirely and much later.
    """
    env = os.environ if env is None else env

    if isinstance(value, str):
        def replace(match: "re.Match") -> str:
            name = match.group(1) or match.group(2)
            found = env.get(name)
            return found if found is not None else match.group(0)

        return ENV_PATTERN.sub(replace, value)
    if isinstance(value, dict):
        return {key: interpolate_env(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [interpolate_env(item, env) for item in value]
    if isinstance(value, tuple):
        return tuple(interpolate_env(item, env) for item in value)
    return value


__all__ = [
    "parse_env",
    "load_layered_env",
    "interpolate_env",
    "EnvFileError",
    "BOOTSTRAP_PREFIXES",
    "ENV_FILE_NAME",
    "ENV_PATTERN",
]
