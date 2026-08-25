"""The hub — Requirements 1 and 2.

Two registries and no I/O. The cases that matter are the stale-disposer guard
and the fact that disposing a registration does not close the backend.
"""

from __future__ import annotations

import pytest

from plugkit import Context

from pydsh.storage import Storage, StorageError, write_file_atomic

pytestmark = pytest.mark.asyncio


class Backend:
    def __init__(self, name: str = "b") -> None:
        self.name = name
        self.kv = object()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


async def hub() -> Context:
    root = Context()
    await root.plugin(Storage)
    return root


# --------------------------------------------------------------------------- #
# Backends (R1.2–R1.5, R1.8)
# --------------------------------------------------------------------------- #
async def test_a_registered_backend_is_reachable_by_name():
    root = await hub()
    backend = Backend()
    root.storage.backend.register("files", backend)
    assert root.storage.backend.get("files") is backend
    assert root.storage.backend.names() == ["files"]


async def test_a_duplicate_name_is_refused():
    root = await hub()
    root.storage.backend.register("files", Backend())
    with pytest.raises(StorageError) as caught:
        root.storage.backend.register("files", Backend())
    assert caught.value.code == "duplicate-backend"


async def test_an_unknown_backend_names_what_is_registered():
    root = await hub()
    root.storage.backend.register("files", Backend())
    with pytest.raises(StorageError) as caught:
        root.storage.backend.get("db")
    assert caught.value.code == "backend-not-found"
    assert "files" in caught.value.message


async def test_an_unknown_backend_with_none_registered_says_so():
    root = await hub()
    with pytest.raises(StorageError, match="registered: none"):
        root.storage.backend.get("db")


async def test_disposing_removes_the_registration():
    root = await hub()
    dispose = root.storage.backend.register("files", Backend())
    dispose()
    assert root.storage.backend.names() == []


async def test_a_stale_disposer_leaves_the_successor_alone():
    """R1.4 (I4) — a plugin unloading after another took the name over."""
    root = await hub()
    stale = root.storage.backend.register("files", Backend("first"))
    stale()
    successor = Backend("second")
    root.storage.backend.register("files", successor)

    stale()
    assert root.storage.backend.get("files") is successor


async def test_disposing_does_not_close_the_backend():
    """R1.8 — the plugin that opened the medium knows when it is finished."""
    root = await hub()
    backend = Backend()
    dispose = root.storage.backend.register("files", backend)
    dispose()
    assert backend.closed is False


# --------------------------------------------------------------------------- #
# Forms (R1.6, R1.7)
# --------------------------------------------------------------------------- #
async def test_a_mounted_form_is_reachable():
    root = await hub()
    facility = object()
    root.storage.mount("domain", facility)
    assert root.storage.form("domain") is facility
    assert root.storage.domain is facility
    assert root.storage.forms() == ["domain"]


async def test_a_duplicate_mount_is_refused():
    root = await hub()
    root.storage.mount("domain", object())
    with pytest.raises(StorageError) as caught:
        root.storage.mount("domain", object())
    assert caught.value.code == "duplicate-mount"


async def test_an_unmounted_form_names_what_is_mounted():
    root = await hub()
    with pytest.raises(StorageError) as caught:
        root.storage.form("domain")
    assert caught.value.code == "form-not-mounted"


async def test_a_stale_unmount_leaves_the_successor_alone():
    root = await hub()
    stale = root.storage.mount("domain", object())
    stale()
    successor = object()
    root.storage.mount("domain", successor)

    stale()
    assert root.storage.form("domain") is successor


# --------------------------------------------------------------------------- #
# R2 — atomic writes
# --------------------------------------------------------------------------- #
async def test_an_atomic_write_creates_parents(tmp_path):
    target = tmp_path / "deep" / "nested" / "unit.json"
    write_file_atomic(target, "hello")
    assert target.read_text() == "hello"


async def test_an_atomic_write_replaces_the_contents(tmp_path):
    target = tmp_path / "unit.json"
    write_file_atomic(target, "first")
    write_file_atomic(target, "second")
    assert target.read_text() == "second"
    assert [p.name for p in tmp_path.iterdir()] == ["unit.json"]


async def test_a_failed_write_leaves_the_original_and_no_temporary(tmp_path):
    """R2.2, R2.3 — the reason a plain open(..., 'w') is not good enough."""
    target = tmp_path / "unit.json"
    write_file_atomic(target, "original")

    class Unwritable:
        def __str__(self) -> str:
            raise RuntimeError("cannot serialize")

    with pytest.raises(TypeError):
        write_file_atomic(target, Unwritable())

    assert target.read_text() == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["unit.json"]


async def test_bytes_are_written_as_bytes(tmp_path):
    target = tmp_path / "unit.bin"
    write_file_atomic(target, b"\x00\x01\x02")
    assert target.read_bytes() == b"\x00\x01\x02"
