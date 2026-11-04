"""Definition of the abstract base class for all configuration providers.

This module declares :class:`ConfigProvider`, the abstract interface that every
concrete provider (e.g. RESTProvider) must implement. It centralises the lifecycle
used by the main engine loop: constructor, entity initialisation, configuration
retrieval and Jinja2 based rendering of the resulting documentation.
"""

import datetime
import logging
import re
from abc import ABC, abstractmethod
from urllib.parse import quote

from jinja2 import Environment, Template, TemplateNotFound, TemplateSyntaxError
from jsonpath_ng import JSONPathError, parse

from doxtruder.Exceptions import (
    DoxtruderError,
    EndpointError,
    RenderError,
)


def default_json_serializer(obj):
    """
    This is a callback for json.dumps() - if the JSON contains a non-serializable type, this function will
    be called. if this function knows a way to serialiize the data, it will do so, otherwise a TypeError
    exception is raised
    """
    if isinstance(obj, datetime.datetime):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class ConfigProvider(ABC):
    """Abstract base class implemented by every configuration provider.

    A provider knows how to read the configuration of one entity from some
    source, store it in a dict :attr:`configdata` and render through a Jinja2
    template. The engine loop is agnostic to the concrete subclass and always
    drives providers through the same fixed interface defined here.
    """

    jinja2env: Environment
    """Shared Jinja2 environment used to load and render templates."""

    jinja2template: Template
    """The resolved Jinja2 template for the current entity."""

    def __init__(self, entityname: str, entity_cfg: dict, dx_cfg: dict, jjenv: Environment):
        """Initializes the ConfigProvider.

        Derived classes cannot change the arguments because this constructor is always called from the main loop. The main loop
        is agnostic to the actual class which is initialized which means the constructor must always have exactly these parameters.

        :param entityname: is taken from the doxtruder.yaml file, first level under entities:
        :param entity_cfg: this is the yaml data below the entity name
        :param dx_cfg: This is the settings: section of the YAML file
        :param jjenv: the shared Jinja2 environment used for template rendering
        """
        self.entityname = entityname
        """Name of the entity this provider handles."""
        self.entity_cfg = entity_cfg
        """Raw YAML configuration data for this entity."""
        self.settings = dx_cfg
        """Global settings section of the YAML configuration."""
        self.jinja2env = jjenv
        self.configdata = {}
        """Retrieved configuration data that is passed to the template."""

    def init_entity(self):
        """Hook for provider specific initialization.

        This is called to initialize any specific initialization before starting to retrieve the configuration.
        Subclasses may override this to perform setup work.
        """
        pass

    def ignore_ssl(self) -> bool:
        """Whether SSL certificate verification should be skipped for this entity.

        True when either the global ignore is active (the ``--ssl-cert-ignore``
        / ``-i`` command line flag, or ``sslcert: ignore`` in the global
        ``settings:`` section — both end up in ``settings['ignore_ssl']``),
        or the entity itself declares ``sslcert: ignore`` in its YAML block.

        :returns: True if requests for this entity must not verify the
            server certificate.
        """
        if self.settings.get("ignore_ssl"):
            return True
        sslcert = self.entity_cfg.get("sslcert") if isinstance(self.entity_cfg, dict) else None
        return isinstance(sslcert, str) and sslcert.strip().lower() == "ignore"

    @abstractmethod
    def retrieve_config(self):
        """Perform the actual configuration retrieval.

        Must be implemented and does the actual conversion of a config into one JSON file.
        The resulting data is expected to be stored in :attr:`configdata`.
        """
        pass

    def render_config(self):
        """Render the configuration to a Markdown file.

        This performs the Jinja2 template rendering and writes the output to
        the configured output path using the entity name as the file name.

        :raises RenderError: if the output file cannot be written or the
            template fails to render.
        """
        logging.info(f"Render configuration using {self.entity_cfg['template']}")
        try:
            rendered = self.jinja2template.render(
                data=self.configdata, settings=self.settings, entityname=self.entityname
            )
        except DoxtruderError:
            raise
        except Exception as e:
            raise RenderError.wrap(
                e,
                f"The template '{self.entity_cfg['template']}' failed to render.",
                entity=self.entityname,
                hint=(
                    "Check that the template matches the JSON structure returned "
                    "by the 'endpoints:' of this entity, and that the data the "
                    "template expects was actually retrieved."
                ),
            )
        try:
            with open(
                f"{self.settings['outputpath']}/{self.entityname}.md", "w", encoding="utf-8"
            ) as outputfile:
                outputfile.write(rendered)
        except OSError as e:
            raise RenderError.wrap(
                e,
                f"Cannot write the document for entity '{self.entityname}'.",
                entity=self.entityname,
                hint=(
                    "Check that 'outputpath:' in the global 'settings:' section "
                    "points to a writable directory."
                ),
            )
        return

    def expand_json_path(self, endpoint_url: str, source_json: dict) -> dict:
        """
        An entity setting may use JSONPath to cover multiple values at once using
        variables from previous calls (e.g. first: read user list, then user details for each user)
        To achieve this, the original endpoint will be expanded to a dictionary - if no placeholders
        are used, it's a one element dictionary, otherwise the new endpoints will be created using the
        JSON Path (e.g. /user/$jpath{$.users[*].username/details/} results in one line per username)
        The JSON Path will be applied to the data which has already been collected.
        When a URL contains more than one ``$jpath{...}`` placeholder, all
        expressions are expanded and combined (cartesian product of matches).
        If an expression matches nothing, an :class:`EndpointError` is raised
        so the failure is logged instead of the endpoint silently disappearing.
        """
        endpoints_expanded = dict()

        json_path_expression = re.findall(r"\$jpath\{(.+?)\}", endpoint_url)

        # JPath expression(s) found: expand each and combine the results
        if len(json_path_expression) > 0:
            # candidates are (key, url) pairs; each expression is substituted
            # across all current candidates (cartesian product of matches).
            # With one expression the key is the matched value, e.g. the
            # owner login; with several they are joined with '/'.
            candidates = [("", endpoint_url)]
            for expr in json_path_expression:
                try:
                    jsonpath_parse_result = parse(expr)
                except JSONPathError as e:
                    raise EndpointError.wrap(
                        e,
                        f"Invalid JSONPath expression '{expr}' in the endpoint URL.",
                        entity=self.entityname,
                        endpoint=endpoint_url,
                        hint=(
                            "Fix the '$jpath{...}' expression. Use JSONPath "
                            "syntax like '$jpath{$.<key>[*].<key>}' referring to "
                            "data retrieved by an earlier endpoint."
                        ),
                    ) from e
                try:
                    jsonpath_results = jsonpath_parse_result.find(source_json)
                except Exception as e:
                    raise EndpointError.wrap(
                        e,
                        f"JSONPath expression '{expr}' did not match the retrieved data.",
                        entity=self.entityname,
                        endpoint=endpoint_url,
                        hint=(
                            "Make sure the endpoint that provides this data is listed "
                            "before the endpoint using $jpath{...}, and check the "
                            "path expression."
                        ),
                    ) from e
                if not jsonpath_results:
                    raise EndpointError(
                        f"The JSONPath expression '{expr}' matched no data, so "
                        f"the endpoint cannot be expanded.",
                        entity=self.entityname,
                        endpoint=endpoint_url,
                        hint=(
                            "The data retrieved by the earlier endpoint does not "
                            "contain the keys of this path. Check the path "
                            "expression and the response of the endpoint that "
                            "provides it (see the stored JSON with --store)."
                        ),
                    )
                jsonpath_original_exp = "$jpath{" + expr + "}"
                next_candidates = []
                for key, working_url in candidates:
                    for match in jsonpath_results:
                        # coerce to str: JSONPath may match numbers/bools, and
                        # str.replace would raise TypeError on a non-string value
                        value = str(match.value)
                        new_key = f"{key}/{value}" if key else value
                        # percent-encode the value that goes into the URL so a
                        # space or a reserved character (? # / & ...) in the
                        # matched data cannot corrupt or redirect the request;
                        # the grouping key stays the raw value
                        url_value = quote(value, safe="")
                        next_candidates.append(
                            (new_key, working_url.replace(jsonpath_original_exp, url_value))
                        )
                candidates = next_candidates

            for key, url in candidates:
                if key in endpoints_expanded:
                    logging.warning(
                        f"Endpoint expansion produced a duplicate key '{key}' "
                        f"for URL '{url}'; the second result overwrites the "
                        f"first. Use a JSONPath that yields unique values."
                    )
                endpoints_expanded[key] = url

        # no JPath expression found
        else:
            endpoints_expanded["SINGLE"] = endpoint_url

        return endpoints_expanded

    def render_init(self):
        """Hook called before the rendering is started.

        Subclasses may override this to prepare data just before :meth:`render_config` runs.
        """
        return

    def render_finalize(self):
        """Hook called after rendering has finished.

        Subclasses may override this to perform cleanup once the output is written.
        """
        return

    def do_render_template(self):
        """Drive the full rendering sequence for the entity.

        Loads the entity's Jinja2 template from :attr:`jinja2env` and then runs the
        :meth:`render_init`, :meth:`render_config` and :meth:`render_finalize` steps in order.

        :raises RenderError: if the template entry is missing, cannot be
            found, or contains a syntax error.
        """
        template_name = (
            self.entity_cfg.get("template") if isinstance(self.entity_cfg, dict) else None
        )
        if not template_name:
            raise RenderError(
                "The entity configuration has no 'template:' entry.",
                entity=self.entityname,
                hint=(
                    "Add 'template: <name>.md.jinja' to this entity block; the "
                    "built-in templates are gitea/header/openrouter/opnsense/"
                    "proxmox/requesty/unifi.md.jinja."
                ),
            )

        try:

            self.jinja2template = self.jinja2env.get_template(template_name)
            self.render_init()
            self.render_config()
            self.render_finalize()

        except DoxtruderError:
            raise
        except TemplateNotFound as e:
            raise RenderError(
                f"Jinja2 template '{template_name}' was not found.",
                entity=self.entityname,
                hint=(
                    "Check the 'template:' entry of this entity. Templates are read "
                    "from the 'templatefolder:' setting or the doxtruder package "
                    "(./doxtruder/templates)."
                ),
            ) from e
        except TemplateSyntaxError as e:
            raise RenderError(
                f"Jinja2 template '{template_name}' has a syntax error: {e.message} (line {e.lineno}).",
                entity=self.entityname,
                hint=f"Fix the Jinja2 syntax in the template file '{template_name}'.",
            ) from e
        except Exception as e:
            raise RenderError.wrap(
                e,
                f"Unexpected error while rendering the document for entity '{self.entityname}'.",
                entity=self.entityname,
            )

        return
