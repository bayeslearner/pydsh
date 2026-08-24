"""Proves a session survives a real process restart (the MVP proof).

Unlike the async store tests, this writes in one Python process and reads in a
separate one, so it exercises the SQLite file durability end to end.
"""

from __future__ import annotations

import pytest

def test_persistence_survives_separate_process(tmp_path):
    """The strongest MVP proof: write in one process, read in a fresh one."""
    import os
    import subprocess
    import sys
    import textwrap

    db = str(tmp_path / "restart.db")
    writer = textwrap.dedent("""
        import asyncio, sys
        from plugkit import Context
        from pydsh.session import SessionStore, SqliteSessionPersistence
        async def main():
            root = Context(); await root.plugin(SessionStore)
            root.sessions.attach_persistence(SqliteSessionPersistence(sys.argv[1]))
            s = root.sessions.create("boot1")
            s.append("user/message", {"content": "persisted", "role": "user", "source": {}})
            await root.sessions.flush(s)
        asyncio.run(main())
    """)
    reader = textwrap.dedent("""
        import asyncio, sys
        from pydsh.session import SqliteSessionPersistence
        async def main():
            b = SqliteSessionPersistence(sys.argv[1])
            s = await b.load("boot1")
            assert [e.type for e in s.events] == ["user/message"]
            assert s.derive_messages()[0]["content"] == "persisted"
            print("RESTART_OK")
        asyncio.run(main())
    """)

    def run(src, name):
        p = tmp_path / name
        p.write_text(src)
        r = subprocess.run([sys.executable, str(p), db], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout

    run(writer, "writer.py")
    assert "RESTART_OK" in run(reader, "reader.py")
