"""Markdown reports: weekly performance + earnings, pending submissions, warnings."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import Session, select

from clipforge.analytics.earnings import PostEarnings, estimate, totals
from clipforge.core.models import ClipCandidate, ClipStatus, Post, RenderedClip, UsageLog
from clipforge.core.settings import Settings
from clipforge.submit.service import pending


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _fmt_row(r: PostEarnings) -> str:
    er = f"{r.engagement_rate:.2%}" if r.engagement_rate is not None else "-"
    flag = " ⚠️" if r.min_engagement_ok is False else ""
    sub = "ja" if r.submitted else "nein"
    url = r.url or "-"
    return (
        f"| {r.post_id} | {r.campaign[:30]} | {r.platform} | {r.views:,} | {er}{flag} | "
        f"${r.net_usd:.2f} | {sub} | {url} |"
    )


def weekly(session: Session, settings: Settings, now: datetime | None = None, days: int = 7) -> str:
    now = now or datetime.now(UTC)
    since = now - timedelta(days=days)
    rows = estimate(session, settings)
    recent_ids = {p.id for p in session.exec(select(Post)).all() if _aware(p.scheduled_at) >= since}
    recent = [r for r in rows if r.post_id in recent_ids]
    t_all, t_week = totals(rows), totals(recent)

    clips = session.exec(select(ClipCandidate)).all()
    produced = [c for c in clips if _aware(c.created_at) >= since]
    approved = sum(
        1
        for c in produced
        if c.status
        not in (
            ClipStatus.CANDIDATE,
            ClipStatus.REJECTED,
            ClipStatus.FAILED,
            ClipStatus.PENDING_REVIEW,
        )
    )
    rendered = {r.candidate_id for r in session.exec(select(RenderedClip)).all()}
    rejected = sum(1 for c in produced if c.status is ClipStatus.REJECTED and c.id in rendered)
    cost = sum(
        u.cost_usd for u in session.exec(select(UsageLog)).all() if _aware(u.created_at) >= since
    )

    lines = [
        f"# ClipForge Wochenbericht ({since:%d.%m.} - {now:%d.%m.%Y})",
        "",
        "## Überblick",
        "",
        f"- Posts veröffentlicht (Woche): {t_week['posts']} · Views: {t_week['views']:,}",
        f"- Geschätzte Einnahmen (Woche, netto nach Fee): ${t_week['net_usd']:.2f}",
        f"- Gesamt seit Start: {t_all['posts']} Posts · {t_all['views']:,} Views · "
        f"${t_all['net_usd']:.2f}",
        f"- Clips gerendert und freigegeben: {approved} · vom Agenten abgelehnt: {rejected}",
        "- Deine Review-Zeit: 0 min (Agent-Freigabe) · Einnahmen pro Review-Stunde: n/a",
        f"- API-Kosten (UsageLog): ${cost:.2f} · Agent läuft über das Claude-Abo",
        "",
        "## Pro Kampagne (gesamt)",
        "",
        "| Kampagne | Posts | Views | Netto |",
        "|---|---:|---:|---:|",
    ]
    for name, c in t_all["by_campaign"].items():
        lines.append(f"| {name} | {int(c['posts'])} | {int(c['views']):,} | ${c['net_usd']:.2f} |")
    lines += [
        "",
        "## Posts",
        "",
        "| Post | Kampagne | Plattform | Views | Engagement | Netto | Eingereicht | URL |",
        "|---:|---|---|---:|---:|---:|---|---|",
    ]
    lines += [_fmt_row(r) for r in sorted(rows, key=lambda r: r.views, reverse=True)]

    with_views = [r for r in rows if r.views]
    if with_views:
        best = max(with_views, key=lambda r: r.views)
        worst = min(with_views, key=lambda r: r.views)
        lines += [
            "",
            "## Bester / schwächster Clip",
            "",
            f"- Bester: Post {best.post_id} mit {best.views:,} Views ({best.url})",
            f"- Schwächster: Post {worst.post_id} mit {worst.views:,} Views ({worst.url})",
        ]

    warnings = [r for r in rows if r.min_engagement_ok is False]
    below = [r for r in rows if r.views and r.net_usd == 0 and r.views_for_min_payout]
    if warnings or below:
        lines += ["", "## Warnungen", ""]
        lines += [
            f"- Post {r.post_id}: Engagement {r.engagement_rate:.2%} unter 0,5 %"
            " (Kampagnen-Mindestwert, Ablehnungsrisiko)"
            for r in warnings
        ]
        lines += [
            f"- Post {r.post_id}: {r.views:,} Views, Mindestauszahlung erst ab"
            f" {r.views_for_min_payout:,} Views"
            for r in below
        ]

    groups = pending(session)
    lines += ["", "## Noch bei Whop einzureichen", ""]
    if not groups:
        lines.append("Alles eingereicht.")
    for g in groups:
        lines.append(f"- **{g['campaign']}** → {g['submit_url']}")
        lines += [f"  - {p['url']} (Post {p['post_id']})" for p in g["posts"]]
    lines += [
        "",
        "_Feature-Auswertung (welche Clip-Merkmale performen) folgt ab ~20 Posts mit "
        "Views (Phase 6)._",
        "",
    ]
    return "\n".join(lines)


def write_weekly(session: Session, settings: Settings, now: datetime | None = None) -> Path:
    now = now or datetime.now(UTC)
    text = weekly(session, settings, now=now)
    path = settings.paths.data_dir / "reports" / f"weekly_{now:%G-W%V}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path
