from app.services.http import HTTPResponse, ResilientHTTPClient


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
