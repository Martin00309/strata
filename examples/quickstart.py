"""A tour of Strata's API.

Run it against a throwaway directory::

    PYTHONPATH=../src python quickstart.py   # from the examples/ folder
    # or, if installed:  python examples/quickstart.py

It writes to ``./quickstart-db`` and cleans up after itself.
"""

from __future__ import annotations

import shutil
import tempfile

import strata


def main() -> None:
    path = tempfile.mkdtemp(prefix="strata-quickstart-")
    try:
        with strata.open(path) as db:
            # --- basic writes and reads --------------------------------- #
            db.put(b"user:1", b'{"name": "Ada"}')
            db[b"user:2"] = b'{"name": "Linus"}'  # dict-style sugar
            print("user:1 ->", db.get(b"user:1"))
            print("missing ->", db.get(b"user:9", default=b"(none)"))
            print("user:2 in db ->", b"user:2" in db)

            # --- an atomic batch ---------------------------------------- #
            batch = strata.WriteBatch()
            batch.put(b"counter", b"1").put(b"flag", b"on").delete(b"user:2")
            db.write(batch)

            # --- an ordered range scan (end is exclusive) --------------- #
            for i in range(10):
                db.put(f"event:{i:03d}".encode(), f"payload-{i}".encode())
            print("events 2..5:")
            for key, value in db.scan(start=b"event:002", end=b"event:005"):
                print("  ", key.decode(), "=", value.decode())

            # --- a consistent snapshot ---------------------------------- #
            snap = db.snapshot()
            db.put(b"user:1", b'{"name": "Ada Lovelace"}')
            as_of = strata.ReadOptions(snapshot=snap)
            print("live    user:1 ->", db.get(b"user:1"))
            print("snapshot user:1 ->", db.get(b"user:1", options=as_of))
            db.release_snapshot(snap)

            # --- durability + a peek at the storage layout -------------- #
            db.put(b"important", b"value", options=strata.WriteOptions(sync=True))
            db.compact()
            print("stats ->", db.stats())
    finally:
        shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    main()
