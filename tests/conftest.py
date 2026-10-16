from pathlib import Path

import pytest
import respx

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


@pytest.fixture
def appsettings_bytes() -> bytes:
    """A snapshot of JobSearchBuilder's live appsettings.json (taken 2026-10-16)."""
    return (FIXTURES / "appsettings.json").read_bytes()
