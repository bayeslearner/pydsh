"""Boot — the front door.

Nineteen sprints of seams, and mounting them still meant twenty `await
root.plugin(...)` calls in the right order. This is the shortest path from
nothing to a running turn::

    from pydsh import Harness

    async with Harness() as harness:
        result = await harness.session().run("what changed today?")
        print(result.final_response)

Four layers under it: `home` resolves where a deployment keeps things,
`envfile` layers the environment beneath it (a `.env` may configure the
application and may **not** decide how the process boots), `profile` turns
"what to mount" into data that can be inspected before anything runs, and
`harness` owns a context's whole life.
"""

from .envfile import (
    BOOTSTRAP_PREFIXES,
    ENV_FILE_NAME,
    EnvFileError,
    interpolate_env,
    load_layered_env,
    parse_env,
)
from .harness import (
    SESSION_ID_PREFIX,
    Harness,
    HarnessError,
    HarnessSession,
    RunResult,
    final_response,
)
from .home import (
    HOME_DIR_NAME,
    HOME_DISPLAY,
    HOME_ENV,
    HomePathError,
    expand_home_path,
    home_display,
    home_path,
    resolve_home,
)
from .profile import (
    CORE_PROFILE,
    PROFILE_ATTRIBUTE,
    ProfileEntry,
    ProfileError,
    core_profile,
    load_profile,
    mount_profile,
    resolve_profile,
    unmount,
)

__all__ = [
    # the SDK
    "Harness",
    "HarnessSession",
    "HarnessError",
    "RunResult",
    "final_response",
    "SESSION_ID_PREFIX",
    # profiles
    "ProfileEntry",
    "ProfileError",
    "resolve_profile",
    "load_profile",
    "mount_profile",
    "unmount",
    "core_profile",
    "CORE_PROFILE",
    "PROFILE_ATTRIBUTE",
    # the environment
    "parse_env",
    "load_layered_env",
    "interpolate_env",
    "EnvFileError",
    "BOOTSTRAP_PREFIXES",
    "ENV_FILE_NAME",
    # home
    "resolve_home",
    "home_path",
    "home_display",
    "expand_home_path",
    "HomePathError",
    "HOME_DIR_NAME",
    "HOME_ENV",
    "HOME_DISPLAY",
]
