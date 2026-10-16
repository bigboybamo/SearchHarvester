import httpx
import pytest
import respx

from harvester.httpclient import USER_AGENT, create_client, get_with_retry

URL = "https://example.test/resource"


def test_create_client_sets_user_agent_and_timeout() -> None:
    with create_client() as client:
        assert client.headers["User-Agent"] == USER_AGENT
        assert client.timeout.read is not None
        assert client.follow_redirects


def test_get_with_retry_success_first_try_no_sleep(
    respx_mock: respx.MockRouter, _no_retry_sleep: list[float]
) -> None:
    route = respx_mock.get(URL).respond(200, json={"ok": True})

    with create_client() as client:
        response = get_with_retry(client, URL)

    assert response.status_code == 200
    assert route.call_count == 1
    assert _no_retry_sleep == []


def test_get_with_retry_server_error_then_ok_returns_ok(
    respx_mock: respx.MockRouter, _no_retry_sleep: list[float]
) -> None:
    route = respx_mock.get(URL)
    route.side_effect = [httpx.Response(503), httpx.Response(502), httpx.Response(200)]

    with create_client() as client:
        response = get_with_retry(client, URL, backoff_seconds=0.5)

    assert response.status_code == 200
    assert route.call_count == 3
    assert _no_retry_sleep == [0.5, 1.0]


def test_get_with_retry_retry_after_header_used_as_delay(
    respx_mock: respx.MockRouter, _no_retry_sleep: list[float]
) -> None:
    respx_mock.get(URL).side_effect = [
        httpx.Response(429, headers={"Retry-After": "7"}),
        httpx.Response(200),
    ]

    with create_client() as client:
        get_with_retry(client, URL)

    assert _no_retry_sleep == [7.0]


def test_get_with_retry_huge_retry_after_capped(
    respx_mock: respx.MockRouter, _no_retry_sleep: list[float]
) -> None:
    respx_mock.get(URL).side_effect = [
        httpx.Response(429, headers={"Retry-After": "3600"}),
        httpx.Response(200),
    ]

    with create_client() as client:
        get_with_retry(client, URL)

    assert _no_retry_sleep == [60.0]


def test_get_with_retry_persistent_server_error_returns_last_response(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.get(URL).respond(500)

    with create_client() as client:
        response = get_with_retry(client, URL, attempts=3)

    assert response.status_code == 500
    assert route.call_count == 3


def test_get_with_retry_not_found_not_retried(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(URL).respond(404)

    with create_client() as client:
        response = get_with_retry(client, URL)

    assert response.status_code == 404
    assert route.call_count == 1


def test_get_with_retry_transport_error_then_ok_returns_ok(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(URL).side_effect = [httpx.ConnectError("boom"), httpx.Response(200)]

    with create_client() as client:
        assert get_with_retry(client, URL).status_code == 200


def test_get_with_retry_persistent_transport_error_raises(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(URL).mock(side_effect=httpx.ConnectError("boom"))

    with create_client() as client, pytest.raises(httpx.ConnectError):
        get_with_retry(client, URL, attempts=2)


def test_get_with_retry_zero_attempts_raises() -> None:
    with create_client() as client, pytest.raises(ValueError):
        get_with_retry(client, URL, attempts=0)
