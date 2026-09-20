import pytest

from app.services.http import HTTPResponse, RateLimitError, ResilientHTTPClient


def test_get_html_uses_html_accept_header() -> None:
    calls: list[dict[str, str]] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> HTTPResponse:
        calls.append(headers)
        return HTTPResponse(200, {"content-type": "text/html"}, b"<html></html>")

    client = ResilientHTTPClient(transport=transport)

    response = client.get_html("https://irstats.com/driver/1286053/races?page=1")

    assert response.status == 200
    assert calls[0]["Accept"] == "text/html,application/xhtml+xml"


def test_get_json_keeps_json_accept_header() -> None:
    calls: list[dict[str, str]] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> HTTPResponse:
        calls.append(headers)
        return HTTPResponse(200, {"content-type": "application/json"}, b"{}")

    client = ResilientHTTPClient(transport=transport)

    payload, _ = client.get_json("https://example.test/results")

    assert payload == {}
    assert calls[0]["Accept"] == "application/json"


def test_429_is_returned_without_fast_retries_or_sleeps() -> None:
    calls = 0
    sleeps: list[float] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> HTTPResponse:
        nonlocal calls
        calls += 1
        return HTTPResponse(429, {}, b"Too Many Requests")

    client = ResilientHTTPClient(transport=transport, sleeper=sleeps.append)

    with pytest.raises(RateLimitError) as error:
        client.get_html("https://irstats.com/driver/1286053/races?page=4")

    assert calls == 1
    assert sleeps == []
    assert error.value.retry_after is None


def test_429_preserves_retry_after_header() -> None:
    def transport(url: str, headers: dict[str, str], timeout: float) -> HTTPResponse:
        return HTTPResponse(429, {"retry-after": "37"}, b"Too Many Requests")

    client = ResilientHTTPClient(transport=transport)

    with pytest.raises(RateLimitError) as error:
        client.get_html("https://irstats.com/driver/1286053/races?page=4")

    assert error.value.retry_after == 37.0
