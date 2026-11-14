"""In-memory stand-in for requests.Session against the GitHub REST API.

Routes map an exact URL path (query ignored) to (status, body). A body is
JSON-serializable, or bytes for a download. Every call is recorded so tests
can prove a command only reads.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any
from urllib.parse import unquote, urlsplit


class FakeResponse:
    def __init__(self, status: int, body: Any, links: dict | None = None) -> None:
        self.status_code = status
        self._body = body
        self.links = links or {}
        self.headers: dict[str, str] = {}

    @property
    def content(self) -> bytes:
        if isinstance(self._body, bytes):
            return self._body
        return json.dumps(self._body).encode()

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        if isinstance(self._body, bytes):
            raise ValueError("not json")
        return self._body

    def iter_content(self, chunk_size: int = 65536):
        data = self.content
        for i in range(0, len(data), chunk_size):
            yield data[i:i + chunk_size]

    def close(self) -> None:
        pass


class FakeSession:
    def __init__(self, routes: dict[str, tuple[int, Any]]) -> None:
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **kwargs) -> FakeResponse:
        assert "timeout" in kwargs, "every request must carry a timeout"
        path = unquote(urlsplit(url).path)
        self.calls.append((method, path))
        if path not in self.routes:
            return FakeResponse(404, {"message": "Not Found"})
        status, body, *links = self.routes[path]
        return FakeResponse(status, body, links[0] if links else None)


def make_zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()
