"""Core engine and execution loop for Doxtruder.

This module implements :func:`run_doxtruder`, which iterates through all entities
defined in the configuration YAML, dynamically loads the requested provider
class, invokes configuration retrieval and triggers Jinja2 template rendering.
It also tracks execution status levels (OK, WARNING, ERROR).
"""

import importlib
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, PackageLoader, select_autoescape

from doxtruder.ConfigProvider import default_json_serializer
from doxtruder.Exceptions import (
    EndpointError,
    EngineError,
    EntityError,
    RenderError,
)

STATUS_OK = 0
"""Status code indicating successful execution without issues."""
STATUS_WARNING = 1
"""Status code indicating execution completed with non-fatal warnings."""
STATUS_ERROR = 2
"""Status code indicating execution encountered fatal errors."""


def _read_settings(dxconfig: dict) -> dict:
    """Validate and return the global ``settings:`` section.

    :param dxconfig: Parsed doxtruder configuration dictionary.
    :returns: The settings dictionary.
    :raises EngineError: if the section is missing or not a mapping.
    """
    settings = dxconfig.get("settings") if isinstance(dxconfig, dict) else None
    if not isinstance(settings, dict):
        raise EngineError("The configuration has no valid top-level 'settings:' section.")
    return settings


def _create_output_paths(settings: dict, store: bool) -> Path | None:
    """Create the output directory (and timestamped store folder if asked).

    :param settings: Global settings section of the configuration.
    :param store: Whether intermediate JSON dumps should be stored.
    :returns: The timestamped store folder, or ``None`` when ``store`` is false.
    :raises EngineError: if the output path cannot be created.
    """
    outputpath = settings.get("outputpath", "./output/")
    # reject a non-path value (e.g. a YAML list, int or null) with a clear
    # error instead of letting pathlib raise a raw TypeError that escapes the
    # whole run and crashes the CLI with a traceback
    if not isinstance(outputpath, (str, os.PathLike)):
        raise EngineError(
            f"'outputpath:' must be a filesystem path string, not " f"{type(outputpath).__name__}.",
            hint=(
                "Set 'outputpath:' in the global 'settings:' section to a "
                "path string like './output' or '~/output'."
            ),
        )
    # normalize to str, then expand a leading '~' — pathlib does NOT do this,
    # so '~/output' would otherwise become a literal '<cwd>/~/output'
    outputpath = os.path.expanduser(str(outputpath))
    # write the (possibly defaulted/expanded) value back so providers that read
    # settings['outputpath'] directly (ConfigProvider.render_config) see it
    settings["outputpath"] = outputpath
    # include seconds so two runs within the same minute do not overwrite each
    # other's JSON dumps in the shared timestamp folder
    folder = Path(outputpath) / datetime.now().strftime("%Y-%m-%d-%H-%M-%S")
    try:
        Path(outputpath).mkdir(parents=True, exist_ok=True)
        if store:
            folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise EngineError.wrap(
            e,
            f"Cannot create the output directory '{outputpath}'.",
            hint=(
                "Set 'outputpath:' in the global 'settings:' section to a "
                "writable directory and check file permissions."
            ),
        )
    except ValueError as e:
        # e.g. a NUL byte in the path; echo no value to keep the log clean
        raise EngineError.wrap(
            e,
            "Invalid output directory path (contains an illegal character " "such as a NUL byte).",
            hint=(
                "Remove control characters from 'outputpath:' in the global " "'settings:' section."
            ),
        )
    return folder if store else None


def _create_jinja_env(settings: dict) -> Environment:
    """Build the shared Jinja2 environment for template rendering.

    :param settings: Global settings section of the configuration.
    :returns: Ready to use Jinja2 environment.
    :raises EngineError: if the template folder cannot be used.
    """
    templatefolder = settings.get("templatefolder")
    # same '~' expansion as for outputpath: FileSystemLoader does not expand it
    if isinstance(templatefolder, str):
        templatefolder = os.path.expanduser(templatefolder)
    try:
        if templatefolder:
            loader = FileSystemLoader(templatefolder)
            logging.info(f"Load templates from {templatefolder}")
        else:
            loader = PackageLoader("doxtruder")
            logging.info("Load templates from doxtruder package (./doxtruder/templates)")

        if logging.root.level <= logging.INFO:
            for tplname in loader.list_templates():
                logging.info(f"Found template {tplname}")

        jinja2env = Environment(
            loader=loader,
            autoescape=select_autoescape(),
            extensions=["jinja2.ext.do"],
        )
        jinja2env.globals["now"] = datetime.now
    except Exception as e:
        raise EngineError.wrap(
            e,
            "Cannot initialise the Jinja2 template environment.",
            hint=(
                "Check the 'templatefolder:' entry in the global 'settings:' "
                "section (or omit it to use the built-in templates)."
            ),
        )
    return jinja2env


def _load_provider(entity: str, entity_cfg: dict) -> type:
    """Resolve the provider class named in an entity's ``provider:`` entry.

    :param entity: Name of the entity being processed.
    :param entity_cfg: Configuration dictionary of the entity.
    :returns: The provider class to instantiate.
    :raises EntityError: if the entry is missing or the module/class fails to
        import.
    """
    mod = entity_cfg.get("provider") if isinstance(entity_cfg, dict) else None
    if not mod:
        raise EntityError(
            f"Entity '{entity}' has no 'provider:' entry.",
            entity=entity,
            hint=(
                "Add 'provider: <ProviderClass>' to this entity, e.g. " "'provider: RESTProvider'."
            ),
        )
    try:
        module = importlib.import_module("doxtruder." + mod)
        return getattr(module, mod)
    except ModuleNotFoundError as e:
        raise EntityError.wrap(
            e,
            f"Provider module 'doxtruder.{mod}' does not exist.",
            entity=entity,
            hint=(
                "Fix the 'provider:' entry of this entity. Valid values are "
                "the module names in the doxtruder package, e.g. "
                "RESTProvider, OpenrouterProvider."
            ),
        )
    except AttributeError as e:
        raise EntityError.wrap(
            e,
            f"Class '{mod}' is not defined in module 'doxtruder.{mod}'.",
            entity=entity,
            hint=(
                "The 'provider:' entry must name a module and a class of "
                "the same name inside the doxtruder package."
            ),
        )


# main engine iterates throught yaml "entities"
# store: True - store files in <timestamp>\entity.json
def run_doxtruder(dxconfig, store: bool, ignore_ssl: bool = False):
    """Run the main doxtruder generation loop.

    Parses global settings, initialises the Jinja2 environment, iterates over
    all configured entities, instantiates the specified provider modules,
    retrieves configuration data, optionally writes the extracted JSON to output files,
    and renders Markdown documentation.

    Failure handling follows the categories defined in
    :mod:`doxtruder.Exceptions`: an :class:`~doxtruder.Exceptions.EngineError`
    is fatal and aborts the run, an
    :class:`~doxtruder.Exceptions.EntityError` or
    :class:`~doxtruder.Exceptions.RenderError` skips the current entity, and an
    :class:`~doxtruder.Exceptions.EndpointError` escaping a provider is caught
    per entity and downgraded to a warning (the entity is skipped, the run
    continues). Any other unexpected exception is converted to an
    :class:`~doxtruder.Exceptions.EntityError`.

    :param dxconfig: Dictionary containing the parsed doxtruder configuration YAML.
    :param store: Boolean flag; if True, stores intermediate JSON configuration dumps in a timestamped folder.
    :param ignore_ssl: Boolean flag; if True, providers do not verify the SSL
        certificates of the API hosts (equivalent to the ``--ssl-cert-ignore``
        command line option). ``sslcert: ignore`` in the global ``settings:``
        section has the same effect; per-entity ``sslcert: ignore`` is handled
        by :meth:`doxtruder.ConfigProvider.ConfigProvider.ignore_ssl`.
    :returns: Overall execution status code (:data:`STATUS_OK`, :data:`STATUS_WARNING`, or :data:`STATUS_ERROR`).
    :raises EngineError: if a fatal problem makes the whole run impossible.
    """

    # process global settings - fatal if broken, no entity can work then.
    # copy the section so injecting 'ignore_ssl'/'outputpath' below does not
    # mutate the caller's configuration dictionary
    settings = dict(_read_settings(dxconfig))
    # the -i flag and 'sslcert: ignore' in the settings section are equivalent
    settings_sslcert = settings.get("sslcert")
    settings["ignore_ssl"] = bool(ignore_ssl) or (
        isinstance(settings_sslcert, str) and settings_sslcert.strip().lower() == "ignore"
    )
    storefolder = _create_output_paths(settings, store)
    jinja2env = _create_jinja_env(settings)

    overall_status = STATUS_OK

    entities = dxconfig.get("entities")
    if not isinstance(entities, dict) or not entities:
        raise EngineError(
            "The configuration contains no entities.",
            hint="Define at least one entity under the top-level 'entities:' key.",
        )

    logging.info(
        f"Working on Documents for: {settings.get('title', 'Doxtruder')} <----------------------------------------"
    )
    logging.debug(f"Settings {settings}")

    # iterate over the entities and call the class listed in "provider"
    for entity, entity_cfg in entities.items():
        logging.info(f"-------------------- Process: {entity} --------------------")

        try:
            # the entity name doubles as the output file name (<entity>.md /
            # <entity>.json), so it must be a plain file name: a '/' or '..'
            # in the YAML key would write outside the output directory
            if (
                not isinstance(entity, str)
                or not entity.strip()
                or entity in (".", "..")
                or Path(entity).name != entity
            ):
                raise EntityError(
                    f"Entity name '{entity}' cannot be used as an output file " "name.",
                    entity=str(entity),
                    hint=(
                        "Entity keys are used verbatim as '<entity>.md' and "
                        "'<entity>.json' file names, so they must not contain "
                        "'/', '\\' or be '.'/'..'. Rename this entity."
                    ),
                )

            # dynamically instantiate class define in "provider" attribute
            ProviderClass = _load_provider(entity, entity_cfg)
            prov = ProviderClass(entity, entity_cfg, settings, jinja2env)
            prov.init_entity()
            ssl_note = " (ignoring SSL certificate)" if prov.ignore_ssl() else ""
            logging.info(f"Retrieving configuration{ssl_note}")
            retrieve_status = int(prov.retrieve_config() or 0)
            if retrieve_status > overall_status:
                overall_status = retrieve_status

            if storefolder is not None:
                filename = storefolder / entity
                try:
                    with open(f"{filename}.json", "w", encoding="utf-8") as op:
                        json.dump(prov.configdata, op, indent=4, default=default_json_serializer)
                except (OSError, TypeError, ValueError) as e:
                    raise EntityError.wrap(
                        e,
                        f"Cannot store the JSON dump for entity '{entity}'.",
                        entity=entity,
                        hint=(
                            "Remove '--store'/-s or check that the 'outputpath:' "
                            "directory is writable; non-serializable data can "
                            "also cause this."
                        ),
                    )

            prov.do_render_template()

        except EngineError as e:
            # fatal: stop the whole run immediately
            e.log()
            return STATUS_ERROR

        except (EntityError, RenderError, EndpointError) as e:
            e.log()
            overall_status = STATUS_WARNING

        except Exception as e:
            EntityError.wrap(
                e, f"Unexpected error while processing entity '{entity}'.", entity=entity
            ).log()
            overall_status = STATUS_WARNING

    # program finalization

    if overall_status == STATUS_WARNING:
        logging.warning("Process finished with warning(s)")
    else:
        logging.info("Process finished successfully")

    return overall_status
