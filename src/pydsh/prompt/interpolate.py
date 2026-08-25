"""Strict ``{{variable}}`` substitution.

Strict is the whole point. A prompt that renders ``{{user_name}}`` as an empty
string is a broken prompt nobody notices — the model just behaves slightly
worse and no one can say why. One that raises at assembly is a bug someone
fixes in a minute. So an unknown name, a name whose provider returned nothing,
and a malformed reference are all errors.

The one thing that is *not* an error: a lone ``{{`` with no ``}}`` anywhere
after it. That is prose (a code sample, a brace in an example), and treating it
as a broken reference would make ordinary text unwritable.
"""

from __future__ import annotations

import re
from typing import Optional

#: A legal variable name. Deliberately narrow: lowercase, digits, underscore.
VARIABLE_NAME = re.compile(r"[a-z][a-z0-9_]*")

#: A complete reference at the current position. ``[^{}]*`` rather than ``.*``
#: so a nested brace fails the match instead of swallowing it.
_REFERENCE = re.compile(r"\{\{([^{}]*)\}\}")


class PromptVariableError(ValueError):
    """A reference could not be resolved, so the prompt is not renderable."""


def interpolate(
    name: str,
    text: str,
    variables: dict[str, Optional[str]],
    kind: str = "section",
) -> str:
    """Substitute every ``{{variable}}`` in one section's or context's text.

    :param name: whose text this is — named in every error, because "unknown
        variable" without it means reading every registration to find the typo.
    :raises PromptVariableError: malformed reference, unknown name, or a
        variable with no value this assembly.
    """
    out: list[str] = []
    last = 0
    opened = text.find("{{", last)

    while opened >= 0:
        reference = _REFERENCE.match(text, opened)
        if reference is None:
            if text.find("}}", opened + 2) >= 0:
                excerpt = text[opened : opened + 16]
                raise PromptVariableError(
                    f"malformed prompt variable reference in {kind} {name!r}: "
                    f"{excerpt!r}… — a reference must be a complete {{{{name}}}} group"
                )
            # A lone "{{" with nothing closing it anywhere: prose, not a bug.
            out.append(text[last : opened + 2])
            last = opened + 2
            opened = text.find("{{", last)
            continue

        variable = reference.group(1)
        if not VARIABLE_NAME.fullmatch(variable):
            raise PromptVariableError(
                f"illegal prompt variable name {{{{{variable}}}}} in {kind} {name!r}: "
                f"a name must match {VARIABLE_NAME.pattern}"
            )
        if variable not in variables:
            known = ", ".join(sorted(variables)) or "(none registered)"
            raise PromptVariableError(
                f"unknown prompt variable {{{{{variable}}}}} in {kind} {name!r}; "
                f"registered: {known}"
            )
        value = variables[variable]
        if value is None:
            raise PromptVariableError(
                f"prompt variable {{{{{variable}}}}} has no value this assembly "
                f"({kind} {name!r}) — rendering it as empty would ship a broken prompt"
            )

        out.append(text[last:opened])
        out.append(value)
        last = reference.end()
        opened = text.find("{{", last)

    out.append(text[last:])
    return "".join(out)


__all__ = ["interpolate", "PromptVariableError", "VARIABLE_NAME"]
