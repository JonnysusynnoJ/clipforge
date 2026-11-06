"""Public Google Drive file shares (official campaign material).

Only files shared as "anyone with the link" are supported; nothing behind a login.
"""

from __future__ import annotations

import re
import urllib.request
from pathlib import Path

USER_AGENT = "clipforge/0.1 (campaign asset downloader)"


class DriveQuotaError(RuntimeError):
    """The public file hit Google's daily download quota."""


def file_id(url: str) -> str | None:
    for pattern in (r"/file/d/([A-Za-z0-9_-]{10,})", r"[?&]id=([A-Za-z0-9_-]{10,})"):
        m = re.search(pattern, url)
        if m:
            return m.group(1)
    return None


def is_drive_file(url: str) -> bool:
    return "drive.google.com" in url and file_id(url) is not None


def download_url(fid: str) -> str:
    return f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"


def download(url: str, target_dir: Path, timeout: float = 3600) -> Path:
    """Download a public Drive file; returns the local path (name from the server)."""
    fid = file_id(url)
    if fid is None:
        raise ValueError(f"not a Google Drive file link: {url}")
    target_dir.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(download_url(fid), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        ctype = resp.headers.get("Content-Type", "")
        if "text/html" in ctype:
            page = resp.read(20000).decode("utf-8", errors="replace")
            if "Quota exceeded" in page:
                raise DriveQuotaError(
                    "Google Drive download quota for this file is exceeded (too many downloads "
                    "today); retry tomorrow or ask the owner for a copy in his own Drive"
                )
            raise RuntimeError("Drive returned a web page: file is not public or needs a login")
        disp = resp.headers.get("Content-Disposition", "")
        m = re.search(r'filename="([^"]+)"', disp)
        name = re.sub(r"[^A-Za-z0-9._-]+", "_", m.group(1) if m else f"{fid}.mp4")
        target = target_dir / name
        if target.exists() and target.stat().st_size > 0:
            return target
        tmp = target.with_suffix(target.suffix + ".part")
        with tmp.open("wb") as f:
            while chunk := resp.read(1 << 22):
                f.write(chunk)
    tmp.rename(target)
    return target
