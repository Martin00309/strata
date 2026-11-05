"""Shared test fixtures and import shim.

The package lives under ``src/``; add it to ``sys.path`` so the tests run
against a checkout without an editable install (CI installs the package, but
``pytest`` from a fresh clone should just work too).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


@pytest.fixture
def dbdir(tmp_path):
    """A fresh, empty directory path for a database (not yet created)."""
    return str(tmp_path / "db")


@pytest.fixture
def db(dbdir):
    """An open database that is closed automatically at teardown."""
    import strata

    handle = strata.open(dbdir)
    try:
        yield handle
    finally:
        handle.close()
