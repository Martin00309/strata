"""Command-line interface for Strata.

A thin, scriptable wrapper over the engine for inspecting and operating a store
from the shell::

    strata put ./db user:1 '{"name": "Ada"}'
    strata get ./db user:1
    strata scan ./db --start user: --limit 20
    strata stats ./db
    strata compact ./db
    strata inspect ./db
    strata bench ./db --count 100000

Keys and values are UTF-8 text by default; pass ``--hex`` to read and write
them as hex, which is the safe path for arbitrary binary data. ``--json`` emits
machine-readable output for ``get``, ``scan``, ``stats`` and ``inspect``.

Exit codes: ``0`` success, ``2`` invalid input/usage, ``3`` key not found.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import strata
from strata.filenames import parse_file_name
from strata.sstable import SSTableReader

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_NOT_FOUND = 3


def _decode_arg(text: str, as_hex: bool) -> bytes:
    if as_hex:
        try:
            return bytes.fromhex(text)
        except ValueError as exc:
            raise SystemExit(f"strata: invalid hex: {exc}") from exc
    return text.encode("utf-8")


def _emit_value(value: bytes, as_hex: bool) -> str:
    if as_hex:
        return value.hex()
    return value.decode("utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def _cmd_put(args) -> int:
    key = _decode_arg(args.key, args.hex)
    value = _decode_arg(args.value, args.hex)
    with strata.open(args.db) as db:
        db.put(key, value, options=strata.WriteOptions(sync=args.sync))
    return EXIT_OK


def _cmd_get(args) -> int:
    key = _decode_arg(args.key, args.hex)
    sentinel = object()
    with strata.open(args.db) as db:
        value = db.get(key, default=sentinel)  # type: ignore[arg-type]
    if value is sentinel:
        if args.json:
            print(json.dumps({"key": args.key, "found": False}))
        else:
            print(f"strata: key not found: {args.key}", file=sys.stderr)
        return EXIT_NOT_FOUND
    if args.json:
        print(json.dumps({"key": args.key, "found": True, "value": _emit_value(value, args.hex)}))
    else:
        print(_emit_value(value, args.hex))
    return EXIT_OK


def _cmd_delete(args) -> int:
    key = _decode_arg(args.key, args.hex)
    with strata.open(args.db) as db:
        db.delete(key, options=strata.WriteOptions(sync=args.sync))
    return EXIT_OK


def _cmd_scan(args) -> int:
    start = _decode_arg(args.start, args.hex) if args.start is not None else None
    end = _decode_arg(args.end, args.hex) if args.end is not None else None
    rows = []
    with strata.open(args.db) as db:
        for i, (key, value) in enumerate(db.scan(start=start, end=end)):
            if args.limit is not None and i >= args.limit:
                break
            rows.append((key, value))

    if args.json:
        print(
            json.dumps(
                [
                    {"key": _emit_value(k, args.hex), "value": _emit_value(v, args.hex)}
                    for k, v in rows
                ]
            )
        )
    else:
        for key, value in rows:
            sys.stdout.write(f"{_emit_value(key, args.hex)}\t{_emit_value(value, args.hex)}\n")
    return EXIT_OK


def _cmd_stats(args) -> int:
    with strata.open(args.db) as db:
        stats = db.stats()
    if args.json:
        print(json.dumps(stats, indent=2))
        return EXIT_OK
    print(f"last sequence : {stats['last_sequence']}")
    print(f"memtable      : {stats['memtable_entries']} entries, {stats['memtable_bytes']} bytes")
    print(f"table files   : {stats['num_files']} files, {stats['total_bytes']} bytes")
    print("levels:")
    for level in stats["levels"]:
        if level["files"]:
            print(f"  L{level['level']}: {level['files']} files, {level['bytes']} bytes")
    print(f"live snapshots: {stats['live_snapshots']}")
    return EXIT_OK


def _cmd_compact(args) -> int:
    with strata.open(args.db) as db:
        db.compact()
        stats = db.stats()
    print(f"compacted: {stats['num_files']} files, {stats['total_bytes']} bytes on disk")
    return EXIT_OK


def _cmd_inspect(args) -> int:
    entries = []
    for name in sorted(os.listdir(args.db)):
        parsed = parse_file_name(name)
        if parsed is None:
            continue
        number, kind = parsed
        full = os.path.join(args.db, name)
        size = os.path.getsize(full)
        info = {"file": name, "kind": kind, "number": number, "bytes": size}
        if kind == "table":
            try:
                reader = SSTableReader.open(full, strata.Options())
                it = reader.iterator()
                it.seek_to_first()
                count = 0
                first = last = None
                while it.valid():
                    if first is None:
                        first = it.key()
                    last = it.key()
                    count += 1
                    it.next()
                reader.close()
                info["entries"] = count
                info["smallest"] = _emit_value(first, args.hex) if first is not None else None
                info["largest"] = _emit_value(last, args.hex) if last is not None else None
            except Exception as exc:  # noqa: BLE001 - inspection is best-effort
                info["error"] = str(exc)
        entries.append(info)

    if args.json:
        print(json.dumps(entries, indent=2))
        return EXIT_OK
    for info in entries:
        line = f"{info['file']:<24} {info['kind']:<9} {info['bytes']:>12} bytes"
        if "entries" in info:
            line += f"  {info['entries']} entries [{info.get('smallest')}..{info.get('largest')}]"
        if "error" in info:
            line += f"  ERROR: {info['error']}"
        print(line)
    return EXIT_OK


def _cmd_bench(args) -> int:
    import random

    rng = random.Random(0)
    keys = [f"bench:{i:012d}".encode() for i in range(args.count)]
    value = bytes(rng.getrandbits(8) for _ in range(args.value_size))

    with strata.open(args.db) as db:
        start = time.perf_counter()
        for k in keys:
            db.put(k, value)
        write_elapsed = time.perf_counter() - start

        start = time.perf_counter()
        hits = 0
        for k in keys:
            if db.get(k) is not None:
                hits += 1
        read_elapsed = time.perf_counter() - start

    total_bytes = args.count * (args.value_size + 20)
    print(
        f"wrote {args.count} keys in {write_elapsed:.3f}s "
        f"({args.count / write_elapsed:,.0f} ops/s, "
        f"{total_bytes / write_elapsed / 1e6:.1f} MB/s)"
    )
    print(
        f"read  {args.count} keys in {read_elapsed:.3f}s "
        f"({args.count / read_elapsed:,.0f} ops/s), {hits} hits"
    )
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="strata", description="Operate a Strata key-value store.")
    parser.add_argument("--version", action="version", version=f"strata {strata.__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p):
        p.add_argument("--hex", action="store_true", help="interpret keys/values as hex")
        p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("put", help="store a key/value pair")
    p.add_argument("db")
    p.add_argument("key")
    p.add_argument("value")
    p.add_argument("--hex", action="store_true", help="interpret key/value as hex")
    p.add_argument("--sync", action="store_true", help="fsync the log before returning")
    p.set_defaults(func=_cmd_put, json=False)

    p = sub.add_parser("get", help="read a key")
    p.add_argument("db")
    p.add_argument("key")
    add_common(p)
    p.set_defaults(func=_cmd_get)

    p = sub.add_parser("delete", aliases=["del", "rm"], help="delete a key")
    p.add_argument("db")
    p.add_argument("key")
    p.add_argument("--hex", action="store_true", help="interpret key as hex")
    p.add_argument("--sync", action="store_true", help="fsync the log before returning")
    p.set_defaults(func=_cmd_delete, json=False)

    p = sub.add_parser("scan", help="iterate keys in order")
    p.add_argument("db")
    p.add_argument("--start", help="inclusive start key")
    p.add_argument("--end", help="exclusive end key")
    p.add_argument("--limit", type=int, help="maximum rows to print")
    add_common(p)
    p.set_defaults(func=_cmd_scan)

    p = sub.add_parser("stats", help="show storage statistics")
    p.add_argument("db")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.set_defaults(func=_cmd_stats, hex=False)

    p = sub.add_parser("compact", help="flush and fully compact the store")
    p.add_argument("db")
    p.set_defaults(func=_cmd_compact, json=False, hex=False)

    p = sub.add_parser("inspect", help="list the files that make up the store")
    p.add_argument("db")
    add_common(p)
    p.set_defaults(func=_cmd_inspect)

    p = sub.add_parser("bench", help="run a quick write/read benchmark")
    p.add_argument("db")
    p.add_argument("--count", type=int, default=100_000, help="number of keys")
    p.add_argument("--value-size", type=int, default=100, help="value size in bytes")
    p.set_defaults(func=_cmd_bench, json=False, hex=False)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except strata.StrataError as exc:
        print(f"strata: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except FileNotFoundError as exc:
        print(f"strata: {exc}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
