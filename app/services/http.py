"""Small dependency-free HTTP client with respectful retry behavior."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class RemoteSourceError(RuntimeError):
    """Base error for remote source failures."""


class SourceBlockedError(RemoteSourceError):
    """Raised when a source returns an anti-bot or access-block response."""


class RateLimitError(RemoteSourceError):
    """Raised when a remote source asks the caller to slow down."""

    def __init__(self, url: str, retry_after: float | None = None) -> None:
        self.url = url
        self.retry_after = retry_after
        detail = (
            f" Retry after about {retry_after:g} seconds."
            if retry_after is not None
            else ""
        )
        super().__init__(f"{url} returned HTTP 429.{detail}")


class RemoteResponseError(RemoteSourceError):
    """Raised for non-retryable HTTP or malformed responses."""


@dataclass(slots=True)
class HTTPResponse:
    status: int
    headers: dict[str, str]
    body: bytes


Transport = Callable[[str, dict[str, str], float], HTTPResponse]


def _default_transport(url: str, headers: dict[str, str], timeout: float) -> HTTPResponse:
    request = Request(url, headers=headers, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - fixed public URLs
            return HTTPResponse(
                status=response.status,
                headers={key.lower(): value for key, value in response.headers.items()},
                body=response.read(),
            )
    except HTTPError as exc:
        return HTTPResponse(
            status=exc.code,
            headers={key.lower(): value for key, value in exc.headers.items()},
            body=exc.read(),
        )


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if target.tzinfo is None:
            target = target.astimezone()
        return max(0.0, target.timestamp() - time.time())


class ResilientHTTPClient:
    """GET-only client with bounded retries and injectable transport for tests."""

    RETRYABLE_STATUS = {500, 502, 503, 504}

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        max_attempts: int = 3,
        base_delay: float = 0.75,
        sleeper: Callable[[float], None] = time.sleep,
        transport: Transport | None = None,
        user_agent: str = "iRacingWeeklyTracker/0.1 (+local app)",
    ) -> None:
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.sleeper = sleeper
        self.transport = transport or _default_transport
        self.user_agent = user_agent

    def get_json(self, url: str) -> tuple[Any, HTTPResponse]:
        response = self.get(url, accept="application/json")
        try:
            return json.loads(response.body.decode("utf-8")), response
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RemoteResponseError(
                f"{url} returned invalid JSON (HTTP {response.status})."
            ) from exc

    def get_html(self, url: str) -> HTTPResponse:
        """Fetch a normal server-rendered HTML page."""
        return self.get(url, accept="text/html,application/xhtml+xml")

    def get(self, url: str, *, accept: str = "application/json") -> HTTPResponse:
        last_error: Exception | None = None
        headers = {
            "Accept": accept,
            "User-Agent": self.user_agent,
        }
        for attempt in range(self.max_attempts):
            try:
                response = self.transport(url, headers, self.timeout)
            except (TimeoutError, URLError) as exc:
                last_error = exc
                if attempt + 1 >= self.max_attempts:
                    break
                self.sleeper(self.base_delay * (2**attempt))
                continue

            if response.status == 403:
                raise SourceBlockedError(
                    f"{url} returned HTTP 403. Imported data has been preserved. "
                    "Try synchronization later; anti-bot protection is not bypassed."
                )

            if response.status == 429:
                retry_after = _retry_after_seconds(
                    response.headers.get("retry-after")
                    or response.headers.get("Retry-After")
                )
                # 429 is handled by the page-level iRStats iterator. Keeping it
                # out of this client's generic retry loop prevents extra
                # requests during the required cooldown.
                raise RateLimitError(url, retry_after)

            if response.status in self.RETRYABLE_STATUS:
                if attempt + 1 >= self.max_attempts:
                    raise RemoteResponseError(
                        f"{url} returned HTTP {response.status} after retries."
                    )
                retry_after = _retry_after_seconds(response.headers.get("retry-after"))
                self.sleeper(
                    retry_after
                    if retry_after is not None
                    else self.base_delay * (2**attempt)
                )
                continue

            if response.status < 200 or response.status >= 300:
                raise RemoteResponseError(f"{url} returned HTTP {response.status}.")
            return response

        raise RemoteSourceError(f"Unable to fetch {url}: {last_error}")
