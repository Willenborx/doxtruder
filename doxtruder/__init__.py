"""Doxtruder: generate Markdown documentation from live system configurations.

This package contains the engine (:mod:`doxtruder.DoxtruderEngine`), the abstract
provider interface (:mod:`doxtruder.ConfigProvider`), the shared REST base class
(:mod:`doxtruder.RESTProvider`), the concrete providers for each supported system and
the exception hierarchy (:mod:`doxtruder.Exceptions`).
"""

__version__ = "0.1.0"
"""The current doxtruder version (PEP 440). Kept in sync with the
``version`` field in ``pyproject.toml`` via dynamic versioning."""
