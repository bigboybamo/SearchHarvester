"""Minimal GitHub REST client for the health-check issue on JobSearchBuilder."""

import logging

import httpx
from pydantic import BaseModel, ConfigDict

from harvester.httpclient import create_client, get_with_retry

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
MAX_ISSUE_PAGES = 10


class GitHubError(Exception):
    """A GitHub API call failed."""


class Issue(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    number: int
    title: str
    html_url: str


class GitHubIssues:
    """Issue operations on one repository.

    Only reads are retried. A retried create after a timeout could open a duplicate issue.
    """

    def __init__(self, http: httpx.Client, repo: str) -> None:
        self._http = http
        self._base = f"{API_BASE}/repos/{repo}/issues"

    def find_open_issue(self, title_prefix: str) -> Issue | None:
        """The oldest open issue whose title starts with `title_prefix`, ignoring pull requests."""
        matches: list[Issue] = []
        for page in range(1, MAX_ISSUE_PAGES + 1):
            url = f"{self._base}?state=open&per_page=100&page={page}"
            items = self._json(get_with_retry(self._http, url), "list open issues")
            if not isinstance(items, list):
                raise GitHubError("Unexpected response when listing issues")
            matches.extend(
                Issue.model_validate(item)
                for item in items
                if "pull_request" not in item
                and str(item.get("title", "")).startswith(title_prefix)
            )
            if len(items) < 100:
                break
        if len(matches) > 1:
            logger.warning(
                "Found %d open health-check issues; updating the oldest (#%d) only",
                len(matches),
                min(issue.number for issue in matches),
            )
        return min(matches, key=lambda issue: issue.number) if matches else None

    def create(self, title: str, body: str, labels: list[str]) -> Issue:
        payload = {"title": title, "body": body, "labels": labels}
        return Issue.model_validate(
            self._json(self._http.post(self._base, json=payload), "create issue")
        )

    def update(self, number: int, title: str, body: str) -> Issue:
        payload = {"title": title, "body": body}
        response = self._http.patch(f"{self._base}/{number}", json=payload)
        return Issue.model_validate(self._json(response, f"update issue #{number}"))

    def comment(self, number: int, body: str) -> None:
        response = self._http.post(f"{self._base}/{number}/comments", json={"body": body})
        self._json(response, f"comment on issue #{number}")

    def close(self, number: int) -> None:
        payload = {"state": "closed", "state_reason": "completed"}
        self._json(self._http.patch(f"{self._base}/{number}", json=payload), f"close #{number}")

    @staticmethod
    def _json(response: httpx.Response, action: str) -> object:
        if not response.is_success:
            raise GitHubError(f"GitHub API: {action} failed with HTTP {response.status_code}")
        return response.json()


def create_github_client(token: str) -> httpx.Client:
    """An HTTP client authenticated for the GitHub REST API."""
    return create_client(
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
