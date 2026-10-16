"""Shared HTTP plumbing: timeouts, User-Agent and retry with backoff."""

import logging
from collections.abc import Callable
from time import sleep as _sleep

import httpx

from harvester import __version__

logger = logging.getLogger(__name__)

USER_AGENT = (
    f"SearchHarvester/{__version__} (+https://github.com/bigboybamo/JobSearchBuilder/issues/45)"
)
DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER_SECONDS = 60.0


def create_client(*, follow_redirects: bool = True) -> httpx.Client:
    """Return an `httpx.Client` with the project's timeout and User-Agent."""
    return httpx.Client(
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=follow_redirects,
    )


def get_with_retry(
    client: httpx.Client,
    url: str,
    *,
    attempts: int = 3,
    backoff_seconds: float = 1.0,
    sleep: Callable[[float], None] | None = None,
) -> httpx.Response:
    """GET `url`, retrying transport errors and 429/5xx responses with exponential backoff.

    The final response is returned whatever its status; callers decide what counts as failure.
    A transport error on the final attempt is re-raised.
    """
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    sleep = sleep or _sleep

    for attempt in range(1, attempts + 1):
        delay = backoff_seconds * 2 ** (attempt - 1)
        try:
            response = client.get(url)
        except httpx.TransportError as exc:
            if attempt == attempts:
                raise
            logger.warning("GET %s failed (%s); retry %d/%d", url, exc, attempt, attempts - 1)
        else:
            if response.status_code not in RETRY_STATUSES or attempt == attempts:
                return response
            delay = _retry_after(response) or delay
            logger.warning(
                "GET %s returned %d; retry %d/%d",
                url,
                response.status_code,
                attempt,
                attempts - 1,
            )
        sleep(delay)

    raise AssertionError("unreachable")


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds from a numeric `Retry-After` header, capped; `None` if absent or not numeric."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)
