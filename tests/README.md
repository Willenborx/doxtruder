# doxtruder — unit tests + coverage

Offline test-suite for the whole doxtruder codebase. Every HTTP request is
intercepted with `requests-mock` (real requests stack) or by patching
`requests.get` (exact-argument checks); the engine drives a fake provider so
no network or live box is needed.

## Setup (one-time)

Create the test environment and install the project deps plus test-only deps:

    python3 -m venv venv
    ./venv/bin/pip install -r requirements.txt
    ./venv/bin/pip install -r tests/requirements-test.txt

Notes:
- `venv` is already in `.gitignore`.
- `tests/requirements-test.txt` pins the test-only packages (pytest,
  requests-mock, pytest-cov); project deps come from `requirements.txt`.

## Run tests + coverage report

From the repo root:

    ./venv/bin/python -m pytest

Config lives in `pytest.ini` at the root:
- `testpaths = tests`
- `pythonpath = .` so the top-level `doxtruder` package imports without install.
- coverage is on by default: `--cov=. --cov-branch`, with a terminal
  (`term-missing`) and an HTML report (`htmlcov/`).

Coverage behavior is set in `.coveragerc`:
- `tests/` is omitted so the report shows production code only.
- branch coverage is on (`--cov-branch` in `pytest.ini`).

The HTML report is written to `htmlcov/` (gitignored); open `htmlcov/index.html`.

If you want verbose output or to target one file:

    ./venv/bin/python -m pytest -v
    ./venv/bin/python -m pytest tests/test_rest_provider.py

## Layout

- `pytest.ini` / `.coveragerc` — runner + coverage config at the repo root.
- `tests/conftest.py` — shared fixtures: the Jinja2 environment, and a
  `FakeProvider` double + import patcher for the engine tests.
- `tests/requirements-test.txt` — test-only pinned deps (pytest,
  requests-mock, pytest-cov).
- `test_config_provider.py` — abstract base contract (and the ABCMeta bug).
- `test_rest_provider.py` — the RESTProvider core: the shared retrieve_config
  / call_api_endpoint orchestration, and the yaml-driven request construction
  (`headers` dict, `auth.basic`) that the removed Gitea/Proxmox/
  OPNSense subclasses used to hardcode in their own `perform_request`.
  RESTProvider is concrete now, so it is tested directly.
- `test_unifi_provider.py` — the per-site RESTProvider configuration used in
  doxtruder.yaml (the UnifiProvider subclass is deleted): devstats/vouchers
  fetched once per site name via $jpath, nested one level under the site
  name, and the per-site template rendering.
- `test_openrouter_provider.py` — OpenrouterProvider (Bearer auth in
  perform_request, the analytics POST / aggregate_usage daily rows and week
  totals, and the warning downgrade when aggregation fails).
- `test_doxtruder_engine.py` — run_doxtruder lifecycle, status propagation,
  per-entity error handling, missing/unknown providers.
- `test_cli.py` — the CLI (`doxtruder/cli.py`, installed as `doxtruder`):
  valid/missing/dir/unreadable/invalid configs, plus the `${ENV}` resolver and
  the `--env` option.
- `test_exceptions.py` — the custom exception classes, incl. the new
  `DoxtruderEngineError`.
