"""Tests for doxtruder.ConfigProvider — the abstract base class.

INTENDED contract: ConfigProvider declares abstract methods and should be
non-instantiable on its own; subclasses implement them. In the current code
that contract is NOT enforced (see the xfail test below).
"""

import pytest
from jinja2 import Environment
from jsonpath_ng import JSONPathError

from doxtruder.ConfigProvider import ConfigProvider
from doxtruder.Exceptions import EndpointError, RenderError


def test_concrete_subclass_stores_init_attributes():
    """A subclass implementing the lifecycle methods is instantiable, and
    __init__ records entityname/yml/settings."""

    class Minimal(ConfigProvider):
        def retrieve_config(self):
            pass

        def render_config(self):
            pass

    provider = Minimal("router", {"a": 1}, {"b": 2}, Environment())
    assert provider.entityname == "router"
    assert provider.entity_cfg == {"a": 1}
    assert provider.settings == {"b": 2}


def test_base_class_is_abstract_and_not_instantiable():
    """ConfigProvider declares abstract methods, so it must NOT be
    instantiable — instantiating it raises TypeError listing the abstract
    methods."""

    with pytest.raises(TypeError, match="abstract"):
        ConfigProvider("router", {}, {}, Environment())


def test_default_json_serializer_handles_datetime_and_rejects_others():
    """default_json_serializer (moved here from RESTProvider) renders
    datetimes to ISO-8601 and raises TypeError naming the offending type for
    anything else, so json.dumps(default=...) aborts on genuinely
    non-serializable payloads with a useful message."""
    from datetime import datetime, timezone

    from doxtruder.ConfigProvider import default_json_serializer

    assert (
        default_json_serializer(datetime(2026, 10, 1, tzinfo=timezone.utc))
        == "2026-10-01T00:00:00+00:00"
    )
    with pytest.raises(TypeError, match="dict"):
        default_json_serializer({"a": 1})


# --------------------------------------------------------------------------
# Template rendering pipeline (do_render_template / render_config)
# --------------------------------------------------------------------------


class RendingProvider(ConfigProvider):
    """Concrete subclass whose render_config delegates to the real body so we
    can exercise the whole Jinja2 rendering pipeline."""

    def retrieve_config(self):
        return

    def render_config(self):
        return super().render_config()


class LifecycleSpy(ConfigProvider):
    """Subclass that records the render lifecycle calls instead of writing
    anything, to assert ordering."""

    def __init__(self, entityname, yml, settings, jjenv):
        super().__init__(entityname, yml, settings, jjenv)
        self.lifecycle = []

    def retrieve_config(self):
        return

    def render_init(self):
        self.lifecycle.append("render_init")

    def render_config(self):
        self.lifecycle.append("render_config")

    def render_finalize(self):
        self.lifecycle.append("render_finalize")


@pytest.fixture
def rendition_yml():
    return {"template": "unifi.md.jinja"}


@pytest.fixture
def rendition_settings(tmp_path):
    return {"outputpath": str(tmp_path)}


@pytest.fixture
def jjenv():
    """A Jinja2 Environment loaded from the packaged doxtruder templates,
    matching what DoxtruderEngine now builds and hands to providers."""
    from datetime import datetime

    from jinja2 import PackageLoader, select_autoescape

    env = Environment(
        loader=PackageLoader("doxtruder"),
        autoescape=select_autoescape(),
    )
    env.globals["now"] = datetime.now
    return env


def test_do_render_template_renders_packaged_template_to_output(
    rendition_yml, rendition_settings, jjenv, tmp_path
):
    """do_render_template loads a real packaged Jinja2 template and
    render_config writes the rendered markdown to
    <outputpath>/<entityname>.md with the entity name substituted."""
    prov = RendingProvider("ap-1", rendition_yml, rendition_settings, jjenv)
    prov.do_render_template()

    out = tmp_path / "ap-1.md"
    assert out.exists()
    content = out.read_text()
    assert "# ap-1" in content
    assert "Timestamp:" in content  # header.md.jinja renders the timestamp line


def test_render_config_opened_with_utf8_encoding(
    rendition_yml, rendition_settings, jjenv, monkeypatch
):
    """The rendered markdown file is opened with an explicit utf-8 encoding,
    so templates/data containing non-ASCII characters cannot break the write
    under a non-UTF-8 locale."""
    opened = {}
    real_open = open

    def spy_open(file, mode="r", **kwargs):
        if "w" in mode and str(file).endswith(".md"):
            opened["encoding"] = kwargs.get("encoding")
        return real_open(file, mode, **kwargs)

    import builtins

    monkeypatch.setattr(builtins, "open", spy_open)
    prov = RendingProvider("ap-1", rendition_yml, rendition_settings, jjenv)
    prov.do_render_template()
    assert opened.get("encoding") == "utf-8"


def test_gitea_template_renders_repos_grouped_by_owner(rendition_settings, jjenv, tmp_path):
    """The gitea template iterates data['repos'] as a dict keyed by owner
    (the JSON Path expansion of /users/$jpath{$.users[*].login}/repos), and
    renders one '# Repositories (<owner>)' section per owner. Owners with an
    empty repo list are skipped by the `if repos` guard."""
    prov = RendingProvider("gitea", {"template": "gitea.md.jinja"}, rendition_settings, jjenv)
    prov.configdata = {
        "users": [
            {
                "login": "alice",
                "full_name": "Alice A",
                "email": "a@x",
                "is_admin": False,
                "active": True,
                "restricted": False,
                "source_id": 0,
                "last_login": "2026-10-24T10:00:00Z",
                "created": "2026-01-01T00:00:00Z",
                "visibility": "private",
            }
        ],
        "repos": {
            "alice": [
                {
                    "name": "repo-a",
                    "html_url": "http://g/a/repo-a",
                    "description": "d1",
                    "language": "Python",
                    "default_branch": "main",
                    "private": True,
                    "archived": False,
                    "size": 12,
                    "updated_at": "2026-10-01T00:00:00Z",
                }
            ],
            "bob": [
                {
                    "name": "repo-b",
                    "html_url": "http://g/b/repo-b",
                    "description": "d2",
                    "language": "Go",
                    "default_branch": "main",
                    "private": False,
                    "archived": True,
                    "size": 34,
                    "updated_at": "2026-09-01T00:00:00Z",
                }
            ],
            "carol": [],  # no repos -> the section must be skipped
        },
    }
    prov.do_render_template()

    content = (tmp_path / "gitea.md").read_text()
    # one section per non-empty owner, with the owner in the heading
    assert "# Repositories (alice)" in content
    assert "# Repositories (bob)" in content
    # owner with zero repos renders nothing
    assert "# Repositories (carol)" not in content
    # each repo lands under its owner's section
    assert content.index("# Repositories (alice)") < content.index("repo-a")
    assert content.index("# Repositories (bob)") < content.index("repo-b")
    assert "[repo-a](http://g/a/repo-a)" in content
    assert "[repo-b](http://g/b/repo-b)" in content
    # users section still renders alongside
    assert "# Users" in content
    assert "alice" in content


def test_do_render_template_runs_lifecycle_in_order(rendition_yml, rendition_settings, jjenv):
    """do_render_template sets up the Jinja2 environment and invokes the
    render lifecycle in the order render_init -> render_config ->
    render_finalize."""
    prov = LifecycleSpy("ap-1", rendition_yml, rendition_settings, jjenv)
    prov.do_render_template()

    assert prov.lifecycle == ["render_init", "render_config", "render_finalize"]
    assert prov.jinja2env is not None
    assert prov.jinja2template is not None


def test_do_render_template_unknown_template_raises_render_error(rendition_settings, jjenv):
    """A template name with no matching packaged file raises RenderError."""
    prov = RendingProvider("ap-1", {"template": "nope.md.jinja"}, rendition_settings, jjenv)
    with pytest.raises(RenderError) as excinfo:
        prov.do_render_template()
    assert "nope.md.jinja' was not found" in excinfo.value.message
    assert excinfo.value.entity == "ap-1"


def test_do_render_template_missing_template_key_raises_render_error(rendition_settings, jjenv):
    """An entity config without a 'template' key raises RenderError with a
    configuration hint (the raw KeyError that used to escape is now caught by
    the explicit guard in do_render_template)."""
    prov = RendingProvider("ap-1", {}, rendition_settings, jjenv)
    with pytest.raises(RenderError) as excinfo:
        prov.do_render_template()
    assert "has no 'template:' entry" in excinfo.value.message
    assert "template:" in excinfo.value.hint


# --------------------------------------------------------------------------
# JSON Path endpoint expansion (expand_json_path)
# --------------------------------------------------------------------------


class JSONPathProvider(ConfigProvider):
    """Concrete subclass exposing the JSON Path expansion helpers so they can
    be tested directly."""

    def retrieve_config(self):
        return

    def render_config(self):
        return


@pytest.fixture
def jp_provider(jjenv):
    return JSONPathProvider("router", {"template": "x"}, {"outputpath": "/tmp"}, jjenv)


def test_expand_json_path_multi_match_expands_to_one_url_per_value(jp_provider):
    """A $jpath{...} expression matching several values expands the original
    URL into one URL per value, substituted in place of the placeholders.
    The data is read from the source_json argument. The result is now a dict
    keyed by the matched value (e.g. the owner login), so retrieve_config can
    group the per-key responses into configdata[endpoint][owner]."""
    data = {"users": [{"username": "alice"}, {"username": "bob"}]}
    expanded = jp_provider.expand_json_path("/user/$jpath{$.users[*].username}/details", data)
    assert expanded == {
        "alice": "/user/alice/details",
        "bob": "/user/bob/details",
    }


def test_expand_json_path_single_match_yields_one_url(jp_provider):
    """A single match still comes back as a one-entry dict keyed by that
    value (no 'SINGLE' sentinel for a real JSON Path match)."""
    data = {"users": [{"username": "alice"}]}
    assert jp_provider.expand_json_path("/user/$jpath{$.users[*].username}/details", data) == {
        "alice": "/user/alice/details"
    }


def test_expand_json_path_no_placeholder_returns_single_key_dict(jp_provider):
    """Without any $jpath{} placeholder the URL is returned verbatim under
    the 'SINGLE' key, which retrieve_config stores directly under the
    endpoint name (a plain, non-grouped endpoint)."""
    jp_provider.configdata = {}
    assert jp_provider.expand_json_path("/plain/path", None) == {"SINGLE": "/plain/path"}


def test_expand_json_path_uses_source_json_arg_not_configdata(jp_provider):
    """The source_json argument now drives expansion (previously it was a
    dead parameter — expansion always read self.configdata). Setting
    configdata must have no effect; only the passed source_json counts."""
    jp_provider.configdata = {"users": [{"username": "IGNORED"}]}  # must not be read
    data = {"users": [{"username": "alice"}, {"username": "bob"}]}
    assert jp_provider.expand_json_path("/user/$jpath{$.users[*].username}", data) == {
        "alice": "/user/alice",
        "bob": "/user/bob",
    }
    # with no data passed, the expression matches nothing → EndpointError
    with pytest.raises(EndpointError):
        jp_provider.expand_json_path("/user/$jpath{$.users[*].username}", None)


def test_expand_json_path_multiple_placeholders_are_combined(jp_provider):
    """When a URL contains more than one $jpath{...} placeholder, every
    expression is expanded and the results combined (cartesian product),
    keyed by the matched values joined with '/'. Previously only the LAST
    expression was expanded and earlier placeholders were left unresolved."""
    data = {"users": ["u1", "u2"], "sites": ["s1", "s2"]}
    assert jp_provider.expand_json_path("/$jpath{$.users[*]}/$jpath{$.sites[*]}", data) == {
        "u1/s1": "/u1/s1",
        "u1/s2": "/u1/s2",
        "u2/s1": "/u2/s1",
        "u2/s2": "/u2/s2",
    }


def test_expand_json_path_no_match_raises_endpoint_error(jp_provider):
    """A JSON Path that matches nothing must be reported: the endpoint is
    skipped with an EndpointError (warning + STATUS_WARNING downstream)
    instead of silently disappearing from the document."""
    data = {"users": []}
    with pytest.raises(EndpointError) as excinfo:
        jp_provider.expand_json_path("/user/$jpath{$.users[*].username}", data)
    assert "matched no data" in excinfo.value.message
    assert excinfo.value.entity == "router"


def test_expand_json_path_invalid_expression_raises_endpoint_error(jp_provider):
    """An unparseable JSON Path is converted to an EndpointError carrying the
    offending expression and a syntax hint (the raw JSONPathError no longer
    escapes the provider)."""
    with pytest.raises(EndpointError) as excinfo:
        jp_provider.expand_json_path("/a/$jpath{$.oops[}/d", {})
    assert "Invalid JSONPath expression" in excinfo.value.message
    assert "$.oops[" in excinfo.value.message
    assert isinstance(excinfo.value.__cause__, JSONPathError)
    assert excinfo.value.entity == "router"


def test_expand_json_path_non_string_value_is_coerced(jp_provider):
    """A non-string JSON Path match value (e.g. an int) is coerced to str so
    it can be substituted into the URL. Previously str.replace raised
    TypeError here and the generic handler reported a misleading 'did not
    match' error; numeric IDs now expand into URLs correctly."""
    assert jp_provider.expand_json_path("/v/$jpath{$.n}", {"n": 5}) == {"5": "/v/5"}


def test_expand_json_path_url_encodes_values_keeps_raw_key(jp_provider):
    """Values substituted into the URL are percent-encoded (a site name with
    a space or '?' must not corrupt the request path), while the grouping key
    stays the raw value so templates and configdata keep the readable name."""
    data = {"sites": [{"name": " lab?1"}, {"name": "main"}]}
    assert jp_provider.expand_json_path("/s/$jpath{$.sites[*].name}/stat", data) == {
        " lab?1": "/s/%20lab%3F1/stat",
        "main": "/s/main/stat",
    }


def test_do_render_template_syntax_error_raises_render_error(rendition_settings, tmp_path, jjenv):
    """A template that fails to parse raises RenderError naming the template,
    the Jinja2 message and the line, instead of leaking the raw
    TemplateSyntaxError."""
    broken = tmp_path / "broken.md.jinja"
    broken.write_text("{% for x in %}\n{% endfor %}\n")
    # the Jinja2 environment is injected, so point its loader at the broken
    # template to exercise the real parse path
    from jinja2 import FileSystemLoader

    jjenv.loader = FileSystemLoader(str(tmp_path))
    prov = RendingProvider("ap-1", {"template": "broken.md.jinja"}, rendition_settings, jjenv)
    with pytest.raises(RenderError) as excinfo:
        prov.do_render_template()

    assert "has a syntax error" in excinfo.value.message
    assert "broken.md.jinja" in excinfo.value.message
    assert "line" in excinfo.value.message
