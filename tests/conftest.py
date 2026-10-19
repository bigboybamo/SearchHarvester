from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from harvester.httpclient import create_client

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _block_network(respx_mock: respx.MockRouter) -> respx.MockRouter:
    """Every test runs under respx, so an unmocked httpx request fails instead of going out."""
    return respx_mock


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record retry backoff delays instead of sleeping through them."""
    delays: list[float] = []
    monkeypatch.setattr("harvester.httpclient._sleep", delays.append)
    return delays


def load_fixture(name: str) -> bytes:
    """Raw bytes of `tests/fixtures/{name}`."""
    return (FIXTURES / name).read_bytes()


@pytest.fixture
def appsettings_bytes() -> bytes:
    """A snapshot of JobSearchBuilder's live appsettings.json (taken 2026-10-16)."""
    return load_fixture("appsettings.json")


@pytest.fixture(scope="session")
def http() -> Iterator[httpx.Client]:
    """One project-configured HTTP client for the whole run; respx still intercepts its requests.

    Shared because building a client loads the CA bundle, which costs most of a second on Windows.
    """
    with create_client() as client:
        yield client
