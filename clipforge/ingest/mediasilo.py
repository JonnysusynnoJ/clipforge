"""Public MediaSilo "spotlight" share pages (official campaign content folders).

The public share page embeds its asset list (with time-limited signed download URLs)
in the server-rendered HTML. We only read that public page; nothing behind a login.
"""

from __future__ import annotations

import html
import json
import re
import urllib.request
from dataclasses import dataclass
from pathlib import Path

USER_AGENT = "clipforge/0.1 (campaign asset downloader)"
_VIDEO_EXT = (".mp4", ".mov", ".m4v", ".mkv", ".webm")


@dataclass(frozen=True)
class RemoteAsset:
    file_name: str
    download_url: str

    @property
    def is_video(self) -> bool:
        return self.file_name.lower().endswith(_VIDEO_EXT)


def fetch_page(url: str, timeout: float = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body: bytes = resp.read()
    return body.decode("utf-8", errors="replace")


def _unescape(url: str) -> str:
    url = url.encode().decode("unicode_escape") if "\\u" in url else url
    return html.unescape(url)


def parse_assets(page: str, prefer_height: int = 1080) -> list[RemoteAsset]:
    """Extract downloadable video assets from a spotlight page.

    For every video file the rendition closest to `prefer_height` (e.g. `x1080.mp4`) is
    chosen; the signed URL carries the original file name in its content disposition.
    """
    if '"allowDownload":true' not in page:
        return []
    urls = {
        _unescape(u)
        for u in re.findall(r'https://[a-z0-9]+\.cloudfront\.net/[^"\\\s]+\.mp4\?[^"\\\s]+', page)
    }
    by_file: dict[str, list[tuple[int, str]]] = {}
    for url in urls:
        m = re.search(r"filename%3D%22([^%]+(?:%2E|\.)[A-Za-z0-9]+)%22", url)
        if not m:
            continue
        name = m.group(1).replace("%2E", ".")
        hm = re.search(r"x(\d{3,4})\.mp4\?", url)
        height = int(hm.group(1)) if hm else 99999  # original/hq rendition
        by_file.setdefault(name, []).append((height, url))
    assets = []
    for name, renditions in sorted(by_file.items()):
        _, url = min(renditions, key=lambda hu: abs(hu[0] - prefer_height))
        assets.append(RemoteAsset(file_name=name, download_url=url))
    return assets


def page_metadata(page: str) -> dict[str, str]:
    names = re.findall(r'"fileName":"([^"]+)"', page)
    return {"file_count": str(len(set(names)))} if names else {}


def download(asset: RemoteAsset, target_dir: Path, timeout: float = 1800) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", asset.file_name)
    target = target_dir / safe
    if target.exists() and target.stat().st_size > 0:
        return target
    tmp = target.with_suffix(target.suffix + ".part")
    req = urllib.request.Request(asset.download_url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp, tmp.open("wb") as f:
        while chunk := resp.read(1 << 20):
            f.write(chunk)
    tmp.rename(target)
    return target


def to_json(assets: list[RemoteAsset]) -> str:
    return json.dumps([a.__dict__ for a in assets], indent=2)
