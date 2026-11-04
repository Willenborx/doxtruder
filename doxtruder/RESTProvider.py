"""Base class for REST API based configuration providers.

Extends :class:`doxtruder.ConfigProvider.ConfigProvider` to automate REST endpoint iteration, variable
substitution in endpoint URLs, error handling for individual endpoints, and
HTTP response validation.
"""

import json
import logging
import re
from http import HTTPStatus
from urllib.parse import quote

import requests
import urllib3
from jinja2 import Environment
from requests import Response
from requests.auth import HTTPBasicAuth

from doxtruder.ConfigProvider import ConfigProvider, default_json_serializer
from doxtruder.DoxtruderEngine import STATUS_OK, STATUS_WARNING
from doxtruder.Exceptions import (
    DoxtruderError,
    EndpointError,
    EntityError,
    is_ssl_verification_error,
)

DEFAULT_TIMEOUT = 10
"""HTTP request timeout in seconds used when the global ``settings:`` section
has no (or an invalid) ``timeout:`` entry."""

ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"})
"""HTTP methods an endpoint may declare with its optional ``method:`` entry
(case-insensitive). Everything else is reported as a configuration error
instead of being sent to the server."""

_warnings_disabled = False
"""Guard so :func:`_disable_insecure_request_warnings` touches the global
urllib3 warning filters at most once per process."""


def _disable_insecure_request_warnings() -> None:
    """Silence urllib3's ``InsecureRequestWarning`` (needed when certificate
    verification is turned off for self-signed hosts).

    Only that single warning category is filtered — not every urllib3 warning —
    and the (process-global) filter change is applied once per process instead
    of on every request, so repeated insecure calls do not keep mutating the
    global warning state.
    """
    global _warnings_disabled
    if _warnings_disabled:
        return
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    _warnings_disabled = True


class RESTProvider(ConfigProvider):
    """Base class for REST-backed configuration providers. If the API is pretty much standard, this provider can
    be used for any API. It's required to know the way the API authenticates, RESTProvider supports Basic Auth and
    Authentication via Headers (e.g. Authorization: Bearer <APIKey>). The
    request timeout is read from the global ``settings: timeout:`` entry and
    defaults to :data:`DEFAULT_TIMEOUT` seconds.

    Iterates through configured API endpoints, performs URL variable substitution,
    calls endpoints via HTTP GET, checks status codes, and aggregates retrieved JSON data.
    """

    response: Response
    """The latest HTTP response object received from an API call."""

    def __init__(self, entityname: str, entity_cfg: dict, dx_cfg: dict, jjenv: Environment):
        """Initializes the RESTProvider.

        :param entityname: Name of the entity being processed.
        :param entity_cfg: Entity-specific configuration dictionary.
        :param dx_cfg: Global settings dictionary.
        :param jjenv: Shared Jinja2 environment.
        :raises EntityError: if the mandatory 'baseURL:' entry is missing.
        """
        super().__init__(entityname, entity_cfg, dx_cfg, jjenv)
        if "baseURL" not in self.entity_cfg:
            raise EntityError(
                "The entity configuration has no 'baseURL:' entry.",
                entity=self.entityname,
                hint="Add 'baseURL: https://<host>' to this entity block.",
            )
        self.baseURL = self.entity_cfg["baseURL"]
        self.response = None
        self.data = {}
        self.auth = None
        self.headers = dict()
        self._request_ctx_ready = False

    def _build_auth(self):
        """Build the ``requests`` auth object from the entity's ``auth:`` block.

        Only HTTP Basic auth is supported, declared as::

            auth:
              basic:
                user: ${USER}
                pass: ${PASS}

        A missing/empty block simply means "no auth". Anything else (a typo in
        the method name, or a ``basic:`` block without ``user``/``pass``) is
        reported instead of being silently ignored, because the resulting 401
        from the API server would otherwise look like a wrong credentials
        problem.

        :returns: An :class:`~requests.auth.HTTPBasicAuth`, or ``None``.
        :raises EntityError: if an ``auth:`` block is present but unusable.
        """
        auth_cfg = self.entity_cfg.get("auth")
        if auth_cfg is None:
            return None
        if not isinstance(auth_cfg, dict) or not auth_cfg:
            raise EntityError(
                "The 'auth:' entry of this entity is empty or not a mapping.",
                entity=self.entityname,
                hint=(
                    "Declare Basic auth as 'auth: {basic: {user: ..., "
                    "pass: ...}}', or remove the 'auth:' entry to send no "
                    "authentication."
                ),
            )
        if "basic" not in auth_cfg:
            raise EntityError(
                f"The 'auth:' block of this entity has no 'basic:' entry "
                f"(found: {', '.join(sorted(auth_cfg))}).",
                entity=self.entityname,
                hint=(
                    "HTTP Basic auth is the only supported method. Rename the "
                    "key to 'basic:' with 'user:'/'pass:' children, or pass "
                    "tokens via the 'headers:' section instead."
                ),
            )
        basic = auth_cfg["basic"]
        if not isinstance(basic, dict) or "user" not in basic or "pass" not in basic:
            raise EntityError(
                "The 'auth: basic:' block of this entity needs 'user:' and 'pass:'.",
                entity=self.entityname,
                hint=(
                    "Complete the entry, e.g. 'user: ${API_USER}' and "
                    "'pass: ${API_PASS}', and define the variables in the "
                    "'.env' file."
                ),
            )
        return HTTPBasicAuth(basic["user"], basic["pass"])

    def _configured_timeout(self) -> float | None:
        """Read and validate the global ``settings: timeout:`` entry.

        ``timeout:`` may be a number (seconds) or a numeric string.

        :returns: The timeout in seconds, or ``None`` when the entry is
            missing, not numeric, or not greater than zero (invalid values
            are reported with a warning).
        """
        raw = self.settings.get("timeout")
        if raw is None:
            return None
        try:
            timeout = float(raw)
        except (TypeError, ValueError):
            logging.warning(
                f"Ignoring the setting 'timeout: {raw}'; using the provider "
                f"default timeout instead."
            )
            return None
        if timeout <= 0:
            logging.warning(
                f"Ignoring the setting 'timeout: {raw}'; it must be greater "
                f"than 0, using the provider default timeout instead."
            )
            return None
        return timeout

    def _request_timeout(self) -> float:
        """HTTP timeout for a single request.

        Uses the global ``settings: timeout:`` entry when it is set and
        valid, otherwise :data:`DEFAULT_TIMEOUT` seconds.

        :returns: Timeout in seconds for every API request.
        """
        timeout = self._configured_timeout()
        return DEFAULT_TIMEOUT if timeout is None else timeout

    def _prepare_request_context(self) -> None:
        """Merge the entity's ``headers:`` and build its auth object once.

        Both come from static YAML configuration, so the identical work is
        done on the first request of the entity instead of on every endpoint
        call. A broken ``auth:`` block raises :class:`EntityError` here, just
        like when it was built per request.
        """
        if self._request_ctx_ready:
            return
        if "headers" in self.entity_cfg:
            logging.debug("adding headers to request")
            for header_name, header_value in self.entity_cfg["headers"].items():
                self.headers[header_name] = header_value
        self.auth = self._build_auth()
        self._request_ctx_ready = True

    def perform_request(self, url: str, data: dict | None, method: str = "GET") -> Response:
        """Perform an HTTP request to the given URL.

        Reads the headers and/or auth configuration from yaml and invokes the request.
        The timeout is taken from the global ``settings: timeout:`` entry and
        defaults to :data:`DEFAULT_TIMEOUT` seconds.

        :param url: Absolute URL to request.
        :param data: Optional request body, JSON-encoded as-is. The entity's
            ``headers:`` should declare ``Content-Type: application/json``
            when a body is sent.
        :param method: HTTP method name (validated against
            :data:`ALLOWED_METHODS` by the caller).
        :returns: HTTP response object.
        """

        self._prepare_request_context()

        if data:
            try:
                body = json.dumps(data, default=default_json_serializer)
            except (TypeError, ValueError) as e:
                raise EndpointError.wrap(
                    e,
                    "The request body of this endpoint cannot be serialized.",
                    entity=self.entityname,
                    hint=(
                        "Check the 'body:' entry of this endpoint in the YAML "
                        "file (only JSON compatible values are allowed)."
                    ),
                )
        else:
            body = None

        # certificate verification is on by default; --ssl-cert-ignore / -i,
        # 'sslcert: ignore' in the global settings, or 'sslcert: ignore' in
        # this entity's block turns it off for self-signed hosts
        ignore_ssl = self.ignore_ssl()
        if ignore_ssl:
            _disable_insecure_request_warnings()

        return requests.request(
            method,
            url,
            timeout=self._request_timeout(),
            verify=not ignore_ssl,
            headers=self.headers,
            auth=self.auth,
            data=body,
        )

    def subst_vars(self, template: str) -> str:
        """Substitute placeholders in endpoint URL templates with data values.

        Finds placeholders matching ``{key}`` and replaces them with corresponding values from :attr:`data`.

        :param template: URL template string containing placeholders.
        :returns: URL string with substituted values.
        :raises EndpointError: if a placeholder has no matching value in the
            data collected so far.
        """
        pattern = re.compile(r"\{(\w+)\}")

        def replace(match):
            key = match.group(1)
            value = self._lookup_placeholder(key)
            if value is None:
                raise EndpointError(
                    f"Placeholder '{'{' + key + '}'}' in the endpoint URL cannot be resolved.",
                    entity=self.entityname,
                    hint=(
                        f"Add a previous endpoint that returns a key '{key}', or "
                        f"use a $jpath{{...}} expression instead of '{{{key}}}'."
                    ),
                )
            # percent-encode so spaces/reserved characters in API data cannot
            # corrupt the URL (a raw '?' or '#' would silently truncate it)
            return quote(str(value), safe="")

        return pattern.sub(replace, template)

    def _lookup_placeholder(self, key: str):
        """Resolve a ``{key}`` URL placeholder against the collected data.

        ``self.data`` is checked first (an explicit override, also used by
        tests); otherwise the key is searched in :attr:`configdata` — the
        data actually gathered by earlier endpoints. A key may sit at the
        top level of an endpoint payload or one level down (e.g. a
        ``{"data": ...}`` wrapper).

        :param key: Placeholder name (without the braces).
        :returns: The matching value, or ``None`` if not found.
        """
        if key in self.data:
            return self.data[key]
        for endpoint, payload in self.configdata.items():
            if isinstance(payload, dict):
                if key in payload:
                    logging.debug(
                        f"Placeholder '{{{key}}}' resolved from endpoint "
                        f"'{endpoint}' (top-level key)."
                    )
                    return payload[key]
                inner = payload.get("data")
                if isinstance(inner, dict) and key in inner:
                    logging.debug(
                        f"Placeholder '{{{key}}}' resolved from endpoint "
                        f"'{endpoint}' (under its 'data' wrapper)."
                    )
                    return inner[key]
        return None

    def retrieve_config(self):
        """Retrieve configuration data from all configured REST endpoints.

        Iterates over all endpoints defined in ``yml['endpoints']``, substitutes variables,
        calls each endpoint, and stores parsed JSON responses in :attr:`configdata`.

        :returns: Status code (:data:`STATUS_OK` or :data:`STATUS_WARNING`).
        :raises EntityError: if the entity configuration is incomplete.
        """

        allover_status = STATUS_OK

        if "endpoints" not in self.entity_cfg:
            raise EntityError(
                "The entity configuration has no 'endpoints:' entry.",
                entity=self.entityname,
                hint=(
                    "Add an 'endpoints:' mapping to this entity, e.g. "
                    "'version: /api2/json/version'."
                ),
            )
        if not isinstance(self.entity_cfg["endpoints"], dict):
            raise EntityError(
                "The 'endpoints:' entry of this entity is not a mapping "
                f"(got {type(self.entity_cfg['endpoints']).__name__}).",
                entity=self.entityname,
                hint=(
                    "List the endpoints as 'endpoints:' with one "
                    "'<name>: /path' child per endpoint, e.g. "
                    "'version: /api2/json/version'."
                ),
            )

        for endpoint in self.entity_cfg["endpoints"]:

            data = None
            method = "GET"
            try:
                # read endpoint URL from yaml and replace ${<var>}s
                endpoint_cfg = self.entity_cfg["endpoints"][endpoint]
                if isinstance(endpoint_cfg, dict):
                    if "url" not in endpoint_cfg:
                        raise EndpointError(
                            f"The endpoint '{endpoint}' has no 'url:' entry.",
                            entity=self.entityname,
                            endpoint=endpoint,
                            hint=(
                                "Define the endpoint either as '<name>: /path' "
                                "or as '<name>:' with a 'url: /path' child."
                            ),
                        )
                    endpoint_url = self.subst_vars(endpoint_cfg["url"])
                    if "body" in endpoint_cfg:
                        data = endpoint_cfg["body"]
                    if "method" in endpoint_cfg:
                        method = str(endpoint_cfg["method"]).strip().upper()
                        if method not in ALLOWED_METHODS:
                            raise EndpointError(
                                f"The endpoint '{endpoint}' declares the HTTP "
                                f"method '{endpoint_cfg['method']}', which is "
                                f"not supported.",
                                entity=self.entityname,
                                endpoint=endpoint,
                                hint=(
                                    "Use one of: "
                                    f"{', '.join(sorted(ALLOWED_METHODS))} "
                                    "(case-insensitive), or remove the "
                                    "'method:' entry to send a GET."
                                ),
                            )
                else:
                    endpoint_url = self.subst_vars(endpoint_cfg)

                # expand JSONPath expressions $jpath{$.<expr>}
                endpoints_expanded = self.expand_json_path(endpoint_url, self.configdata)

                # iterate through all endpoints (may be multiple if JSONPath uses *)
                if endpoints_expanded:
                    for endpoint_expanded_key, endpoint_expanded_url in endpoints_expanded.items():
                        try:
                            self.response = self.call_api_endpoint(
                                endpoint_expanded_url, data, method
                            )
                            self.store_response(endpoint, endpoint_expanded_key, self.response)
                        except EndpointError as e:
                            e.log()
                            allover_status = STATUS_WARNING

            except EndpointError as e:
                e.log()
                allover_status = STATUS_WARNING

        return allover_status

    def store_response(self, endpoint: str, expanded_key: str, response: Response) -> None:
        """Convert a successful response into the entity's config data structure.

        :param endpoint: Name of the endpoint in the YAML configuration.
        :param expanded_key: ``"SINGLE"`` or the JSONPath expansion key.
        :param response: Validated HTTP response object.
        :raises EndpointError: if the response body is no valid JSON.
        """
        try:
            payload = response.json()
        except ValueError as e:
            raise EndpointError.wrap(
                e,
                f"The response of {response.url} is not valid JSON.",
                entity=self.entityname,
                endpoint=endpoint,
                hint=(
                    "Check the 'baseURL:' and the URL of this endpoint — the "
                    "server probably returned an HTML error or login page."
                ),
            )
        if expanded_key == "SINGLE":
            self.configdata[endpoint] = payload
        else:
            if endpoint not in self.configdata:
                self.configdata[endpoint] = {}
            self.configdata[endpoint][expanded_key] = payload

    def call_api_endpoint(self, endpoint: str, data: dict | None, method: str = "GET") -> Response:
        """Helper method to call a single API endpoint and validate the HTTP response.

        Disables insecure request warnings, builds the full URL, invokes :meth:`perform_request`,
        and verifies the status code is HTTP 200 OK.

        :param endpoint: Relative endpoint path.
        :param data: Optional request body (JSON-encoded by
            :meth:`perform_request`).
        :param method: HTTP method for this endpoint (default ``GET``).
        :returns: Validated HTTP response object.
        :raises EntityError: if the server certificate cannot be verified
            (verification is off for every endpoint of the entity, so the
            entity is skipped and no further endpoints are called).
        :raises EndpointError: if the request fails, the status code is not
            200 OK, or the endpoint configuration is incomplete.
        """
        response = None
        try:
            url = self.baseURL + endpoint
            logging.info(f"Calling endpoint {method} {url}")

            response = self.perform_request(url, data, method)
            logging.debug(f"HTTP Status: {response.status_code} {response.reason}")

            if response.status_code != HTTPStatus.OK:
                raise EndpointError(
                    f"Could not retrieve data from {url} / "
                    f"{response.status_code} {response.reason}.",
                    entity=self.entityname,
                    endpoint=endpoint,
                    hint=(
                        "Check the URL of this endpoint and the "
                        "authentication ('headers:'/'auth:') of this entity; a "
                        "401/403 usually means a wrong or expired API key."
                    ),
                )

            return response

        except DoxtruderError as e:
            # an EntityError from _build_auth() (broken 'auth:' block) must not
            # be reclassified as an endpoint problem by the handler below
            raise e
        except (requests.exceptions.RequestException, OSError) as e:
            if is_ssl_verification_error(e):
                # an untrusted certificate affects every endpoint of this
                # entity, so abort the whole entity (no further API calls)
                # instead of retrying endpoint by endpoint
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
            raise EndpointError.wrap(
                e,
                f"Connection problem while calling {self.baseURL}{endpoint}.",
                entity=self.entityname,
                endpoint=endpoint,
                hint=(
                    "Check that the host in 'baseURL:' is reachable from this "
                    "machine and that the endpoint path is correct."
                ),
            )
        except Exception as e:
            raise EndpointError.wrap(
                e,
                f"Unexpected error while calling the endpoint {endpoint}.",
                entity=self.entityname,
                endpoint=endpoint,
                hint=(
                    "Check the 'endpoints:' entry of this entity (URL, "
                    "'body:', 'headers:'/'auth:') in the YAML file."
                ),
            )

    def render_config(self):
        """Render the entity configuration using the base class renderer."""
        return super().render_config()
