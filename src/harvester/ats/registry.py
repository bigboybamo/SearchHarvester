"""Finds the `AtsClient` that owns a URL."""

from collections.abc import Iterable

import httpx

from harvester.ats.ashby import AshbyClient
from harvester.ats.base import AtsClient
from harvester.ats.greenhouse import GreenhouseClient
from harvester.ats.lever import LeverClient
from harvester.ats.workable import WorkableClient


class AtsRegistry:
    """A fixed set of ATS clients, looked up by URL or by name."""

    def __init__(self, clients: Iterable[AtsClient]) -> None:
        self._clients = tuple(clients)
        names = [client.name for client in self._clients]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate ATS client names: {names}")
        self._by_name = {client.name: client for client in self._clients}

    @property
    def clients(self) -> tuple[AtsClient, ...]:
        return self._clients

    def get(self, name: str) -> AtsClient:
        """The client stored as `Companies.AtsName == name`. Raises `KeyError` if unknown."""
        return self._by_name[name]

    def client_for(self, url: str) -> AtsClient | None:
        """The client whose board hosts include `url`'s host, if any."""
        return next((client for client in self._clients if client.matches(url)), None)

    def resolve(self, url: str) -> tuple[AtsClient, str] | None:
        """The owning client and company slug for a board or posting URL, if both are found."""
        client = self.client_for(url)
        if client is None:
            return None
        slug = client.parse_url(url)
        return (client, slug) if slug is not None else None


def default_registry(http: httpx.Client) -> AtsRegistry:
    """Every supported ATS, sharing one HTTP client."""
    return AtsRegistry(
        [GreenhouseClient(http), LeverClient(http), AshbyClient(http), WorkableClient(http)]
    )
