"""Export profiles and UI safe zones per platform (1080x1920 canvas)."""

from __future__ import annotations

from pydantic import BaseModel


class SafeZone(BaseModel):
    top: int
    bottom: int
    left: int
    right: int


class ExportProfile(BaseModel):
    name: str
    width: int = 1080
    height: int = 1920
    fps: int = 30
    video_bitrate: str = "8M"
    max_bitrate: str = "12M"
    audio_bitrate: str = "192k"
    max_seconds: float = 90
    safe: SafeZone


PROFILES: dict[str, ExportProfile] = {
    # Safe zones keep text clear of platform UI (username, caption, buttons).
    "tiktok": ExportProfile(
        name="tiktok", max_seconds=600, safe=SafeZone(top=160, bottom=480, left=60, right=140)
    ),
    "instagram": ExportProfile(
        name="instagram", max_seconds=90, safe=SafeZone(top=220, bottom=420, left=60, right=120)
    ),
    "youtube": ExportProfile(
        name="youtube", max_seconds=60, safe=SafeZone(top=160, bottom=400, left=60, right=140)
    ),
}

# Union of all safe zones: one render works for every platform.
UNIVERSAL = ExportProfile(
    name="universal",
    max_seconds=60,
    safe=SafeZone(
        top=max(p.safe.top for p in PROFILES.values()),
        bottom=max(p.safe.bottom for p in PROFILES.values()),
        left=max(p.safe.left for p in PROFILES.values()),
        right=max(p.safe.right for p in PROFILES.values()),
    ),
)


def get_profile(name: str) -> ExportProfile:
    if name == "universal":
        return UNIVERSAL
    try:
        return PROFILES[name]
    except KeyError as exc:
        raise ValueError(f"unknown export profile {name!r}") from exc
