"""Reading history — Requirements 1 to 5, properties 1 to 3.

A real corpus of persisted sessions, really filtered. The two properties that
carry weight are that search text is treated literally, and that a reference
URI round-trips any id — including the awkward ones.
"""

from __future__ import annotations

import pytest

from plugkit import Context

from pydsh.message import MessageSource, TextBlock, create_user_message, encode_payload
from pydsh.query import (
    QueryError,
    SessionQueryEngine,
    SessionReferenceError,
    SessionReferences,
    compile_text_filter,
    decode_reference_uri,
    encode_reference_uri,
    format_mention,
    materialise_event_filters,
    materialise_session_filters,
    parse_references,
    tag_safe_json,
)
from pydsh.session import SessionStore, SqliteSessionPersistence

pytestmark = pytest.mark.asyncio


def user(text: str) -> dict:
    return encode_payload(create_user_message([TextBlock(text)], MessageSource("user")))


async def corpus(tmp_path, sessions: dict) -> Context:
    """Build and persist a corpus: ``{id: (created_at, cwd, [texts])}``."""
    root = Context()
    await root.plugin(SessionStore)
    root.sessions.attach_persistence(SqliteSessionPersistence(str(tmp_path / "log.db")))
    await root.plugin(SessionQueryEngine)

    for session_id, (created_at, cwd, texts) in sessions.items():
        session = root.sessions.create(session_id, cwd=cwd, meta={"created_at": created_at})
        for text in texts:
            session.append("user/message", user(text))
        await root.sessions.flush(session)
    return root


# --------------------------------------------------------------------------- #
# The corpus (R1)
# --------------------------------------------------------------------------- #
async def test_the_corpus_lists_every_session_newest_first(tmp_path):
    root = await corpus(tmp_path, {
        "old": (100.0, "/a", ["hello"]),
        "new": (300.0, "/b", ["world"]),
        "mid": (200.0, "/a", ["between"]),
    })
    records = await root.session_query.list_sessions()
    assert [r["id"] for r in records] == ["new", "mid", "old"]


async def test_a_record_says_where_it_can_be_read_from(tmp_path):
    root = await corpus(tmp_path, {"live-one": (100.0, "/a", ["hi"])})
    records = await root.session_query.list_sessions()
    # Created here and flushed, so it is both.
    assert set(records[0]["availability"]) == {"live", "persisted"}


async def test_a_purely_persisted_session_says_so(tmp_path):
    await corpus(tmp_path, {"archived": (100.0, "/a", ["hi"])})

    reader = Context()
    await reader.plugin(SessionStore)
    reader.sessions.attach_persistence(SqliteSessionPersistence(str(tmp_path / "log.db")))
    await reader.plugin(SessionQueryEngine)

    records = await reader.session_query.list_sessions()
    assert records[0]["availability"] == ["persisted"]


async def test_reading_a_missing_session_is_coded(tmp_path):
    root = await corpus(tmp_path, {})
    with pytest.raises(QueryError) as caught:
        await root.session_query.read_session("never")
    assert caught.value.code == "SESSION_QUERY_NOT_FOUND"


async def test_searching_without_a_corpus_says_so():
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(SessionQueryEngine)
    with pytest.raises(QueryError) as caught:
        await root.session_query.list_sessions()
    assert caught.value.code == "SESSION_QUERY_NO_CORPUS"


# --------------------------------------------------------------------------- #
# Session filters (R2) — property 3
# --------------------------------------------------------------------------- #
async def test_filters_compose_as_and(tmp_path):
    """Property 3 (I4)."""
    root = await corpus(tmp_path, {
        "a": (100.0, "/one", ["x"]),
        "b": (300.0, "/one", ["x"]),
        "c": (300.0, "/two", ["x"]),
    })
    matched = await root.session_query.filter_sessions([
        {"kind": "cwd", "values": ["/one"]},
        {"kind": "created-at", "from": 200.0},
    ])
    assert [r["id"] for r in matched] == ["b"]


async def test_values_within_a_clause_are_or(tmp_path):
    root = await corpus(tmp_path, {
        "a": (100.0, "/one", ["x"]),
        "b": (200.0, "/two", ["x"]),
        "c": (300.0, "/three", ["x"]),
    })
    matched = await root.session_query.filter_sessions([
        {"kind": "cwd", "values": ["/one", "/three"]},
    ])
    assert {r["id"] for r in matched} == {"a", "c"}


async def test_an_unknown_filter_kind_is_refused():
    with pytest.raises(QueryError, match="unknown session filter kind"):
        materialise_session_filters([{"kind": "vibes", "values": ["good"]}])


async def test_an_inverted_range_is_refused():
    with pytest.raises(QueryError, match="inverted"):
        materialise_session_filters([{"kind": "created-at", "from": 300, "to": 100}])


async def test_an_open_bound_is_allowed():
    clauses = materialise_session_filters([{"kind": "created-at", "from": 100}])
    assert clauses[0] == {"kind": "created-at", "from": 100}


async def test_an_unknown_availability_is_refused():
    with pytest.raises(QueryError, match="availability filter value"):
        materialise_session_filters([{"kind": "availability", "values": ["maybe"]}])


# --------------------------------------------------------------------------- #
# Event filters (R3) — property 2
# --------------------------------------------------------------------------- #
async def test_text_search_finds_what_was_said(tmp_path):
    """R3.6 — the message, not the JSON it was stored in."""
    root = await corpus(tmp_path, {"s": (100.0, "/a", ["the retry policy is fine", "unrelated"])})
    hits = await root.session_query.filter_session_events("s", [
        {"kind": "text", "text": "retry policy"},
    ])
    assert len(hits) == 1
    assert "retry policy" in hits[0]["text"]


async def test_text_search_is_flexible_about_whitespace(tmp_path):
    """A phrase that wrapped across lines still matches when typed on one."""
    root = await corpus(tmp_path, {"s": (100.0, "/a", ["the retry\n   policy"])})
    hits = await root.session_query.filter_session_events("s", [
        {"kind": "text", "text": "retry policy"},
    ])
    assert len(hits) == 1


async def test_text_search_is_literal_not_a_pattern():
    """Property 2 (I2) — a searcher-supplied pattern is an injection.

    `(a+)+b` in a search box is a denial of service; `a.*b` silently returns
    things nobody asked about.
    """
    pattern = compile_text_filter("a.*b")
    assert pattern.search("a.*b") is not None
    assert pattern.search("axxxb") is None

    dangerous = compile_text_filter("(a+)+b")
    assert dangerous.search("(a+)+b") is not None
    assert dangerous.search("aaaab") is None


async def test_empty_search_text_is_refused():
    """Returning the whole corpus for an empty box is worse than an error."""
    with pytest.raises(QueryError, match="non-whitespace"):
        compile_text_filter("   ")


async def test_filtering_by_type_and_seq(tmp_path):
    root = await corpus(tmp_path, {"s": (100.0, "/a", ["one", "two", "three"])})
    hits = await root.session_query.filter_session_events("s", [
        {"kind": "type", "values": ["user/message"]},
        {"kind": "seq", "from": 2},
    ])
    assert [h["seq"] for h in hits] == [2, 3]


async def test_an_unknown_surface_class_is_refused():
    with pytest.raises(QueryError, match="surface filter value"):
        materialise_event_filters([{"kind": "surface", "values": ["invisible"]}])


async def test_a_compacted_event_reads_as_shadowed(tmp_path):
    """R3.3 — the class that only exists because of compaction."""
    root = await corpus(tmp_path, {"s": (100.0, "/a", ["first", "second", "third"])})
    session = root.sessions.get("s")
    session.append("user/message", user("a summary"),
                   surface_op={"op": "replace", "start": 1, "end": 2})

    documents = await root.session_query.list_events("s")
    by_seq = {d["seq"]: d["surface"] for d in documents}
    assert by_seq[1] == "shadowed"
    assert by_seq[2] == "shadowed"
    assert by_seq[3] == "current"
    assert by_seq[4] == "current"  # the summary


async def test_reading_the_surface_omits_what_was_shadowed(tmp_path):
    root = await corpus(tmp_path, {"s": (100.0, "/a", ["first", "second"])})
    session = root.sessions.get("s")
    session.append("user/message", user("a summary"),
                   surface_op={"op": "replace", "start": 1, "end": 2})

    surface = await root.session_query.read_surface("s")
    assert [d["text"] for d in surface] == ["a summary"]


# --------------------------------------------------------------------------- #
# References (R4) — property 1
# --------------------------------------------------------------------------- #
async def test_a_uri_round_trips_any_id():
    """Property 1 (I3) — ids are arbitrary strings."""
    for session_id in [
        "simple",
        "with spaces",
        "with/slashes",
        "with]bracket)paren",
        "with\nnewline",
        "unicode ☃ and émoji",
        "",
    ]:
        assert decode_reference_uri(encode_reference_uri(session_id)) == session_id


async def test_a_uri_that_decodes_but_is_not_canonical_is_refused():
    """R4.2 — one reference has exactly one spelling.

    This payload decodes perfectly well to "chat-1" — the JSON just has a
    trailing space. Without the re-encode check it would resolve, and there
    would be two spellings of one reference, so equality stops working.
    """
    import base64

    sneaky = base64.urlsafe_b64encode(b'"chat-1" ').decode("ascii").rstrip("=")
    with pytest.raises(SessionReferenceError, match="not canonical"):
        decode_reference_uri(f"dsh-session:{sneaky}")


async def test_a_corrupted_payload_is_refused():
    canonical = encode_reference_uri("chat-1")
    with pytest.raises(SessionReferenceError):
        decode_reference_uri(canonical + "A")


async def test_a_uri_without_the_scheme_is_refused():
    with pytest.raises(SessionReferenceError, match="not a session reference"):
        decode_reference_uri("https://example.com/chat-1")


async def test_a_uri_with_an_illegal_payload_is_refused():
    with pytest.raises(SessionReferenceError, match="not valid"):
        decode_reference_uri("dsh-session:has spaces")


async def test_a_mention_escapes_its_label():
    """R4.3 — a bracket in a label would otherwise end the link early."""
    mention = format_mention("chat-1", "the [tricky] one")
    parsed = parse_references(mention)
    assert parsed["references"] == [
        {"session_id": "chat-1", "label": "the [tricky] one"}
    ]


async def test_mentions_are_parsed_in_order():
    text = f"see {format_mention('a', 'first')} and {format_mention('b', 'second')}"
    parsed = parse_references(text)
    assert [r["session_id"] for r in parsed["references"]] == ["a", "b"]
    assert parsed["text"] == "see @first and @second"


async def test_a_bare_uri_is_recognised():
    parsed = parse_references(f"see {encode_reference_uri('chat-1')} for detail")
    assert parsed["references"][0]["session_id"] == "chat-1"


async def test_text_with_no_references_is_unchanged():
    assert parse_references("nothing here")["references"] == []


async def test_serialised_reference_content_cannot_build_markup():
    """A referenced session's content is whatever someone typed elsewhere."""
    assert "<" not in tag_safe_json({"text": "<script>alert(1)</script>"})


# --------------------------------------------------------------------------- #
# Retained references (R5)
# --------------------------------------------------------------------------- #
async def test_resolving_retains_a_bounded_view(tmp_path):
    root = await corpus(tmp_path, {"other": (100.0, "/a", ["x" * 5000])})
    await root.plugin(SessionReferences, {"max_bytes": 200})

    resolved = await root.session_references.resolve(
        f"as in {format_mention('other', 'the other one')}"
    )
    reference = resolved["references"][0]
    assert reference["resolved"] is True
    assert len(reference["conversation"]) < 400
    assert "Omitted" in reference["notice"]


async def test_an_unresolvable_reference_is_reported_not_raised(tmp_path):
    """R5.5 — one deleted session must not make a paragraph unrenderable."""
    root = await corpus(tmp_path, {"present": (100.0, "/a", ["hi"])})
    await root.plugin(SessionReferences)

    resolved = await root.session_references.resolve(
        f"{format_mention('present')} and {format_mention('gone')}"
    )
    assert resolved["references"][0]["resolved"] is True
    assert resolved["references"][1]["resolved"] is False
    assert "gone" in resolved["references"][1]["reason"]


async def test_too_many_references_are_capped(tmp_path):
    root = await corpus(tmp_path, {"s": (100.0, "/a", ["hi"])})
    await root.plugin(SessionReferences, {"max_references": 2})

    text = " ".join(format_mention("s", f"n{i}") for i in range(5))
    resolved = await root.session_references.resolve(text)
    assert len(resolved["references"]) == 2
    assert resolved["dropped"] == 3
