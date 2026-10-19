"""The `AtsClient` protocol and helpers shared by every ATS client."""

import json
import re
from collections.abc import Callable
from html.parser import HTMLParser
from typing import Protocol, runtime_checkable
from urllib.parse import SplitResult, urlsplit

import httpx

from harvester.httpclient import get_with_retry
from harvester.models import Posting, ProbeResult

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class AtsError(Exception):
    """An ATS API call failed or returned something unusable."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@runtime_checkable
class AtsClient(Protocol):
    """One applicant tracking system: its URL patterns and its public jobs API."""

    name: str
    """Stored as `Companies.AtsName`."""

    canary_slugs: tuple[str, ...]
    """Large, long-lived boards the health check probes to tell a platform outage or move from
    one company closing its board. At least one must stay healthy."""

    def matches(self, url: str) -> bool:
        """True when `url` is on one of this ATS's board hosts."""
        ...

    def parse_url(self, url: str) -> str | None:
        """The company slug in a board or posting URL, or `None` if there isn't one."""
        ...

    def board_url(self, slug: str) -> str:
        """The public board page for `slug` on the ATS's current host (`Companies.BoardUrl`)."""
        ...

    def fetch_jobs(self, slug: str) -> list[Posting]:
        """Every open posting on the board. Raises `AtsError` on failure."""
        ...

    def probe(self, slug: str) -> ProbeResult:
        """Check that the board still answers. Never raises for network or HTTP failures."""
        ...


def split_url(url: str) -> SplitResult:
    """`urlsplit` with `https://` assumed when the URL has no scheme."""
    text = url.strip()
    return urlsplit(text if "://" in text else f"https://{text}")


def host_of(url: str) -> str | None:
    """Lower-cased host of `url`, or `None` if it has none."""
    return split_url(url).hostname


def path_segments(parts: SplitResult) -> list[str]:
    """Non-empty path segments of a split URL."""
    return [segment for segment in parts.path.split("/") if segment]


def normalise_slug(candidate: str | None) -> str | None:
    """Lower-cased slug if it looks like a board slug, else `None`.

    Lever slugs are case-sensitive and always lower case; Greenhouse ignores case. Lower-casing
    therefore works for both and keeps `Companies` free of case-only duplicates.
    """
    if candidate is None:
        return None
    slug = candidate.strip().lower()
    return slug if SLUG_PATTERN.match(slug) else None


def require_slug(slug: str) -> str:
    """Validated slug for building an API URL. Raises `ValueError` for anything unsafe."""
    normalised = normalise_slug(slug)
    if normalised is None:
        raise ValueError(f"invalid board slug {slug!r}")
    return normalised


def get_json(http: httpx.Client, url: str) -> object:
    """GET `url` with retries and decode JSON. Raises `AtsError` on non-200 or a non-JSON body."""
    try:
        response = get_with_retry(http, url)
    except httpx.HTTPError as exc:
        raise AtsError(f"GET {url} failed: {exc}") from exc

    if response.status_code != httpx.codes.OK:
        raise AtsError(
            f"GET {url} returned HTTP {response.status_code}", status_code=response.status_code
        )
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise AtsError(f"GET {url} returned a non-JSON body", status_code=200) from exc


def probe_endpoint(
    ats_name: str,
    slug: str,
    fetch: Callable[[], object],
    count_jobs: Callable[[object], int],
) -> ProbeResult:
    """Run `fetch` and turn the outcome into a `ProbeResult` instead of raising."""
    try:
        body = fetch()
        job_count = count_jobs(body)
    except AtsError as exc:
        return ProbeResult(
            ats_name=ats_name, slug=slug, ok=False, status_code=exc.status_code, error=str(exc)
        )
    return ProbeResult(ats_name=ats_name, slug=slug, ok=True, status_code=200, job_count=job_count)


_BLOCK_TAGS = frozenset(
    {"address", "article", "blockquote", "br", "div", "footer", "h1", "h2", "h3", "h4", "h5",
     "h6", "header", "hr", "li", "ol", "p", "pre", "section", "table", "td", "th", "tr", "ul"}
)  # fmt: skip
_SKIPPED_TAGS = frozenset({"script", "style"})


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip_depth = max(self._skip_depth - 1, 0)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            # Source newlines are insignificant in HTML; only block tags break lines.
            self.parts.append(_WHITESPACE.sub(" ", data))


_WHITESPACE = re.compile(r"\s+")


def html_to_text(markup: str) -> str:
    """Plain text from an HTML fragment: tags dropped, one line per block, whitespace collapsed."""
    extractor = _TextExtractor()
    extractor.feed(markup)
    extractor.close()
    lines = (" ".join(line.split()) for line in "".join(extractor.parts).split("\n"))
    return "\n".join(line for line in lines if line)
