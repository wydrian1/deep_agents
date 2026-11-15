"""Map arbitrary exceptions to a stable message key and an HTTP status. Raw errors never reach users or the DB."""

from __future__ import annotations

_UNAVAILABLE_HINTS = ("Connection", "ConnectError", "Timeout", "InternalServerError", "ServiceUnavailable")


def _chain(error: BaseException | None) -> list[BaseException]:
    chain: list[BaseException] = []
    while error is not None and error not in chain and len(chain) < 6:
        chain.append(error)
        error = error.__cause__ or error.__context__
    return chain


def http_status(error: BaseException) -> int | None:
    """HTTP status of the failed call, from the exception or its ``response`` (looking through causes)."""
    for item in _chain(error):
        status = getattr(item, "status_code", None)
        if not isinstance(status, int):
            status = getattr(getattr(item, "response", None), "status_code", None)
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            return status
    return None


def classify_error(error: BaseException) -> str:
    """Classify by HTTP status first, then by exception name, looking through wrapped causes."""
    chain = _chain(error)
    statuses = [s for e in chain if isinstance(s := getattr(e, "status_code", None), int)]
    if (status := http_status(error)) is not None:
        statuses.append(status)
    names = [type(e).__name__ for e in chain]
    if 429 in statuses or "RateLimitError" in names:
        return "error_rate_limited"
    if any(s in (401, 403) for s in statuses) or {"AuthenticationError", "PermissionDeniedError"} & set(names):
        return "error_gateway_auth"
    if any(s in (400, 404, 422) for s in statuses) or {"BadRequestError", "NotFoundError", "UnprocessableEntityError"} & set(names):
        return "error_gateway_request"
    if any(s >= 500 for s in statuses) or any(h in name for name in names for h in _UNAVAILABLE_HINTS):
        return "error_gateway_unavailable"
    if any(isinstance(e, TimeoutError | ConnectionError) for e in chain):
        return "error_gateway_unavailable"
    return "run_failed"
