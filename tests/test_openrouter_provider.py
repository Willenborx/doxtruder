"""Tests for doxtruder.OpenrouterProvider.

OpenrouterProvider overrides `perform_request` with its own Bearer-token auth
(the one surviving subclass that still keeps auth in code rather than yaml)
and adds an `aggregate_usage()` step on top of the shared RESTProvider
orchestration. `retrieve_config()` first runs the normal GET endpoints, then
POSTs an analytics query for the last 7 days and stores the per-day rows and
week totals under ``configdata['usage']``. A failure of the analytics step
(KeyError/TypeError/ValueError) is downgraded to a warning, keeping the rest
of the document intact.

All network I/O is faked (requests_mock / patched requests.get).
"""

import pytest
import requests
from requests_mock import Mocker

from doxtruder.DoxtruderEngine import STATUS_OK, STATUS_WARNING
from doxtruder.Exceptions import EndpointError, EntityError
from doxtruder.OpenrouterProvider import OpenrouterProvider


@pytest.fixture
def base_url():
    return "https://openrouter.example.local"


@pytest.fixture
def or_yml(base_url):
    return {
        "provider": "OpenrouterProvider",
        "baseURL": base_url,
        "APIKey": "or-key-123",
        "headers": {
            "Authorization": "Bearer or-key-123",
            "accept": "application/json",
            "Content-Type": "application/json",
        },
        "endpoints": {
            "credits": "/api/v1/credits",
            "keys": "/api/v1/keys",
        },
    }


@pytest.fixture
def settings():
    return {"title": "test-docs"}


@pytest.fixture
def or_provider(or_yml, settings, jjenv):
    return OpenrouterProvider("openrouter", or_yml, settings, jjenv)


@pytest.fixture
def or_urls(base_url, or_yml):
    return [base_url + p for p in or_yml["endpoints"].values()]


@pytest.fixture
def analytics_url(base_url):
    return base_url + "/analytics/query"


def _analytics_body(rows):
    """The analytics response nests its rows under data.data."""
    return {"data": {"data": rows}}


def _row(day, requests_=10, tokens=100, prompt=60, completion=40, usage="1.25"):
    return {
        "date__day": day,
        "request_count": requests_,
        "tokens_total": tokens,
        "tokens_prompt": prompt,
        "tokens_completion": completion,
        "total_usage": usage,
    }


# --------------------------------------------------------------------------
# Construction / init_entity
# --------------------------------------------------------------------------


def test_init_parses_base_url(or_provider, base_url):
    assert or_provider.baseURL == base_url
    assert or_provider.entityname == "openrouter"


def test_init_entity_is_a_noop(or_provider):
    """Unlike UnifiProvider, OpenRouter needs no site resolution;
    init_entity must not touch the network."""
    assert or_provider.init_entity() is None
    assert or_provider.data == {}


# --------------------------------------------------------------------------
# perform_request
# --------------------------------------------------------------------------


def test_perform_request_sends_bearer_header(or_provider, base_url, monkeypatch):
    """OpenRouter authenticates via 'Authorization: Bearer <APIKey>' plus
    JSON accept/content-type headers, certificate verification on and
    timeout 10."""
    captured = {}

    class Response:
        status_code = 200
        reason = "OK"

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        captured["url"] = url
        return Response()

    monkeypatch.setattr(requests, "get", fake_get)
    prov = or_provider
    prov.perform_request(base_url + "/api/v1/credits", None)

    assert captured["url"] == base_url + "/api/v1/credits"
    assert captured["headers"]["Authorization"] == "Bearer or-key-123"
    assert captured["headers"]["accept"] == "application/json"
    assert captured["headers"]["Content-Type"] == "application/json"
    assert captured["verify"] is True
    assert captured["timeout"] == 10


def test_perform_request_uses_timeout_from_settings(base_url, or_yml, jjenv, monkeypatch):
    """A global 'timeout:' setting is honoured by the OpenRouter GETs too
    (previously the 10 s value was hardcoded in perform_request)."""
    prov = OpenrouterProvider("openrouter", or_yml, {"timeout": 45}, jjenv)
    captured = {}

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        return type("Response", (), {"status_code": 200, "reason": "OK"})()

    monkeypatch.setattr(requests, "get", fake_get)
    prov.perform_request(base_url + "/api/v1/credits", None)
    assert captured["timeout"] == 45


def test_analytics_timeout_defaults_and_override(base_url, or_yml, jjenv):
    """The analytics POST uses the slower ANALYTICS_TIMEOUT (30 s) default,
    but an explicit global 'timeout:' setting wins over it."""
    from doxtruder.OpenrouterProvider import ANALYTICS_TIMEOUT

    prov = OpenrouterProvider("openrouter", or_yml, {}, jjenv)
    assert prov._analytics_timeout() == ANALYTICS_TIMEOUT

    prov = OpenrouterProvider("openrouter", or_yml, {"timeout": 60}, jjenv)
    assert prov._analytics_timeout() == 60


@pytest.mark.parametrize("bad", ["soon", 0, -5, [1]])
def test_analytics_timeout_invalid_keeps_analytics_default(base_url, or_yml, jjenv, bad):
    """An invalid or non-positive global 'timeout:' must NOT downgrade the
    analytics query to the 10 s GET default — the aggregate POST keeps its
    slower ANALYTICS_TIMEOUT (the previous bug: any 'timeout:' key present
    routed through _request_timeout(), whose fallback is 10 s)."""
    from doxtruder.OpenrouterProvider import ANALYTICS_TIMEOUT

    prov = OpenrouterProvider("openrouter", or_yml, {"timeout": bad}, jjenv)
    assert prov._analytics_timeout() == ANALYTICS_TIMEOUT
    # plain GETs still fall back to the normal 10 s default
    assert prov._request_timeout() == 10


def test_perform_request_ignore_ssl_disables_verification(base_url, or_yml, jjenv, monkeypatch):
    """With settings['ignore_ssl'] (the --ssl-cert-ignore/-i flag), the
    OpenRouter GET is sent with verify=False."""
    prov = OpenrouterProvider("openrouter", or_yml, {"ignore_ssl": True}, jjenv)
    captured = {}

    class Response:
        status_code = 200
        reason = "OK"

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(requests, "get", fake_get)
    prov.perform_request(base_url + "/api/v1/credits", None)

    assert captured["verify"] is False


def test_perform_request_entity_sslcert_ignore_disables_verification(
    base_url, or_yml, jjenv, monkeypatch
):
    """'sslcert: ignore' in the entity block disables certificate
    verification for this OpenRouter entity even without the -i flag."""
    or_yml["sslcert"] = "ignore"
    prov = OpenrouterProvider("openrouter", or_yml, {}, jjenv)
    captured = {}

    class Response:
        status_code = 200
        reason = "OK"

    def fake_get(url, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(requests, "get", fake_get)
    prov.perform_request(base_url + "/api/v1/credits", None)

    assert captured["verify"] is False


# --------------------------------------------------------------------------
# retrieve_config: GET endpoints via the shared RESTProvider orchestration
# --------------------------------------------------------------------------


def test_retrieve_config_ok_when_all_endpoints_succeed(or_provider, or_urls, analytics_url):
    with Mocker() as m:
        for url in or_urls:
            m.get(url, status_code=200, json={"data": {}})
        m.post(analytics_url, status_code=200, json=_analytics_body([]))
        status = or_provider.retrieve_config()

    assert status == STATUS_OK
    assert set(or_provider.configdata) == {"credits", "keys", "usage"}


def test_retrieve_config_partial_failure_yields_warning_and_continues(
    or_provider, or_urls, analytics_url
):
    """A failing GET endpoint is skipped with a warning (the non-200
    EndpointError is caught per endpoint); the analytics POST still runs and
    the document survives."""
    with Mocker() as m:
        m.get(or_urls[0], status_code=403)
        m.get(or_urls[1], status_code=200, json={"data": []})
        m.post(analytics_url, status_code=200, json=_analytics_body([_row("2026-10-01")]))
        status = or_provider.retrieve_config()

    assert status == STATUS_WARNING
    assert "credits" not in or_provider.configdata
    assert "keys" in or_provider.configdata
    assert or_provider.configdata["usage"]["week_totals"]["days"] == 1


def test_retrieve_config_missing_endpoints_raises_entity_error(base_url, settings, jjenv):
    yml = {"baseURL": base_url, "APIKey": "or-key-123"}
    provider = OpenrouterProvider("openrouter", yml, settings, jjenv)
    with pytest.raises(EntityError) as excinfo:
        provider.retrieve_config()
    assert "has no 'endpoints:' entry" in str(excinfo.value.message)


def test_call_api_endpoint_http_error_raises_endpoint_error(or_provider, or_urls):
    """A non-200 response propagates as EndpointError (the per-endpoint
    category, caught by retrieve_config's endpoint loop)."""
    with Mocker() as m:
        m.get(or_urls[0], status_code=500)
        with pytest.raises(EndpointError) as excinfo:
            or_provider.call_api_endpoint("/api/v1/credits", None)
    assert "500" in str(excinfo.value.message)


# --------------------------------------------------------------------------
# aggregate_usage: the analytics POST
# --------------------------------------------------------------------------


def test_aggregate_usage_posts_expected_query(or_provider, analytics_url):
    """The POST carries the Bearer auth header, the five usage metrics,
    day granularity and a 7-day UTC window ending tomorrow."""
    with Mocker() as m:
        m.post(analytics_url, status_code=200, json=_analytics_body([]))
        or_provider.aggregate_usage()

    req = m.request_history[0]
    assert req.headers["Authorization"] == "Bearer or-key-123"
    assert req.headers["Content-Type"] == "application/json"
    body = req.json()
    assert body["granularity"] == "day"
    assert body["limit"] == 10
    assert set(body["metrics"]) == {
        "total_usage",
        "tokens_total",
        "tokens_prompt",
        "tokens_completion",
        "request_count",
    }
    start = body["time_range"]["start"]
    end = body["time_range"]["end"]
    assert start.endswith("Z") and end.endswith("Z")
    # window spans exactly 7 calendar days (start 00:00 six days back,
    # end 00:00 tomorrow)
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    expected_start = (now - timedelta(days=6)).strftime("%Y-%m-%dT00:00:00")
    expected_end = (now + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00")
    assert start.startswith(expected_start)
    assert end.startswith(expected_end)


def test_aggregate_usage_builds_daily_rows_and_week_totals(or_provider, analytics_url):
    """Rows are keyed by date__day, sorted chronologically, with numeric
    coercion of the string amounts and summed week_totals."""
    rows = [
        _row("2026-10-03", requests_=5, tokens=50, prompt=30, completion=20, usage="0.5"),
        _row("2026-10-01", requests_=10, tokens=100, prompt=60, completion=40, usage="1.25"),
        _row("2026-10-02", requests_=1, tokens=2, prompt=1, completion=1, usage="2.0"),
    ]
    with Mocker() as m:
        m.post(analytics_url, status_code=200, json=_analytics_body(rows))
        or_provider.aggregate_usage()

    usage = or_provider.configdata["usage"]
    assert [d["date"] for d in usage["daily"]] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert usage["daily"][0]["requests"] == 10
    assert usage["daily"][0]["cost"] == pytest.approx(1.25)
    totals = usage["week_totals"]
    assert totals["days"] == 3
    assert totals["requests"] == 16
    assert totals["tokens"] == 152
    assert totals["prompt_tokens"] == 91
    assert totals["completion_tokens"] == 61
    assert totals["cost"] == pytest.approx(3.75)


def test_aggregate_usage_tolerates_missing_or_bad_int_values(or_provider, analytics_url):
    """The int-backed fields (requests/tokens) degrade to 0 when absent or
    non-numeric, via the as_int() guard. cost is likewise guarded (as_float),
    so a numeric value here is simply parsed normally."""
    rows = [
        {"date__day": "2026-10-01"},  # all fields missing
        _row("2026-10-02", requests_="not-a-number", tokens=None, usage="3.5"),
    ]
    with Mocker() as m:
        m.post(analytics_url, status_code=200, json=_analytics_body(rows))
        or_provider.aggregate_usage()

    daily = or_provider.configdata["usage"]["daily"]
    assert daily[0]["requests"] == 0
    assert daily[0]["cost"] == 0.0
    assert daily[1]["requests"] == 0
    assert daily[1]["tokens"] == 0
    assert daily[1]["cost"] == pytest.approx(3.5)


def test_aggregate_usage_non_numeric_cost_degrades_to_zero(or_provider, analytics_url):
    """A non-numeric total_usage no longer aborts the whole 7-day aggregation:
    the as_float() guard (mirroring as_int) degrades the single bad row's cost
    to 0.0 so the remaining days survive."""
    with Mocker() as m:
        m.post(
            analytics_url, status_code=200, json=_analytics_body([_row("2026-10-01", usage="junk")])
        )
        or_provider.aggregate_usage()

    daily = or_provider.configdata["usage"]["daily"]
    assert daily[0]["cost"] == 0.0


def test_aggregate_usage_handles_empty_payload(or_provider, analytics_url):
    """An empty (or null) data section yields an empty daily list and
    zeroed week totals."""
    with Mocker() as m:
        m.post(analytics_url, status_code=200, json={"data": {"data": None}})
        or_provider.aggregate_usage()

    usage = or_provider.configdata["usage"]
    assert usage["daily"] == []
    assert usage["week_totals"]["days"] == 0
    assert usage["week_totals"]["cost"] == 0


# --------------------------------------------------------------------------
# retrieve_config error downgrade around aggregate_usage
# --------------------------------------------------------------------------


def test_retrieve_config_aggregation_error_downgrades_to_warning(
    or_provider, or_urls, analytics_url, caplog
):
    """A failing analytics step (here: the analytics POST returns 500, so
    raise_for_status() raises HTTPError) is caught by retrieve_config, logged
    and downgraded to STATUS_WARNING — the already retrieved endpoint data
    survives and 'usage' is not set."""
    with Mocker() as m:
        for url in or_urls:
            m.get(url, status_code=200, json={"data": {}})
        m.post(analytics_url, status_code=500, json={"error": "boom"})
        with caplog.at_level("WARNING"):
            status = or_provider.retrieve_config()

    assert status == STATUS_WARNING
    assert "Cannot retrieve the OpenRouter usage analytics" in caplog.text
    assert "usage" not in or_provider.configdata


def test_retrieve_config_missing_auth_headers_block_warns_and_continues(base_url, settings, jjenv):
    """Without a `headers` block (the config-driven auth replacement for the
    old APIKey field), OpenrouterProvider.perform_request raises KeyError,
    which call_api_endpoint maps to EntityError; retrieve_config now catches
    that EntityError and warns-and-continues per endpoint (instead of the old
    fatal 'could not be processed'), and the analytics POST's missing APIKey
    is also downgraded to a warning. The run therefore completes with
    STATUS_WARNING, not a raised error."""
    yml = {"baseURL": base_url, "endpoints": {"credits": "/api/v1/credits"}}
    provider = OpenrouterProvider("openrouter", yml, settings, jjenv)
    with Mocker() as m:
        m.get(base_url + "/api/v1/credits", status_code=200, json={})
        m.post(base_url + "/analytics/query", status_code=200, json={"data": {"data": []}})
        status = provider.retrieve_config()
    assert status == STATUS_WARNING
    assert "credits" not in provider.configdata


def test_retrieve_config_analytics_http_error_downgrades_to_warning(
    or_provider, or_urls, analytics_url, caplog
):
    """A failing analytics POST (raise_for_status -> requests.HTTPError) is
    caught by retrieve_config's RequestException handler and downgraded to a
    warning — the entity keeps the data retrieved from the GET endpoints
    instead of aborting the whole run."""
    with Mocker() as m:
        for url in or_urls:
            m.get(url, status_code=200, json={"data": {}})
        m.post(analytics_url, status_code=500)
        with caplog.at_level("WARNING"):
            status = or_provider.retrieve_config()

    assert status == STATUS_WARNING
    assert "usage" not in or_provider.configdata
    assert "keys" in or_provider.configdata


# --------------------------------------------------------------------------
# End-to-end render with realistic mock data (shaped after the real
# OpenRouter.json account dump that ships under output/<ts>/)
# --------------------------------------------------------------------------

# Daily usage as returned by the real analytics POST (row shape), reduced to
# the 5 active days. week_totals derived from these sum to the same values
# as the real dump (cost 1.3511).
MOCK_DAILY_ROWS = [
    _row(
        "2026-10-08", requests_=5, tokens=134854, prompt=129306, completion=5548, usage="0.008156"
    ),
    _row(
        "2026-10-10",
        requests_=288,
        tokens=2428980,
        prompt=2329427,
        completion=99553,
        usage="0.440487",
    ),
    _row(
        "2026-10-12",
        requests_=229,
        tokens=12022748,
        prompt=11871570,
        completion=151178,
        usage="0.852562",
    ),
    _row(
        "2026-10-13",
        requests_=49,
        tokens=1304219,
        prompt=1274373,
        completion=29846,
        usage="0.041688",
    ),
    _row(
        "2026-10-14", requests_=6, tokens=141479, prompt=139461, completion=2018, usage="0.008176"
    ),
]


def _credits_payload():
    return {"data": {"total_credits": 30, "total_usage": 25.333767208}}


def _key_payload():
    return {
        "data": {
            "label": "sk-or-v1-916...a1a",
            "is_management_key": True,
            "limit": None,
            "usage": 0,
            "expires_at": None,
        }
    }


def _keys_payload():
    names = ["agent hermes api key", "VS Code", "ai.willenb.orx"]
    return {
        "data": [
            {
                "name": name,
                "label": f"sk-or-v1-label-{i}",
                "disabled": False,
                "limit": None,
                "usage": 17.30539272,
                "created_at": "2026-08-31T16:00:20.782Z",
                "last_used_at": "2026-10-12T13:48:38.611223Z",
                "workspace_id": "14dc1a07-9f04-58f9-ac58-a4c538293ddf",
            }
            for i, name in enumerate(names)
        ]
    }


def _workspaces_payload():
    return {
        "data": [
            {
                "id": "14dc1a07-9f04-58f9-ac58-a4c538293ddf",
                "name": "Default Workspace",
                "slug": "default",
                "created_at": "2026-08-28T10:21:56.434Z",
                "default_text_model": None,
            }
        ]
    }


def _guardrails_payload():
    return {
        "data": [
            {
                "id": "17f8f219-0aaa-5ff7-8b21-a70680fa12fe",
                "name": "Workspace Default",
                "limit_usd": 25,
                "reset_interval": "monthly",
                "allowed_models": None,
                "ignored_models": None,
                "allowed_providers": None,
                "enforce_zdr": False,
            }
        ]
    }


def test_retrieve_and_render_roundtrip_with_realistic_payloads(base_url, settings, tmp_path, jjenv):
    """End-to-end: a provider configured with the real management endpoints,
    fed the account-style payloads from the OpenRouter.json dump, produces a
    rendered markdown whose key tables are populated. Guards against the
    template drifting out of sync with the real response shapes."""
    yml = {
        "provider": "OpenrouterProvider",
        "baseURL": base_url,
        "APIKey": "or-key-123",
        "headers": {
            "Authorization": "Bearer or-key-123",
            "accept": "application/json",
            "Content-Type": "application/json",
        },
        "template": "openrouter.md.jinja",
        "endpoints": {
            "keys": "/keys",
            "key": "/auth/key",
            "credits": "/credits",
            "workspaces": "/workspaces",
            "guardrails": "/guardrails",
            "byok": "/byok",
            "presets": "/presets",
        },
    }
    settings["outputpath"] = str(tmp_path)
    provider = OpenrouterProvider("openrouter", yml, settings, jjenv)

    with Mocker() as m:
        m.get(base_url + "/keys", status_code=200, json=_keys_payload())
        m.get(base_url + "/auth/key", status_code=200, json=_key_payload())
        m.get(base_url + "/credits", status_code=200, json=_credits_payload())
        m.get(base_url + "/workspaces", status_code=200, json=_workspaces_payload())
        m.get(base_url + "/guardrails", status_code=200, json=_guardrails_payload())
        m.get(base_url + "/byok", status_code=200, json={"data": [], "total_count": 0})
        m.get(base_url + "/presets", status_code=200, json={"data": [], "total_count": 0})
        m.post(
            base_url + "/analytics/query", status_code=200, json=_analytics_body(MOCK_DAILY_ROWS)
        )

        status = provider.retrieve_config()
        assert status == STATUS_OK
        provider.do_render_template()

    # aggregated usage, sorted and summed from the realistic rows
    usage = provider.configdata["usage"]
    assert usage["week_totals"]["days"] == 5
    assert usage["week_totals"]["requests"] == 577
    assert usage["week_totals"]["tokens"] == 16032280
    assert usage["week_totals"]["cost"] == pytest.approx(1.3511)

    out = tmp_path / "openrouter.md"
    content = out.read_text()
    # Credentials section from credits payload
    assert "| 30 | 25.333767208 |" in content
    # Key rows from the realistic keys list
    assert "agent hermes api key" in content
    assert "ai.willenb.orx" in content
    # Workspace and guardrail rows
    assert "Default Workspace" in content
    assert "Workspace Default" in content
    # Weekly usage totals rendered from the realistic analytics rows
    assert "## Week Totals" in content
    assert "**Total tokens: 16032280**" in content
