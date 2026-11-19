# Contributing

Thanks for taking a look. This project is small on purpose: standard library
only, no host dependencies, every engine optional.

## Setup

```bash
git clone https://github.com/TsukimioRinka/agg-search
cd agg-search
python3 -m venv .venv && . .venv/bin/activate
python -m pip install -e .
```

Python 3.10+ is required. There are no runtime dependencies to install.

## Tests

```bash
python -m unittest discover -s tests -v
```

The suite is offline: no network calls, no API quota. If you add a test that
needs a live engine, mock the HTTP layer instead of calling out.

## Ground rules

- **No secrets in the tree.** Keys come from the environment only. A PR that
  hardcodes a key is closed without discussion.
- **One change per PR.** Bug fix or feature, not both.
- **Degrade, never crash.** Every network path must tolerate a missing key, a
  timeout, a 429 and a malformed response. The result list is the contract.
- **Keep the JSON shape stable.** `query`, `engines`, `expand`, `rerank`,
  `answer`, `answer_note`, `engine_health`, `results` are public API.
- **New engine checklist:** add an `eng_<name>` function, register it in the
  engine list, map its `--recency` arguments, document its key in
  `docs/environment.md`, and add a row to the README engine table.

## Commit messages

Conventional prefixes: `feat:`, `fix:`, `docs:`, `test:`, `ci:`, `refactor:`.
Body explains the failure mode, not the diff.

## Releases

1. Bump `version` in `pyproject.toml`.
2. Add a `CHANGELOG.md` entry.
3. Tag `vX.Y.Z` and publish a GitHub Release.
4. The `publish` workflow uploads to PyPI through trusted publishing; see
   `docs/releasing.md` for the one-time PyPI setup.
