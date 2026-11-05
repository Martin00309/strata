# Contributing to Strata

Thanks for your interest in Strata! The project aims to be a correct, readable,
dependency-free LSM storage engine, so changes are judged first on correctness
and clarity.

## Getting started

```bash
git clone https://github.com/Martin00309/strata.git
cd strata
pip install -e ".[dev]"
```

## Running the tests

```bash
pytest -ra
```

The engine imports only the Python standard library; the test suite needs only
`pytest`. Tests are organised per layer — `test_coding.py`, `test_skiplist.py`,
`test_sstable.py`, `test_compaction.py`, `test_db.py`, `test_recovery.py`, and
so on. **Add or update tests for any behaviour you change**, and prefer tests
that assert observable behaviour (round-trips, invariants, recovery) over
internal structure.

For storage-engine changes, a correctness check against a reference model (a
plain `dict`) under a randomized workload is worth a lot — see
`test_db.py::test_randomised_against_reference` for the pattern.

## Style

- Keep runtime code dependency-free (standard library only).
- Type-hint public functions and methods.
- Run the linter and formatter before opening a pull request:

  ```bash
  ruff check .
  ruff format .
  ```

## Design notes

- The on-disk format is specified in [docs/format.md](docs/format.md). Any
  format change must bump the table magic / manifest handling and keep
  recovery able to read older data, or clearly document that it does not.
- Preserve the invariants compaction relies on: newer versions of a key live in
  shallower levels (and newer level-0 files), and levels ≥ 1 are
  non-overlapping and sorted.

## Pull requests

1. Open an issue first for anything non-trivial so we can agree on the approach.
2. Keep changes focused; one logical change per pull request.
3. Update `CHANGELOG.md` under `[Unreleased]`.
4. Make sure CI is green across the supported Python versions.

## Reporting bugs

Open an issue with a minimal reproduction: the sequence of operations, the
`Options` you used, what you expected, and what happened. Include your OS and
Python version. Data-corruption or lost-write reports are especially welcome
and will be prioritised.
