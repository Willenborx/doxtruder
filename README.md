# Doxtruder

Doxtruder collects configurations from your infrastructure (REST APIs) and renders them into Markdown documentation using Jinja2 templates. Point it at your homelab — Gitea, Proxmox, UniFi, OPNSense, OpenRouter, Requesty, or any other REST API — and get a readable, up-to-date snapshot of your setup.

Development supported by AI

## How it works

1. A YAML config file (`doxtruder.yaml`) defines **entities** — one per source.
2. Each entity names a **provider** (how to fetch data) and a **template** (how to render it).
3. The engine fetches all configured endpoints, aggregates the JSON, and renders one Markdown file per entity into the output path.

## Installation

Requires Python 3.10+.

```bash
python3 -m venv venv
./venv/bin/pip install -e .
```

This installs the dependencies (declared in `pyproject.toml`) and creates the
`doxtruder` console script used to run the tool.

## Quick start

1. Copy the example config and adapt it:

   ```bash
   cp doxtruder-example.yaml doxtruder.yaml
   ```

2. Create a `.env` file with the URLs and API keys referenced in the config:

   ```
   GITEA_URL=https://gitea.example.com
   GITEA_KEY=<token>
   PROXMOX_BASE=https://pve.example.com:8006
   PROXMOX_KEY=<token>
   ...
   ```

3. Run:

   ```bash
   ./venv/bin/doxtruder
   ```

   Markdown files are written to `./output/` (configurable via `settings.outputpath`).

## Command line

```
doxtruder [OPTIONS]
```

| Option | Description |
|---|---|
| `--config, -c` | Configuration file (default: `./doxtruder.yaml`) |
| `--loglevel, -v` | 1:critical … 5:debug (default: 4) |
| `--env, -e` | Environment file with `<KEY>=<VALUE>` definitions (default: `.env`) |
| `--store, -s` | Also store the raw JSON in a timestamped folder under the output path |
| `--templates, -t` | Path to a custom template folder (recommended: copy the built-in templates and trim them) |
| `--ssl-cert-ignore, -i` | Skip SSL certificate verification (e.g. self-signed hosts); same effect as `sslcert: ignore` in the `settings:` section |
| `--version` | Print the doxtruder version and exit |
| `--exception, -x` | (hidden, dev) Reraise exceptions to see the full stack trace on failure |

Exit codes: `0` = OK, `1` = completed with warnings, `2` = error (safe to use in scripts/CI).

## Configuration

Top-level structure of `doxtruder.yaml`:

```yaml
settings:
  title: My Environment
  outputpath: ./output
  # timeout: 30                # optional, HTTP request timeout in seconds (default: 10)
  # templatefolder: <folder>   # optional, overrides built-in templates

entities:
  Gitea:
    provider: RESTProvider
    template: gitea.md.jinja
    title: Gitea
    baseURL: ${GITEA_URL}
    headers:
      Authorization: token ${GITEA_KEY}
      accept: "application/json"
    endpoints:
      users: /admin/users
      repos: /users/$jpath{$.users[*].login}/repos
```

- **`${VAR}`** placeholders in any string are resolved from the environment (loaded from the `--env` file).
- **`$jpath{...}`** in endpoint URLs substitutes values from previously fetched data (JSONPath), e.g. fetch one repo list per user.
- **`headers:`** — arbitrary HTTP headers (Bearer tokens, API keys, …).
- **`auth.basic:`** — HTTP Basic authentication (user/pass), used e.g. by OPNSense.
- **`sslcert: ignore`** — skip SSL certificate verification (self-signed hosts). Set it on a single entity to affect only that entity, or in the global `settings:` section to affect all entities (same effect as `--ssl-cert-ignore` / `-i`).
- An endpoint can be a plain path (`name: /path`) or a mapping with `url:`, plus optional `body:` (a JSON request body) and `method:` (default `GET`; one of `GET`/`POST`/`PUT`/`PATCH`/`DELETE`/`HEAD`, case-insensitive). Use `method: POST` with a `body:` for endpoints that only accept POST, e.g.:

```yaml
    endpoints:
      keyusage:
        url: /apikey/$jpath{$.keys.keys[*].id}/usage
        method: post
        body:
          start: 2026-10-17T00:00:00Z
          resolution: month
```

### Providers

- **`RESTProvider`** — the generic provider; works with any REST API that authenticates via headers or basic auth.
- **`OpenrouterProvider`** — extends RESTProvider with OpenRouter-specific analytics (daily usage via POST `/analytics/query`).

### Templates

Built-in Jinja2 templates live in `doxtruder/templates/` (`gitea`, `proxmox`, `unifi`, `opnsense`, `openrouter`, `requesty`, plus a shared `header`). Each receives the aggregated JSON of its entity under the endpoint names. For custom output, copy the templates to your own folder and pass it with `--templates` / `settings.templatefolder`.

## Tests

Offline test suite (all HTTP mocked, no network needed):

```bash
./venv/bin/pip install -r tests/requirements-test.txt
./venv/bin/python -m pytest
```

Coverage (branch) is enabled by default; the HTML report is written to `htmlcov/`. See [tests/README.md](tests/README.md) for details.

## Documentation

API documentation is generated with Sphinx:

```bash
pip install -r requirements-docs.txt
cd docs/sphinx && make html
```

## Project layout

```
doxtruder/              package
  cli.py                CLI entry point (Click), installed as `doxtruder`
  DoxtruderEngine.py    execution loop, status codes, Jinja2 setup
  ConfigProvider.py     abstract provider base class
  RESTProvider.py       generic REST provider
  OpenrouterProvider.py OpenRouter-specific provider
  Exceptions.py         exception hierarchy with user-facing hints
  templates/            built-in Jinja2 templates
pyproject.toml          packaging, ruff/black configuration
.gitea/workflows/ci.yml CI pipeline (lint + tests)
doxtruder-example.yaml  example configuration
tests/                  pytest suite (offline, mocked)
docs/sphinx/            Sphinx documentation
```

## Development

```bash
./venv/bin/pip install -e ".[test,dev]"
ruff check . && black --check .   # lint / format
python -m pytest                  # tests with coverage
```
