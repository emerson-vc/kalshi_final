"""Typed Kalshi API errors."""

from __future__ import annotations

from typing import Any


class KalshiError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None, details: Any = None, body: Any = None):
        super().__init__(f"HTTP {status}: {code or ''} {message}".strip())
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.body = body


class BadRequest(KalshiError):
    pass


class Unauthorized(KalshiError):
    pass


class Forbidden(KalshiError):
    pass


class NotFound(KalshiError):
    pass


class Conflict(KalshiError):
    pass


class RateLimited(KalshiError):
    pass


class ServerError(KalshiError):
    pass


class NotConfigured(Exception):
    """Authenticated call attempted without credentials."""


def from_response(status: int, body: Any) -> KalshiError:
    code = None
    message = ""
    details = None
    if isinstance(body, dict):
        err = body.get("error", body)
        if isinstance(err, dict):
            code = err.get("code")
            message = err.get("message") or ""
            details = err.get("details")
        elif isinstance(err, str):
            message = err
    elif isinstance(body, str):
        message = body[:500]
    cls: type[KalshiError] = {
        400: BadRequest,
        401: Unauthorized,
        403: Forbidden,
        404: NotFound,
        409: Conflict,
        429: RateLimited,
    }.get(status, ServerError if status >= 500 else KalshiError)
    return cls(status, message or f"status {status}", code=code, details=details, body=body)
