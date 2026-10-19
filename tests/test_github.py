import json

import httpx
import pytest
import respx

from harvester.github import GitHubError, GitHubIssues, create_github_client

REPO = "bigboybamo/JobSearchBuilder"
ISSUES = f"https://api.github.com/repos/{REPO}/issues"
PREFIX = "ATS domain health check:"


def _issue(number: int, title: str, **extra: object) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "html_url": f"https://github.com/{REPO}/issues/{number}",
        **extra,
    }


@pytest.fixture
def issues(http: httpx.Client) -> GitHubIssues:
    return GitHubIssues(http, REPO)


def test_create_github_client_sets_auth_and_api_headers() -> None:
    with create_github_client("tok") as client:
        assert client.headers["Authorization"] == "Bearer tok"
        assert client.headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert client.headers["User-Agent"].startswith("SearchHarvester/")


def test_find_open_issue_matching_title_returned(
    issues: GitHubIssues, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ISSUES).respond(
        200, json=[_issue(9, "Unrelated"), _issue(12, f"{PREFIX} 3 problem(s)")]
    )

    found = issues.find_open_issue(PREFIX)

    assert found is not None
    assert found.number == 12


def test_find_open_issue_pull_requests_ignored(
    issues: GitHubIssues, respx_mock: respx.MockRouter
) -> None:
    pr = _issue(5, f"{PREFIX} fix", pull_request={"url": "x"})
    respx_mock.get(ISSUES).respond(200, json=[pr])

    assert issues.find_open_issue(PREFIX) is None


def test_find_open_issue_several_matches_returns_oldest(
    issues: GitHubIssues, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ISSUES).respond(
        200, json=[_issue(30, f"{PREFIX} 1 problem(s)"), _issue(21, f"{PREFIX} 2 problem(s)")]
    )

    found = issues.find_open_issue(PREFIX)

    assert found is not None
    assert found.number == 21


def test_find_open_issue_second_page_searched(
    issues: GitHubIssues, respx_mock: respx.MockRouter
) -> None:
    page1 = [_issue(n, f"Other {n}") for n in range(100, 200)]
    route = respx_mock.get(ISSUES)
    route.side_effect = [
        httpx.Response(200, json=page1),
        httpx.Response(200, json=[_issue(7, f"{PREFIX} 1 problem(s)")]),
    ]

    found = issues.find_open_issue(PREFIX)

    assert found is not None
    assert found.number == 7
    assert route.calls[1].request.url.params["page"] == "2"


def test_find_open_issue_api_error_raises(
    issues: GitHubIssues, respx_mock: respx.MockRouter
) -> None:
    respx_mock.get(ISSUES).respond(401, json={"message": "Bad credentials"})

    with pytest.raises(GitHubError, match="HTTP 401"):
        issues.find_open_issue(PREFIX)


def test_create_posts_title_body_and_labels(
    issues: GitHubIssues, respx_mock: respx.MockRouter
) -> None:
    route = respx_mock.post(ISSUES).respond(201, json=_issue(40, "T"))

    created = issues.create("T", "B", ["bug"])

    assert created.number == 40
    assert json.loads(route.calls.last.request.content) == {
        "title": "T",
        "body": "B",
        "labels": ["bug"],
    }


def test_create_server_error_not_retried(
    issues: GitHubIssues, respx_mock: respx.MockRouter
) -> None:
    # A retried POST after a 502 whose request actually landed would open a duplicate issue.
    route = respx_mock.post(ISSUES).respond(502)

    with pytest.raises(GitHubError):
        issues.create("T", "B", ["bug"])

    assert route.call_count == 1


def test_update_patches_title_and_body(issues: GitHubIssues, respx_mock: respx.MockRouter) -> None:
    route = respx_mock.patch(f"{ISSUES}/12").respond(200, json=_issue(12, "New"))

    issues.update(12, "New", "Body")

    assert json.loads(route.calls.last.request.content) == {"title": "New", "body": "Body"}


def test_comment_and_close(issues: GitHubIssues, respx_mock: respx.MockRouter) -> None:
    comment = respx_mock.post(f"{ISSUES}/12/comments").respond(201, json={"id": 1})
    close = respx_mock.patch(f"{ISSUES}/12").respond(200, json=_issue(12, "T"))

    issues.comment(12, "All good")
    issues.close(12)

    assert json.loads(comment.calls.last.request.content) == {"body": "All good"}
    assert json.loads(close.calls.last.request.content) == {
        "state": "closed",
        "state_reason": "completed",
    }
