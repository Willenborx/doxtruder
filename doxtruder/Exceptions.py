"""Exception hierarchy used in Doxtruder.

Every Doxtruder error derives from :class:`DoxtruderError` and belongs to
exactly one of four categories, which define how the engine reacts to the
failure:

- :class:`EngineError`   -- fatal, aborts the whole run immediately.
- :class:`EntityError`   -- the current entity is skipped, the next entity
  is processed.
- :class:`EndpointError` -- a single API endpoint of the current entity is
  skipped, retrieval continues with the next endpoint.
- :class:`RenderError`   -- rendering of the current entity failed, the next
  entity is processed.

Every exception carries a human readable ``message`` describing *what* went
wrong and a ``hint`` describing *what the user can change in the
configuration* to fix it. Call :meth:`DoxtruderError.log` to emit a single
consistent log line that contains both, together with the category and the
entity/endpoint context captured at raise time.

Foreign exceptions (``KeyError``, ``JSONPathError``, ``requests`` errors,
``TemplateNotFound`` ...) must never escape a provider: convert them with the
:class:`DoxtruderError.wrap` classmethod, which keeps the original exception
as ``__cause__`` for debugging.
"""

import logging


def is_ssl_verification_error(exc: BaseException) -> bool:
    """Check whether an exception (or its cause chain) is a TLS certificate error.

    Mirrors the detection used by :class:`doxtruder.RESTProvider.RESTProvider`
    so the same message and ``-i`` hint are produced everywhere. The
    ``__cause__``/``__context__`` chain is walked because requests often
    surfaces a TLS failure as a plain ``ConnectionError`` wrapping the raw
    :class:`ssl.SSLCertVerificationError`.

    :param exc: The exception to inspect.
    :returns: True if the error was caused by an untrusted or invalid SSL
        certificate (e.g. a self-signed host).
    """
    import ssl

    import requests

    ssl_errors = (
        requests.exceptions.SSLError,
        ssl.SSLError,
        ssl.SSLCertVerificationError,
    )
    # walk the cause/context chain; 'seen' guards against pathological
    # exception cycles (a.__cause__ -> b.__cause__ -> a)
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, ssl_errors):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ if exc.__cause__ is not None else exc.__context__
    return False


class DoxtruderError(Exception):
    """Abstract base class for all Doxtruder errors.

    :param message: Human readable description of what went wrong.
    :param hint: Optional advice on how the user can fix the issue in the
        configuration. Falls back to the class level :attr:`default_hint`
        when omitted.
    :param entity: Name of the entity in whose context the error occurred.
    :param endpoint: Name or URL of the endpoint in whose context the error
        occurred.
    """

    #: Short label describing the engine's reaction to this category.
    action = "abort processing"
    #: Suggestion shown when the raise site does not provide a specific hint.
    default_hint = "Check the doxtruder.yaml configuration."
    #: Log level used by :meth:`log` for this category.
    log_level = logging.ERROR

    def __init__(
        self,
        message: str,
        hint: str | None = None,
        entity: str | None = None,
        endpoint: str | None = None,
    ):
        self.message = message
        self.hint = hint or self.default_hint
        self.entity = entity
        self.endpoint = endpoint
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message

    def context(self) -> str:
        """Return the entity/endpoint context as a short prefix string.

        :returns: e.g. ``"Proxmox-Main/api/version"``, ``"Proxmox-Main"`` or
            ``""`` when no context is known.
        """
        if self.entity and self.endpoint:
            return f"{self.entity}/{self.endpoint}"
        if self.entity:
            return self.entity
        return ""

    def log(self, level: int | None = None) -> None:
        """Log this error once, in a uniform format.

        The line contains the category, the context, the message and the
        configuration hint, so a single log entry tells the user what
        happened *and* what to do about it. The original exception (if this
        error was wrapped) is attached as ``exc_info`` at debug level.

        :param level: Optional log level override; defaults to the level of
            the exception category.
        """
        ctx = self.context()
        prefix = f"[{type(self).__name__}{'/' + ctx if ctx else ''}] "
        text = f"{prefix}{self.message} | {self.action} | Fix: {self.hint}"
        logging.log(level if level is not None else self.log_level, text)
        if self.__cause__ is not None:
            logging.debug("Original exception:", exc_info=self.__cause__)

    @classmethod
    def wrap(
        cls,
        exc: BaseException,
        message: str,
        hint: str | None = None,
        entity: str | None = None,
        endpoint: str | None = None,
    ) -> "DoxtruderError":
        """Convert a foreign exception into the appropriate Doxtruder error.

        Use as ``raise EngineError.wrap(exc, ...)`` (or any subclass) so the
        original traceback stays available for debugging while the engine
        only ever sees :class:`DoxtruderError` categories.

        :param exc: The original (foreign) exception to wrap.
        :param message: Human readable description of what went wrong.
        :param hint: Configuration advice; defaults to the category hint.
        :param entity: Entity name context.
        :param endpoint: Endpoint name/URL context.
        :returns: A new instance of ``cls`` with ``exc`` set as its cause.
        """
        err = cls(message, hint=hint, entity=entity, endpoint=endpoint)
        err.__cause__ = exc
        return err


class EngineError(DoxtruderError):
    """Fatal error: Doxtruder cannot continue and aborts the whole run.

    Reserved for problems that affect every entity, e.g. a broken global
    ``settings:`` section or an unusable output directory.

    :param message: Human readable description of what went wrong.
    :param hint: Optional fix advice for the log message.
    :param entity: Unused for engine errors, kept for a uniform signature.
    :param endpoint: Unused for engine errors, kept for a uniform signature.
    """

    action = "fatal - doxtruder stops"
    default_hint = "Check the global 'settings:' section of the doxtruder " "configuration file."
    log_level = logging.ERROR


class EntityError(DoxtruderError):
    """The entity cannot be processed and is skipped; the next entity runs.

    Raised when entity-level configuration is missing or unusable, e.g. an
    unknown provider module or a missing ``baseURL``/``endpoints`` entry.

    :param message: Human readable description of what went wrong.
    :param hint: Optional fix advice for the log message.
    :param entity: Name of the entity being processed.
    :param endpoint: Unused for entity errors, kept for a uniform signature.
    """

    action = "skip entity, continue with next entity"
    default_hint = (
        "Check this entity's block in the 'entities:' section of "
        "the doxtruder configuration file."
    )
    log_level = logging.WARNING


class EndpointError(DoxtruderError):
    """One API endpoint of an entity failed; retrieval continues with the
    next endpoint and the document is still rendered with the remaining data.

    Raised for HTTP errors, connection problems, bad JSON responses or
    unresolvable URL placeholders on a single endpoint.

    :param message: Human readable description of what went wrong.
    :param hint: Optional fix advice for the log message.
    :param entity: Name of the entity being processed.
    :param endpoint: Name or URL of the failing endpoint.
    """

    action = "skip endpoint, continue with next endpoint"
    default_hint = (
        "Check the 'endpoints:' entry of this entity (URL, "
        "authentication headers/credentials) in the doxtruder "
        "configuration file."
    )
    log_level = logging.WARNING


class RenderError(DoxtruderError):
    """Rendering the collected data of one entity failed; the next entity runs.

    Raised when the Jinja2 template is missing, contains syntax errors, or
    fails while producing the document for an entity.

    :param message: Human readable description of what went wrong.
    :param hint: Optional fix advice for the log message.
    :param entity: Name of the entity whose document failed to render.
    :param endpoint: Unused for render errors, kept for a uniform signature.
    """

    action = "skip rendering of this entity, continue with next entity"
    default_hint = (
        "Check the 'template:' entry of this entity and the "
        "'templatefolder:' setting in the doxtruder "
        "configuration file."
    )
    log_level = logging.WARNING
