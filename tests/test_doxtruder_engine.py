"""Tests for doxtruder.DoxtruderEngine.run_doxtruder.

The engine walks the top-level YAML 'entities' dict, dynamically imports each
entity's provider class and drives it through init_entity / retrieve_config /
do_render_template. Per-entity failures are isolated: an EntityError,
RenderError or EndpointError from one entity is logged and skipped while the
remaining entities still run; a generic Exception is converted to an
EntityError with the same effect.

Failure categories (doxtruder.Exceptions):
- EngineError (raised during setup or by a provider) is fatal: the run stops
  immediately and STATUS_ERROR is returned.
- EntityError / RenderError / EndpointError downgrade the run to
  STATUS_WARNING and processing continues with the next entity.

`run_doxtruder` returns the overall status; it only raises EngineError for
fatal global problems (broken settings, no entities). The old end-of-run
"raise when STATUS_ERROR" hack is gone. The provider loading is stubbed via
the `fake_provider` fixture so no network or real provider is involved.
"""

import os

import pytest

from doxtruder import DoxtruderEngine
from doxtruder.DoxtruderEngine import STATUS_ERROR, STATUS_OK, STATUS_WARNING
from doxtruder.Exceptions import (
    EndpointError,
    EngineError,
    EntityError,
    RenderError,
)


def test_happy_path_runs_full_lifecycle(fake_provider, engine_yml, caplog):
    """A valid, OK-status entity goes through __init__ -> init_entity ->
    retrieve_config -> do_render_template and the run reports a clean finish
    with STATUS_OK returned."""
    with caplog.at_level("INFO"):
        status = DoxtruderEngine.run_doxtruder(engine_yml, False)

    methods = [c[0] for c in fake_provider.get_calls()]
    assert methods == [
        "__init__",
        "init_entity",
        "retrieve_config",
        "do_render_template",
    ]
    assert status == STATUS_OK
    assert "Process finished successfully" in caplog.text


def test_warning_status_entity_yields_warning(fake_provider, engine_yml, caplog):
    fake_provider.set_status(STATUS_WARNING)
    with caplog.at_level("INFO"):
        status = DoxtruderEngine.run_doxtruder(engine_yml, False)
    assert status == STATUS_WARNING
    assert "Process finished with warning(s)" in caplog.text


def test_error_status_entity_returns_error_status(fake_provider, engine_yml, caplog):
    """A provider reporting STATUS_ERROR propagates as the run's return value
    (the old end-of-run raise-if-ERROR hack is gone; only EngineError aborts)."""
    fake_provider.set_status(STATUS_ERROR)
    with caplog.at_level("INFO"):
        status = DoxtruderEngine.run_doxtruder(engine_yml, False)
    assert status == STATUS_ERROR


def test_engine_error_from_provider_aborts_run(fake_provider, engine_yml, caplog):
    """An EngineError raised while processing an entity is fatal: it is
    logged, the run stops immediately and STATUS_ERROR is returned without
    touching the remaining entities."""
    failing_yml = dict(engine_yml)
    failing_yml["entities"] = {
        "broken": {"provider": "FakeProvider", "baseURL": "x", "endpoints": {}},
        "ok": {
            "provider": "FakeProvider",
            "baseURL": "https://fake.local/api",
            "endpoints": {"health": "/status"},
        },
    }
    fake_provider.set_error(EngineError("global config unusable"))

    with caplog.at_level("INFO"):
        status = DoxtruderEngine.run_doxtruder(failing_yml, False)

    assert status == STATUS_ERROR
    assert "global config unusable" in caplog.text
    entities_init = [c[1] for c in fake_provider.get_calls() if c[0] == "__init__"]
    assert entities_init == ["broken"]  # the run aborted before the next entity


def test_entity_error_is_caught_and_run_continues(fake_provider, engine_yml, caplog):
    """An EntityError raised during retrieve_config of one entity is logged
    (its message) and the run downgrades to a warning, still trying the rest."""
    failing_yml = dict(engine_yml)
    failing_yml["entities"] = {
        "broken": {"provider": "FakeProvider", "baseURL": "x", "endpoints": {}},
        "ok": {
            "provider": "FakeProvider",
            "baseURL": "https://fake.local/api",
            "endpoints": {"health": "/status"},
        },
    }
    fake_provider.set_error(EntityError("boom during retrieval"))

    with caplog.at_level("INFO"):
        status = DoxtruderEngine.run_doxtruder(failing_yml, False)

    assert "boom during retrieval" in caplog.text
    assert "Process finished with warning(s)" in caplog.text
    entities_init = [c[1] for c in fake_provider.get_calls() if c[0] == "__init__"]
    assert entities_init == ["broken", "ok"]  # the healthy entity still ran
    assert status == STATUS_WARNING


def test_entity_without_provider_key_is_skipped(fake_provider, engine_yml, caplog):
    """An entity lacking the 'provider' key raises KeyError, which the engine
    swallows as a warning and continues with the remaining entities."""
    two_entities = dict(engine_yml)
    two_entities["entities"] = {
        "broken": {"baseURL": "x"},  # no 'provider' key
        "ok": {
            "provider": "FakeProvider",
            "baseURL": "https://fake.local/api",
            "endpoints": {"health": "/status"},
        },
    }

    with caplog.at_level("INFO"):
        status = DoxtruderEngine.run_doxtruder(two_entities, False)

    assert "Entity 'broken' has no 'provider:' entry" in caplog.text
    entities_init = [c[1] for c in fake_provider.get_calls() if c[0] == "__init__"]
    assert entities_init == ["ok"]  # the healthy entity still ran
    assert status == STATUS_WARNING


def test_unknown_provider_module_is_skipped(fake_provider, engine_yml, caplog):
    """A provider name with no matching module triggers ModuleNotFoundError,
    which the engine logs and skips, continuing with the rest."""
    bad_yml = dict(engine_yml)
    bad_yml["entities"] = {
        "ghost": {"provider": "NoSuchProvider", "baseURL": "x", "endpoints": {}},
        "ok": {
            "provider": "FakeProvider",
            "baseURL": "https://fake.local/api",
            "endpoints": {"health": "/status"},
        },
    }

    with caplog.at_level("INFO"):
        status = DoxtruderEngine.run_doxtruder(bad_yml, False)

    assert "Provider module 'doxtruder.NoSuchProvider' does not exist" in caplog.text
    entities_init = [c[1] for c in fake_provider.get_calls() if c[0] == "__init__"]
    assert entities_init == ["ok"]  # the healthy entity after it still ran
    assert status == STATUS_WARNING


def test_missing_entities_key_raises_engine_error(fake_provider, caplog):
    """A top-level config without 'entities' is fatal: the engine raises
    EngineError naming the missing entities section."""
    yml = {"settings": {"title": "t", "outputpath": "/tmp/unused"}}
    with caplog.at_level("INFO"):
        with pytest.raises(EngineError) as excinfo:
            DoxtruderEngine.run_doxtruder(yml, False)
    assert "The configuration contains no entities." in str(excinfo.value.message)


def test_missing_settings_raises_engine_error(fake_provider):
    """A config without a valid 'settings:' section is fatal before any
    entity is instantiated."""
    with pytest.raises(EngineError) as excinfo:
        DoxtruderEngine.run_doxtruder({"entities": {}}, False)
    assert "no valid top-level 'settings:' section" in str(excinfo.value.message)


# --------------------------------------------------------------------------
# Per-entity exception handling added with the exception-handling rework
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        RenderError("template blew up"),
        EndpointError("endpoint returned 500"),
        EntityError("entity config broken"),
    ],
    ids=["render_error", "endpoint_error", "entity_error"],
)
def test_exception_categories_are_caught_and_run_continues(
    fake_provider, engine_yml, caplog, error
):
    """RenderError, EndpointError and EntityError raised by one entity are
    caught by the engine: the message is logged, the run downgrades to a
    warning and the following entities still execute."""
    failing_yml = dict(engine_yml)
    failing_yml["entities"] = {
        "broken": {"provider": "FakeProvider", "baseURL": "x", "endpoints": {}},
        "ok": {
            "provider": "FakeProvider",
            "baseURL": "https://fake.local/api",
            "endpoints": {"health": "/status"},
        },
    }
    fake_provider.set_error(error)

    with caplog.at_level("INFO"):
        status = DoxtruderEngine.run_doxtruder(failing_yml, False)

    assert error.message in caplog.text
    assert error.action in caplog.text
    assert "Process finished with warning(s)" in caplog.text
    entities_init = [c[1] for c in fake_provider.get_calls() if c[0] == "__init__"]
    assert entities_init == ["broken", "ok"]  # the healthy entity still ran
    assert status == STATUS_WARNING


def test_settings_templatefolder_selects_filesystem_loader(fake_provider, tmp_path, caplog):
    """'templatefolder' in settings makes the engine build a FileSystemLoader
    from that path instead of the packaged templates (PackageLoader)."""
    tdir = tmp_path / "mytemplates"
    tdir.mkdir()
    yml = {
        "settings": {
            "title": "test-docs",
            "outputpath": str(tmp_path),
            "templatefolder": str(tdir),
        },
        "entities": {
            "router": {
                "provider": "FakeProvider",
                "baseURL": "https://fake.local/api",
                "endpoints": {"health": "/status"},
            },
        },
    }

    with caplog.at_level("INFO"):
        status = DoxtruderEngine.run_doxtruder(yml, False)

    assert status == STATUS_OK
    assert f"Load templates from {tdir}" in caplog.text


def test_settings_without_outputpath_defaults_to_output_dir(
    fake_provider, tmp_path, monkeypatch, caplog
):
    """'outputpath' is optional in settings now (yml['settings'].get with a
    './output/' default): the run succeeds and creates the directory instead
    of raising KeyError -> DoxtruderEngineError."""
    monkeypatch.chdir(tmp_path)
    yml = {
        "settings": {"title": "test-docs"},  # no 'outputpath' key
        "entities": {
            "router": {
                "provider": "FakeProvider",
                "baseURL": "https://fake.local/api",
                "endpoints": {"health": "/status"},
            },
        },
    }

    with caplog.at_level("INFO"):
        status = DoxtruderEngine.run_doxtruder(yml, False)

    assert status == STATUS_OK
    assert (tmp_path / "output").is_dir()


def test_outputpath_with_tilde_expands_to_home(
    fake_provider, engine_yml, tmp_path, monkeypatch, caplog
):
    """'outputpath: ~/output' expands the leading '~' to the user's home
    directory. pathlib does NOT expand tildes, so without expanduser the run
    would create a literal '<cwd>/~/output' directory and write the docs into
    it instead of the intended <home>/output."""
    monkeypatch.setenv("HOME", str(tmp_path))
    yml = dict(engine_yml)
    yml["settings"] = {"title": "test-docs", "outputpath": "~/output"}

    with caplog.at_level("INFO"):
        status = DoxtruderEngine.run_doxtruder(yml, False)

    assert status == STATUS_OK
    assert (tmp_path / "output").is_dir()  # expanded to the (fake) home dir
    # providers receive the same expanded value so render_config writes there
    assert fake_provider.get_settings()[-1]["outputpath"] == str(tmp_path / "output")


def test_outputpath_multi_level_relative_created(fake_provider, engine_yml, tmp_path, monkeypatch):
    """A nested relative 'outputpath:' (a/b/c) is created automatically: all
    intermediate directories are made by the engine before rendering."""
    monkeypatch.chdir(tmp_path)
    yml = dict(engine_yml)
    yml["settings"] = {"title": "test-docs", "outputpath": "docs/rendered"}
    status = DoxtruderEngine.run_doxtruder(yml, False)
    assert status == STATUS_OK
    assert (tmp_path / "docs" / "rendered").is_dir()


def test_outputpath_multi_level_absolute_and_store(fake_provider, engine_yml, tmp_path):
    """A nested absolute 'outputpath:' with --store creates the full path and
    puts the timestamped JSON store folder inside it (multi-level + nested)."""
    out = tmp_path / "a" / "b" / "c"
    yml = dict(engine_yml)
    yml["settings"] = {"title": "test-docs", "outputpath": str(out)}
    status = DoxtruderEngine.run_doxtruder(yml, True)
    assert status == STATUS_OK
    assert out.is_dir()
    assert list(out.glob("*/router.json"))  # timestamp folder nested inside c/


def test_outputpath_existing_file_raises_engine_error(fake_provider, engine_yml, tmp_path):
    """'outputpath:' pointing at an existing file (not a directory) is caught
    as a clean EngineError instead of an escaping FileExistsError."""
    blocker = tmp_path / "blocker.md"
    blocker.write_text("i am a file")
    yml = dict(engine_yml)
    yml["settings"] = {"title": "test-docs", "outputpath": str(blocker)}
    with pytest.raises(EngineError) as excinfo:
        DoxtruderEngine.run_doxtruder(yml, False)
    assert "Cannot create the output directory" in str(excinfo.value.message)


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_outputpath_without_write_permission_raises_engine_error(
    fake_provider, engine_yml, tmp_path
):
    """An 'outputpath:' the user cannot write is caught as a clean EngineError,
    not an unhandled PermissionError."""
    noperm = tmp_path / "noperm"
    noperm.mkdir()
    noperm.chmod(0o555)
    try:
        yml = dict(engine_yml)
        yml["settings"] = {"title": "test-docs", "outputpath": str(noperm / "sub")}
        with pytest.raises(EngineError) as excinfo:
            DoxtruderEngine.run_doxtruder(yml, False)
        assert "Cannot create the output directory" in str(excinfo.value.message)
    finally:
        noperm.chmod(0o755)


@pytest.mark.parametrize("bad", [["a", "b"], 123, None], ids=["list", "int", "none"])
def test_outputpath_non_string_raises_engine_error(fake_provider, engine_yml, bad):
    """A non-string 'outputpath:' (list / int / null from YAML) is rejected with
    a clean EngineError naming the offending type, instead of a raw TypeError
    traceback from pathlib."""
    yml = dict(engine_yml)
    yml["settings"] = {"title": "test-docs", "outputpath": bad}
    with pytest.raises(EngineError) as excinfo:
        DoxtruderEngine.run_doxtruder(yml, False)
    assert "'outputpath:' must be a filesystem path string" in str(excinfo.value.message)
    assert type(bad).__name__ in str(excinfo.value.message)


def test_outputpath_with_null_byte_raises_engine_error(fake_provider, engine_yml):
    """An 'outputpath:' containing a NUL byte is caught as a clean EngineError
    instead of an unhandled ValueError ('embedded null character')."""
    yml = dict(engine_yml)
    yml["settings"] = {"title": "test-docs", "outputpath": "ab\x00cd"}
    with pytest.raises(EngineError) as excinfo:
        DoxtruderEngine.run_doxtruder(yml, False)
    assert "Invalid output directory path" in str(excinfo.value.message)


def test_store_flag_writes_json_dump_in_timestamp_folder(fake_provider, engine_yml, caplog):
    """store=True creates a timestamped folder under outputpath and dumps each
    entity's configdata as <entity>.json into it."""
    with caplog.at_level("INFO"):
        status = DoxtruderEngine.run_doxtruder(engine_yml, True)

    assert status == STATUS_OK
    import json
    from pathlib import Path

    dumps = list(Path(engine_yml["settings"]["outputpath"]).glob("*/router.json"))
    assert len(dumps) == 1
    assert json.loads(dumps[0].read_text()) == {"stored": True}


def test_store_folder_name_includes_seconds(fake_provider, engine_yml):
    """The timestamped store folder name has second precision (YYYY-MM-DD-
    HH-MM-SS) so two runs in the same minute do not overwrite each other's
    JSON dumps."""
    import re
    from pathlib import Path

    DoxtruderEngine.run_doxtruder(engine_yml, True)
    folders = [p.name for p in Path(engine_yml["settings"]["outputpath"]).iterdir() if p.is_dir()]
    assert len(folders) == 1
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}", folders[0])


@pytest.mark.parametrize("bad_name", ["Foo/Bar", "../escape", "sub/dir/x", ".", "..", " "])
def test_entity_name_with_path_separator_skips_entity(fake_provider, engine_yml, bad_name):
    """An entity key that cannot be a plain file name (path separator, '..'
    or blank) is skipped with an EntityError instead of writing <entity>.md /
    <entity>.json outside the output directory (path traversal)."""
    engine_yml["entities"][bad_name] = {"provider": "FakeProvider"}
    status = DoxtruderEngine.run_doxtruder(engine_yml, False)
    assert status == STATUS_WARNING
    # only the valid 'router' entity ran through the provider lifecycle
    assert [c for c in fake_provider.get_calls() if c[0] == "__init__"] == [("__init__", "router")]


def test_run_doxtruder_does_not_mutate_caller_settings(fake_provider, tmp_path):
    """run_doxtruder injects 'ignore_ssl'/'outputpath' into a copy of the
    settings section; the caller's configuration dict stays untouched."""
    yml = {
        "settings": {"title": "t", "outputpath": str(tmp_path)},
        "entities": {"router": {"provider": "FakeProvider"}},
    }
    before = dict(yml["settings"])
    DoxtruderEngine.run_doxtruder(yml, False, ignore_ssl=True)
    assert yml["settings"] == before


def test_store_dump_opened_with_utf8_encoding(fake_provider, engine_yml, monkeypatch):
    """The JSON dump file is opened with an explicit utf-8 encoding, so a
    non-UTF-8 locale cannot raise UnicodeEncodeError on non-ASCII API data."""
    opened = {}
    real_open = open

    def spy_open(file, mode="r", **kwargs):
        if "w" in mode and str(file).endswith(".json"):
            opened["encoding"] = kwargs.get("encoding")
        return real_open(file, mode, **kwargs)

    import builtins

    monkeypatch.setattr(builtins, "open", spy_open)
    DoxtruderEngine.run_doxtruder(engine_yml, True)
    assert opened.get("encoding") == "utf-8"


def test_ignore_ssl_is_injected_into_settings(fake_provider, engine_yml, tmp_path):
    """run_doxtruder(..., ignore_ssl=True) exposes settings['ignore_ssl'] so
    providers can pick it up for certificate verification (default False)."""
    DoxtruderEngine.run_doxtruder(engine_yml, False, ignore_ssl=True)
    # the engine passes the same settings dict to every provider and mutates
    # it per run, so capture the value before starting the next run
    assert fake_provider.get_settings()[-1]["ignore_ssl"] is True
    seen = dict(fake_provider.get_settings()[-1])

    other = {
        "settings": {"title": "other", "outputpath": str(tmp_path)},
        "entities": {
            "second": {
                "provider": "FakeProvider",
                "baseURL": "https://fake.local/api",
                "endpoints": {"health": "/status"},
            },
        },
    }
    DoxtruderEngine.run_doxtruder(other, False)
    assert seen["ignore_ssl"] is True
    assert fake_provider.get_settings()[-1]["ignore_ssl"] is False


def test_sslcert_ignore_in_settings_matches_ignore_ssl_flag(fake_provider, engine_yml):
    """'sslcert: ignore' in the global settings: section has the same effect
    as the -i/--ssl-cert-ignore flag: settings['ignore_ssl'] becomes True
    even when the flag itself is not set."""
    engine_yml["settings"]["sslcert"] = "ignore"
    DoxtruderEngine.run_doxtruder(engine_yml, False)
    assert fake_provider.get_settings()[-1]["ignore_ssl"] is True


def test_sslcert_value_other_than_ignore_keeps_verification(fake_provider, engine_yml):
    """An sslcert value that is not 'ignore' (e.g. a typo or a CA path in the
    settings section) does not disable certificate verification."""
    engine_yml["settings"]["sslcert"] = "check"
    DoxtruderEngine.run_doxtruder(engine_yml, False)
    assert fake_provider.get_settings()[-1]["ignore_ssl"] is False


def test_sslcert_ignore_logs_retrieving_configuration_with_notice(
    fake_provider, engine_yml, caplog
):
    """When an entity ignores SSL (here via its own 'sslcert: ignore'), the
    per-entity log line says 'Retrieving configuration (ignoring SSL
    certificate)'."""
    engine_yml["entities"]["router"]["sslcert"] = "ignore"
    with caplog.at_level("INFO"):
        DoxtruderEngine.run_doxtruder(engine_yml, False)
    assert "Retrieving configuration (ignoring SSL certificate)" in caplog.text


def test_retrieving_configuration_log_without_ssl_notice_by_default(
    fake_provider, engine_yml, caplog
):
    """Without -i or sslcert: ignore the plain 'Retrieving configuration' line
    is logged and carries no SSL notice."""
    with caplog.at_level("INFO"):
        DoxtruderEngine.run_doxtruder(engine_yml, False)
    assert "Retrieving configuration" in caplog.text
    assert "ignoring SSL certificate" not in caplog.text
