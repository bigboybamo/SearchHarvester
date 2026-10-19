import httpx
import pytest

from harvester.ats import AtsClient, GreenhouseClient, LeverClient
from harvester.ats.registry import AtsRegistry, default_registry


@pytest.fixture
def registry(http: httpx.Client) -> AtsRegistry:
    return default_registry(http)


def test_default_registry_clients_satisfy_protocol(registry: AtsRegistry) -> None:
    assert [client.name for client in registry.clients] == ["Greenhouse", "Lever"]
    assert all(isinstance(client, AtsClient) for client in registry.clients)


def test_client_for_greenhouse_url_returns_greenhouse(registry: AtsRegistry) -> None:
    client = registry.client_for("https://boards.greenhouse.io/discord/jobs/1")

    assert isinstance(client, GreenhouseClient)


def test_client_for_lever_url_returns_lever(registry: AtsRegistry) -> None:
    assert isinstance(registry.client_for("jobs.lever.co/spotify"), LeverClient)


@pytest.mark.parametrize(
    "url", ["https://apply.workable.com/acme", "https://example.com", "not a url", ""]
)
def test_client_for_unsupported_url_returns_none(registry: AtsRegistry, url: str) -> None:
    assert registry.client_for(url) is None


def test_resolve_posting_url_returns_client_and_slug(registry: AtsRegistry) -> None:
    resolved = registry.resolve("https://jobs.lever.co/spotify/223f9722/apply")

    assert resolved is not None
    client, slug = resolved
    assert client.name == "Lever"
    assert slug == "spotify"


def test_resolve_known_host_without_slug_returns_none(registry: AtsRegistry) -> None:
    assert registry.resolve("https://job-boards.greenhouse.io/") is None


def test_get_known_name_returns_client(registry: AtsRegistry) -> None:
    assert isinstance(registry.get("Greenhouse"), GreenhouseClient)


def test_get_unknown_name_raises(registry: AtsRegistry) -> None:
    with pytest.raises(KeyError):
        registry.get("Workable")


def test_registry_duplicate_names_raises(http: httpx.Client) -> None:
    with pytest.raises(ValueError, match="duplicate"):
        AtsRegistry([LeverClient(http), LeverClient(http)])
