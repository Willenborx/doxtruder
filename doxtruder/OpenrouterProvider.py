"""Configuration provider for OpenRouter API analytics and usage.

Extends :class:`doxtruder.RESTProvider.RESTProvider` to fetch keys, credits, and analytics statistics from OpenRouter.

Example configuration in ``doxtruder.yaml``:

.. code-block:: yaml

    entities:

      OpenRouter:
        provider: OpenrouterProvider
        template: openrouter.md.jinja
        title: My OpenRouter
        baseURL: https://openrouter.ai/api/v1
        APIKey: ${OPENROUTER_KEY} # account/management key for the analytics POST
        endpoints:
          keys: /keys
          key: /auth/key
          credits: /credits
          workspaces: /workspaces
          guardrails: /guardrails
          byok: /byok
          presets: /presets
          # daily usage is queried via POST /analytics/query in aggregate_usage()
"""

import logging
from datetime import datetime, timedelta, timezone

import requests
from jinja2 import Environment

from doxtruder.DoxtruderEngine import STATUS_WARNING
from doxtruder.Exceptions import EndpointError, EntityError, is_ssl_verification_error
from doxtruder.RESTProvider import RESTProvider, _disable_insecure_request_warnings

ANALYTICS_TIMEOUT = 30
"""Timeout in seconds for the OpenRouter analytics POST (a multi-day aggregate
query is slower than the single-resource GETs) when the global
``settings: timeout:`` entry is absent or invalid."""


class OpenrouterProvider(RESTProvider):
    """Provider for OpenRouter (https://openrouter.ai).

    Uses the account/management key from ``APIKey`` to retrieve:

    - ``keys`` - list of API keys (GET /api/v1/keys)
    - ``key`` - usage of the key itself (GET /api/v1/auth/key)
    - ``credits`` - total credits / usage (GET /api/v1/credits)
    - ``usage`` - daily token and cost statistics for the last 7 days (POST /api/v1/analytics/query), see :meth:`aggregate_usage`
    - ``workspaces`` - list of workspaces (GET /api/v1/workspaces)
    - ``guardrails`` - spending limits / model blocklists (GET /api/v1/guardrails)
    - ``byok`` - bring-your-own-key provider credentials (GET /api/v1/byok)
    - ``presets`` - saved presets (GET /api/v1/presets)
    - ``activity`` - last-30-days usage grouped by endpoint (GET /api/v1/analytics/activity)

    The endpoints actually called are the ones listed under ``endpoints:`` in
    the YAML entity; any that fail (403/404/501 with this account type) are
    skipped with a warning, keeping the rest of the document intact.

    Follows the ProxmoxProvider pattern: only :meth:`perform_request` and the
    analytics POST are implemented here, the rest of the endpoint
    orchestration lives in RESTProvider.
    """

    def __init__(self, entityname: str, entity_cfg: dict, dx_cfg: dict, jjenv: Environment):
        """Initializes the OpenrouterProvider.

        :param entityname: Name of the entity.
        :param entity_cfg: YAML configuration dictionary.
        :param dx_cfg: Global settings dictionary.
        :param jjenv: Shared Jinja2 environment.
        """
        super().__init__(entityname, entity_cfg, dx_cfg, jjenv)

    def init_entity(self):
        """Initialize OpenRouter entity."""
        logging.debug(f"OpenRouter entity {self.entityname} initialized")

    def _analytics_timeout(self) -> float:
        """Timeout for the analytics query.

        Uses the global ``settings: timeout:`` entry when it is set and valid
        (see :meth:`doxtruder.RESTProvider.RESTProvider._configured_timeout`);
        otherwise the higher :data:`ANALYTICS_TIMEOUT` default applies, since
        the aggregate query takes longer than the plain GET endpoints. A
        missing *or invalid* timeout therefore keeps the 30 s default rather
        than silently dropping to the 10 s GET default.

        :returns: Timeout in seconds.
        """
        timeout = self._configured_timeout()
        return ANALYTICS_TIMEOUT if timeout is None else timeout

    def perform_request(self, url: str, data: dict, method: str = "GET") -> requests.Response:
        """Perform an HTTP GET request to OpenRouter with Bearer token headers.

        OpenRouter's resource endpoints are all GETs (the analytics aggregate
        is a separate POST in :meth:`aggregate_usage`), so ``method`` is
        accepted only to keep the signature compatible with the base class
        call path and is not otherwise honoured here.

        :param url: Absolute endpoint URL.
        :param data: Optional request body (unused for these GET calls).
        :param method: Ignored; OpenRouter resource calls are always GET.
        :returns: HTTP response.
        :raises EndpointError: if the 'headers:' configuration is incomplete.
        """
        try:
            authorization = self.entity_cfg["headers"]["Authorization"]
        except (KeyError, TypeError) as e:
            raise EndpointError.wrap(
                e,
                "The OpenRouter entity has no 'headers: Authorization:' entry.",
                entity=self.entityname,
                endpoint=url,
                hint=(
                    "Add a 'headers:' section to this entity with "
                    "'Authorization: Bearer <key>' (define the key via an "
                    "environment variable placeholder)."
                ),
            ) from e

        headers = {
            "Authorization": authorization,
            "accept": "application/json",
            "Content-Type": "application/json",
        }
        ignore_ssl = self.ignore_ssl()
        if ignore_ssl:
            _disable_insecure_request_warnings()
        return requests.get(
            url, timeout=self._request_timeout(), verify=not ignore_ssl, headers=headers
        )

    def retrieve_config(self) -> int:
        """Retrieve configuration and aggregate analytics usage.

        :returns: Status code (:data:`STATUS_OK` or :data:`STATUS_WARNING`).
        :raises EntityError: if the analytics call fails with an SSL
            certificate error (the whole entity is skipped).
        """
        status = super().retrieve_config()
        try:
            self.aggregate_usage()
        except EndpointError as e:
            e.log()
            status = STATUS_WARNING
        except (requests.exceptions.RequestException, OSError) as e:
            if is_ssl_verification_error(e):
                # an untrusted certificate is an entity level problem:
                # skip the entity instead of downgrading to a warning
                raise EntityError.wrap(
                    e,
                    f"SSL certificate of {self.baseURL} is not trusted.",
                    entity=self.entityname,
                    hint=(
                        "The host uses a self-signed or unknown certificate. "
                        "Either add the CA certificate to the system trust "
                        "store, or skip certificate verification with "
                        "--ssl-cert-ignore / -i, 'sslcert: ignore' in this "
                        "entity, or 'sslcert: ignore' in the settings section."
                    ),
                )
            EndpointError.wrap(
                e,
                f"Cannot retrieve the OpenRouter usage analytics from {self.baseURL}/analytics/query.",
                entity=self.entityname,
                endpoint="/analytics/query",
                hint=(
                    "Check that the 'APIKey:' of this entity is a valid account key and "
                    "that the analytics endpoint is available for your plan."
                ),
            ).log()
            status = STATUS_WARNING
        except (KeyError, TypeError, ValueError) as e:
            EndpointError.wrap(
                e,
                "The OpenRouter usage analytics response could not be aggregated.",
                entity=self.entityname,
                endpoint="/analytics/query",
            ).log()
            status = STATUS_WARNING
        return status

    def aggregate_usage(self):
        """Query per-day usage stats for the last 7 days via the Analytics API.

        OpenRouter has no single GET endpoint with daily costs, so this posts
        an analytics query with granularity ``day`` over the last 7 days. The
        result (rows keyed by ``date__day``) is refactored into a
        ``configdata['usage']`` structure with ``daily`` (list) and
        ``week_totals`` (dict) for the template.

        :raises EndpointError: if the ``APIKey:`` entry is missing.
        :raises requests.exceptions.RequestException: if the analytics query
            fails (converted to an :class:`EndpointError`, or to an
            :class:`EntityError` for SSL certificate problems, by
            :meth:`retrieve_config`).
        """
        try:
            api_key = self.entity_cfg["APIKey"]
        except KeyError as e:
            raise EndpointError.wrap(
                e,
                "The OpenRouter entity has no 'APIKey:' entry.",
                entity=self.entityname,
                endpoint="/analytics/query",
                hint=(
                    "Add 'APIKey: ${OPENROUTER_KEY}' to this entity block and "
                    "define the variable in the .env file."
                ),
            ) from e

        headers = {
            "Authorization": f"Bearer {api_key}",
            "accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.ignore_ssl():
            _disable_insecure_request_warnings()

        # last 7 calendar days (UTC), exclusive end
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        body = {
            "metrics": [
                "total_usage",
                "tokens_total",
                "tokens_prompt",
                "tokens_completion",
                "request_count",
            ],
            "granularity": "day",
            "time_range": {
                "start": start.isoformat().replace("+00:00", "Z"),
                "end": end.isoformat().replace("+00:00", "Z"),
            },
            "limit": 10,
        }

        response = requests.post(
            f"{self.baseURL}/analytics/query",
            headers=headers,
            json=body,
            timeout=self._analytics_timeout(),
            verify=not self.ignore_ssl(),
        )
        response.raise_for_status()
        rows = response.json().get("data", {}).get("data", []) or []

        def as_int(value):
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        def as_float(value):
            try:
                return float(value or 0)
            except (TypeError, ValueError):
                return 0.0

        daily = []
        for row in rows:
            daily.append(
                {
                    "date": row.get("date__day", ""),
                    "requests": as_int(row.get("request_count")),
                    "tokens": as_int(row.get("tokens_total")),
                    "prompt_tokens": as_int(row.get("tokens_prompt")),
                    "completion_tokens": as_int(row.get("tokens_completion")),
                    "cost": as_float(row.get("total_usage", 0)),
                }
            )
        daily.sort(key=lambda d: d["date"])

        week_totals = {
            "days": len(daily),
            "requests": sum(d["requests"] for d in daily),
            "tokens": sum(d["tokens"] for d in daily),
            "prompt_tokens": sum(d["prompt_tokens"] for d in daily),
            "completion_tokens": sum(d["completion_tokens"] for d in daily),
            "cost": round(sum(d["cost"] for d in daily), 4),
        }

        self.configdata["usage"] = {"daily": daily, "week_totals": week_totals}
