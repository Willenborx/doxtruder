"""Tests for the Unifi entity as configured in doxtruder.yaml.

The UnifiProvider subclass is being deleted: since the configuration was
switched to per-site reads the entity is a plain RESTProvider with a ``sites``
endpoint plus ``devstats``/``vouchers`` endpoints expanded per site name via
``$jpath{$.sites.data[*].name}`` — one HTTP call per site. Because of that
expansion the retrieved data gained one level under the site name
(``configdata['devstats'][<site>]`` instead of a single flat
``configdata['devstats']``).

These tests drive the real RESTProvider against that doxtruder.yaml Unifi
shape, and pin the per-site template rendering. All network I/O is faked
(requests_mock).
"""

import pytest
from requests_mock import Mocker

from doxtruder.DoxtruderEngine import STATUS_OK, STATUS_WARNING
from doxtruder.RESTProvider import RESTProvider


@pytest.fixture
def base_url():
    return "https://unifi.example.local"


@pytest.fixture
def settings():
    return {"title": "test-docs"}


@pytest.fixture
def per_site_unifi_yml(base_url):
    """The Unifi entity as configured in doxtruder.yaml after the per-site
    change: a plain RESTProvider, a `sites` endpoint, and devstats/vouchers
    expanded by $jpath{$.sites.data[*].name} — one HTTP call per site."""
    return {
        "provider": "RESTProvider",
        "baseURL": base_url,
        "headers": {
            "x-api-key": "key-123",
            "accept": "application/json",
            "content-type": "application/json",
        },
        "endpoints": {
            "sites": "/api/self/sites",
            "devstats": "/api/s/$jpath{$.sites.data[*].name}/stat/device",
            "vouchers": "/api/s/$jpath{$.sites.data[*].name}/stat/voucher",
        },
    }


def _multi_site_body():
    return {
        "meta": {"rc": "ok"},
        "data": [
            {"name": "default", "desc": "Default"},
            {"name": "8gb2k6wn", "desc": "Willenborx"},
        ],
    }


def test_per_site_config_nests_devstats_and_vouchers_under_site_name(
    base_url, per_site_unifi_yml, settings, jjenv
):
    """devstats/vouchers are fetched once per site name from the sites
    endpoint; the responses are stored one level deeper than before, keyed by
    site name (configdata['devstats'][<site>] instead of a single flat
    configdata['devstats'])."""
    devstats_body = {"meta": {"rc": "ok"}, "data": [{"name": "AP-Office", "type": "uap"}]}
    vouchers_body = {"meta": {"rc": "ok"}, "data": [{"note": "Guest", "code": "ABC"}]}

    with Mocker() as m:
        m.get(base_url + "/api/self/sites", status_code=200, json=_multi_site_body())
        for sn in ("default", "8gb2k6wn"):
            m.get(base_url + f"/api/s/{sn}/stat/device", status_code=200, json=devstats_body)
            m.get(base_url + f"/api/s/{sn}/stat/voucher", status_code=200, json=vouchers_body)
        prov = RESTProvider("Unifi-Willenborx", per_site_unifi_yml, settings, jjenv)
        status = prov.retrieve_config()

    assert status == STATUS_OK
    assert prov.configdata["sites"] == _multi_site_body()
    # one level per site name added for both devstats and vouchers
    assert set(prov.configdata["devstats"]) == {"default", "8gb2k6wn"}
    assert set(prov.configdata["vouchers"]) == {"default", "8gb2k6wn"}
    assert prov.configdata["devstats"]["8gb2k6wn"]["data"][0]["name"] == "AP-Office"
    assert prov.configdata["vouchers"]["8gb2k6wn"]["data"][0]["code"] == "ABC"
    assert m.call_count == 5  # sites + devstats x2 + vouchers x2


def test_per_site_config_with_no_sites_warns_and_skips_expanded_endpoints(
    base_url, per_site_unifi_yml, settings, jjenv
):
    """With an empty sites payload the $jpath expansion matches nothing:
    each dependent endpoint raises an EndpointError (logged, downgraded to
    STATUS_WARNING) instead of being silently skipped. Only the sites call
    happens and devstats/vouchers stay absent."""
    with Mocker() as m:
        m.get(base_url + "/api/self/sites", status_code=200, json={"data": []})
        prov = RESTProvider("Unifi-Willenborx", per_site_unifi_yml, settings, jjenv)
        status = prov.retrieve_config()

    assert status == STATUS_WARNING
    assert m.call_count == 1
    assert "devstats" not in prov.configdata
    assert "vouchers" not in prov.configdata


def test_unifi_template_renders_per_site_sections_labeled_with_site_desc(jjenv):
    """The unifi template renders one Devices section per site, labeled with
    the site *description* (not the internal name), reading
    data['devstats'][<site name>]; sites without data render no section.
    Vouchers render per site as well."""
    data = {
        "sites": {
            "data": [
                {"name": "default", "desc": "Default"},
                {"name": "8gb2k6wn", "desc": "Willenborx"},
            ]
        },
        "devstats": {
            "default": {"data": []},
            "8gb2k6wn": {
                "data": [
                    {
                        "name": "AP-Office",
                        "type": "uap",
                        "shortname": "U6-LR",
                        "mac": "aa:bb:cc",
                        "ip": "10.0.0.5",
                        "state": "1",
                        "adopted": True,
                        "upgradable": False,
                        "num_sta": 3,
                        "user-num_sta": 2,
                        "guest-num_sta": 1,
                    }
                ]
            },
        },
        "vouchers": {
            "default": {"data": []},
            "8gb2k6wn": {
                "data": [
                    {
                        "note": "Guest Wifi",
                        "code": "ABC-123",
                        "duration": 1440,
                        "used": 0,
                        "quota": 1,
                        "status": "VALID_NEW",
                    }
                ]
            },
        },
    }
    out = jjenv.get_template("unifi.md.jinja").render(
        data=data, settings={}, entityname="Unifi-Willenborx"
    )

    assert "## Willenborx" in out  # device section labeled with site desc
    assert "AP-Office" in out
    assert "## Default" not in out  # site without data renders no section
    assert "### Willenborx" in out  # vouchers per-site heading
    assert "ABC-123" in out
    assert "aa:bb:cc" in out
