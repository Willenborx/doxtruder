"""Command-line interface entry point for Doxtruder.

Parses command-line arguments via Click, loads environment variables from .env,
resolves ``${VAR}`` placeholders in configuration YAML files, configures logging,
and invokes :func:`doxtruder.DoxtruderEngine.run_doxtruder`.

The module is installed as the ``doxtruder`` console script (see
``pyproject.toml``).
"""

import logging
import os
import re

import click
import yaml
from dotenv import load_dotenv

from doxtruder import __version__
from doxtruder.DoxtruderEngine import STATUS_ERROR, STATUS_OK, run_doxtruder
from doxtruder.Exceptions import DoxtruderError, EngineError

PARAM = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def resolve(value):
    """Recursively resolve environment variable references in YAML structures.

    Finds placeholders matching ``${VAR}`` in strings, lists, or dictionaries
    and substitutes them with corresponding values from ``os.environ``.
    Unset variables are left in place and reported with a warning, because a
    literal ``${VAR}`` in a URL or header fails confusingly later (e.g. as a
    401 from the API).

    :param value: The YAML node (dict, list, string, or scalar) to resolve.
    :returns: Resolved YAML node with environment variables interpolated.
    """
    if isinstance(value, dict):
        return {k: resolve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v) for v in value]
    if isinstance(value, str):
        missing: set[str] = set()

        def substitute(match: re.Match) -> str:
            name = match.group(1)
            env_value = os.environ.get(name)
            if env_value is None:
                missing.add(name)
                return match.group(0)
            return env_value

        resolved = PARAM.sub(substitute, value)
        if missing:
            logging.warning(
                "Environment variable(s) not set: "
                + ", ".join(sorted(missing))
                + " (placeholder left as-is; define it in the .env file)"
            )
        return resolved
    return value


@click.command()
@click.pass_context
@click.option(
    "--config",
    "-c",
    default="./doxtruder.yaml",
    help="Configuration file (defaults to doxtruder.yaml)",
)
@click.option(
    "--loglevel",
    "-v",
    default=4,
    help="1:critical, 2:error, 3:warning, 4:info, 5:debug",
    type=click.IntRange(1, 5),
)
@click.option(
    "--env",
    "-e",
    default=".env",
    help="environment file with <KEY>=<VALUE> definitions (defaults "
    "to .env), will replace ${<KEY>} in config by <VALUE>",
)
@click.option(
    "--exception",
    "-x",
    is_flag=True,
    hidden=True,
    help="if set, Exceptions are reraised to see the stack trace. " "For developers",
)
@click.option(
    "--store",
    "-s",
    is_flag=True,
    help="Store the JSON files in the output path. A folder with the " "timestamp will be created",
)
@click.option(
    "--templates",
    "-t",
    help="Set the path to the templates (recommended: use a copy of " "the original templates)",
)
@click.option(
    "--ssl-cert-ignore",
    "-i",
    is_flag=True,
    help="Ignore SSL certificate errors (do not verify server "
    "certificates, e.g. for self-signed hosts)",
)
@click.version_option(version=__version__, prog_name="doxtruder")
def main(
    ctx,
    config: str,
    loglevel: int,
    env: str,
    exception: bool,
    store: bool,
    templates: str,
    ssl_cert_ignore: bool,
):
    """Click command line interface entry point.

    Doxtruder collects configurations from different sources and converts the
    (json) data using Jinja2 templates (converts to md by default).

    (c) 2026 Holger Willenborg, development supported by AI

    \f

    :param config: Path to the configuration YAML file.
    :param loglevel: Logging verbosity level (1 to 5).
    :param env: Path to the .env environment variables file.
    :param exception: Reraise exceptions on failure when set.
    :param store: Store intermediate JSON configuration files when set.
    :param templates: Path to custom template folder.
    :param ssl_cert_ignore: Skip SSL certificate verification when set.
    :returns: Execution status code (:data:`STATUS_OK` or :data:`STATUS_ERROR`).
    """
    overall_status = STATUS_OK
    loglevels = (logging.CRITICAL, logging.ERROR, logging.WARNING, logging.INFO, logging.DEBUG)
    logformat = (
        "%(asctime)s|%(levelname)s|%(message)s"
        if (loglevel < 5)
        else "%(asctime)s|%(levelname)s|%(filename)s(%(lineno)d)|" "%(funcName)s|%(message)s"
    )

    logging.basicConfig(level=loglevels[loglevel - 1], format=logformat)
    logging.debug(f"Set log level to {loglevel}")

    try:
        # load .env file
        if not os.path.exists(env):
            err = f"Environment file {env} does not exist"
            if env == ".env":
                logging.warning(err)
            else:
                raise EngineError(
                    err,
                    hint=("Create the file, or set --env/-e to an existing " "<KEY>=<VALUE> file."),
                )
        else:
            logging.info(f"Reading ENV Vars from {env}")

        load_dotenv(env)

        logging.info(
            f"Opening config file: {config}"
            + (" (use --config to use different file)" if (config == "./doxtruder.yaml") else "")
        )

        try:
            with open(config, encoding="utf-8") as stream:
                logging.debug("reading config")
                configyaml = yaml.safe_load(stream)
        except FileNotFoundError as exc:
            raise EngineError.wrap(
                exc,
                f"The configuration file '{config}' was not found.",
                hint="Use --config/-c to point to an existing doxtruder YAML " "file.",
            )
        except IsADirectoryError as exc:
            raise EngineError.wrap(
                exc,
                f"'{config}' is a directory, not a configuration file.",
                hint="Use --config/-c to point to the doxtruder YAML file " "itself.",
            )
        except PermissionError as exc:
            raise EngineError.wrap(
                exc,
                f"No permission to read the configuration file '{config}'.",
                hint="Check the file permissions of the configuration file.",
            )
        except yaml.YAMLError as exc:
            raise EngineError.wrap(
                exc,
                f"Error parsing the configuration file '{config}'.",
                hint="Check the YAML syntax (indentation, colons, quotes) of "
                "the configuration file.",
            )

        if not isinstance(configyaml, dict):
            raise EngineError(
                f"The configuration file '{config}' does not contain a YAML " "mapping.",
                hint="The file must start with a top-level 'settings:' section.",
            )

        logging.debug("Resolving ${ENV} variables and run doxtruder")
        yamlsettings = resolve(configyaml)
        if templates:
            if not isinstance(yamlsettings.get("settings"), dict):
                raise EngineError(
                    "The configuration has no valid top-level 'settings:' " "section.",
                    hint="Add a 'settings:' section before using --templates/-t.",
                )
            yamlsettings["settings"]["templatefolder"] = templates
        overall_status = run_doxtruder(yamlsettings, store=store, ignore_ssl=ssl_cert_ignore)

    # reraise exceptions if command line option -x is used (dev mode)

    except DoxtruderError as exc:
        exc.log()
        if isinstance(exc, EngineError):
            overall_status = STATUS_ERROR
        if exception:
            raise exc

    # Click 8.x standalone mode discards a command's return value, so the
    # process would always exit 0. ctx.exit() propagates the status as the
    # real process exit code so scripts/CI can detect a failed run.
    ctx.exit(overall_status)


if __name__ == "__main__":
    main()
