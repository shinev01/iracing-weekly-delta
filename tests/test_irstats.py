from pathlib import Path

import pytest

from app.services.http import (
    HTTPResponse,
    RateLimitError,
    ResilientHTTPClient,
    SourceBlockedError,
)
from app.services.irstats import (
    IrstatsClient,
    parse_irstats_page,
    parse_irstats_race_page,
)


def test_irstats_parser_extracts_structured_race_fields() -> None:
    html = (Path(__file__).parent / "fixtures" / "irstats_page.html").read_text(
        encoding="utf-8"
    )

    page = parse_irstats_page(html, cust_id=1294360, page_number=1)

    assert page.per_page == 50
    assert page.total_count == 702
    assert page.total_pages == 15
    assert page.has_more is True
    assert len(page.races) == 1
    race = page.races[0]
    assert race.subsession_id == 88756192
    assert race.series_name == "Global Mazda MX-5 Cup by Fanatec"
    assert race.car_name == "Global Mazda MX-5 Cup"
    assert race.track_name is None
    assert race.finish_position == 1
    assert race.start_time_utc is not None


class FakeHTML:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.urls: list[str] = []

    def get_html(self, url: str) -> HTTPResponse:
        self.urls.append(url)
        return HTTPResponse(200, {"content-type": "text/html"}, self.body)


class FakeBrowser:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.urls: list[str] = []

    def get_html(self, url: str) -> HTTPResponse:
        self.urls.append(url)
        return HTTPResponse(200, {"content-type": "text/html"}, self.body)


def test_irstats_client_fetches_server_rendered_html_without_json_query() -> None:
    html = (Path(__file__).parent / "fixtures" / "irstats_page.html").read_bytes()
    http = FakeHTML(html)
    client = IrstatsClient(http=http, browser=None, page_delay=0)

    page = client.fetch_page(1286053, 1)

    assert http.urls == ["https://irstats.com/driver/1286053/races?page=1"]
    assert page.races[0].subsession_id == 88756192


def test_irstats_client_uses_browser_only_after_direct_html_is_blocked() -> None:
    html = (Path(__file__).parent / "fixtures" / "irstats_page.html").read_bytes()

    class BlockedHTML:
        def get_html(self, url: str) -> HTTPResponse:
            raise SourceBlockedError(f"{url} returned HTTP 403")

    browser = FakeBrowser(html)
    client = IrstatsClient(http=BlockedHTML(), browser=browser, page_delay=0)

    page = client.fetch_page(1286053, 1)

    assert browser.urls == ["https://irstats.com/driver/1286053/races?page=1"]
    assert page.races[0].subsession_id == 88756192


def test_irstats_client_stays_browser_only_after_first_direct_403() -> None:
    html = (Path(__file__).parent / "fixtures" / "irstats_page.html").read_bytes()

    class BlockedHTML:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def get_html(self, url: str) -> HTTPResponse:
            self.urls.append(url)
            raise SourceBlockedError(f"{url} returned HTTP 403")

    direct = BlockedHTML()
    browser = FakeBrowser(html)
    client = IrstatsClient(http=direct, browser=browser, page_delay=0)

    client.fetch_page(1286053, 1)
    client.fetch_page(1286053, 2)

    assert client.browser_only is True
    assert direct.urls == ["https://irstats.com/driver/1286053/races?page=1"]
    assert browser.urls == [
        "https://irstats.com/driver/1286053/races?page=1",
        "https://irstats.com/driver/1286053/races?page=2",
    ]


def test_irstats_page_delay_defaults_to_six_seconds(monkeypatch) -> None:
    monkeypatch.delenv("IRSTATS_PAGE_DELAY", raising=False)
    client = IrstatsClient(http=FakeHTML(b""), browser=FakeBrowser(b""))

    assert client.page_delay == 6.0


def _history_html(page: int, total_pages: int = 4) -> bytes:
    next_link = (
        f'<a rel="next" href="/driver/123/races?page={page + 1}">Next</a>'
        if page < total_pages
        else ""
    )
    return f"""
        <html><body>
          <p>Showing {(page - 1) * 50 + 1}–{page * 50} of {total_pages * 50} races</p>
          {next_link}
          <table><tbody>
            <tr><td><a href="/race/{1000 + page}">Race {page}</a></td></tr>
          </tbody></table>
        </body></html>
    """.encode("utf-8")


def test_irstats_stops_on_429_without_retry() -> None:
    responses = {
        1: [HTTPResponse(200, {}, _history_html(1))],
        2: [HTTPResponse(200, {}, _history_html(2))],
        3: [
            HTTPResponse(429, {}, b"Too Many Requests"),
        ],
        4: [HTTPResponse(200, {}, _history_html(4))],
    }
    calls: list[int] = []
    sleeps: list[float] = []
    progress: list[str] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> HTTPResponse:
        del headers, timeout
        page = int(url.rsplit("=", 1)[1])
        calls.append(page)
        return responses[page].pop(0)

    client = IrstatsClient(
        http=ResilientHTTPClient(transport=transport),
        browser=None,
        page_delay=0,
        sleeper=sleeps.append,
        progress=progress.append,
    )

    with pytest.raises(RateLimitError) as error:
        list(client.iter_history(123, max_pages=None))

    assert calls == [1, 2, 3]
    assert sleeps == []
    assert error.value.page == 3
    assert error.value.rate_limit_retries == 0


def test_irstats_rate_limit_does_not_wait_or_retry() -> None:
    sleeps: list[float] = []
    retry_after_values = ["10", "120"]
    observed_retry_after: list[float | None] = []

    for retry_after in retry_after_values:
        responses = [
            HTTPResponse(429, {"retry-after": retry_after}, b"Too Many Requests"),
            HTTPResponse(200, {}, _history_html(1, total_pages=1)),
        ]

        def transport(
            url: str,
            headers: dict[str, str],
            timeout: float,
            *,
            _responses=responses,
        ) -> HTTPResponse:
            del url, headers, timeout
            return _responses.pop(0)

        client = IrstatsClient(
            http=ResilientHTTPClient(transport=transport),
            browser=None,
            page_delay=0,
            sleeper=sleeps.append,
        )
        with pytest.raises(RateLimitError) as error:
            list(client.iter_history(123, max_pages=1))
        observed_retry_after.append(error.value.retry_after)

    assert sleeps == []
    assert observed_retry_after == [10.0, 120.0]


def test_irstats_rate_limit_raises_on_first_429() -> None:
    calls = 0
    sleeps: list[float] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> HTTPResponse:
        nonlocal calls
        del headers, timeout
        calls += 1
        return HTTPResponse(429, {}, b"Too Many Requests")

    client = IrstatsClient(
        http=ResilientHTTPClient(transport=transport),
        browser=None,
        page_delay=0,
        sleeper=sleeps.append,
    )

    with pytest.raises(RateLimitError) as error:
        list(client.iter_history(123, max_pages=1))

    assert calls == 1
    assert sleeps == []
    assert error.value.page == 1
    assert error.value.rate_limit_retries == 0


def test_irstats_stops_in_browser_only_client_on_429() -> None:
    class BlockedHTML:
        def __init__(self) -> None:
            self.calls = 0

        def get_html(self, url: str) -> HTTPResponse:
            del url
            self.calls += 1
            raise SourceBlockedError("direct HTTP is blocked")

    class RateLimitedBrowser:
        def __init__(self) -> None:
            self.calls = 0

        def get_html(self, url: str) -> HTTPResponse:
            del url
            self.calls += 1
            return HTTPResponse(429, {}, b"Too Many Requests")

    direct = BlockedHTML()
    browser = RateLimitedBrowser()
    sleeps: list[float] = []
    client = IrstatsClient(
        http=direct,
        browser=browser,
        page_delay=0,
        sleeper=sleeps.append,
    )

    with pytest.raises(RateLimitError):
        list(client.iter_history(123, max_pages=1))

    assert client.browser_only is True
    assert client.browser is browser
    assert direct.calls == 1
    assert browser.calls == 1
    assert sleeps == []


def test_irstats_race_page_fallback_converts_display_week_to_internal_week() -> None:
    page = parse_irstats_race_page(
        """
        <main>
          <h1>Indianapolis Motor Speedway (Road Course) · Week 1</h1>
          <dl><dt>Category</dt><dd>Sports Car</dd></dl>
        </main>
        """
    )

    assert page.race_week_num == 0
    assert page.category == "sports_car"
