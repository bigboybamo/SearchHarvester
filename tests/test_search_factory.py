import httpx
import pytest

from harvester.config import ConfigError, load_settings
from harvester.search import BraveProvider, SerpApiProvider, create_provider


def test_create_provider_default_setting_returns_brave(http: httpx.Client) -> None:
    settings = load_settings({"BRAVE_API_KEY": "b"})

    assert isinstance(create_provider(settings, http), BraveProvider)


def test_create_provider_serpapi_setting_returns_serpapi(http: httpx.Client) -> None:
    settings = load_settings({"SEARCH_PROVIDER": "serpapi", "SERPAPI_API_KEY": "s"})

    assert isinstance(create_provider(settings, http), SerpApiProvider)


def test_create_provider_explicit_name_overrides_setting(http: httpx.Client) -> None:
    settings = load_settings({"BRAVE_API_KEY": "b", "SERPAPI_API_KEY": "s"})

    assert isinstance(create_provider(settings, http, "SerpAPI"), SerpApiProvider)


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({}, "BRAVE_API_KEY"),
        ({"SEARCH_PROVIDER": "serpapi", "BRAVE_API_KEY": "b"}, "SERPAPI_API_KEY"),
    ],
)
def test_create_provider_missing_key_raises(
    http: httpx.Client, environ: dict[str, str], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        create_provider(load_settings(environ), http)


def test_create_provider_unknown_name_raises(http: httpx.Client) -> None:
    with pytest.raises(ConfigError, match="Unknown search provider 'google'"):
        create_provider(load_settings({"BRAVE_API_KEY": "b"}), http, "google")
