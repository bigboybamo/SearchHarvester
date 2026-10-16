import json
import logging
from pathlib import Path

import httpx
import pytest
import respx

from harvester.config import (
    DEFAULT_APPSETTINGS_URL,
    ConfigError,
    domains_for_groups,
    load_settings,
    load_source_groups,
    parse_appsettings,
    parse_domain_entry,
    read_appsettings,
)
from harvester.models import AtsSourceGroup, DomainKind

BOM = b"\xef\xbb\xbf"
SNAPSHOT_GROUP_IDS = list(range(1, 27))


def _group(group_id: int, *domains: str) -> AtsSourceGroup:
    return AtsSourceGroup(
        id=group_id,
        name=f"Group {group_id}",
        domains=tuple(parse_domain_entry(d) for d in domains),
    )


def _by_id(groups: list[AtsSourceGroup]) -> dict[int, AtsSourceGroup]:
    return {group.id: group for group in groups}


# --- load_settings -----------------------------------------------------------------------------


def test_load_settings_empty_environ_returns_defaults() -> None:
    settings = load_settings({})

    assert settings.search_provider == "brave"
    assert settings.appsettings_url == DEFAULT_APPSETTINGS_URL
    assert settings.connection_string is None
    assert settings.github_token is None


def test_load_settings_all_values_set_reads_each() -> None:
    settings = load_settings(
        {
            "JSB_CONNECTION_STRING": "Driver={x};Server=.",
            "SEARCH_PROVIDER": " SerpAPI ",
            "BRAVE_API_KEY": "b",
            "SERPAPI_API_KEY": "s",
            "JSB_GITHUB_TOKEN": "t",
            "JSB_APPSETTINGS_URL": "local.json",
        }
    )

    assert settings.search_provider == "serpapi"
    assert settings.connection_string == "Driver={x};Server=."
    assert settings.brave_api_key == "b"
    assert settings.serpapi_api_key == "s"
    assert settings.github_token == "t"
    assert settings.appsettings_url == "local.json"


def test_load_settings_blank_values_treated_as_unset() -> None:
    settings = load_settings(
        {"SEARCH_PROVIDER": "  ", "BRAVE_API_KEY": "", "JSB_APPSETTINGS_URL": ""}
    )

    assert settings.search_provider == "brave"
    assert settings.brave_api_key is None
    assert settings.appsettings_url == DEFAULT_APPSETTINGS_URL


def test_load_settings_unknown_provider_raises() -> None:
    with pytest.raises(ConfigError, match="SEARCH_PROVIDER"):
        load_settings({"SEARCH_PROVIDER": "google"})


def test_settings_repr_secrets_not_shown() -> None:
    settings = load_settings(
        {
            "BRAVE_API_KEY": "brave-secret",
            "JSB_GITHUB_TOKEN": "gh-secret",
            "JSB_CONNECTION_STRING": "pwd=x",
        }
    )

    text = repr(settings)
    assert "brave-secret" not in text
    assert "gh-secret" not in text
    assert "pwd=x" not in text


# --- parse_domain_entry ------------------------------------------------------------------------


def test_parse_domain_entry_bare_host_returns_host_kind() -> None:
    entry = parse_domain_entry("jobs.lever.co")

    assert entry.kind is DomainKind.HOST
    assert entry.host == "jobs.lever.co"
    assert entry.url == "https://jobs.lever.co"
    assert entry.tracking_params == ()


def test_parse_domain_entry_trailing_slash_returns_host_kind() -> None:
    assert parse_domain_entry("jobs.lever.co/").kind is DomainKind.HOST


def test_parse_domain_entry_path_and_query_returns_url_kind() -> None:
    raw = "worldbankgroup.csod.com/ux/ats/careersite/1/home?c=worldbankgroup"

    entry = parse_domain_entry(raw)

    assert entry.kind is DomainKind.URL
    assert entry.host == "worldbankgroup.csod.com"
    assert entry.url == f"https://{raw}"
    assert entry.raw == raw
    assert entry.tracking_params == ()


def test_parse_domain_entry_explicit_scheme_kept() -> None:
    entry = parse_domain_entry("http://example.org/careers")

    assert entry.url == "http://example.org/careers"
    assert entry.host == "example.org"


def test_parse_domain_entry_mixed_case_host_lowercased_raw_kept() -> None:
    entry = parse_domain_entry("  Jobs.Example.COM/Path ")

    assert entry.host == "jobs.example.com"
    assert entry.raw == "Jobs.Example.COM/Path"


def test_parse_domain_entry_ga_tracking_token_reported() -> None:
    raw = (
        "jobs.undp.org/cj_view_jobs.cfm?_gl=1*hv1lh5*_ga*ODMwMjA3MzcwLjE3NzgyNzg1NzE."
        "*_ga_PBF14M9C6G*czE3ODE0NTQ5ODUkbzIkZzAkdDE3ODE0NTQ5OTMkajUyJGwwJGgw"
    )

    entry = parse_domain_entry(raw)

    assert entry.kind is DomainKind.URL
    assert entry.host == "jobs.undp.org"
    assert entry.tracking_params == ("_gl",)


def test_parse_domain_entry_utm_params_reported() -> None:
    entry = parse_domain_entry("example.org/jobs?utm_source=x&page=2&gclid=abc")

    assert entry.tracking_params == ("utm_source", "gclid")


def test_parse_domain_entry_tld_returns_tld_kind_without_host() -> None:
    entry = parse_domain_entry(".ai")

    assert entry.kind is DomainKind.TLD
    assert entry.host is None
    assert entry.url is None
    assert entry.raw == ".ai"


@pytest.mark.parametrize("raw", ["", "   ", "https://", "/just/a/path"])
def test_parse_domain_entry_no_host_raises(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_domain_entry(raw)


# --- parse_appsettings -------------------------------------------------------------------------


def test_parse_appsettings_live_snapshot_returns_all_groups(appsettings_bytes: bytes) -> None:
    groups = parse_appsettings(appsettings_bytes)

    assert [group.id for group in groups] == SNAPSHOT_GROUP_IDS
    assert all(group.domains for group in groups)


def test_parse_appsettings_utf8_bom_bytes_parsed(appsettings_bytes: bytes) -> None:
    assert not appsettings_bytes.startswith(BOM)

    assert parse_appsettings(BOM + appsettings_bytes) == parse_appsettings(appsettings_bytes)


def test_parse_appsettings_bom_in_str_parsed(appsettings_bytes: bytes) -> None:
    text = appsettings_bytes.decode("utf-8")

    assert parse_appsettings("﻿" + text) == parse_appsettings(text)


def test_parse_appsettings_corrupted_names_kept_and_flagged(appsettings_bytes: bytes) -> None:
    groups = _by_id(parse_appsettings(appsettings_bytes))

    corrupted = sorted(group_id for group_id, group in groups.items() if group.has_corrupted_name)
    assert corrupted == list(range(1, 12))
    assert groups[1].name == "ATS Set #1 � Greenhouse / Lever / Workable"
    assert not groups[12].has_corrupted_name
    assert "—" in groups[12].name


def test_parse_appsettings_corrupted_name_group_identified_by_id(appsettings_bytes: bytes) -> None:
    groups = _by_id(parse_appsettings(appsettings_bytes))

    assert [entry.host for entry in groups[1].domains] == [
        "job-boards.greenhouse.io",
        "jobs.lever.co",
        "apply.workable.com",
    ]


def test_parse_appsettings_path_style_domains_parsed(appsettings_bytes: bytes) -> None:
    entries = {
        entry.raw: entry
        for group in parse_appsettings(appsettings_bytes)
        for entry in group.domains
    }

    world_bank = entries["worldbankgroup.csod.com/ux/ats/careersite/1/home?c=worldbankgroup"]
    assert world_bank.kind is DomainKind.URL
    assert world_bank.host == "worldbankgroup.csod.com"

    itu = entries["jobs.itu.int/go/View-all-categories/8942455"]
    assert itu.kind is DomainKind.URL
    assert itu.url == "https://jobs.itu.int/go/View-all-categories/8942455"

    assert entries["jobs.lever.co"].kind is DomainKind.HOST
    assert entries[".ai"].kind is DomainKind.TLD


def test_parse_appsettings_tracking_tokens_reported(appsettings_bytes: bytes) -> None:
    tracked = sorted(
        entry.host
        for group in parse_appsettings(appsettings_bytes)
        for entry in group.domains
        if entry.tracking_params
    )

    assert tracked == ["jobs.iadb.org", "jobs.undp.org"]


def test_parse_appsettings_blank_domain_skipped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    content = json.dumps({"AtsSourceGroups": [{"Id": 1, "Name": "A", "Domains": ["a.com", " "]}]})

    with caplog.at_level(logging.WARNING):
        groups = parse_appsettings(content)

    assert [entry.raw for entry in groups[0].domains] == ["a.com"]
    assert "source group 1" in caplog.text


def test_parse_appsettings_duplicate_ids_raises() -> None:
    content = json.dumps(
        {
            "AtsSourceGroups": [
                {"Id": 1, "Name": "A", "Domains": ["a.com"]},
                {"Id": 1, "Name": "B", "Domains": ["b.com"]},
            ]
        }
    )

    with pytest.raises(ConfigError, match="duplicate Id 1"):
        parse_appsettings(content)


def test_parse_appsettings_missing_section_raises() -> None:
    with pytest.raises(ConfigError, match="unexpected shape"):
        parse_appsettings('{"ConnectionString": "x"}')


def test_parse_appsettings_invalid_json_raises() -> None:
    with pytest.raises(ConfigError, match="not valid JSON"):
        parse_appsettings(b"{not json")


def test_parse_appsettings_invalid_utf8_raises() -> None:
    with pytest.raises(ConfigError, match="not valid UTF-8"):
        parse_appsettings(b'{"AtsSourceGroups": "\xff"}')


# --- read_appsettings / load_source_groups -----------------------------------------------------


def test_read_appsettings_local_path_returns_bytes(tmp_path: Path) -> None:
    path = tmp_path / "appsettings.json"
    path.write_bytes(BOM + b"{}")

    assert read_appsettings(str(path)) == BOM + b"{}"


def test_read_appsettings_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Cannot read"):
        read_appsettings(str(tmp_path / "missing.json"))


def test_read_appsettings_url_returns_body(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(DEFAULT_APPSETTINGS_URL).respond(200, content=b"{}")

    assert read_appsettings(DEFAULT_APPSETTINGS_URL) == b"{}"
    assert route.calls.last.request.headers["User-Agent"].startswith("SearchHarvester/")


def test_read_appsettings_url_not_found_raises(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(DEFAULT_APPSETTINGS_URL).respond(404)

    with pytest.raises(ConfigError, match="HTTP 404"):
        read_appsettings(DEFAULT_APPSETTINGS_URL)


def test_read_appsettings_url_connection_error_raises(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(DEFAULT_APPSETTINGS_URL).mock(side_effect=httpx.ConnectError("down"))

    with pytest.raises(ConfigError, match="Cannot fetch"):
        read_appsettings(DEFAULT_APPSETTINGS_URL)


def test_load_source_groups_url_parses_fixture(
    respx_mock: respx.MockRouter, appsettings_bytes: bytes
) -> None:
    respx_mock.get(DEFAULT_APPSETTINGS_URL).respond(200, content=BOM + appsettings_bytes)

    groups = load_source_groups(DEFAULT_APPSETTINGS_URL)

    assert [group.id for group in groups] == SNAPSHOT_GROUP_IDS


# --- domains_for_groups ------------------------------------------------------------------------


def test_domains_for_groups_overlapping_groups_returns_distinct_in_order() -> None:
    groups = [_group(1, "a.com", "b.com"), _group(2, "b.com", "c.com"), _group(3, "d.com")]

    entries = domains_for_groups(groups, [2, 1])

    assert [entry.raw for entry in entries] == ["b.com", "c.com", "a.com"]


def test_domains_for_groups_unknown_id_skipped_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        entries = domains_for_groups([_group(1, "a.com")], [1, 99])

    assert [entry.raw for entry in entries] == ["a.com"]
    assert "99" in caplog.text
