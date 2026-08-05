"""Console-layer errors carrying the HTTP status the API layer should emit.

Service modules (inventory/derive/chunklab) stay FastAPI-free; they raise these
and ``app.py`` maps them onto responses.
"""

from __future__ import annotations


class ConsoleError(Exception):
    """Base console error; subclasses pin the HTTP status code."""

    status_code = 500
    code = "console_error"


class BadRequest(ConsoleError):
    status_code = 400
    code = "bad_request"


class NotFound(ConsoleError):
    status_code = 404
    code = "not_found"


class Conflict(ConsoleError):
    status_code = 409
    code = "conflict"


class SetupRequired(ConsoleError):
    """The local Console has not received a usable runtime configuration yet."""

    status_code = 409
    code = "setup_required"
