from pathlib import Path

from app.services.http import HTTPResponse, SourceBlockedError
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
