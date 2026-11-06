"""Import per-post metrics from Metricool analytics (`getAnalyticsDataByMetrics`).

The agent requests the metrics listed in `METRIC_SETS` for each network and saves the raw
answer together with the requested ids; `import_metrics` maps rows to posts by URL and
stores a `MetricSnapshot` per post.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from clipforge.core.models import MetricSnapshot, Platform, Post, PostStatus

# field ids per network: url, views, likes, comments, shares, avg watch time
METRIC_SETS: dict[str, dict[str, str]] = {
    "instagram": {
        "url": "IGRE06",
        "views": "IGRE23",
        "likes": "IGRE10",
        "comments": "IGRE07",
        "shares": "IGRE21",
        "watch_time_s": "IGRE24",
    },
    "tiktok": {
        "url": "TKPO03",
        "views": "TKPO07",
        "likes": "TKPO08",
        "comments": "TKPO09",
        "shares": "TKPO10",
        "watch_time_s": "TKPO15",
    },
}


def request_spec() -> dict[str, list[str]]:
    """Metric ids the agent should request per network."""
    return {net: list(fields.values()) for net, fields in METRIC_SETS.items()}


def _norm_url(url: str) -> str:
    url = url.strip().split("?")[0].rstrip("/").lower()
    return re.sub(r"^https?://(www\.|m\.)?", "", url)


def _num(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return 0.0


def _row_dict(row: Any, requested: list[str]) -> dict[str, Any]:
    """Rows may be dicts keyed by field id (or label) or lists aligned with `requested`."""
    if isinstance(row, dict):
        out: dict[str, Any] = {}
        for key, value in row.items():
            m = re.match(r"^([A-Z]{4}\d{2,4})", str(key))
            out[m.group(1) if m else str(key)] = value
        return out
    if isinstance(row, list | tuple):
        return dict(zip(requested, row, strict=False))
    return {}


def import_metrics(
    session: Session,
    network: str,
    requested: list[str],
    answer: dict[str, Any],
    *,
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    if network not in METRIC_SETS:
        raise ValueError(f"unsupported network {network!r}")
    fields = METRIC_SETS[network]
    captured_at = captured_at or datetime.now(UTC)
    posts = session.exec(
        select(Post).where(
            Post.status == PostStatus.PUBLISHED,
            Post.platform == Platform(network),
        )
    ).all()
    by_url = {_norm_url(p.published_url): p for p in posts if p.published_url}
    matched, unmatched = [], 0
    for raw in answer.get("rows", []):
        row = _row_dict(raw, requested)
        url = row.get(fields["url"])
        post = by_url.get(_norm_url(str(url))) if url else None
        if post is None:
            unmatched += 1
            continue
        snap = MetricSnapshot(
            post_id=post.id or 0,
            captured_at=captured_at,
            views=int(_num(row.get(fields["views"]))),
            likes=int(_num(row.get(fields["likes"]))),
            comments=int(_num(row.get(fields["comments"]))),
            shares=int(_num(row.get(fields["shares"]))),
            watch_time_s=_num(row.get(fields["watch_time_s"])) or None,
        )
        session.add(snap)
        matched.append({"post_id": post.id, "views": snap.views, "likes": snap.likes})
    session.flush()
    return {"matched": matched, "unmatched_rows": unmatched, "published_posts": len(posts)}


def latest_snapshots(session: Session) -> dict[int, MetricSnapshot]:
    latest: dict[int, MetricSnapshot] = {}
    for s in session.exec(select(MetricSnapshot)).all():
        cur = latest.get(s.post_id)
        if cur is None or s.captured_at >= cur.captured_at:
            latest[s.post_id] = s
    return latest
