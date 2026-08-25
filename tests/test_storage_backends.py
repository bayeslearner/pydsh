"""The unit contract — Requirements 3 and 4, property 3.

Every test here runs against **both** media. "A consumer swaps media by
configuration" is only true if they behave alike, and the only way to know that
is to hold them to one suite.
"""

from __future__ import annotations

import json

import pytest

from plugkit import Context

from pydsh.storage import (
    JsonBackend,
    JsonStorage,
    SqliteBackend,
    SqliteStorage,
    Storage,
    StorageError,
)

pytestmark = pytest.mark.asyncio


BACKENDS = ["json", "sqlite"]


async def mounted(kind: str, tmp_path, name: str = "default") -> Context:
    root = Context()
    await root.plugin(Storage)
    if kind == "json":
        await root.plugin(JsonStorage, {"root": str(tmp_path / "units"), "name": name})
    else:
        await root.plugin(
            SqliteStorage, {"path": str(tmp_path / "store.db"), "name": name}
        )
    return root


def descriptor(version: int = 1, tables=("entries",), name: str = "goals") -> dict:
    return {
        "name": name,
        "version": version,
        "tables": list(tables),
        "has_global": True,
    }


@pytest.fixture(params=BACKENDS)
def kind(request):
    return request.param


# --------------------------------------------------------------------------- #
# Opening (R3.2, R3.3, R4.4)
# --------------------------------------------------------------------------- #
async def test_a_new_unit_starts_empty(kind, tmp_path):
    root = await mounted(kind, tmp_path)
    unit = await root.storage.backend.get("default").kv.open(descriptor())
    assert await unit.load_all() == {"tables": {"entries": {}}, "global": None}


async def test_a_unit_reopens_with_what_was_written(kind, tmp_path):
    root = await mounted(kind, tmp_path)
    facet = root.storage.backend.get("default").kv

    unit = await facet.open(descriptor())
    await unit.put_record("entries", "g1", {"text": "ship it"})
    await unit.set_global({"active": "g1"})
    await unit.close()

    reopened = await facet.open(descriptor())
    assert await reopened.load_all() == {
        "tables": {"entries": {"g1": {"text": "ship it"}}},
        "global": {"active": "g1"},
    }


async def test_a_version_mismatch_refuses_to_open(kind, tmp_path):
    """R3.3 (I3) — reading v1 bytes as the v2 shape is how data is corrupted."""
    root = await mounted(kind, tmp_path)
    facet = root.storage.backend.get("default").kv
    unit = await facet.open(descriptor(version=1))
    await unit.close()

    with pytest.raises(StorageError) as caught:
        await facet.open(descriptor(version=2))
    assert caught.value.code == "version-mismatch"


async def test_a_table_added_since_the_last_write_starts_empty(kind, tmp_path):
    """The descriptor is the current shape; the medium is history."""
    root = await mounted(kind, tmp_path)
    facet = root.storage.backend.get("default").kv
    unit = await facet.open(descriptor(tables=("entries",)))
    await unit.put_record("entries", "g1", {"text": "one"})
    await unit.close()

    reopened = await facet.open(descriptor(tables=("entries", "notes")))
    stored = await reopened.load_all()
    assert stored["tables"]["notes"] == {}
    assert stored["tables"]["entries"] == {"g1": {"text": "one"}}


# --------------------------------------------------------------------------- #
# Records (R3.1)
# --------------------------------------------------------------------------- #
async def test_put_overwrites_and_delete_removes(kind, tmp_path):
    root = await mounted(kind, tmp_path)
    unit = await root.storage.backend.get("default").kv.open(descriptor())

    await unit.put_record("entries", "g1", {"n": 1})
    await unit.put_record("entries", "g1", {"n": 2})
    assert (await unit.load_all())["tables"]["entries"] == {"g1": {"n": 2}}

    await unit.delete_record("entries", "g1")
    assert (await unit.load_all())["tables"]["entries"] == {}


async def test_deleting_something_absent_is_not_an_error(kind, tmp_path):
    root = await mounted(kind, tmp_path)
    unit = await root.storage.backend.get("default").kv.open(descriptor())
    await unit.delete_record("entries", "never-there")


async def test_values_survive_as_json(kind, tmp_path):
    """Values are opaque to a backend, but they must come back equal."""
    root = await mounted(kind, tmp_path)
    facet = root.storage.backend.get("default").kv
    value = {"s": "text", "n": 1, "f": 1.5, "b": True, "z": None, "l": [1, {"k": "v"}]}

    unit = await facet.open(descriptor())
    await unit.put_record("entries", "g1", value)
    await unit.close()

    reopened = await facet.open(descriptor())
    assert (await reopened.load_all())["tables"]["entries"]["g1"] == value


# --------------------------------------------------------------------------- #
# Closing (R3.5)
# --------------------------------------------------------------------------- #
async def test_every_call_after_close_is_refused(kind, tmp_path):
    root = await mounted(kind, tmp_path)
    unit = await root.storage.backend.get("default").kv.open(descriptor())
    await unit.close()

    for call in (
        unit.load_all(),
        unit.put_record("entries", "g1", {}),
        unit.delete_record("entries", "g1"),
        unit.set_global({}),
    ):
        with pytest.raises(StorageError) as caught:
            await call
        assert caught.value.code == "closed"


async def test_closing_twice_is_fine(kind, tmp_path):
    root = await mounted(kind, tmp_path)
    unit = await root.storage.backend.get("default").kv.open(descriptor())
    await unit.close()
    await unit.close()


# --------------------------------------------------------------------------- #
# Property 3 — the media agree
# --------------------------------------------------------------------------- #
async def test_both_media_produce_the_same_reads(tmp_path):
    """The claim the hub's whole named-registry design rests on."""
    results = []
    for index, kind in enumerate(BACKENDS):
        root = await mounted(kind, tmp_path / str(index))
        facet = root.storage.backend.get("default").kv
        unit = await facet.open(descriptor())
        await unit.put_record("entries", "b", {"n": 2})
        await unit.put_record("entries", "a", {"n": 1})
        await unit.delete_record("entries", "b")
        await unit.set_global({"seen": 2})
        await unit.close()

        reopened = await facet.open(descriptor())
        results.append(await reopened.load_all())

    assert results[0] == results[1]


# --------------------------------------------------------------------------- #
# JSON specifics (R3.4, R3.6)
# --------------------------------------------------------------------------- #
async def test_json_reports_an_unparseable_file_rather_than_crashing(tmp_path):
    root = await mounted("json", tmp_path)
    (tmp_path / "units").mkdir(parents=True, exist_ok=True)
    (tmp_path / "units" / "goals.json").write_text("{ not json")

    with pytest.raises(StorageError) as caught:
        await root.storage.backend.get("default").kv.open(descriptor())
    assert caught.value.code == "malformed-medium"


async def test_json_reports_a_file_of_the_wrong_shape(tmp_path):
    root = await mounted("json", tmp_path)
    (tmp_path / "units").mkdir(parents=True, exist_ok=True)
    (tmp_path / "units" / "goals.json").write_text(json.dumps([1, 2, 3]))

    with pytest.raises(StorageError) as caught:
        await root.storage.backend.get("default").kv.open(descriptor())
    assert caught.value.code == "malformed-medium"


async def test_json_writes_through_an_atomic_replace(tmp_path):
    """R3.6 — no temporary is left behind, and the file is complete."""
    root = await mounted("json", tmp_path)
    unit = await root.storage.backend.get("default").kv.open(descriptor())
    await unit.put_record("entries", "g1", {"text": "ship it"})

    units = tmp_path / "units"
    assert [p.name for p in units.iterdir()] == ["goals.json"]
    assert json.loads((units / "goals.json").read_text())["tables"]["entries"]


# --------------------------------------------------------------------------- #
# Two backends at once — the reason the registry is named
# --------------------------------------------------------------------------- #
async def test_two_backends_serve_different_units(tmp_path):
    root = Context()
    await root.plugin(Storage)
    # Registered directly: a service class can provide its name only once, and
    # the hub is deliberately plural.
    root.storage.backend.register("files", JsonBackend(str(tmp_path / "j")))
    root.storage.backend.register("db", SqliteBackend(str(tmp_path / "s.db")))

    assert root.storage.backend.names() == ["db", "files"]

    on_files = await root.storage.backend.get("files").kv.open(descriptor(name="a"))
    on_db = await root.storage.backend.get("db").kv.open(descriptor(name="b"))
    await on_files.put_record("entries", "k", {"where": "files"})
    await on_db.put_record("entries", "k", {"where": "db"})

    assert (await on_files.load_all())["tables"]["entries"]["k"] == {"where": "files"}
    assert (await on_db.load_all())["tables"]["entries"]["k"] == {"where": "db"}
