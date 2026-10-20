"""Picks the active `SearchProvider` from settings, so switching is a config change."""

import httpx

from harvester.config import ConfigError, Settings
from harvester.search.base import SearchProvider
from harvester.search.brave import BraveProvider
from harvester.search.serpapi import SerpApiProvider


def create_provider(
    settings: Settings, http: httpx.Client, name: str | None = None
) -> SearchProvider:
    """The provider named by `name`, or by `SEARCH_PROVIDER` when `name` is `None`.

    Raises `ConfigError` if that provider's API key isn't set.
    """
    chosen = (name or settings.search_provider).lower()
    if chosen == "brave":
        if not settings.brave_api_key:
            raise ConfigError("BRAVE_API_KEY is required for the brave search provider")
        return BraveProvider(http, settings.brave_api_key)
    if chosen == "serpapi":
        if not settings.serpapi_api_key:
            raise ConfigError("SERPAPI_API_KEY is required for the serpapi search provider")
        return SerpApiProvider(http, settings.serpapi_api_key)
    raise ConfigError(f"Unknown search provider {chosen!r}; expected 'brave' or 'serpapi'")
