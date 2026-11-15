"""Map arbitrary exceptions to a stable message key. Raw errors never reach users or the DB."""

from __future__ import annotations


def classify_error(error: BaseException) -> str:
    name = type(error).__name__
    status = getattr(error, "status_code", None)
    if status == 429 or name == "RateLimitError":
        return "error_rate_limited"
    if status in (401, 403) or name in ("AuthenticationError", "PermissionDeniedError"):
        return "error_gateway_auth"
    if status in (400, 404, 422) or name in ("BadRequestError", "NotFoundError", "UnprocessableEntityError"):
        return "error_gateway_request"
    if (isinstance(status, int) and status >= 500) or name in (
        "APIConnectionError", "APITimeoutError", "InternalServerError", "TimeoutError", "ConnectError", "ReadTimeout",
    ):
        return "error_gateway_unavailable"
    if isinstance(error, TimeoutError | ConnectionError):
        return "error_gateway_unavailable"
    return "run_failed"
