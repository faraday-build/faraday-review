"""Budgeted, retrying GitHub REST client. Every request has a timeout."""

from __future__ import annotations

import time
from typing import Any

import requests

from ..config import GitHubApiConfig
from ..errors import GitHubApiError
from .budget import RequestBudget

API_ROOT = "https://api.github.com"
REQUEST_TIMEOUT_SECONDS = 30
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _is_secondary_rate_limit(response: requests.Response) -> bool:
    if response.status_code != 403:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    return "secondary rate limit" in str(body.get("message", "")).lower()


class GitHubClient:
    def __init__(self, token: str, config: GitHubApiConfig, budget: RequestBudget,
                 session: requests.Session | None = None, sleeper=time.sleep) -> None:
        self._config = config
        self.budget = budget
        self._sleep = sleeper
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            }
        )

    def request(self, method: str, path: str, *, json_body: Any = None,
                params: dict[str, Any] | None = None,
                timeout: float = REQUEST_TIMEOUT_SECONDS) -> requests.Response:
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        last_error: str = "no attempt made"
        for attempt in range(self._config.retry_attempts + 1):
            self.budget.spend(is_retry=attempt > 0)
            try:
                response = self._session.request(
                    method, url, json=json_body, params=params, timeout=timeout,
                )
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self._config.retry_attempts:
                    self._sleep(self.budget.backoff_seconds(attempt))
                continue

            retryable = response.status_code in RETRYABLE_STATUS or _is_secondary_rate_limit(response)
            if not retryable:
                if response.status_code >= 400:
                    raise GitHubApiError(
                        f"{method} {path} -> {response.status_code}: {response.text[:200]}"
                    )
                return response

            last_error = f"HTTP {response.status_code}"
            if attempt < self._config.retry_attempts:
                retry_after_header = response.headers.get("Retry-After")
                retry_after = float(retry_after_header) if retry_after_header else None
                self._sleep(self.budget.backoff_seconds(attempt, retry_after=retry_after))

        raise GitHubApiError(f"{method} {path} failed after retries: {last_error}")

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params).json()

    def download(self, path: str, max_bytes: int,
                 timeout: float = REQUEST_TIMEOUT_SECONDS) -> bytes:
        """GET a binary body (an artifact zip), at most `max_bytes`. One
        attempt, budgeted; any HTTP error or oversize body raises. The
        Authorization header is dropped by requests on the cross-host
        redirect to blob storage."""
        url = path if path.startswith("http") else f"{API_ROOT}{path}"
        self.budget.spend()
        try:
            response = self._session.request("GET", url, stream=True, timeout=timeout)
        except requests.RequestException as exc:
            raise GitHubApiError(f"GET {path} failed: {type(exc).__name__}") from exc
        try:
            if response.status_code >= 400:
                raise GitHubApiError(f"GET {path} -> {response.status_code}")
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(chunk_size=65536):
                total += len(chunk)
                if total > max_bytes:
                    raise GitHubApiError(f"GET {path}: body exceeds {max_bytes} bytes")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            response.close()

    def paginate_key(self, path: str, key: str,
                     params: dict[str, Any] | None = None) -> list[Any]:
        """Like `paginate` for endpoints that wrap the list in an object
        (`{"total_count": n, key: [...]}`)."""
        items: list[Any] = []
        params = dict(params or {})
        params.setdefault("per_page", 100)
        url: str | None = path
        while url:
            response = self.request("GET", url, params=params if url == path else None)
            page = response.json()
            if not isinstance(page, dict) or not isinstance(page.get(key), list):
                raise GitHubApiError(f"expected an object with list {key!r} at {url}")
            items.extend(page[key])
            url = response.links.get("next", {}).get("url")
        return items

    def paginate(self, path: str, params: dict[str, Any] | None = None) -> list[Any]:
        """Follow RFC 5988 Link headers; every page spends budget."""
        items: list[Any] = []
        params = dict(params or {})
        params.setdefault("per_page", 100)
        url: str | None = path
        while url:
            response = self.request("GET", url, params=params if url == path else None)
            page = response.json()
            if not isinstance(page, list):
                raise GitHubApiError(f"paginate expected a list at {url}")
            items.extend(page)
            url = response.links.get("next", {}).get("url")
        return items
