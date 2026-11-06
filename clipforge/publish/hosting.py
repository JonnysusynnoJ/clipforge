"""Public media hosting for Metricool: Cloudflare R2 via the Cloudflare REST API.

The API token is injected by the session's network proxy for api.cloudflare.com; the code
only sends a placeholder Authorization header and never sees the real value.
"""

from __future__ import annotations

import json
import mimetypes
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

API = "https://api.cloudflare.com/client/v4"
PLACEHOLDER_AUTH = "Bearer proxy-injected"
USER_AGENT = "clipforge/0.1 (media check)"


class HostingError(RuntimeError):
    pass


class MediaHost(Protocol):
    def upload(self, path: Path, key: str) -> str: ...

    def verify(self, url: str) -> bool: ...

    def delete(self, key: str) -> None: ...


@dataclass(frozen=True)
class R2Host:
    account_id: str
    bucket: str
    public_base_url: str
    timeout: float = 600

    def _object_url(self, key: str) -> str:
        return f"{API}/accounts/{self.account_id}/r2/buckets/{self.bucket}/objects/{key}"

    def public_url(self, key: str) -> str:
        return f"{self.public_base_url.rstrip('/')}/{key}"

    def upload(self, path: Path, key: str) -> str:
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        req = urllib.request.Request(
            self._object_url(key),
            data=path.read_bytes(),
            method="PUT",
            headers={
                "Content-Type": ctype,
                "Authorization": PLACEHOLDER_AUTH,
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise HostingError(f"R2 upload failed ({exc.code}): {exc.read()[:300]!r}") from exc
        if not body.get("success", False):
            raise HostingError(f"R2 upload failed: {body.get('errors')}")
        return self.public_url(key)

    def verify(self, url: str) -> bool:
        # Cloudflare rejects the default Python-urllib user agent
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.status == 200 and int(resp.headers.get("Content-Length", "0")) > 0
        except urllib.error.URLError:
            return False

    def delete(self, key: str) -> None:
        req = urllib.request.Request(
            self._object_url(key), method="DELETE", headers={"Authorization": PLACEHOLDER_AUTH}
        )
        try:
            urllib.request.urlopen(req, timeout=60).close()
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise HostingError(f"R2 delete failed ({exc.code})") from exc
