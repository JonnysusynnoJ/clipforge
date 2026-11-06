"""Build requests for the Metricool connector (`createScheduledPost`).

ClipForge does not call Metricool itself: the Claude agent session submits the request
through the Metricool MCP connector and records the result with `clipforge publish-record`.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from clipforge.core.models import Platform

TOOL = "mcp__Metricool_Social_Media_Management__createScheduledPost"


def _title(caption: str, limit: int = 100) -> str:
    first = next((ln.strip() for ln in caption.splitlines() if ln.strip()), "Clip")
    words = [w for w in first.split() if not w.startswith(("#", "@"))]
    title = " ".join(words) or "Clip"
    return title if len(title) <= limit else title[: limit - 1].rstrip() + "…"


def network_data(platform: Platform, caption: str, paid: bool) -> dict[str, Any]:
    if platform is Platform.INSTAGRAM:
        return {"instagramData": {"type": "REEL", "showReelOnFeed": True, "isAiGenerated": False}}
    if platform is Platform.TIKTOK:
        return {
            "tiktokData": {
                "privacyOption": "PUBLIC_TO_EVERYONE",
                "commercialContentThirdParty": paid,  # platform-level ad disclosure
                "commercialContentOwnBrand": False,
                "title": _title(caption, 90),
                "isAigc": False,
                "autoAddMusic": False,
            }
        }
    if platform is Platform.YOUTUBE:
        return {
            "youtubeData": {
                "title": _title(caption),
                "type": "short",
                "privacy": "public",
                "madeForKids": False,
                "isAiGeneratedContent": False,
            }
        }
    raise ValueError(f"unsupported platform {platform}")


def build_request(
    *,
    blog_id: str,
    platform: Platform,
    when: datetime,
    timezone: str,
    caption: str,
    media_url: str,
    draft: bool,
    paid: bool = True,
) -> dict[str, Any]:
    """Arguments for createScheduledPost. `info` is a JSON string as the tool expects."""
    if when.tzinfo is None:
        raise ValueError("publish time must be timezone-aware")
    info: dict[str, Any] = {
        "autoPublish": True,
        "draft": draft,
        "descendants": [],
        "firstCommentText": "",
        "hasNotReadNotes": False,
        "media": [media_url],
        "mediaAltText": [],
        "providers": [{"network": platform.value}],
        "publicationDate": {"dateTime": when.strftime("%Y-%m-%dT%H:%M:%S"), "timezone": timezone},
        "shortener": False,
        "smartLinkData": {"ids": []},
        "text": caption,
        **network_data(platform, caption, paid),
    }
    return {
        "tool": TOOL,
        "args": {
            "blogId": blog_id,
            "date": when.isoformat(timespec="seconds"),
            "info": json.dumps(info, ensure_ascii=False),
        },
    }
