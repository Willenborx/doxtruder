"""Tests for the custom exception classes in doxtruder.Exceptions.

The hierarchy (rewritten) is a common :class:`DoxtruderError` base with four
categories — :class:`EngineError`, :class:`EntityError`,
:class:`EndpointError` and :class:`RenderError` — each carrying a ``message``,
a configuration ``hint``, an ``entity``/``endpoint`` context and a uniform
:meth:`~DoxtruderError.log` line.
"""

import logging

import pytest

from doxtruder.Exceptions import (
    DoxtruderError,
    EndpointError,
    EngineError,
    EntityError,
    RenderError,
    is_ssl_verification_error,
)


def test_engine_error_is_exception_with_message():
    e = EngineError("boom")
    assert isinstance(e, DoxtruderError)
    assert isinstance(e, Exception)
    assert e.message == "boom"
    assert str(e) == "boom"


def test_engine_error_is_catchable():
    with pytest.raises(EngineError) as excinfo:
        raise EngineError("fatal")
    assert excinfo.value.message == "fatal"


def test_entity_error_stores_message():
    e = EntityError("entity failed")
    assert e.message == "entity failed"
    assert str(e) == "entity failed"


def test_endpoint_error_stores_message():
    e = EndpointError("server said 500")
    assert e.message == "server said 500"
    assert str(e) == "server said 500"


def test_render_error_stores_message():
    e = RenderError("template blew up")
    assert e.message == "template blew up"


def test_categories_are_distinct_from_base():
    for cls in (EngineError, EntityError, EndpointError, RenderError):
        assert issubclass(cls, DoxtruderError)
    # the four categories are siblings, not subclasses of one another
    assert not issubclass(EntityError, EngineError)
    assert not issubclass(EndpointError, EntityError)


def test_hint_defaults_to_category_default():
    assert EngineError("x").hint == EngineError.default_hint
    assert EntityError("x", hint="custom").hint == "custom"


def test_context_builds_entity_and_endpoint_prefix():
    assert EndpointError("x", entity="prox", endpoint="version").context() == "prox/version"
    assert EntityError("x", entity="prox").context() == "prox"
    assert EntityError("x").context() == ""


def test_wrap_preserves_original_as_cause():
    cause = ValueError("bad json")
    err = EndpointError.wrap(cause, "could not parse", entity="prox", endpoint="/x")
    assert isinstance(err, EndpointError)
    assert err.__cause__ is cause
    assert err.entity == "prox"
    assert err.endpoint == "/x"


def test_log_emits_uniform_line_with_action_and_hint(caplog):
    with caplog.at_level(logging.WARNING):
        EndpointError("boom", entity="prox", endpoint="version").log()
    line = caplog.text
    assert "EndpointError/prox/version" in line
    assert "boom" in line
    assert "skip endpoint" in line
    assert "Fix:" in line


def test_log_level_follows_category(caplog):
    with caplog.at_level(logging.DEBUG):
        EngineError("fatal").log()
    assert any(r.levelno == logging.ERROR for r in caplog.records)


# --------------------------------------------------------------------------
# is_ssl_verification_error
# --------------------------------------------------------------------------


def test_is_ssl_verification_error_detects_direct_ssl_error():
    import ssl

    import requests

    assert is_ssl_verification_error(requests.exceptions.SSLError("boom")) is True
    assert is_ssl_verification_error(ssl.SSLCertVerificationError("boom")) is True


def test_is_ssl_verification_error_walks_cause_chain():
    """requests frequently surfaces a TLS failure as a plain ConnectionError
    wrapping the raw ssl.SSLCertVerificationError; the detector must find the
    SSL error through __cause__ (and __context__) instead of only checking the
    outermost exception."""
    import ssl

    import requests

    inner = requests.exceptions.ConnectionError("N/A")
    inner.__cause__ = ssl.SSLCertVerificationError("certificate verify failed")
    assert is_ssl_verification_error(inner) is True

    ctx = requests.exceptions.ConnectionError("N/A")
    try:
        raise ssl.SSLError("verify failed")
    except ssl.SSLError:
        try:
            raise ctx
        except requests.exceptions.ConnectionError as e:
            assert is_ssl_verification_error(e) is True


def test_is_ssl_verification_error_false_for_plain_error():
    import requests

    assert is_ssl_verification_error(requests.exceptions.ConnectionError("down")) is False
    assert is_ssl_verification_error(ValueError("nope")) is False


def test_is_ssl_verification_error_survives_cause_cycle():
    """A pathological exception cycle (a.__cause__ -> b.__cause__ -> a) must
    not hang the chain walk."""
    a = RuntimeError("a")
    b = RuntimeError("b")
    a.__cause__ = b
    b.__cause__ = a
    assert is_ssl_verification_error(a) is False
