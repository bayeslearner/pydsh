"""Pointing at another session.

A session id is an arbitrary string, so a reference needs an encoding that
round-trips *any* of them — including ids containing a bracket, which would
otherwise break the Markdown mention that carries it.

Two rules make that hold:

**A URI is canonical.** Decoding re-encodes and requires an exact match, so
there is exactly one spelling of a reference. Without that, base64's tolerance
for padding gives several spellings of one thing, equality stops working, and a
malformed URI can be coaxed into resolving.

**A retained reference is bounded.** Pointing at a conversation must not mean
pasting it. What comes back says what it omitted, in sprint 09's vocabulary.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any, Optional

from plugkit import Service

from ..bounded import TextRetainer, format_retention_notice

#: The scheme every reference carries.
SCHEME = "dsh-session:"

#: How many references one message may carry, so a paste of a hundred mentions
#: cannot turn into a hundred session reads.
DEFAULT_MAX_REFERENCES = 8

#: Bytes of a referenced conversation retained inline.
DEFAULT_MAX_REFERENCE_BYTES = 8_192

_PAYLOAD = re.compile(r"[A-Za-z0-9_-]+")

_MENTION = re.compile(
    r"@\[((?:\\.|[^\\\]])*)\]\((dsh-session:[^\s)]*)\)|(dsh-session:[A-Za-z0-9_-]+)"
)


class SessionReferenceError(ValueError):
    """A reference that cannot be understood."""

    def __init__(self, message: str, code: str = "SESSION_REFERENCE_INVALID") -> None:
        super().__init__(message)
        self.code = code


def encode_reference_uri(session_id: str) -> str:
    """Any session id as a canonical, lossless URI."""
    payload = base64.urlsafe_b64encode(
        json.dumps(session_id).encode("utf-8")
    ).decode("ascii")
    return f"{SCHEME}{payload.rstrip('=')}"


def decode_reference_uri(uri: str) -> str:
    """The session id behind a URI, or a refusal.

    The final check — re-encode and compare — is what makes the encoding
    canonical. base64 accepts several spellings of one value, and without this
    a reference has no single identity.
    """
    if not isinstance(uri, str) or not uri.startswith(SCHEME):
        raise SessionReferenceError(f"not a session reference URI: {uri!r}")
    payload = uri[len(SCHEME):]
    if not _PAYLOAD.fullmatch(payload):
        raise SessionReferenceError(f"session reference payload is not valid: {uri!r}")

    padded = payload + "=" * (-len(payload) % 4)
    try:
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as error:  # noqa: BLE001 - any decode failure is the same answer
        raise SessionReferenceError(
            f"session reference URI could not be decoded: {uri!r}"
        ) from error
    if not isinstance(decoded, str):
        raise SessionReferenceError(f"session reference does not name a session: {uri!r}")
    if encode_reference_uri(decoded) != uri:
        raise SessionReferenceError(
            f"session reference URI is not canonical: {uri!r} — one reference has "
            "exactly one spelling"
        )
    return decoded


def _escape_label(label: str) -> str:
    return re.sub(r"[\\\]]", lambda m: f"\\{m.group(0)}", label)


def _unescape_label(label: str) -> str:
    return re.sub(r"\\(.)", r"\1", label)


def format_mention(session_id: str, label: Optional[str] = None) -> str:
    """A reference as host-neutral Markdown.

    The label is escaped: an id or a title containing ``]`` would otherwise end
    the link early and leave the rest as loose text.
    """
    return f"@[{_escape_label(label or session_id)}]({encode_reference_uri(session_id)})"


def parse_references(text: str) -> dict:
    """Pull mentions and bare URIs out of a block of text.

    :returns: readable text, plus the references in the order they appeared.
    """
    references: list[dict] = []

    def replace(match: re.Match) -> str:
        raw_label, markdown_uri, bare_uri = match.groups()
        uri = markdown_uri or bare_uri
        session_id = decode_reference_uri(uri)
        label = _unescape_label(raw_label) if raw_label is not None else session_id
        references.append({"session_id": session_id, "label": label})
        return f"@{label}"

    return {"text": _MENTION.sub(replace, text), "references": references}


def tag_safe_json(value: Any) -> str:
    """Serialise, with ``<`` escaped.

    A referenced session's content is whatever a model or a user typed into a
    *different* conversation. Without this it can construct markup the host
    renders as its own.
    """
    return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")


class SessionReferences(Service):
    """Provides ``ctx.session_references`` — resolving a pointer to a session."""

    provide = "session_references"
    inject = ["session_query"]

    def __init__(self, ctx: Any, config: Any = None) -> None:
        super().__init__(ctx)
        config = config or {}
        self.max_references = int(config.get("max_references", DEFAULT_MAX_REFERENCES))
        self.max_bytes = int(config.get("max_bytes", DEFAULT_MAX_REFERENCE_BYTES))

    async def resolve(self, text: str) -> dict:
        """Parse a message's references and retain a bounded view of each.

        An unresolvable reference is *reported*, not raised: references arrive
        several to a message, and one deleted session should not make a whole
        paragraph unrenderable.
        """
        parsed = parse_references(text)
        resolved = []
        for reference in parsed["references"][: self.max_references]:
            try:
                retained = await self._retain(reference["session_id"])
            except Exception as error:  # noqa: BLE001
                resolved.append({**reference, "resolved": False, "reason": str(error)})
                continue
            resolved.append({**reference, "resolved": True, **retained})

        dropped = len(parsed["references"]) - len(resolved)
        return {
            "text": parsed["text"],
            "references": resolved,
            "dropped": dropped,
        }

    async def _retain(self, session_id: str) -> dict:
        """A bounded projection of another conversation."""
        documents = await self.ctx.session_query.read_surface(session_id)
        body = "\n".join(f"[{d['type']}] {d['text']}" for d in documents if d["text"])

        retainer = TextRetainer.head_tail(
            int(self.max_bytes * 0.75), max(1, self.max_bytes // 4)
        )
        retainer.push(body)
        kept = retainer.finish()
        notice = format_retention_notice(
            {"omitted": kept["omitted_bytes"], "unit": "bytes"},
            lambda n: "" if n["omitted"]["kind"] == "none" else "Open the session for the rest.",
        )
        return {"conversation": kept["text"], "notice": notice}


__all__ = [
    "SessionReferences",
    "SessionReferenceError",
    "encode_reference_uri",
    "decode_reference_uri",
    "format_mention",
    "parse_references",
    "tag_safe_json",
    "SCHEME",
    "DEFAULT_MAX_REFERENCES",
    "DEFAULT_MAX_REFERENCE_BYTES",
]
