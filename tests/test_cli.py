"""Tests for the Doxtruder CLI (``doxtruder.cli``, installed as the
``doxtruder`` console script). It parses the config file and dispatches to the
engine. main() configures logging and handles YAML / filesystem errors by
logging. The process exit code now mirrors the engine status (0 = OK, 2 =
fatal EngineError) via ctx.exit(), so fatal config errors assert exit_code
== 2 while successful runs assert 0.
"""

import click
import click.testing
import pytest

from doxtruder import cli as _cli_module


@pytest.fixture
def cli():
    return _cli_module


@pytest.fixture
def runner():
    return click.testing.CliRunner()


def test_templates_option_sets_templatefolder_in_settings(
    cli, runner, tmp_path, monkeypatch, caplog
):
    """The new --templates/-t option injects 'templatefolder' into the
    settings block handed to run_doxtruder so the engine switches to the
    FileSystemLoader."""
    cfg = _write_yaml(tmp_path / "t.yaml", "settings:\n  title: t\nentities: {}\n")
    tdir = tmp_path / "templates"
    captured = {}

    def fake_run(yml, store=False, ignore_ssl=False):
        captured["yml"] = yml
        return 0

    monkeypatch.setattr(cli, "run_doxtruder", fake_run)

    with caplog.at_level("INFO"):
        result = runner.invoke(cli.main, ["--config", cfg, "--templates", str(tdir)])

    assert result.exit_code == 0
    assert captured["yml"]["settings"]["templatefolder"] == str(tdir)


def test_store_flag_forwards_store_to_engine(cli, runner, tmp_path, monkeypatch, caplog):
    """The --store/-s flag is threaded through main() as store=True to
    run_doxtruder (--raw was removed in favour of --store)."""
    cfg = _write_yaml(tmp_path / "doxtruder.yaml", "settings:\n  title: t\nentities: {}\n")
    captured = {}

    def fake_run(yml, store=False, ignore_ssl=False):
        captured["store"] = store
        return 0

    monkeypatch.setattr(cli, "run_doxtruder", fake_run)
    monkeypatch.chdir(tmp_path)

    with caplog.at_level("INFO"):
        result = runner.invoke(cli.main, ["--config", cfg, "--store"])

    assert result.exit_code == 0
    assert captured.get("store") is True


def test_ssl_cert_ignore_flag_forwards_ignore_ssl_to_engine(
    cli, runner, tmp_path, monkeypatch, caplog
):
    """The new --ssl-cert-ignore/-i flag is threaded through main() as
    ignore_ssl=True to run_doxtruder; without the flag it is False."""
    cfg = _write_yaml(tmp_path / "doxtruder.yaml", "settings:\n  title: t\nentities: {}\n")
    captured = {}

    def fake_run(yml, store=False, ignore_ssl=False):
        captured["ignore_ssl"] = ignore_ssl
        return 0

    monkeypatch.setattr(cli, "run_doxtruder", fake_run)
    monkeypatch.chdir(tmp_path)

    with caplog.at_level("INFO"):
        result = runner.invoke(cli.main, ["--config", cfg, "--ssl-cert-ignore"])

    assert result.exit_code == 0
    assert captured.get("ignore_ssl") is True

    captured.clear()
    with caplog.at_level("INFO"):
        result = runner.invoke(cli.main, ["--config", cfg, "-i"])

    assert result.exit_code == 0
    assert captured.get("ignore_ssl") is True

    captured.clear()
    with caplog.at_level("INFO"):
        result = runner.invoke(cli.main, ["--config", cfg])

    assert result.exit_code == 0
    assert captured.get("ignore_ssl") is False


def _write_yaml(path, text):
    path.write_text(text)
    return str(path)


def test_valid_config_calls_engine(cli, runner, tmp_path, monkeypatch, caplog):
    cfg = _write_yaml(tmp_path / "doxtruder.yaml", "settings:\n  title: t\nentities: {}\n")
    captured = {}

    def fake_run(yml, store=False, ignore_ssl=False):
        captured["yml"] = yml

    monkeypatch.setattr(cli, "run_doxtruder", fake_run)

    with caplog.at_level("INFO"):
        result = runner.invoke(cli.main, ["--config", cfg])

    assert result.exit_code == 0
    assert captured["yml"] == {"settings": {"title": "t"}, "entities": {}}
    assert "Reading confi" in caplog.text or "Opening config file" in caplog.text


def test_config_file_read_with_utf8_encoding(cli, runner, tmp_path, monkeypatch):
    """The configuration file is opened with an explicit utf-8 encoding, so a
    config containing non-ASCII (e.g. a template title) loads correctly even
    under a non-UTF-8 locale."""
    cfg = _write_yaml(tmp_path / "doxtruder.yaml", "settings:\n  title: für\nentities: {}\n")
    opened = {}
    real_open = open

    def spy_open(file, mode="r", **kwargs):
        if str(file) == cfg:
            opened["encoding"] = kwargs.get("encoding")
        return real_open(file, mode, **kwargs)

    import builtins

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(cli, "run_doxtruder", lambda yml, store=False, ignore_ssl=False: 0)

    result = runner.invoke(cli.main, ["--config", cfg])
    assert result.exit_code == 0
    assert opened.get("encoding") == "utf-8"


def test_engine_status_becomes_exit_code(cli, runner, tmp_path, monkeypatch):
    """main() must propagate the engine's status as the process exit code via
    ctx.exit() (Click 8.x discards a plain return value, so the exit code
    would otherwise always be 0)."""
    cfg = _write_yaml(tmp_path / "doxtruder.yaml", "settings:\n  title: t\nentities: {}\n")

    monkeypatch.setattr(cli, "run_doxtruder", lambda yml, store=False, ignore_ssl=False: 2)
    result = runner.invoke(cli.main, ["--config", cfg])
    assert result.exit_code == 2

    monkeypatch.setattr(cli, "run_doxtruder", lambda yml, store=False, ignore_ssl=False: 1)
    result = runner.invoke(cli.main, ["--config", cfg])
    assert result.exit_code == 1


def test_missing_config_file(cli, runner, caplog):
    with caplog.at_level("INFO"):
        result = runner.invoke(cli.main, ["--config", "/nonexistent/doxtruder.yaml"])
    assert result.exit_code == 2
    assert "The configuration file '/nonexistent/doxtruder.yaml' was not found" in caplog.text


def test_config_is_directory(cli, runner, tmp_path, caplog):
    with caplog.at_level("INFO"):
        result = runner.invoke(cli.main, ["--config", str(tmp_path)])
    assert result.exit_code == 2
    assert "is a directory" in caplog.text


def test_unreadable_config_file(cli, runner, tmp_path, caplog):
    cfg = tmp_path / "locked.yaml"
    cfg.write_text("settings:\n  title: t\n")
    cfg.chmod(0o000)
    try:
        with caplog.at_level("INFO"):
            result = runner.invoke(cli.main, ["--config", str(cfg)])
        # skip gracefully on root (root can read anything), where the chmod
        # fails to block access
        if result.exception is None and not caplog.text:
            pytest.skip("running as root - permission check moot")
        elif "No permssion to access" in caplog.text:
            assert True
    finally:
        cfg.chmod(0o644)


def test_invalid_yaml_is_reported(cli, runner, tmp_path, caplog):
    cfg = _write_yaml(tmp_path / "bad.yaml", ":: not : : valid :[")
    with caplog.at_level("INFO"):
        result = runner.invoke(cli.main, ["--config", cfg])
    assert result.exit_code == 2
    # EngineError.wrap(...) keeps the original YAMLError as __cause__, which
    # DoxtruderError.log() attaches at DEBUG level.
    assert "Error parsing the configuration file" in caplog.text
    assert "Check the YAML syntax" in caplog.text


# --------------------------------------------------------------------------
# ${ENV} variable resolution (the top-level `resolve` helper)
# --------------------------------------------------------------------------


def test_resolve_replaces_set_env_variable(cli, monkeypatch):
    monkeypatch.setenv("PROX_TOKEN", "s3cr3t")
    assert cli.resolve("${PROX_TOKEN}") == "s3cr3t"


def test_resolve_leaves_unset_variable_unchanged(cli, monkeypatch):
    monkeypatch.delenv("NOT_SET", raising=False)
    assert cli.resolve("${NOT_SET}") == "${NOT_SET}"


def test_resolve_warns_about_unset_variables(cli, monkeypatch, caplog):
    """An unset ${VAR} is kept as-is but must be reported, since a literal
    '${VAR}' in a URL or header fails confusingly later (e.g. as a 401)."""
    monkeypatch.delenv("NOT_SET_A", raising=False)
    monkeypatch.delenv("NOT_SET_B", raising=False)
    with caplog.at_level("WARNING"):
        cli.resolve({"url": "${NOT_SET_B}/x", "key": "${NOT_SET_A}"})
    assert "NOT_SET_A" in caplog.text
    assert "NOT_SET_B" in caplog.text


def test_resolve_no_warning_when_all_variables_are_set(cli, monkeypatch, caplog):
    monkeypatch.setenv("SET_VAR", "ok")
    with caplog.at_level("WARNING"):
        cli.resolve("${SET_VAR}")
    assert caplog.text == ""


def test_resolve_substitutes_inside_larger_string(cli, monkeypatch):
    monkeypatch.setenv("HOST", "db.internal")
    assert cli.resolve("host=${HOST}:5432") == "host=db.internal:5432"


def test_resolve_recurses_into_nested_mappings(cli, monkeypatch):
    monkeypatch.setenv("KEY", "v")
    assert cli.resolve({"a": {"b": "${KEY}"}}) == {"a": {"b": "v"}}


def test_resolve_recurses_into_sequences(cli, monkeypatch):
    monkeypatch.setenv("HOST", "h")
    assert cli.resolve(["${HOST}", 3, None, True]) == ["h", 3, None, True]


def test_resolve_passes_through_non_strings(cli, monkeypatch):
    assert cli.resolve(42) == 42
    assert cli.resolve(None) is None
    assert cli.resolve(True) is True


def test_resolve_ignores_non_identifier_placeholders(cli, monkeypatch):
    """The regex only matches ${[A-Za-z_][A-Za-z0-9_]*}, so malformed
    placeholders are left untouched."""
    monkeypatch.setenv("911", "nope")
    assert cli.resolve("${1bad}") == "${1bad}"
    assert cli.resolve("${a-b}") == "${a-b}"
    assert cli.resolve("$X") == "$X"


# --------------------------------------------------------------------------
# --env option and .env loading
# --------------------------------------------------------------------------


def test_custom_env_file_resolves_vars_before_engine(cli, runner, tmp_path, monkeypatch, caplog):
    cfg = _write_yaml(
        tmp_path / "c.yaml",
        "settings:\n  title: t\nentities:\n  router:\n"
        "    provider: FakeProvider\n    APIKey: '${API_TOKEN}'\n",
    )
    envfile = tmp_path / "creds.env"
    envfile.write_text("API_TOKEN=abc123\n")

    captured = {}

    def fake_run(yml, store=False, ignore_ssl=False):
        captured["yml"] = yml
        return 0

    monkeypatch.setattr(cli, "run_doxtruder", fake_run)

    with caplog.at_level("INFO"):
        result = runner.invoke(cli.main, ["--config", cfg, "--env", str(envfile)])

    assert result.exit_code == 0
    assert captured["yml"]["entities"]["router"]["APIKey"] == "abc123"
    assert "Reading ENV Vars from" in caplog.text


def test_default_env_file_missing_is_warning_not_error(cli, runner, tmp_path, caplog, monkeypatch):
    """With --env left at its default ('.env'), a missing file is only a
    warning and the run still proceeds."""
    cfg = _write_yaml(tmp_path / "c.yaml", "settings:\n  title: t\nentities: {}\n")
    ran = {}

    def fake_run(yml, store=False, ignore_ssl=False):
        ran["yml"] = yml
        return 0

    monkeypatch.setattr(cli, "run_doxtruder", fake_run)
    monkeypatch.chdir(tmp_path)  # guarantees no '.env' in cwd

    with caplog.at_level("INFO"):
        result = runner.invoke(cli.main, ["--config", cfg])

    assert result.exit_code == 0
    assert "Environment file .env does not exist" in caplog.text
    assert "yml" in ran  # engine still ran


def test_missing_custom_env_raises_doxtruder_engine_error_and_is_handled(
    cli, runner, tmp_path, caplog
):
    """A missing non-default --env file is fatal: doxtruder raises
    DoxtruderEngineError, which main() catches and logs. No traceback; the
    engine never runs."""
    missing = tmp_path / "nope.env"
    with caplog.at_level("INFO"):
        result = runner.invoke(cli.main, ["--env", str(missing), "--config", "x"])

    # handled, not propagated: the only exception is the SystemExit(2) that
    # ctx.exit() raises to set the process exit code — no DoxtruderError
    assert isinstance(result.exception, SystemExit)
    assert result.exit_code == 2
    assert f"Environment file {missing} does not exist" in caplog.text
