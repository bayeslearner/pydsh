"""The operating core — what a harness needs to be operated, not merely run.

- ``ctx.settings`` — namespaced, validated configuration that can change while
  the process runs.
- ``ctx.credentials`` — a name in config, the secret resolved at the moment of
  use, and never returned by ``describe``.
- ``ctx.commands`` — slash-commands that run without a model turn, and never
  raise at the person who typed them.
- ``ctx.anonymous_user_id`` — a stable, home-scoped machine id.
"""

from .commands import CommandHandler, CommandInvocation, CommandResult, Commands
from .credentials import (
    CREDENTIALS_UPDATED,
    REF_PATTERN,
    CredentialRefError,
    Credentials,
    credential_ref,
)
from .identity import (
    ID_FILE_NAME,
    AnonymousUserId,
    get_or_create_anonymous_user_id,
)
from .settings import Settings, SettingsScope, UnknownNamespaceError

__all__ = [
    "Settings",
    "SettingsScope",
    "UnknownNamespaceError",
    "Credentials",
    "credential_ref",
    "CredentialRefError",
    "REF_PATTERN",
    "CREDENTIALS_UPDATED",
    "Commands",
    "CommandInvocation",
    "CommandResult",
    "CommandHandler",
    "AnonymousUserId",
    "get_or_create_anonymous_user_id",
    "ID_FILE_NAME",
]
