"""The domain form — Requirement 5, properties 1 and 2.

The two properties are the reason this layer exists at all: a rejected write
must leave no trace, and concurrent writers must land in a defined order. Both
are tested against a backend that can be made to fail and to stall on demand,
because neither is observable with a backend that always succeeds instantly.
"""

from __future__ import annotations

import asyncio

import pytest

from plugkit import Context

from pydsh.storage import (
    DOMAIN_CHANGED,
    DomainError,
    JsonBackend,
    JsonStorage,
    Storage,
    StorageDomain,
    StorageError,
    define_domain,
    domain_table,
)

pytestmark = pytest.mark.asyncio


# Declaration checks are synchronous; they run at import time in real use.


def is_goal(value):
    if not isinstance(value, dict) or "text" not in value:
        raise ValueError("a goal needs a text")
    return value


GOALS = define_domain(
    "goals",
    version=1,
    tables={"entries": domain_table(is_goal)},
    global_={"validate": is_goal, "initial": {"text": "none yet"}},
)

PLAIN = define_domain("plain", version=1, tables={"rows": domain_table()})


async def mounted(tmp_path, **config) -> Context:
    root = Context()
    await root.plugin(Storage)
    await root.plugin(JsonStorage, {"root": str(tmp_path / "units")})
    await root.plugin(StorageDomain, config)
    return root


# --------------------------------------------------------------------------- #
# Declaration (R5.1, R5.2) — fails at import, not at first save
# --------------------------------------------------------------------------- #
async def test_an_unsafe_domain_name_is_rejected():
    with pytest.raises(ValueError, match="must match"):
        define_domain("../escape", version=1)


async def test_an_unsafe_table_name_is_rejected():
    with pytest.raises(ValueError, match="table"):
        define_domain("ok", version=1, tables={"Bad Name": domain_table()})


async def test_a_negative_version_is_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        define_domain("ok", version=-1)


async def test_a_nullable_global_schema_is_rejected():
    """R5.2 — null is the medium's 'never written' sentinel."""
    with pytest.raises(ValueError, match="must not accept null"):
        define_domain("ok", version=1, global_={"validate": lambda v: v})


async def test_a_global_without_a_validator_is_allowed():
    spec = define_domain("ok", version=1, global_={"initial": {"n": 0}})
    assert spec.global_["initial"] == {"n": 0}


# --------------------------------------------------------------------------- #
# Opening (R5.3–R5.5)
# --------------------------------------------------------------------------- #
async def test_an_opened_domain_starts_from_the_declaration(tmp_path):
    root = await mounted(tmp_path)
    goals = await root.storage_domain.open(GOALS)
    assert goals.table("entries").size == 0
    assert goals.global_.get() == {"text": "none yet"}


async def test_reopening_reads_what_was_stored(tmp_path):
    root = await mounted(tmp_path)
    goals = await root.storage_domain.open(GOALS)
    await goals.table("entries").put("g1", {"text": "ship it"})
    await goals.close()

    reopened = await root.storage_domain.open(GOALS)
    assert reopened.table("entries").get("g1") == {"text": "ship it"}


async def test_opening_twice_is_refused(tmp_path):
    """R5.4 — two runtimes would each believe their memory is authoritative."""
    root = await mounted(tmp_path)
    await root.storage_domain.open(GOALS)
    with pytest.raises(DomainError) as caught:
        await root.storage_domain.open(GOALS)
    assert caught.value.code == "duplicate-domain"


async def test_a_stored_record_that_no_longer_validates_raises_at_open(tmp_path):
    """R5.5 — dropping it loses data; keeping it spreads an invalid value."""
    root = await mounted(tmp_path)
    plainish = define_domain("goals", version=1, tables={"entries": domain_table()})
    loose = await root.storage_domain.open(plainish)
    await loose.table("entries").put("g1", {"wrong": "shape"})
    await loose.close()

    with pytest.raises(DomainError) as caught:
        await root.storage_domain.open(GOALS)
    assert caught.value.code == "invalid-record"
    assert caught.value.detail == {"table": "entries", "key": "g1"}


async def test_a_backend_without_a_kv_facet_fails_loudly(tmp_path):
    root = Context()
    await root.plugin(Storage)
    await root.plugin(StorageDomain)

    class Mute:
        kv = None

    root.storage.backend.register("default", Mute())
    with pytest.raises(StorageError) as caught:
        await root.storage_domain.open(PLAIN)
    assert caught.value.code == "no-facet"


async def test_a_domain_can_be_routed_to_a_named_backend(tmp_path):
    root = Context()
    await root.plugin(Storage)
    # Two backends at once — registered directly, which is what the hub's
    # named registry is for.
    root.storage.backend.register("primary", JsonBackend(str(tmp_path / "a")))
    root.storage.backend.register("secondary", JsonBackend(str(tmp_path / "b")))
    await root.plugin(StorageDomain, {"backend": "primary", "routes": {"plain": "secondary"}})

    plain = await root.storage_domain.open(PLAIN)
    await plain.table("rows").put("k", {"n": 1})
    assert (tmp_path / "b" / "plain.json").exists()
    assert not (tmp_path / "a" / "plain.json").exists()


# --------------------------------------------------------------------------- #
# Reads (R5.6)
# --------------------------------------------------------------------------- #
async def test_reads_are_synchronous(tmp_path):
    """I6 — the point of the layer: asking what is stored does not await."""
    root = await mounted(tmp_path)
    plain = await root.storage_domain.open(PLAIN)
    await plain.table("rows").put("k", {"n": 1})

    table = plain.table("rows")
    assert table.get("k") == {"n": 1}
    assert list(table.keys()) == ["k"]
    assert list(table.entries()) == [("k", {"n": 1})]
    assert table.size == 1


async def test_an_undeclared_table_is_refused(tmp_path):
    root = await mounted(tmp_path)
    plain = await root.storage_domain.open(PLAIN)
    with pytest.raises(DomainError, match="declares no table"):
        plain.table("nope")


async def test_a_domain_without_a_global_refuses_the_handle(tmp_path):
    root = await mounted(tmp_path)
    plain = await root.storage_domain.open(PLAIN)
    with pytest.raises(DomainError, match="no global"):
        plain.global_


# --------------------------------------------------------------------------- #
# Writes (R5.7, R5.10, R5.11)
# --------------------------------------------------------------------------- #
async def test_a_change_event_carries_the_write(tmp_path):
    root = await mounted(tmp_path)
    seen = []
    root.on(DOMAIN_CHANGED, lambda change: seen.append(change))

    plain = await root.storage_domain.open(PLAIN)
    await plain.table("rows").put("k", {"n": 1})
    await plain.table("rows").delete("k")

    assert seen == [
        {"domain": "plain", "table": "rows", "key": "k", "operation": "put", "value": {"n": 1}},
        {"domain": "plain", "table": "rows", "key": "k", "operation": "deleted"},
    ]


async def test_deleting_a_missing_key_returns_false_without_writing(tmp_path):
    root = await mounted(tmp_path)
    seen = []
    root.on(DOMAIN_CHANGED, lambda change: seen.append(change))
    plain = await root.storage_domain.open(PLAIN)

    assert await plain.table("rows").delete("never") is False
    assert seen == []


async def test_update_reads_and_writes_in_one_slot(tmp_path):
    root = await mounted(tmp_path)
    plain = await root.storage_domain.open(PLAIN)
    await plain.table("rows").put("k", {"n": 1})

    result = await plain.table("rows").update("k", lambda v: {"n": v["n"] + 1})
    assert result == {"n": 2}
    assert plain.table("rows").get("k") == {"n": 2}


async def test_update_on_a_missing_key_raises(tmp_path):
    root = await mounted(tmp_path)
    plain = await root.storage_domain.open(PLAIN)
    with pytest.raises(DomainError) as caught:
        await plain.table("rows").update("nope", lambda v: v)
    assert caught.value.code == "missing-key"


async def test_a_value_failing_its_schema_is_refused_before_the_write(tmp_path):
    root = await mounted(tmp_path)
    goals = await root.storage_domain.open(GOALS)
    with pytest.raises(DomainError) as caught:
        await goals.table("entries").put("g1", {"no": "text"})
    assert caught.value.code == "invalid-record"
    assert goals.table("entries").size == 0


async def test_the_global_round_trips(tmp_path):
    root = await mounted(tmp_path)
    goals = await root.storage_domain.open(GOALS)
    await goals.global_.set({"text": "current"})
    await goals.close()

    reopened = await root.storage_domain.open(GOALS)
    assert reopened.global_.get() == {"text": "current"}


async def test_a_failing_change_listener_does_not_break_the_write(tmp_path):
    """R5.11 (I5) — the commit point has passed; a listener is not a participant."""
    root = await mounted(tmp_path)

    def bad(change):
        raise RuntimeError("observer bug")

    root.on(DOMAIN_CHANGED, bad)
    plain = await root.storage_domain.open(PLAIN)
    await plain.table("rows").put("k", {"n": 1})
    assert plain.table("rows").get("k") == {"n": 1}


# --------------------------------------------------------------------------- #
# Property 1 — a rejected write leaves no trace
# --------------------------------------------------------------------------- #
class Flaky:
    """A backend whose writes can be made to fail or to stall on demand."""

    def __init__(self) -> None:
        self.fail = False
        self.gate: asyncio.Event | None = None
        self.order: list[str] = []
        self.records: dict = {}
        self.kv = self

    async def open(self, descriptor):
        return self

    async def load_all(self):
        return {"tables": {t: {} for t in ("rows",)}, "global": None}

    async def put_record(self, table, key, value):
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise StorageError("malformed-medium", "the disk said no")
        self.records[key] = value
        self.order.append(key)

    async def delete_record(self, table, key):
        self.records.pop(key, None)

    async def set_global(self, value):
        pass

    async def close(self):
        pass


async def flaky_domain(tmp_path) -> tuple[Context, object, Flaky]:
    root = Context()
    await root.plugin(Storage)
    await root.plugin(StorageDomain)
    backend = Flaky()
    root.storage.backend.register("default", backend)
    return root, await root.storage_domain.open(PLAIN), backend


async def test_a_rejected_write_leaves_memory_and_the_stream_untouched(tmp_path):
    """Property 1 (I1) — the reason writes go durable-first.

    Update memory first and a refused write leaves the reader seeing a value
    that is stored nowhere. Reads and writes fork, silently, and the next
    process to open the unit disagrees with the one still running.
    """
    root, plain, backend = await flaky_domain(tmp_path)
    seen = []
    root.on(DOMAIN_CHANGED, lambda change: seen.append(change))

    backend.fail = True
    with pytest.raises(StorageError, match="the disk said no"):
        await plain.table("rows").put("k", {"n": 1})

    assert plain.table("rows").get("k") is None  # memory never moved
    assert backend.records == {}  # nor the medium
    assert seen == []  # and nothing was announced


async def test_the_domain_still_works_after_a_rejected_write(tmp_path):
    root, plain, backend = await flaky_domain(tmp_path)
    backend.fail = True
    with pytest.raises(StorageError):
        await plain.table("rows").put("k", {"n": 1})

    backend.fail = False
    await plain.table("rows").put("k", {"n": 2})
    assert plain.table("rows").get("k") == {"n": 2}


# --------------------------------------------------------------------------- #
# Property 2 — concurrent writers land in a defined order
# --------------------------------------------------------------------------- #
async def test_concurrent_writes_are_serialized(tmp_path):
    """Property 2 (I2) — the medium's order and the event order agree."""
    root, plain, backend = await flaky_domain(tmp_path)
    events = []
    root.on(DOMAIN_CHANGED, lambda change: events.append(change["key"]))

    backend.gate = asyncio.Event()
    writes = [
        asyncio.ensure_future(plain.table("rows").put(key, {"n": index}))
        for index, key in enumerate(["a", "b", "c"])
    ]
    await asyncio.sleep(0)
    backend.gate.set()
    await asyncio.gather(*writes)

    assert backend.order == ["a", "b", "c"]
    assert events == ["a", "b", "c"]


async def test_a_delete_queued_after_a_put_sees_it(tmp_path):
    """The existence check happens in the slot, not against a stale view."""
    root, plain, backend = await flaky_domain(tmp_path)
    backend.gate = asyncio.Event()

    put = asyncio.ensure_future(plain.table("rows").put("k", {"n": 1}))
    delete = asyncio.ensure_future(plain.table("rows").delete("k"))
    await asyncio.sleep(0)
    backend.gate.set()
    await put

    assert await delete is True


# --------------------------------------------------------------------------- #
# Closing (R5.12, R5.13)
# --------------------------------------------------------------------------- #
async def test_close_refuses_new_writes_and_reads(tmp_path):
    root = await mounted(tmp_path)
    plain = await root.storage_domain.open(PLAIN)
    await plain.close()

    with pytest.raises(DomainError) as caught:
        plain.table("rows").get("k")
    assert caught.value.code == "closed"
    with pytest.raises(DomainError):
        await plain.table("rows").put("k", {"n": 1})


async def test_close_drains_writes_already_queued(tmp_path):
    """R5.12 — queued work finishes and announces; it is not cancelled."""
    root, plain, backend = await flaky_domain(tmp_path)
    seen = []
    root.on(DOMAIN_CHANGED, lambda change: seen.append(change["key"]))

    backend.gate = asyncio.Event()
    write = asyncio.ensure_future(plain.table("rows").put("k", {"n": 1}))
    await asyncio.sleep(0)
    closing = asyncio.ensure_future(plain.close())
    await asyncio.sleep(0)
    backend.gate.set()
    await write
    await closing

    assert seen == ["k"]
    assert backend.records == {"k": {"n": 1}}


async def test_close_is_idempotent(tmp_path):
    root = await mounted(tmp_path)
    plain = await root.storage_domain.open(PLAIN)
    await plain.close()
    await plain.close()


async def test_closing_releases_the_name_for_reopening(tmp_path):
    root = await mounted(tmp_path)
    plain = await root.storage_domain.open(PLAIN)
    assert root.storage_domain.is_open("plain") is True
    await plain.close()
    assert root.storage_domain.is_open("plain") is False
    await root.storage_domain.open(PLAIN)


async def test_close_all_closes_everything_open(tmp_path):
    root = await mounted(tmp_path)
    await root.storage_domain.open(PLAIN)
    await root.storage_domain.open(GOALS)
    await root.storage_domain.close_all()
    assert root.storage_domain.is_open("plain") is False
    assert root.storage_domain.is_open("goals") is False
