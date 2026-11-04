"""Shared fixtures for the doxtruder test-suite.

The provider subclasses GiteaProvider / ProxmoxProvider / OPNSenseProvider
were removed; their per-service auth/header logic now lives in
``RESTProvider.perform_request`` and is driven by the per-entity YAML config
(``headers`` dict and ``auth.basic``). The shared fixtures here only
supply the Jinja2 environment and the engine-test provider double, both of
which are provider-agnostic.
"""

import types

import pytest
from jinja2 import Environment, PackageLoader, select_autoescape


@pytest.fixture
def jjenv():
    """A Jinja2 Environment for provider construction, mirroring what
    DoxtruderEngine now builds (PackageLoader over the packaged templates +
    autoescape, plus the `now` global) and hands to providers. Jinja2 is
    initialized outside ConfigProvider, so every provider test must supply
    its own environment."""
    from datetime import datetime

    env = Environment(
        loader=PackageLoader("doxtruder"),
        autoescape=select_autoescape(),
    )
    env.globals["now"] = datetime.now
    return env


# --------------------------------------------------------------------------
# Engine-test double: a fake provider class that never touches the network
# --------------------------------------------------------------------------


class FakeProvider:
    """Stand-in for a real provider class, driven through importlib just like
    DoxtruderEngine drives the real ones. Records every lifecycle call and
    returns a configurable status from retrieve_config."""

    retrieve_status = 0  # overridden per-test via the fixture
    retrieve_error = None  # if set, retrieve_config raises it
    calls = []  # [(method, ...)] log, reset by the fixture
    settings_seen = []  # settings dicts passed to __init__, for assertions

    def __init__(self, entityname, yml, settings, jjenv):
        self.calls.append(("__init__", entityname))
        self.settings_seen.append(settings)
        self.entity_cfg = yml
        self.settings = settings
        self.configdata = {"stored": True}

    def init_entity(self):
        self.calls.append(("init_entity",))

    def ignore_ssl(self):
        """Mirror ConfigProvider.ignore_ssl(): global -i/sslcert or per-entity
        sslcert: ignore disables certificate verification."""
        if self.settings.get("ignore_ssl"):
            return True
        sslcert = self.entity_cfg.get("sslcert") if isinstance(self.entity_cfg, dict) else None
        return isinstance(sslcert, str) and sslcert.strip().lower() == "ignore"

    def retrieve_config(self):
        self.calls.append(("retrieve_config",))
        if FakeProvider.retrieve_error:
            raise FakeProvider.retrieve_error
        return self.retrieve_status

    def do_render_template(self):
        self.calls.append(("do_render_template",))


@pytest.fixture
def fake_provider(monkeypatch):
    """Patches DoxtruderEngine.importlib.import_module so that only modules
    explicitly registered in `available` resolve; anything else raises
    ModuleNotFoundError (mirrors an unknown provider name)."""
    from doxtruder import DoxtruderEngine

    FakeProvider.calls = []
    FakeProvider.retrieve_status = 0
    FakeProvider.retrieve_error = None
    FakeProvider.settings_seen = []

    module_names = {"FakeProvider"}

    def fake_import(name):
        mod_name = name.split(".")[-1]
        if mod_name in module_names:
            return types.SimpleNamespace(**{mod_name: FakeProvider})
        raise ModuleNotFoundError(f"No module named {name!r}")

    monkeypatch.setattr(DoxtruderEngine.importlib, "import_module", fake_import)

    # returns helpers tests can use to configure the double
    return types.SimpleNamespace(
        set_status=lambda s: setattr(FakeProvider, "retrieve_status", s),
        set_error=lambda e: setattr(FakeProvider, "retrieve_error", e),
        get_calls=lambda: list(FakeProvider.calls),
        get_settings=lambda: list(FakeProvider.settings_seen),
        modules=module_names,
    )


@pytest.fixture
def engine_yml(tmp_path):
    """A top-level config for the engine: settings plus one entity that maps
    to the FakeProvider. `outputpath` is now required by run_doxtruder (it
    mkdir's the dir and renders output into it)."""
    return {
        "settings": {"title": "test-docs", "outputpath": str(tmp_path)},
        "entities": {
            "router": {
                "provider": "FakeProvider",
                "baseURL": "https://fake.local/api",
                "endpoints": {"health": "/status"},
            },
        },
    }
