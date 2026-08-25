"""Sidecars — Requirements 1–4, properties 1–3.

Real bytes on a real filesystem, a real storage domain, and — for memory — a
real agent loop over a fake adapter, because the capture/recall pair is only
honest when something actually drives a turn.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterator

import pytest

from plugkit import Context, PointsService, ToolsService

from pydsh import (
    Agent,
    AgentOptions,
    ChunkType,
    GenerateOptions,
    LlmAdapter,
    LlmProviderInfo,
    LlmService,
    SessionStore,
    StreamChunk,
    decode_payload,
)
from pydsh.sidecar import (
    AttachmentError,
    FeedbackError,
    InvocationDescriptor,
    LocalAttachments,
    LongTermMemory,
    MessageFeedback,
    TypertRegistry,
    build_recall,
    content_id,
    memory_key,
    remote,
    remote_scope,
    tokenize,
)
from pydsh.sidecar.memory import RECALL_FORM, RECALL_HEADER
from pydsh.storage import JsonStorage, Storage, StorageDomain

pytestmark = pytest.mark.asyncio

PNG = "image/png"


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
async def storage_root(tmp_path, name: str = "store") -> Context:
    root = Context()
    await root.plugin(Storage)
    await root.plugin(JsonStorage, {"root": str(tmp_path / name)})
    await root.plugin(StorageDomain)
    return root


class Answerer(LlmAdapter):
    """Answers with a fixed line, recording what it was asked."""

    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply
        self.requests: list[GenerateOptions] = []

    async def stream(self, options: GenerateOptions) -> AsyncIterator[StreamChunk]:
        self.requests.append(options)
        yield StreamChunk(type=ChunkType.TEXT_DELTA, index=0, text=self.reply)
        yield StreamChunk(type=ChunkType.FINISH, finish={"kind": "stop"})

    def provider_info(self, provider: str) -> LlmProviderInfo:
        return LlmProviderInfo(id=provider, name=provider)


async def memory_loop(tmp_path, adapter, name: str = "m", config=None):
    """A real loop with long-term memory mounted over a real storage domain."""
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(LlmService)
    await root.plugin(PointsService)
    await root.plugin(ToolsService)
    await root.plugin(Storage)
    await root.plugin(JsonStorage, {"root": str(tmp_path / name)})
    await root.plugin(StorageDomain)
    await root.plugin(LongTermMemory, config or {})
    await root.long_term_memory.start()
    root.llm.register_adapter(["acme"], adapter)
    session = root.sessions.create(f"chat-{name}")
    return root, Agent(root, session, AgentOptions(provider="acme", model="a-1"))


def plugin_messages(session, plugin: str) -> list:
    out = []
    for message in session.derive_messages():
        decoded = decode_payload(message)
        if getattr(decoded.source, "kind", None) == "plugin" and (
            decoded.source.plugin == plugin
        ):
            out.append(decoded)
    return out


class FakeHeader:
    def __init__(self, id: str, created_at: float, cwd: str = "/w") -> None:
        self.id = id
        self.created_at = created_at
        self.cwd = cwd


class FakeSession:
    """Only what feedback reads: an id and a lifetime-bearing header."""

    def __init__(self, id: str, created_at: float = 1.0, cwd: str = "/w") -> None:
        self.id = id
        self.header = FakeHeader(id, created_at, cwd)


# --------------------------------------------------------------------------- #
# R1 — attachments
# --------------------------------------------------------------------------- #
async def test_the_same_bytes_give_the_same_id(tmp_path):
    """Property 1 (R1.2, I2) — saving twice is one id and one file."""
    root = Context()
    await root.plugin(LocalAttachments, {"root": str(tmp_path / "att")})

    first = await root.attachments.save_image(b"\x89PNG-one", PNG)
    second = await root.attachments.save_image(b"\x89PNG-one", PNG)

    assert first["id"] == second["id"] == content_id(b"\x89PNG-one")
    files = [p for p in (tmp_path / "att").rglob("*") if p.is_file()]
    assert len(files) == 1


async def test_reading_returns_exactly_what_was_stored(tmp_path):
    root = Context()
    await root.plugin(LocalAttachments, {"root": str(tmp_path / "att")})
    saved = await root.attachments.save_image(b"pixels", PNG)
    assert await root.attachments.read_image(saved["id"]) == b"pixels"


async def test_a_tampered_file_fails_to_read(tmp_path):
    """Property 1 (R1.5) — a swapped file is caught, not served."""
    root = Context()
    await root.plugin(LocalAttachments, {"root": str(tmp_path / "att")})
    saved = await root.attachments.save_image(b"original", PNG)

    stored = next(p for p in (tmp_path / "att").rglob("*") if p.is_file())
    stored.write_bytes(b"substituted")

    with pytest.raises(AttachmentError) as caught:
        await root.attachments.read_image(saved["id"])
    assert caught.value.code == "attachment-corrupt"


async def test_validate_stores_nothing(tmp_path):
    """R1.3 — a caller can ask without committing."""
    root = Context()
    await root.plugin(LocalAttachments, {"root": str(tmp_path / "att")})
    root.attachments.validate_image(b"maybe", PNG)
    assert not list((tmp_path / "att").rglob("*"))


async def test_oversized_content_is_refused_and_not_stored(tmp_path):
    """R1.4, I1 — validation precedes the write, so nothing lands."""
    root = Context()
    await root.plugin(
        LocalAttachments, {"root": str(tmp_path / "att"), "max_image_bytes": 4}
    )
    with pytest.raises(AttachmentError) as caught:
        await root.attachments.save_image(b"far too long", PNG)
    assert caught.value.code == "attachment-too-large"
    assert not list((tmp_path / "att").rglob("*"))


async def test_an_unsupported_type_is_refused(tmp_path):
    root = Context()
    await root.plugin(LocalAttachments, {"root": str(tmp_path / "att")})
    with pytest.raises(AttachmentError) as caught:
        await root.attachments.save_image(b"x", "application/x-msdownload")
    assert caught.value.code == "attachment-unsupported-type"


async def test_empty_content_is_refused(tmp_path):
    root = Context()
    await root.plugin(LocalAttachments, {"root": str(tmp_path / "att")})
    with pytest.raises(AttachmentError) as caught:
        await root.attachments.save_image(b"", PNG)
    assert caught.value.code == "attachment-empty"


async def test_a_reference_is_not_a_path(tmp_path):
    """The decision itself: an id names bytes, never a place to look."""
    root = Context()
    await root.plugin(LocalAttachments, {"root": str(tmp_path / "att")})
    saved = await root.attachments.save_image(b"pixels", PNG)
    assert saved["id"].startswith("sha256:")
    assert str(tmp_path) not in saved["id"]


async def test_a_malformed_id_is_refused_rather_than_resolved(tmp_path):
    """A traversal attempt is not an id at all, so it never reaches the path."""
    root = Context()
    await root.plugin(LocalAttachments, {"root": str(tmp_path / "att")})
    for bad in ("../../etc/passwd", "sha256:../x", "sha256:abc"):
        with pytest.raises(AttachmentError) as caught:
            await root.attachments.read_image(bad)
        assert caught.value.code == "attachment-invalid-id"


async def test_an_absent_attachment_says_so(tmp_path):
    root = Context()
    await root.plugin(LocalAttachments, {"root": str(tmp_path / "att")})
    with pytest.raises(AttachmentError) as caught:
        await root.attachments.read_image(content_id(b"never saved"))
    assert caught.value.code == "attachment-not-found"


async def test_limits_are_configurable_and_reported(tmp_path):
    """R1.7 — a client needs the limit before it uploads, not after."""
    root = Context()
    await root.plugin(
        LocalAttachments,
        {
            "root": str(tmp_path / "att"),
            "max_image_bytes": 99,
            "allowed_image_types": ["image/png"],
        },
    )
    assert root.attachments.image_limits() == {
        "max_bytes": 99,
        "allowed_types": ["image/png"],
    }


# --------------------------------------------------------------------------- #
# R2 — message feedback
# --------------------------------------------------------------------------- #
async def test_feedback_round_trips(tmp_path):
    root = await storage_root(tmp_path)
    await root.plugin(MessageFeedback)
    session = FakeSession("chat-1")

    entry = await root.message_feedback.put(session, "m1", rating="up", note="good")
    assert entry["rating"] == "up"
    assert await root.message_feedback.get(session, "m1") == entry
    assert list(await root.message_feedback.list(session)) == ["m1"]


async def test_two_writers_cannot_both_win(tmp_path):
    """Property 2 (R2.3, I4) — compare-and-set, not last-write-wins."""
    root = await storage_root(tmp_path)
    await root.plugin(MessageFeedback)
    session = FakeSession("chat-1")

    first = await root.message_feedback.put(session, "m1", rating="up")
    await root.message_feedback.put(
        session, "m1", rating="down", version=first["version"]
    )

    # The second writer still holds the *first* token.
    with pytest.raises(FeedbackError) as caught:
        await root.message_feedback.put(
            session, "m1", note="mine", version=first["version"]
        )
    assert caught.value.code == "version-mismatch"


async def test_creating_over_an_existing_entry_is_refused(tmp_path):
    """A `None` token means "I believe there is nothing here"."""
    root = await storage_root(tmp_path)
    await root.plugin(MessageFeedback)
    session = FakeSession("chat-1")
    await root.message_feedback.put(session, "m1", rating="up")

    with pytest.raises(FeedbackError):
        await root.message_feedback.put(session, "m1", rating="down")


async def test_an_identical_put_does_not_churn_the_version(tmp_path):
    """R2.4 — a retry must not invalidate every other client's token."""
    root = await storage_root(tmp_path)
    await root.plugin(MessageFeedback)
    session = FakeSession("chat-1")

    first = await root.message_feedback.put(session, "m1", rating="up", note="good")
    again = await root.message_feedback.put(
        session, "m1", rating="up", note="good", version=first["version"]
    )
    assert again["version"] == first["version"]


async def test_a_blank_or_oversized_note_is_refused_with_its_own_code(tmp_path):
    root = await storage_root(tmp_path)
    await root.plugin(MessageFeedback)
    session = FakeSession("chat-1")

    with pytest.raises(FeedbackError) as blank:
        await root.message_feedback.put(session, "m1", note="   ")
    assert blank.value.code == "note-blank"

    with pytest.raises(FeedbackError) as big:
        await root.message_feedback.put(session, "m1", note="x" * 5000)
    assert big.value.code == "note-too-large"


async def test_an_unknown_rating_is_refused(tmp_path):
    root = await storage_root(tmp_path)
    await root.plugin(MessageFeedback)
    with pytest.raises(FeedbackError) as caught:
        await root.message_feedback.put(FakeSession("c"), "m1", rating="sideways")
    assert caught.value.code == "rating-invalid"


async def test_delete_is_idempotent_when_absent_and_checked_when_present(tmp_path):
    """R2.6."""
    root = await storage_root(tmp_path)
    await root.plugin(MessageFeedback)
    session = FakeSession("chat-1")

    assert await root.message_feedback.delete(session, "never") is True

    entry = await root.message_feedback.put(session, "m1", rating="up")
    with pytest.raises(FeedbackError):
        await root.message_feedback.delete(session, "m1", version="wrong")
    assert await root.message_feedback.delete(session, "m1", version=entry["version"])
    assert await root.message_feedback.get(session, "m1") is None


async def test_a_reused_session_id_does_not_surface_the_previous_life(tmp_path):
    """R2.2, I3 — the fence is the lifetime, not the name."""
    root = await storage_root(tmp_path)
    await root.plugin(MessageFeedback)

    first_life = FakeSession("chat-1", created_at=1.0)
    await root.message_feedback.put(first_life, "m1", rating="up")

    rebuilt = FakeSession("chat-1", created_at=2.0)
    assert await root.message_feedback.list(rebuilt) == {}
    assert await root.message_feedback.get(rebuilt, "m1") is None


async def test_concurrent_writes_for_one_session_are_serialised(tmp_path):
    """R2.7 — two puts on different messages do not interleave a read-write."""
    root = await storage_root(tmp_path)
    await root.plugin(MessageFeedback)
    session = FakeSession("chat-1")

    await asyncio.gather(
        *(
            root.message_feedback.put(session, f"m{i}", rating="up")
            for i in range(20)
        )
    )
    assert len(await root.message_feedback.list(session)) == 20


async def test_feedback_survives_a_restart(tmp_path):
    """It is durable, which is the reason it is in a storage domain at all."""
    root = await storage_root(tmp_path, "shared")
    await root.plugin(MessageFeedback)
    session = FakeSession("chat-1")
    await root.message_feedback.put(session, "m1", rating="down", note="wrong")

    second = await storage_root(tmp_path, "shared")
    await second.plugin(MessageFeedback)
    restored = await second.message_feedback.get(session, "m1")
    assert restored["rating"] == "down" and restored["note"] == "wrong"


# --------------------------------------------------------------------------- #
# R3 — typert
# --------------------------------------------------------------------------- #
@remote_scope("calc")
class Calculator:
    @remote()
    def add(self, a: int, b: int) -> int:
        return a + b

    @remote("times")
    async def multiply(self, a: int, b: int) -> int:
        return a * b

    @remote()
    def explode(self) -> None:
        raise RuntimeError("handler fell over")

    def secret(self) -> str:
        """Public, unmarked, and therefore unreachable."""
        return "not for the wire"


class Unmarked:
    def anything(self) -> None: ...


async def test_register_and_invoke(tmp_path):
    root = Context()
    await root.plugin(TypertRegistry)
    root.typert.register(Calculator())

    result = await root.typert.invoke(
        InvocationDescriptor(service="calc", method="add", args={"a": 2, "b": 3})
    )
    assert result.ok and result.value == 5


async def test_an_async_method_and_a_wire_alias(tmp_path):
    """R3.1 — the wire name is the contract, not the Python name."""
    root = Context()
    await root.plugin(TypertRegistry)
    root.typert.register(Calculator())

    result = await root.typert.invoke(
        InvocationDescriptor(service="calc", method="times", args={"a": 3, "b": 4})
    )
    assert result.ok and result.value == 12

    missed = await root.typert.invoke(
        InvocationDescriptor(service="calc", method="multiply", args={"a": 1, "b": 1})
    )
    assert not missed.ok and missed.error.code == "METHOD_NOT_FOUND"


async def test_nothing_is_remotable_by_accident(tmp_path):
    """Property 3 (R3.1, I5) — public is not the same as exposed."""
    root = Context()
    await root.plugin(TypertRegistry)
    root.typert.register(Calculator())

    result = await root.typert.invoke(
        InvocationDescriptor(service="calc", method="secret")
    )
    assert not result.ok and result.error.code == "METHOD_NOT_FOUND"
    assert "secret" not in root.typert.list()[0]["methods"]


async def test_registering_an_object_with_no_remotable_methods_raises(tmp_path):
    """R3.4 — registering nothing looks exactly like registering something."""
    root = Context()
    await root.plugin(TypertRegistry)
    with pytest.raises(ValueError):
        root.typert.register(Unmarked(), scope="nope")


async def test_an_object_with_no_scope_name_raises(tmp_path):
    root = Context()
    await root.plugin(TypertRegistry)
    with pytest.raises(ValueError):
        root.typert.register(Unmarked())


async def test_an_unknown_scope_names_what_is_available(tmp_path):
    """R3.6 — a caller on the far side of a wire cannot read the source."""
    root = Context()
    await root.plugin(TypertRegistry)
    root.typert.register(Calculator())

    result = await root.typert.invoke(
        InvocationDescriptor(service="ghost", method="add")
    )
    assert not result.ok and result.error.code == "SCOPE_NOT_FOUND"
    assert "calc" in result.error.message


async def test_a_handler_failure_is_a_result_not_an_exception(tmp_path):
    """R3.5 — a transport cannot carry a Python traceback."""
    root = Context()
    await root.plugin(TypertRegistry)
    root.typert.register(Calculator())

    result = await root.typert.invoke(
        InvocationDescriptor(service="calc", method="explode")
    )
    assert not result.ok and result.error.code == "FAILED"
    assert "handler fell over" in result.error.message


async def test_bad_arguments_are_told_apart_from_a_handler_failure(tmp_path):
    """The caller's mistake and the server's fault are different problems."""
    root = Context()
    await root.plugin(TypertRegistry)
    root.typert.register(Calculator())

    result = await root.typert.invoke(
        InvocationDescriptor(service="calc", method="add", args={"a": 1})
    )
    assert not result.ok and result.error.code == "BAD_ARGUMENTS"


async def test_list_describes_every_endpoint(tmp_path):
    """R3.7."""
    root = Context()
    await root.plugin(TypertRegistry)
    root.typert.register(Calculator())
    assert root.typert.list() == [
        {"service": "calc", "methods": ["add", "explode", "times"]}
    ]


async def test_disposing_removes_only_this_registration(tmp_path):
    """The identity guard: a re-registration owns the scope now."""
    root = Context()
    await root.plugin(TypertRegistry)
    dispose = root.typert.register(Calculator())
    root.typert.register(Calculator())  # the scope now belongs to this one
    dispose()
    assert root.typert.has_scope("calc")


async def test_a_client_proxy_invokes_by_attribute(tmp_path):
    root = Context()
    await root.plugin(TypertRegistry)
    root.typert.register(Calculator())
    client = root.typert.client_for("calc")
    result = await client.add(a=1, b=1)
    assert result.ok and result.value == 2


async def test_scanning_does_not_run_property_getters(tmp_path):
    """A scan with side effects is a scan that can fail, or worse, succeed."""
    touched = []

    @remote_scope("watched")
    class WithProperty:
        @property
        def expensive(self) -> str:
            touched.append(1)
            return "ran"

        @remote()
        def ping(self) -> str:
            return "pong"

    root = Context()
    await root.plugin(TypertRegistry)
    root.typert.register(WithProperty())
    assert not touched


# --------------------------------------------------------------------------- #
# R4 — long-term memory
# --------------------------------------------------------------------------- #
async def test_a_turn_is_captured_and_recalled_in_a_later_session(tmp_path):
    """The integration that matters: across two sessions, over real storage."""
    adapter = Answerer("the deploy key lives in the vault")
    root, agent = await memory_loop(tmp_path, adapter, "shared")
    await agent.run("where is the deploy key?")
    await agent.when_idle()
    await root.long_term_memory.drain()

    stored = root.long_term_memory.all()
    assert stored and "deploy key" in stored[0]["text"]

    # A different session, the same store.
    second_root, second = await memory_loop(tmp_path, Answerer(), "shared")
    await second.run("remind me about the deploy key")
    await second.when_idle()

    recalled = plugin_messages(second.session, "long-term-memory")
    assert recalled, "nothing was recalled into the later session"
    assert recalled[0].source.form == RECALL_FORM
    assert "the deploy key lives in the vault" in recalled[0].content[0].text


async def test_recall_reaches_the_model_as_history_not_prompt(tmp_path):
    """R4.2 — the same rule every other context in this port follows."""
    adapter = Answerer("penguins are birds")
    root, agent = await memory_loop(tmp_path, adapter, "hist")
    await agent.run("tell me about penguins")
    await agent.when_idle()
    await root.long_term_memory.drain()

    second_adapter = Answerer()
    _, second = await memory_loop(tmp_path, second_adapter, "hist")
    await second.run("more about penguins")
    await second.when_idle()

    system = second_adapter.requests[0].system or ""
    assert RECALL_HEADER not in system
    texts = [
        m.content[0].text for m in plugin_messages(second.session, "long-term-memory")
    ]
    assert any(RECALL_HEADER in t for t in texts)


async def test_the_same_exchange_is_not_stored_twice(tmp_path):
    """R4.1 — keyed by content, so a replay does not accumulate copies."""
    root, agent = await memory_loop(tmp_path, Answerer("same answer"), "dedup")
    await agent.run("same question")
    await agent.when_idle()
    await root.long_term_memory.drain()

    # Capturing the same session again finds nothing new.
    assert await root.long_term_memory.capture(agent.session) == 0
    assert len(root.long_term_memory.all()) == 1


async def test_recall_is_injected_once_per_turn_not_once_per_step(tmp_path):
    """The first-step rule: a recall from step one is still true at step three."""
    root, agent = await memory_loop(tmp_path, Answerer("a fact"), "once")
    await agent.run("a question")
    await agent.when_idle()
    await root.long_term_memory.drain()

    _, second = await memory_loop(tmp_path, Answerer(), "once")
    await second.run("a question")
    await second.when_idle()
    assert len(plugin_messages(second.session, "long-term-memory")) == 1


async def test_a_recall_is_not_captured_as_a_memory(tmp_path):
    """Otherwise every turn stores a memory inside a memory, compounding."""
    root, agent = await memory_loop(tmp_path, Answerer("x is y"), "feed")
    await agent.run("what is x?")
    await agent.when_idle()
    await root.long_term_memory.drain()

    second_root, second = await memory_loop(tmp_path, Answerer("x is y"), "feed")
    await second.run("what is x?")
    await second.when_idle()
    await second_root.long_term_memory.drain()

    for entry in second_root.long_term_memory.all():
        assert RECALL_HEADER not in entry["text"]


async def test_relevance_is_overlap_with_a_recency_fallback(tmp_path):
    """R4.3 — and unglamorous on purpose."""
    root = await storage_root(tmp_path)
    await root.plugin(LongTermMemory, {"recent_count": 2})
    await root.long_term_memory.start()

    for text in ("apples and pears", "hydraulic presses", "orbital mechanics"):
        await root.long_term_memory.remember(text)

    hits = root.long_term_memory.retrieve("tell me about pears")
    assert [h["text"] for h in hits] == ["apples and pears"]

    fallback = root.long_term_memory.retrieve("zzzz")
    assert [f["text"] for f in fallback] == ["hydraulic presses", "orbital mechanics"]


async def test_recall_is_bounded_by_count_and_by_characters(tmp_path):
    """R4.4, NF2."""
    root = await storage_root(tmp_path)
    await root.plugin(LongTermMemory, {"max_recalled": 2})
    await root.long_term_memory.start()
    for i in range(6):
        await root.long_term_memory.remember(f"shared word number {i}")

    hits = root.long_term_memory.retrieve("shared word")
    assert len(hits) == 2

    rendered = build_recall(hits, max_chars=len(RECALL_HEADER) + 5)
    assert rendered == RECALL_HEADER  # nothing fit, and nothing was truncated mid-line


async def test_capture_can_be_switched_off(tmp_path):
    root, agent = await memory_loop(tmp_path, Answerer(), "off", {"capture": False})
    await agent.run("anything")
    await agent.when_idle()
    await root.long_term_memory.drain()
    assert root.long_term_memory.all() == []


async def test_the_memory_plugin_makes_no_model_calls(tmp_path):
    """R4.5 — recall is retrieval, not a second conversation."""
    adapter = Answerer("noted")
    root, agent = await memory_loop(tmp_path, adapter, "calls")
    await agent.run("one question")
    await agent.when_idle()
    await root.long_term_memory.drain()
    assert len(adapter.requests) == 1


async def test_a_half_exchange_is_not_captured(tmp_path):
    """A question whose answer is missing recalls as a question with no answer."""
    root = Context()
    await root.plugin(SessionStore)
    await root.plugin(Storage)
    await root.plugin(JsonStorage, {"root": str(tmp_path / "half")})
    await root.plugin(StorageDomain)
    await root.plugin(LongTermMemory)
    await root.long_term_memory.start()

    from pydsh import MessageSource, TextBlock, create_user_message, encode_payload

    session = root.sessions.create("chat-half")
    session.append(
        "user/message",
        encode_payload(
            create_user_message([TextBlock("unanswered")], source=MessageSource("user"))
        ),
    )
    assert await root.long_term_memory.capture(session) == 0


async def test_tokenize_and_the_content_key(tmp_path):
    assert tokenize("Deploy KEY, v2!") == ["deploy", "key", "v2"]
    assert memory_key("a") == memory_key("a") != memory_key("b")
