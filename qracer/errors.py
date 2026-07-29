"""Exception taxonomy for qracer.

A small, shared hierarchy so callers can catch qracer's own errors distinctly from
arbitrary third-party exceptions. ``QracerError`` is the common base; specific errors
subclass it. (Historically the only custom exception was ``ConfigParseError`` in the
config loader, which now also subclasses ``QracerError``.)
"""

from __future__ import annotations


class QracerError(Exception):
    """Base class for all errors raised deliberately by qracer."""


class AdapterError(QracerError):
    """A data/LLM adapter could not fulfil a request (network, parse, or API error)."""


class ProviderUnavailable(QracerError):
    """A required provider/capability is not configured or has no usable credentials."""


__all__ = ["AdapterError", "ProviderUnavailable", "QracerError"]
