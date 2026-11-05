"""Tests for the command-line interface."""

from __future__ import annotations

import json

import pytest

from strata.cli import main


def run(capsys, *argv):
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_put_get_roundtrip(capsys, dbdir):
    assert run(capsys, "put", dbdir, "name", "Ada")[0] == 0
    code, out, _ = run(capsys, "get", dbdir, "name")
    assert code == 0
    assert out.strip() == "Ada"


def test_get_missing_exits_3(capsys, dbdir):
    run(capsys, "put", dbdir, "a", "1")
    code, _out, err = run(capsys, "get", dbdir, "missing")
    assert code == 3
    assert "not found" in err


def test_delete(capsys, dbdir):
    run(capsys, "put", dbdir, "k", "v")
    run(capsys, "delete", dbdir, "k")
    code, _out, _err = run(capsys, "get", dbdir, "k")
    assert code == 3


def test_delete_alias_rm(capsys, dbdir):
    run(capsys, "put", dbdir, "k", "v")
    assert run(capsys, "rm", dbdir, "k")[0] == 0
    assert run(capsys, "get", dbdir, "k")[0] == 3


def test_scan_prints_in_order(capsys, dbdir):
    for k, v in [("b", "2"), ("a", "1"), ("c", "3")]:
        run(capsys, "put", dbdir, k, v)
    code, out, _ = run(capsys, "scan", dbdir)
    assert code == 0
    assert out == "a\t1\nb\t2\nc\t3\n"


def test_scan_range_and_limit(capsys, dbdir):
    for i in range(10):
        run(capsys, "put", dbdir, f"k{i}", str(i))
    _, out, _ = run(capsys, "scan", dbdir, "--start", "k2", "--end", "k5")
    assert out == "k2\t2\nk3\t3\nk4\t4\n"
    _, out, _ = run(capsys, "scan", dbdir, "--limit", "2")
    assert len(out.strip().splitlines()) == 2


def test_json_output(capsys, dbdir):
    run(capsys, "put", dbdir, "k", "v")
    code, out, _ = run(capsys, "get", dbdir, "k", "--json")
    assert code == 0
    payload = json.loads(out)
    assert payload == {"key": "k", "found": True, "value": "v"}


def test_scan_json(capsys, dbdir):
    run(capsys, "put", dbdir, "a", "1")
    run(capsys, "put", dbdir, "b", "2")
    _, out, _ = run(capsys, "scan", dbdir, "--json")
    assert json.loads(out) == [{"key": "a", "value": "1"}, {"key": "b", "value": "2"}]


def test_hex_mode_roundtrips_binary(capsys, dbdir):
    # 0xDEADBEEF as a key and value, round-tripped through --hex.
    assert run(capsys, "put", dbdir, "deadbeef", "00ff10", "--hex")[0] == 0
    code, out, _ = run(capsys, "get", dbdir, "deadbeef", "--hex")
    assert code == 0
    assert out.strip() == "00ff10"


def test_stats(capsys, dbdir):
    for i in range(20):
        run(capsys, "put", dbdir, f"k{i:02d}", "v")
    code, out, _ = run(capsys, "stats", dbdir)
    assert code == 0
    assert "last sequence : 20" in out


def test_stats_json(capsys, dbdir):
    run(capsys, "put", dbdir, "k", "v")
    _, out, _ = run(capsys, "stats", dbdir, "--json")
    payload = json.loads(out)
    assert payload["last_sequence"] == 1
    assert "levels" in payload


def test_compact(capsys, dbdir):
    for i in range(200):
        run(capsys, "put", dbdir, f"k{i:04d}", "value")
    code, out, _ = run(capsys, "compact", dbdir)
    assert code == 0
    assert "compacted" in out
    # Data still readable after compaction.
    assert run(capsys, "get", dbdir, "k0100")[1].strip() == "value"


def test_inspect(capsys, dbdir):
    for i in range(100):
        run(capsys, "put", dbdir, f"k{i:03d}", "v")
    run(capsys, "compact", dbdir)
    code, out, _ = run(capsys, "inspect", dbdir)
    assert code == 0
    assert "CURRENT" in out
    assert ".sst" in out
    # JSON form reports table entry counts.
    _, out, _ = run(capsys, "inspect", dbdir, "--json")
    payload = json.loads(out)
    tables = [e for e in payload if e["kind"] == "table"]
    assert tables and all("entries" in t for t in tables)


def test_bench(capsys, dbdir):
    code, out, _ = run(capsys, "bench", dbdir, "--count", "500", "--value-size", "32")
    assert code == 0
    assert "wrote 500 keys" in out
    assert "read  500 keys" in out


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
