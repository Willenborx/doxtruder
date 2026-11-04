"""Tests for doxtruder.RESTProvider — the shared engine of the HTTP providers.

The provider subclasses GiteaProvider / ProxmoxProvider / OPNSenseProvider
were removed: their per-service authentication (Gitea ``Authorization:
token ...``, Proxmox ``PVEAPIToken=...``, OPNSense HTTP Basic, Unifi
``x-api-key``) is no longer coded into ``perform_request`` overrides. Instead
``RESTProvider.perform_request`` reads the per-entity YAML config — a
``headers`` dict and an ``auth.basic`` block — and is now concrete
(no longer abstract). So this module tests RESTProvider directly, covering
both the shared orchestration and the yaml-driven request construction that
used to live in the deleted subclasses.

All network I/O is faked with requests_mock / patched requests.get.
"""

import json
import logging

import pytest
import requests
from requests.auth import HTTPBasicAuth
from requests_mock import Mocker

from doxtruder.DoxtruderEngine import STATUS_OK, STATUS_WARNING
from doxtruder.Exceptions import EndpointError, EntityError
from doxtruder.RESTProvider import DEFAULT_TIMEOUT, RESTProvider


@pytest.fixture
def base_url():
    return "https://box.example.local/api"


@pytest.fixture
def entity_cfg(base_url):
    """A per-entity YAML block in the new shape: auth/header info that used to
    be coded into subclasses is now supplied in the config."""
    return {
        "provider": "RESTProvider",
        "baseURL": base_url,
        "headers": {
            "Authorization": "Bearer token-123",
            "accept": "application/json",
        },
        "auth": {"basic": {"user": "key-123", "pass": "secret-456"}},
        "endpoints": {"a": "/one", "b": "/two", "c": "/three"},
    }


@pytest.fixture
def dx_cfg():
    return {"title": "t"}


@pytest.fixture
def provider(entity_cfg, dx_cfg, jjenv):
    return RESTProvider("router", entity_cfg, dx_cfg, jjenv)


@pytest.fixture
def urls(base_url):
    return [base_url + p for p in ("/one", "/two", "/three")]


# --------------------------------------------------------------------------
# Abstraction + construction
# --------------------------------------------------------------------------


def test_rest_provider_is_now_concrete():
    """perform_request is no longer abstract: after the subclass removal,
    RESTProvider can be instantiated directly."""
    prov = RESTProvider("router", {"baseURL": "https://x.local"}, {}, None)
    assert prov.entityname == "router"


def test_init_stores_baseurl_entities_and_settings(provider, base_url, entity_cfg, dx_cfg):
    assert provider.entityname == "router"
    assert provider.baseURL == base_url
    assert provider.entity_cfg == entity_cfg
    assert provider.settings == dx_cfg
    assert provider.response is None
    assert provider.data == {}
    assert provider.auth is None
    assert provider.headers == {}


# --------------------------------------------------------------------------
# perform_request: yaml-driven headers and auth (moved out of the removed
# subclasses)
# --------------------------------------------------------------------------


def _capture_requests_get(monkeypatch):
    captured = {}

    class Response:
        status_code = 200
        reason = "OK"

        def json(self):
            return {"ok": True}

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured.update(kwargs)
        captured["url"] = url
        return Response()

    monkeypatch.setattr(requests, "request", fake_request)
    return captured


def test_perform_request_sends_configured_headers(monkeypatch, base_url, dx_cfg, jjenv):
    """The removed Gitea/Proxmox-style Authorization headers now come from the
    yaml `headers` dict, merged and sent on the request."""
    yml = {
        "baseURL": base_url,
        "headers": {
            "Authorization": "token tok-123",
            "accept": "application/json",
            "Content-Type": "application/json",
        },
        "endpoints": {"a": "/one"},
    }
    prov = RESTProvider("router", yml, dx_cfg, jjenv)
    captured = _capture_requests_get(monkeypatch)

    prov.perform_request(base_url + "/one", None)

    assert captured["url"] == base_url + "/one"
    assert captured["headers"] == {
        "Authorization": "token tok-123",
        "accept": "application/json",
        "Content-Type": "application/json",
    }
    # certificate verification is ON by default (only --ssl-cert-ignore turns it off)
    assert captured["verify"] is True
    assert captured["timeout"] == 10
    # no auth block in config -> no explicit auth sent
    assert captured["auth"] is None


def test_perform_request_sends_pve_api_token_header(monkeypatch, base_url, dx_cfg, jjenv):
    """Proxmox-style PVEAPIToken header, previously hardcoded in
    ProxmoxProvider.perform_request, is now supplied via yaml `headers`."""
    yml = {
        "baseURL": base_url,
        "headers": {"Authorization": "PVEAPIToken=key-123"},
        "endpoints": {"a": "/one"},
    }
    prov = RESTProvider("router", yml, dx_cfg, jjenv)
    captured = _capture_requests_get(monkeypatch)

    prov.perform_request(base_url + "/one", None)

    assert captured["headers"] == {"Authorization": "PVEAPIToken=key-123"}
    assert captured["auth"] is None


def test_perform_request_sends_basic_auth_from_config(monkeypatch, base_url, dx_cfg, jjenv):
    """OPNSense-style HTTP Basic auth, previously hardcoded in
    OPNSenseProvider.perform_request, is now built from the yaml
    `auth.basic` block as a real HTTPBasicAuth object."""
    yml = {
        "baseURL": base_url,
        "auth": {"basic": {"user": "api-key", "pass": "api-secret"}},
        "endpoints": {"a": "/one"},
    }
    prov = RESTProvider("router", yml, dx_cfg, jjenv)
    captured = _capture_requests_get(monkeypatch)

    prov.perform_request(base_url + "/one", None)

    assert isinstance(captured["auth"], HTTPBasicAuth)
    assert captured["auth"].username == "api-key"
    assert captured["auth"].password == "api-secret"
    assert captured["verify"] is True
    assert captured["timeout"] == 10
    # no headers block in config -> empty headers dict is sent
    assert captured["headers"] == {}


def test_perform_request_sends_combined_headers_and_basic_auth(provider, monkeypatch, base_url):
    """Both `headers` and `auth.basic` from the config are applied on the same
    request (the fixture config exercises the combined path)."""
    captured = _capture_requests_get(monkeypatch)

    provider.perform_request(base_url + "/one", None)

    assert captured["headers"] == {
        "Authorization": "Bearer token-123",
        "accept": "application/json",
    }
    assert isinstance(captured["auth"], HTTPBasicAuth)
    assert captured["auth"].username == "key-123"
    assert captured["auth"].password == "secret-456"


def test_perform_request_with_no_auth_config(monkeypatch, base_url, dx_cfg, jjenv):
    """With neither `headers` nor `auth` in the config, the request still goes
    out with empty headers and auth=None."""
    yml = {"baseURL": base_url, "endpoints": {"a": "/one"}}
    prov = RESTProvider("router", yml, dx_cfg, jjenv)
    captured = _capture_requests_get(monkeypatch)

    prov.perform_request(base_url + "/one", None)

    assert captured["headers"] == {}
    assert captured["auth"] is None


def test_perform_request_ignore_ssl_disables_verification(monkeypatch, base_url, jjenv):
    """With settings['ignore_ssl'] set (the --ssl-cert-ignore/-i flag),
    requests.get is called with verify=False so self-signed hosts work."""
    yml = {"baseURL": base_url, "endpoints": {"a": "/one"}}
    prov = RESTProvider("router", yml, {"title": "t", "ignore_ssl": True}, jjenv)
    captured = _capture_requests_get(monkeypatch)

    prov.perform_request(base_url + "/one", None)

    assert captured["verify"] is False


def test_perform_request_entity_sslcert_ignore_disables_verification(
    monkeypatch, base_url, dx_cfg, jjenv
):
    """'sslcert: ignore' in the entity block disables certificate
    verification for that entity even without the global -i flag."""
    yml = {"baseURL": base_url, "sslcert": "ignore", "endpoints": {"a": "/one"}}
    prov = RESTProvider("router", yml, dx_cfg, jjenv)
    captured = _capture_requests_get(monkeypatch)

    prov.perform_request(base_url + "/one", None)

    assert captured["verify"] is False


def test_perform_request_entity_sslcert_other_value_keeps_verification(
    monkeypatch, base_url, dx_cfg, jjenv
):
    """An sslcert value other than 'ignore' in the entity block leaves
    certificate verification on."""
    yml = {"baseURL": base_url, "sslcert": "validate", "endpoints": {"a": "/one"}}
    prov = RESTProvider("router", yml, dx_cfg, jjenv)
    captured = _capture_requests_get(monkeypatch)

    prov.perform_request(base_url + "/one", None)

    assert captured["verify"] is True


def test_perform_request_serializes_data_body_with_json_serializer(
    monkeypatch, base_url, dx_cfg, jjenv
):
    """A `data` dict on the request is JSON-encoded — datetime values via the
    new default_json_serializer — and sent as the request body (the
    data/body endpoint feature)."""
    from datetime import datetime, timezone

    yml = {"baseURL": base_url, "endpoints": {"a": "/one"}}
    prov = RESTProvider("router", yml, dx_cfg, jjenv)
    captured = _capture_requests_get(monkeypatch)

    prov.perform_request(
        base_url + "/one",
        {"ts": datetime(2026, 10, 1, tzinfo=timezone.utc), "n": 5},
    )

    import json

    assert json.loads(captured["data"]) == {
        "ts": "2026-10-01T00:00:00+00:00",
        "n": 5,
    }


# --------------------------------------------------------------------------
# call_api_endpoint
# --------------------------------------------------------------------------


def test_call_api_endpoint_calls_perform_request_and_returns_response(
    provider, base_url, monkeypatch
):
    captured = _capture_requests_get(monkeypatch)

    ret = provider.call_api_endpoint("/one", None)

    assert captured["url"] == base_url + "/one"
    assert captured["headers"]["Authorization"] == "Bearer token-123"
    assert isinstance(captured["auth"], HTTPBasicAuth)
    assert captured["verify"] is True
    assert captured["timeout"] == 10
    assert ret.status_code == 200
    # call_api_endpoint returns the response instead of stashing it; populating
    # self.response is left to retrieve_config.
    assert provider.response is None


def test_call_api_endpoint_ok_returns_response(provider, urls):
    with Mocker() as m:
        m.get(urls[0], status_code=200, json={"ok": True})
        result = provider.call_api_endpoint("/one", None)
    assert result is not None
    assert result.status_code == 200


def test_call_api_endpoint_http_error_raises_endpoint_error(provider, urls):
    """A non-200 response propagates as EndpointError (the per-endpoint
    category: retrieve_config warns and continues with the next endpoint)."""
    with Mocker() as m:
        m.get(urls[0], status_code=404)
        with pytest.raises(EndpointError) as excinfo:
            provider.call_api_endpoint("/one", None)
    assert "404" in str(excinfo.value.message)
    assert excinfo.value.entity == "router"


def test_call_api_endpoint_connect_timeout_raises_endpoint_error(provider, urls):
    """A connection-level failure (timeout or refused) surfaces as an
    EndpointError, which retrieve_config treats as a warning and continues
    past."""
    with Mocker() as m:
        m.get(urls[0], exc=requests.exceptions.ConnectTimeout)
        with pytest.raises(EndpointError) as excinfo:
            provider.call_api_endpoint("/one", None)
    assert "Connection problem" in str(excinfo.value.message)


def test_call_api_endpoint_connection_error_raises_endpoint_error(provider, urls):
    """requests.exceptions.ConnectionError (e.g. connection refused) is also
    mapped to EndpointError, not left to crash the run."""
    with Mocker() as m:
        m.get(urls[0], exc=requests.exceptions.ConnectionError("refused"))
        with pytest.raises(EndpointError) as excinfo:
            provider.call_api_endpoint("/one", None)
    assert "Connection problem" in str(excinfo.value.message)


def test_call_api_endpoint_ssl_error_gets_dedicated_message_and_hint(provider, urls):
    """An untrusted certificate must NOT be reported as a generic 'Connection
    problem': the message names the SSL issue and the hint points at the
    -i/--ssl-cert-ignore flag. It is an EntityError so the whole entity is
    skipped instead of retrying the remaining endpoints."""
    with Mocker() as m:
        m.get(
            urls[0],
            exc=requests.exceptions.SSLError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                "self signed certificate (_ssl.c:1000)"
            ),
        )
        with pytest.raises(EntityError) as excinfo:
            provider.call_api_endpoint("/one", None)
    msg = excinfo.value.message
    assert "SSL certificate" in msg
    assert "not trusted" in msg
    assert "Connection problem" not in msg
    assert "-i" in excinfo.value.hint
    assert "--ssl-cert-ignore" in excinfo.value.hint
    assert isinstance(excinfo.value.__cause__, requests.exceptions.SSLError)


def test_retrieve_config_ssl_error_aborts_entity_without_further_calls(provider, urls):
    """When the first API call fails with an SSL certificate error, the
    entity is skipped via EntityError and no further endpoints are called."""
    with Mocker() as m:
        m.get(
            urls[0],
            exc=requests.exceptions.SSLError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
                "self signed certificate (_ssl.c:1000)"
            ),
        )
        for url in urls[1:]:
            m.get(url, status_code=200, json={"ok": True})
        with pytest.raises(EntityError) as excinfo:
            provider.retrieve_config()
    assert "SSL certificate" in excinfo.value.message
    # only the failing first endpoint was called, the rest were skipped
    assert m.call_count == 1


def test_call_api_endpoint_raw_sslcert_verification_error_also_detected(provider, urls):
    """urllib3/requests sometimes surface the raw ssl.SSLCertVerificationError
    (an OSError subclass); it must take the same SSL branch."""
    import ssl

    with Mocker() as m:
        m.get(
            urls[0],
            exc=ssl.SSLCertVerificationError("certificate verify failed: self signed certificate"),
        )
        with pytest.raises(EntityError) as excinfo:
            provider.call_api_endpoint("/one", None)
    assert "SSL certificate" in excinfo.value.message
    assert "-i" in excinfo.value.hint


def test_call_api_endpoint_missing_auth_pass_raises_entity_error(base_url, dx_cfg, jjenv):
    """An `auth.basic` block missing `pass` is a broken entity config: the new
    _build_auth() validation raises EntityError (skip the entity) instead of a
    raw KeyError being downgraded to an EndpointError per endpoint."""
    bad = {
        "baseURL": base_url,
        "auth": {"basic": {"user": "key-123"}},  # no 'pass'
        "endpoints": {"a": "/one"},
    }
    provider = RESTProvider("router", bad, dx_cfg, jjenv)
    with pytest.raises(EntityError) as excinfo:
        provider.call_api_endpoint("/one", None)
    assert "'user:' and 'pass:'" in excinfo.value.message


def test_perform_request_unknown_auth_method_raises_entity_error(base_url, dx_cfg, jjenv):
    """An `auth:` block without a 'basic:' key (e.g. a typo like 'bearer:') is
    reported as an EntityError naming the found keys instead of being silently
    ignored, which would otherwise surface as a confusing 401."""
    bad = {
        "baseURL": base_url,
        "auth": {"bearer": {"token": "abc"}},
        "endpoints": {"a": "/one"},
    }
    provider = RESTProvider("router", bad, dx_cfg, jjenv)
    with pytest.raises(EntityError) as excinfo:
        provider.call_api_endpoint("/one", None)
    assert "no 'basic:' entry" in excinfo.value.message
    assert "bearer" in excinfo.value.message


def test_perform_request_empty_auth_block_raises_entity_error(base_url, dx_cfg, jjenv):
    """`auth:` present but empty/not a mapping is an EntityError; removing the
    entry entirely remains the way to send no auth."""
    for bad_auth in ({}, "basic", None):
        cfg = {
            "baseURL": base_url,
            "endpoints": {"a": "/one"},
        }
        if bad_auth is not None:
            cfg["auth"] = bad_auth
        provider = RESTProvider("router", cfg, dx_cfg, jjenv)
        if bad_auth is None:
            # no auth block at all -> no error, auth stays None
            assert provider._build_auth() is None
        else:
            with pytest.raises(EntityError):
                provider._build_auth()


# --------------------------------------------------------------------------
# configurable request timeout (settings: timeout:)
# --------------------------------------------------------------------------


def test_perform_request_uses_default_timeout(provider, monkeypatch, base_url):
    """Without a 'timeout:' setting, requests use DEFAULT_TIMEOUT (10 s)."""
    captured = _capture_requests_get(monkeypatch)
    provider.perform_request(base_url + "/one", None)
    assert captured["timeout"] == DEFAULT_TIMEOUT


def test_perform_request_uses_timeout_from_settings(monkeypatch, base_url, jjenv):
    """'timeout: 30' in the global settings section is used for every request."""
    dx_cfg = {"outputpath": "/tmp", "timeout": 30}
    provider = RESTProvider("router", {"baseURL": base_url}, dx_cfg, jjenv)
    captured = _capture_requests_get(monkeypatch)
    provider.perform_request(base_url + "/one", None)
    assert captured["timeout"] == 30


def test_perform_request_timeout_accepts_string(monkeypatch, base_url, jjenv):
    """YAML/ENV values arrive as strings; a numeric string is accepted."""
    dx_cfg = {"outputpath": "/tmp", "timeout": "2.5"}
    provider = RESTProvider("router", {"baseURL": base_url}, dx_cfg, jjenv)
    captured = _capture_requests_get(monkeypatch)
    provider.perform_request(base_url + "/one", None)
    assert captured["timeout"] == 2.5


def test_perform_request_invalid_timeout_falls_back_with_warning(
    monkeypatch, base_url, jjenv, caplog
):
    """A non-numeric or non-positive 'timeout:' is ignored with a warning and
    the DEFAULT_TIMEOUT is used instead."""
    dx_cfg = {"outputpath": "/tmp", "timeout": "soon"}
    provider = RESTProvider("router", {"baseURL": base_url}, dx_cfg, jjenv)
    captured = _capture_requests_get(monkeypatch)
    with caplog.at_level("WARNING"):
        provider.perform_request(base_url + "/one", None)
    assert captured["timeout"] == DEFAULT_TIMEOUT
    assert "timeout" in caplog.text

    dx_cfg["timeout"] = 0
    captured.clear()
    provider.perform_request(base_url + "/one", None)
    assert captured["timeout"] == DEFAULT_TIMEOUT


# --------------------------------------------------------------------------
# subst_vars / self.data + endpoint template substitution
# --------------------------------------------------------------------------


def test_subst_vars_replaces_placeholders_from_data(provider):
    """{name} placeholders are replaced from the collected self.data dict."""
    provider.data["site"] = "site-42"
    assert provider.subst_vars("/foo/{site}/bar") == "/foo/site-42/bar"


def test_subst_vars_returns_template_unchanged_without_placeholders(provider):
    assert provider.subst_vars("/one") == "/one"


def test_subst_vars_missing_key_raises_endpoint_error(provider):
    """An unknown placeholder raises EndpointError with a fix hint (the raw
    KeyError that used to escape is now converted per endpoint)."""
    with pytest.raises(EndpointError) as excinfo:
        provider.subst_vars("/{unknown}")
    assert "cannot be resolved" in excinfo.value.message


def test_retrieve_config_substitutes_variables_in_endpoint_urls(base_url, dx_cfg, jjenv):
    """Endpoints containing {var} placeholders are resolved via self.data
    before the request is issued."""
    yml = {
        "baseURL": base_url,
        "endpoints": {"site": "/site/{site}/devices"},
    }
    prov = RESTProvider("router", yml, dx_cfg, jjenv)
    prov.data["site"] = "site-42"

    with Mocker() as m:
        m.get(base_url + "/site/site-42/devices", status_code=200, json={"ok": True})
        status = prov.retrieve_config()

    assert status == STATUS_OK
    assert m.call_count == 1


def test_subst_vars_resolves_from_configdata(provider):
    """{var} placeholders now resolve against the collected configdata (the
    real data source), not just the never-populated self.data. A key at the
    top level of an earlier endpoint's payload is substituted."""
    provider.configdata["sites"] = {"name": "site-42"}
    assert provider.subst_vars("/foo/{name}/bar") == "/foo/site-42/bar"


def test_subst_vars_resolves_from_configdata_data_wrapper(provider):
    """A key nested one level under a 'data' wrapper (common API shape) is
    also found by the placeholder lookup."""
    provider.configdata["sites"] = {"data": {"name": "site-99"}}
    assert provider.subst_vars("/foo/{name}/bar") == "/foo/site-99/bar"


def test_subst_vars_data_overrides_configdata(provider):
    """self.data still takes precedence over configdata (explicit override)."""
    provider.configdata["sites"] = {"name": "from-configdata"}
    provider.data["name"] = "from-data"
    assert provider.subst_vars("/foo/{name}/bar") == "/foo/from-data/bar"


def test_subst_vars_url_encodes_values(provider):
    """Placeholder values are percent-encoded so a space or a reserved URL
    character in API data cannot corrupt the request URL (a raw '?' would
    silently turn the rest of the path into a query string)."""
    provider.data["site"] = "my site?x=1"
    assert provider.subst_vars("/foo/{site}/bar") == "/foo/my%20site%3Fx%3D1/bar"


def test_lookup_placeholder_logs_winning_endpoint(provider, caplog):
    """When two endpoints provide the same key, the first match wins; the
    resolution logs a debug line naming the endpoint it came from so the
    choice is traceable."""
    provider.configdata["alpha"] = {"name": "from-alpha"}
    provider.configdata["beta"] = {"name": "from-beta"}
    with caplog.at_level(logging.DEBUG):
        assert provider.subst_vars("/x/{name}") == "/x/from-alpha"
    assert "resolved from endpoint 'alpha'" in caplog.text


def test_retrieve_config_non_dict_endpoints_raises_entity_error(base_url, dx_cfg, jjenv):
    """An 'endpoints:' entry that is present but not a mapping (e.g. a YAML
    list, or null) is reported as a clean EntityError with a hint instead of
    dying later with a raw TypeError caught by the engine as an 'unexpected
    error'."""
    for broken in (["/one", "/two"], None, "text"):
        yml = {"baseURL": base_url, "endpoints": broken}
        prov = RESTProvider("router", yml, dx_cfg, jjenv)
        with pytest.raises(EntityError) as excinfo:
            prov.retrieve_config()
        assert "not a mapping" in excinfo.value.message
        assert "endpoints:" in excinfo.value.hint


# --------------------------------------------------------------------------
# retrieve_config
# --------------------------------------------------------------------------


def test_retrieve_config_expands_json_path_into_multiple_calls(base_url, dx_cfg, jjenv):
    """An endpoint URL containing $jpath{...} is expanded against the
    data already collected (configdata) into one HTTP call per match.
    Uses string values, which is the working case. The list endpoint's JSON
    is stored as configdata['users'] (a dict {"users": [...]}), so the
    expression is written against that nesting: $.users.users[*].username.
    Because the match value is the owner key, the per-owner responses are
    grouped: configdata['user'] = {owner: <response JSON>} for every owner."""
    yml = {
        "baseURL": base_url,
        "endpoints": {
            "users": "/users",
            "user": "/users/$jpath{$.users.users[*].username}/details",
        },
    }
    prov = RESTProvider("router", yml, dx_cfg, jjenv)

    with Mocker() as m:
        m.get(
            base_url + "/users",
            status_code=200,
            json={"users": [{"username": "alice"}, {"username": "bob"}]},
        )
        m.get(base_url + "/users/alice/details", status_code=200, json={"ok": True})
        m.get(base_url + "/users/bob/details", status_code=200, json={"ok": True})
        status = prov.retrieve_config()

    # list endpoint + one call per JSON Path match
    assert m.call_count == 3
    assert status == STATUS_OK
    assert prov.configdata["user"] == {
        "alice": {"ok": True},
        "bob": {"ok": True},
    }


def test_retrieve_config_numeric_json_path_value_expands_and_continues(base_url, dx_cfg, jjenv):
    """A JSON Path that matches non-string values (e.g. numeric IDs) is coerced
    to str during expansion (see expand_json_path), so the endpoint expands
    into one call per ID and every response is stored grouped under the
    numeric key. (This used to be a BUG PIN for the old str.replace TypeError;
    the str() coercion fix made the pin obsolete.)"""
    yml = {
        "baseURL": base_url,
        "endpoints": {
            "users": "/users",
            "user": "/users/$jpath{$.users.users[*].id}/details",
        },
    }
    prov = RESTProvider("router", yml, dx_cfg, jjenv)

    with Mocker() as m:
        m.get(base_url + "/users", status_code=200, json={"users": [{"id": 1}, {"id": 2}]})
        m.get(base_url + "/users/1/details", status_code=200, json={"ok": 1})
        m.get(base_url + "/users/2/details", status_code=200, json={"ok": 2})
        status = prov.retrieve_config()

    assert status == STATUS_OK
    assert m.call_count == 3
    assert prov.configdata["user"] == {"1": {"ok": 1}, "2": {"ok": 2}}


def test_retrieve_config_ok_when_all_endpoints_succeed(provider, urls):
    with Mocker() as m:
        for url in urls:
            m.get(url, status_code=200, json={"ok": True})
        status = provider.retrieve_config()
    assert status == STATUS_OK


def test_retrieve_config_dict_endpoint_sends_body(base_url, dx_cfg, jjenv, monkeypatch):
    """An endpoint configured as a dict with a `body` passes that body through
    retrieve_config -> call_api_endpoint -> perform_request, covering the new
    data/body endpoint shape (RESTProvider body branch)."""
    yml = {
        "baseURL": base_url,
        "headers": {},
        "endpoints": {"a": {"url": "/one", "body": {"k": "v"}}},
    }
    prov = RESTProvider("router", yml, dx_cfg, jjenv)
    body_seen = {}

    class Response:
        status_code = 200
        reason = "OK"

        def json(self):
            return {"ok": True}

    def fake_request(method, url, **kwargs):
        body_seen["method"] = method
        body_seen.update(kwargs)
        return Response()

    monkeypatch.setattr(requests, "request", fake_request)

    status = prov.retrieve_config()

    assert status == STATUS_OK
    assert json.loads(body_seen["data"]) == {"k": "v"}
    assert prov.configdata["a"] == {"ok": True}  # Response.json on the fake


def test_retrieve_config_partial_failure_yields_warning_and_continues(provider, urls):
    """A non-200 endpoint surfaces as EndpointError; retrieve_config warns,
    keeps visiting the remaining endpoints and reports STATUS_WARNING."""
    with Mocker() as m:
        m.get(urls[0], status_code=500)
        for url in urls[1:]:
            m.get(url, status_code=200, json={"ok": True})
        status = provider.retrieve_config()
    assert m.call_count == len(urls)
    assert status == STATUS_WARNING


def test_retrieve_config_endpoint_method_post_is_dispatched(base_url, dx_cfg, jjenv, monkeypatch):
    """An endpoint dict with 'method: post' is sent as a POST (through
    requests.request) instead of a GET, so a JSON 'body:' reaches the server
    the way POST-only analytics/usage endpoints expect."""
    yml = {
        "baseURL": base_url,
        "endpoints": {"usage": {"url": "/usage", "method": "post", "body": {"k": "v"}}},
    }
    prov = RESTProvider("router", yml, dx_cfg, jjenv)
    seen = {}

    class Response:
        status_code = 200
        reason = "OK"

        def json(self):
            return {"ok": True}

    def fake_request(method, url, **kwargs):
        seen["method"] = method
        seen["url"] = url
        return Response()

    monkeypatch.setattr(requests, "request", fake_request)
    status = prov.retrieve_config()
    assert status == STATUS_OK
    assert seen["method"] == "POST"
    assert seen["url"] == base_url + "/usage"


def test_retrieve_config_default_method_is_get(base_url, dx_cfg, jjenv, monkeypatch):
    """Without a 'method:' entry the endpoint is still a GET (backward
    compatible with the old always-GET behaviour)."""
    yml = {"baseURL": base_url, "endpoints": {"a": "/one"}}
    prov = RESTProvider("router", yml, dx_cfg, jjenv)
    seen = {}

    def fake_request(method, url, **kwargs):
        seen["method"] = method
        return type("Response", (), {"status_code": 200, "reason": "OK", "json": lambda self: {}})()

    monkeypatch.setattr(requests, "request", fake_request)
    prov.retrieve_config()
    assert seen["method"] == "GET"


def test_retrieve_config_invalid_method_warns_and_skips_endpoint(
    base_url, dx_cfg, jjenv, monkeypatch
):
    """An unsupported 'method:' (e.g. a typo 'GTE') is reported per endpoint
    (EndpointError -> warning) instead of being sent to the server; the
    remaining endpoints still run."""
    yml = {
        "baseURL": base_url,
        "endpoints": {"bad": {"url": "/one", "method": "GTE"}, "good": "/two"},
    }
    prov = RESTProvider("router", yml, dx_cfg, jjenv)
    called = []

    def fake_request(method, url, **kwargs):
        called.append(url)
        return type("Response", (), {"status_code": 200, "reason": "OK", "json": lambda self: {}})()

    monkeypatch.setattr(requests, "request", fake_request)
    status = prov.retrieve_config()
    assert status == STATUS_WARNING
    assert "bad" not in prov.configdata
    assert base_url + "/two" in called  # the good endpoint still ran


def test_perform_request_builds_auth_and_headers_once(provider, monkeypatch, base_url):
    """The request context (headers merge + auth build) is prepared once for
    the entity, not rebuilt on every endpoint call."""
    captured = _capture_requests_get(monkeypatch)
    build_calls = []
    real_build = provider._build_auth

    def counting_build():
        build_calls.append(1)
        return real_build()

    provider._build_auth = counting_build
    provider.perform_request(base_url + "/one", None)
    provider.perform_request(base_url + "/two", None)
    assert len(build_calls) == 1
    assert captured["url"] == base_url + "/two"


def test_retrieve_config_connection_error_yields_warning_and_continues(provider, urls):
    """A connection failure (EndpointError) on one endpoint is a warning, and
    the remaining endpoints are still visited."""
    with Mocker() as m:
        m.get(urls[0], exc=requests.exceptions.ConnectTimeout)
        for url in urls[1:]:
            m.get(url, status_code=200, json={"ok": True})
        status = provider.retrieve_config()
    assert m.call_count == len(urls)
    assert status == STATUS_WARNING


def test_retrieve_config_missing_endpoints_raises_entity_error(base_url, dx_cfg, jjenv):
    yml = {"baseURL": base_url}
    provider = RESTProvider("router", yml, dx_cfg, jjenv)
    with pytest.raises(EntityError) as excinfo:
        provider.retrieve_config()
    assert "has no 'endpoints:' entry" in str(excinfo.value.message)


def test_init_missing_baseurl_raises_entity_error(dx_cfg, jjenv):
    """A config without 'baseURL' fails at construction time with EntityError
    (fatal for the entity, caught by the engine)."""
    with pytest.raises(EntityError) as excinfo:
        RESTProvider("router", {"endpoints": {"a": "/one"}}, dx_cfg, jjenv)
    assert "has no 'baseURL:' entry" in str(excinfo.value.message)
