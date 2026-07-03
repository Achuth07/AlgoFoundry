"""Shared pytest fixtures.

The critical guarantee here: tests must NEVER touch the real
``algofoundry.db``. ``app.db`` reads its path from ``ALGOFOUNDRY_DB`` at
import time (module-level ``_DB_PATH``), so we set the env var to a temp file
and reload the module before handing it to a test.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Return a freshly-initialised ``app.db`` module bound to a temp SQLite
    file. Also reloads ``app.longterm.instruments`` so its ``from .. import
    db`` reference points at the reloaded module.
    """
    db_file = tmp_path / "test_algofoundry.db"
    monkeypatch.setenv("ALGOFOUNDRY_DB", str(db_file))

    import app.db as db_mod

    db_mod = importlib.reload(db_mod)
    assert db_mod._DB_PATH == str(db_file)  # never the real DB

    db_mod.init_db()
    return db_mod


@pytest.fixture
def instruments(db):
    """Return the ``app.longterm.instruments`` module reloaded so it uses the
    temp-DB-bound ``app.db``."""
    import app.longterm.instruments as inst_mod

    inst_mod = importlib.reload(inst_mod)
    return inst_mod
