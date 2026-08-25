"""A profile — what to mount, with what config, in what order.

Data, not code that mounts. That distinction earns its keep twice: a profile
can be inspected and diffed before anything runs, and it can be assembled from
more than one source — the core profile plus a consumer's own — without either
of them knowing about the other.

Everything is resolved **before** anything mounts. A profile with a typo in
entry nine must not leave eight plugins mounted and a context nobody can use.
"""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .envfile import interpolate_env

#: The attribute a profile module exposes.
PROFILE_ATTRIBUTE = "PROFILE"


class ProfileError(ValueError):
    """A profile entry that cannot be mounted."""


@dataclass(frozen=True)
class ProfileEntry:
    """One plugin and the config it is mounted with."""

    plugin: Any
    config: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return getattr(self.plugin, "__name__", None) or type(self.plugin).__name__


def _entry(raw: Any, index: int, env: Optional[Any]) -> ProfileEntry:
    """One entry, validated and interpolated. Errors name the index (I5)."""
    if isinstance(raw, ProfileEntry):
        plugin, config = raw.plugin, raw.config
    elif isinstance(raw, (tuple, list)):
        if not raw or len(raw) > 2:
            raise ProfileError(
                f"profile entry {index} must be (plugin, config) or (plugin,), "
                f"got {len(raw)} items"
            )
        plugin = raw[0]
        config = raw[1] if len(raw) == 2 else {}
    else:
        plugin, config = raw, {}

    if plugin is None:
        raise ProfileError(f"profile entry {index} has no plugin")
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise ProfileError(
            f"profile entry {index} ({_describe(plugin)}) has a "
            f"{type(config).__name__} config; it must be a mapping"
        )
    return ProfileEntry(plugin, interpolate_env(config, env))


def _describe(plugin: Any) -> str:
    return getattr(plugin, "__name__", None) or repr(plugin)


def resolve_profile(profile: Any, env: Optional[Any] = None) -> list[ProfileEntry]:
    """Validate and interpolate a whole profile, mounting nothing."""
    if profile is None:
        return []
    if isinstance(profile, (str, Path)):
        profile = load_profile(profile)
    if not isinstance(profile, (list, tuple)):
        raise ProfileError(
            f"a profile is a list of entries, got {type(profile).__name__}"
        )
    return [_entry(raw, index, env) for index, raw in enumerate(profile)]


def load_profile(module_path: Any) -> list:
    """Read a profile from a Python module — a dotted name or a file path.

    :raises ProfileError: the module cannot be loaded, or exposes no profile.
    """
    text = str(module_path)
    try:
        if text.endswith(".py"):
            module = _load_file(Path(text))
        else:
            module = importlib.import_module(text)
    except ProfileError:
        raise
    except Exception as error:  # noqa: BLE001 - any import failure
        raise ProfileError(f"the profile {text!r} could not be loaded: {error}") from error

    profile = getattr(module, PROFILE_ATTRIBUTE, None)
    if profile is None:
        raise ProfileError(
            f"the profile {text!r} exposes no {PROFILE_ATTRIBUTE}"
        )
    return list(profile)


def _load_file(path: Path) -> Any:
    if not path.is_file():
        raise ProfileError(f"there is no profile at {path}")
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ProfileError(f"{path} is not an importable module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def mount_profile(ctx: Any, entries: Any) -> list:
    """Mount resolved entries in order, returning what mounted.

    The returned fibers are how a caller takes them back off — plugkit's
    ``ctx.plugin`` hands one back per mount, and disposing it is the only
    precise unmount. On failure the ones already mounted are disposed, so a
    half-built context never escapes.
    """
    mounted: list = []
    for index, entry in enumerate(entries):
        try:
            # An empty config is passed as *nothing*, not as `{}`. plugkit's
            # own services (ToolsService, PointsService) define no `__init__`,
            # so the second argument lands on `Service.__init__`'s `name`
            # parameter — and a dict there fails deep inside the reflect layer
            # with "unhashable type", nowhere near the profile that caused it.
            mounted.append(
                await ctx.plugin(entry.plugin, entry.config or None)
            )
        except Exception as error:  # noqa: BLE001 - named, then re-raised
            await unmount(mounted)
            raise ProfileError(
                f"profile entry {index} ({entry.name}) failed to mount: {error}"
            ) from error
    return mounted


async def unmount(mounted: Any) -> None:
    """Dispose mounted fibers, newest first. Never raises."""
    for fiber in reversed(list(mounted or ())):
        try:
            result = fiber.dispose()
            if hasattr(result, "__await__"):
                await result
        except Exception:  # noqa: BLE001 - teardown is best-effort
            pass


def core_profile() -> list:
    """The seams a conversation needs, in dependency order.

    Deliberately *not* everything this package ships: no tools, no provider
    adapter, no MCP. A consumer extends this list rather than retyping it, and
    what it leaves out is what a consumer is expected to choose.
    """
    from plugkit import PointsService, ToolsService

    from ..agent import AgentLoop, AgentRegistry
    from ..llm import LlmService, TokenMeter
    from ..operating import Commands, Credentials, Settings
    from ..session import SessionProjections, SessionStats, SessionStore
    from ..storage import Storage, StorageDomain

    return [
        (PointsService, {}),
        (ToolsService, {}),
        (SessionStore, {}),
        (SessionProjections, {}),
        (SessionStats, {}),
        (Storage, {}),
        (StorageDomain, {}),
        (Settings, {}),
        (Credentials, {}),
        (Commands, {}),
        (LlmService, {}),
        (TokenMeter, {}),
        (AgentRegistry, {}),
        (AgentLoop, {}),
    ]


#: The core profile, resolved lazily so importing this module does not import
#: every seam in the package.
CORE_PROFILE = core_profile


__all__ = [
    "ProfileEntry",
    "ProfileError",
    "resolve_profile",
    "load_profile",
    "mount_profile",
    "unmount",
    "core_profile",
    "CORE_PROFILE",
    "PROFILE_ATTRIBUTE",
]
