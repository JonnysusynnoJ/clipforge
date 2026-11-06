"""ClipForge command line interface."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from clipforge.core import db, doctor
from clipforge.core.logging import configure_logging
from clipforge.core.settings import Settings, get_settings

app = typer.Typer(
    help="ClipForge: clipping pipeline for Whop Content Rewards.", no_args_is_help=True
)
db_app = typer.Typer(help="Database commands.", no_args_is_help=True)
app.add_typer(db_app, name="db")

console = Console()

_STYLE = {doctor.Status.OK: "green", doctor.Status.WARN: "yellow", doctor.Status.FAIL: "red"}


@app.callback()
def _main() -> None:
    settings = get_settings()
    configure_logging(settings.paths.logs)


@app.command("doctor")
def doctor_cmd() -> None:
    """Check ffmpeg, folders, database, ML backends, secrets and publishing config."""
    results = doctor.run_all(get_settings())
    table = Table(title="clipforge doctor")
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    for r in results:
        table.add_row(r.name, f"[{_STYLE[r.status]}]{r.status.value.upper()}[/]", r.detail)
    console.print(table)
    fails = sum(r.status is doctor.Status.FAIL for r in results)
    warns = sum(r.status is doctor.Status.WARN for r in results)
    console.print(f"{len(results)} checks: {fails} failed, {warns} warnings")
    raise typer.Exit(code=1 if fails else 0)


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Apply database migrations."""
    settings = get_settings()
    settings.ensure_dirs()
    db.upgrade(settings.database_url, revision)
    console.print(f"database at {revision}: {settings.paths.db_file}")


# --------------------------------------------------------------------------- pipeline


def _engine(settings: Settings):  # type: ignore[no-untyped-def]
    settings.ensure_dirs()
    db.upgrade(settings.database_url)
    return db.make_engine(settings.database_url)


def _load_list(path: Path, model):  # type: ignore[no-untyped-def]
    data = json.loads(path.read_text(encoding="utf-8"))
    items = data["items"] if isinstance(data, dict) and "items" in data else data
    return [model.model_validate(x) for x in items]


@app.command("campaign-add")
def campaign_add(campaign_json: Path) -> None:
    """Create or update a campaign from a campaign spec JSON file."""
    from clipforge.campaigns.rules import CampaignSpec
    from clipforge.pipeline import upsert_campaign

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        c = upsert_campaign(s, CampaignSpec.load(campaign_json))
        console.print(f"campaign {c.id}: {c.brand} ({c.status.value})")


@app.command("discover")
def discover_cmd() -> None:
    """Discover public campaigns, load details for the best ones and score them."""
    from clipforge.campaigns.service import discover_and_score

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        rows = discover_and_score(s, settings)
        console.print(f"{len(rows)} campaigns discovered/updated")
    campaigns_cmd()


@app.command("campaigns")
def campaigns_cmd(limit: int = 20) -> None:
    """List campaigns by score with budget, runway, expected earnings and flags."""
    from clipforge.campaigns.service import summary_rows

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        rows = summary_rows(s, limit)
    table = Table(title="campaigns (score = expected net $/clip at median views)")
    for name in ("id", "title", "status", "score", "cpm", "left $", "runway", "ready", "flags"):
        table.add_column(name, overflow="ellipsis", no_wrap=name != "flags")
    for r in rows:
        table.add_row(
            str(r["id"]), r["title"], r["status"], f"{r['score'] or 0:.2f}",
            f"{r['cpm'] or 0:.2f}", f"{r['budget_left'] or 0:,.0f}",
            f"{r['runway_days']}d" if r["runway_days"] else "-",
            "yes" if r["ready"] else "no", "; ".join(r["flags"])[:70],
        )  # fmt: skip
    console.print(table)


@app.command("campaign-add-url")
def campaign_add_url(url: str) -> None:
    """Add a campaign by its public contentrewards.com URL."""
    from clipforge.campaigns.service import add_by_url

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        c = add_by_url(s, settings, url)
        console.print(f"campaign {c.id}: {c.title} (score {c.score})")


@app.command("campaign-show")
def campaign_show(campaign_id: int) -> None:
    """Show raw brief text next to the parsed rules and the score report."""
    from rich.columns import Columns
    from rich.panel import Panel

    from clipforge.core.models import Campaign

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        c = s.get(Campaign, campaign_id)
        if c is None:
            raise typer.BadParameter(f"campaign {campaign_id} not found")
        console.print(f"[bold]{c.title or c.brand}[/] ({c.status.value}) {c.whop_url}")
        console.print(f"submit: {c.submit_url or '-'}  brief: {c.brief_url or '-'}")
        console.print_json(json.dumps(c.score_report or {}))
        console.print(Columns([
            Panel(c.raw_rules_text or "(no brief)", title="raw brief", width=70),
            Panel(json.dumps(c.parsed_rules, indent=1) if c.parsed_rules else "(not parsed)",
                  title="parsed rules", width=70),
        ]))  # fmt: skip


@app.command("campaign-brief")
def campaign_brief(
    campaign_id: int,
    url: Annotated[str, typer.Option(help="Public brief link (e.g. Google Doc)")] = "",
    text_file: Annotated[Path | None, typer.Option(help="Brief as text file")] = None,
) -> None:
    """Store a campaign brief and write rules_request.json for the agent."""
    from clipforge.campaigns.service import set_brief
    from clipforge.core.models import Campaign

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        c = s.get(Campaign, campaign_id)
        if c is None:
            raise typer.BadParameter(f"campaign {campaign_id} not found")
        text = text_file.read_text(encoding="utf-8") if text_file else None
        path = set_brief(s, settings, c, url=url or None, text=text)
    console.print(f"brief stored; agent request: {path}")


@app.command("campaign-rules")
def campaign_rules_cmd(campaign_id: int, rules_file: Path) -> None:
    """Validate the agent's parsed rules and store them (exit 1 with errors to fix)."""
    from clipforge.campaigns.service import apply_rules
    from clipforge.core.models import Campaign

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        c = s.get(Campaign, campaign_id)
        if c is None:
            raise typer.BadParameter(f"campaign {campaign_id} not found")
        error, warnings = apply_rules(s, c, rules_file.read_text(encoding="utf-8"))
    if error:
        console.print(f"[red]rules rejected, fix and retry:[/]\n{error}")
        raise typer.Exit(1)
    for w in warnings:
        console.print(f"[yellow]warning:[/] {w}")
    console.print("rules stored")


@app.command("campaign-select")
def campaign_select() -> None:
    """Activate the best ready campaigns (max campaigns.max_active)."""
    from clipforge.campaigns.service import select_active

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        console.print_json(json.dumps(select_active(s, settings)))


@app.command("account-add")
def account_add(
    platform: str,
    handle: str,
    metricool_blog_id: str = "",
    whop_linked: bool = False,
    daily_limit: int = 0,
) -> None:
    """Register a social account used for posting (or update it)."""
    from sqlmodel import select as sql_select

    from clipforge.core.models import Platform, SocialAccount

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        p = Platform(platform)
        acc = s.exec(sql_select(SocialAccount).where(
            SocialAccount.platform == p, SocialAccount.handle == handle)).first()  # fmt: skip
        acc = acc or SocialAccount(platform=p, handle=handle)
        acc.metricool_blog_id = metricool_blog_id or acc.metricool_blog_id
        acc.whop_linked = whop_linked
        acc.daily_limit = daily_limit or None
        s.add(acc)
        s.flush()
        console.print(f"account {acc.id}: {p.value} @{handle} whop_linked={whop_linked}")


@app.command("accounts")
def accounts_cmd() -> None:
    """List registered social accounts."""
    from sqlmodel import select as sql_select

    from clipforge.core.models import SocialAccount

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        for a in s.exec(sql_select(SocialAccount)).all():
            console.print(
                f"{a.id}: {a.platform.value} @{a.handle} metricool={a.metricool_blog_id} "
                f"whop_linked={a.whop_linked} active={a.active}"
            )


@app.command("ingest")
def ingest_cmd(
    campaign_id: int,
    file: Path,
    origin: Annotated[str, typer.Option(help="Where the file came from (URL)")] = "",
    license_note: Annotated[str, typer.Option(help="Manual authorization note")] = "",
) -> None:
    """Import an authorized local file for a campaign (deduplicated by hash)."""
    from clipforge.core.models import Campaign
    from clipforge.ingest.local import import_file

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        campaign = s.get(Campaign, campaign_id)
        if campaign is None:
            raise typer.BadParameter(f"campaign {campaign_id} not found")
        asset = import_file(
            s,
            campaign,
            file,
            sources_dir=settings.paths.sources,
            origin_url=origin or file.resolve().as_uri(),
            license_note=license_note,
        )
        console.print(f"asset {asset.id}: {asset.local_path} ({asset.duration:.1f}s)")


@app.command("ingest-mediasilo")
def ingest_mediasilo(campaign_id: int, url: str, prefer_height: int = 1080) -> None:
    """Download all video files of a public MediaSilo share listed in the campaign rules."""
    from clipforge.core.models import Campaign
    from clipforge.ingest import mediasilo
    from clipforge.ingest.local import authorize, import_file
    from clipforge.pipeline import campaign_rules

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        campaign = s.get(Campaign, campaign_id)
        if campaign is None:
            raise typer.BadParameter(f"campaign {campaign_id} not found")
        authorize(campaign_rules(campaign), url, "")  # raises if not an allowed source
        assets = [
            a
            for a in mediasilo.parse_assets(mediasilo.fetch_page(url), prefer_height)
            if a.is_video
        ]
        if not assets:
            console.print("[red]no downloadable videos found on the page[/]")
            raise typer.Exit(1)
        tmp = settings.paths.sources / "_download"
        for remote in assets:
            local = mediasilo.download(remote, tmp)
            asset = import_file(
                s, campaign, local, sources_dir=settings.paths.sources, origin_url=url, move=True
            )
            console.print(f"asset {asset.id}: {remote.file_name} ({asset.duration:.1f}s)")


@app.command("transcribe")
def transcribe_cmd(asset_id: int) -> None:
    """Transcribe a source asset (cached by content hash)."""
    from clipforge.core.models import SourceAsset
    from clipforge.pipeline import stage_transcribe

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        asset = s.get(SourceAsset, asset_id)
        if asset is None:
            raise typer.BadParameter(f"asset {asset_id} not found")
        t = stage_transcribe(s, settings, asset)
        console.print(f"{len(t.words)} words, {t.speech_seconds:.1f}s speech, lang={t.language}")


@app.command("candidates")
def candidates_cmd(asset_id: int, run_id: str = "") -> None:
    """Generate diverse clip candidates + contact sheets; writes candidates.json."""
    from clipforge.core.models import Campaign, SourceAsset
    from clipforge.pipeline import new_run_id, run_dir, stage_candidates, stage_transcribe

    settings = get_settings()
    run_id = run_id or new_run_id()
    with db.session_scope(_engine(settings)) as s:
        asset = s.get(SourceAsset, asset_id)
        if asset is None:
            raise typer.BadParameter(f"asset {asset_id} not found")
        campaign = s.get(Campaign, asset.campaign_id)
        assert campaign is not None
        t = stage_transcribe(s, settings, asset)
        rows, _ = stage_candidates(
            s, settings, campaign=campaign, asset=asset, transcript=t, run_id=run_id
        )
    console.print(
        f"run {run_id}: {len(rows)} candidates -> {run_dir(settings, run_id) / 'candidates.json'}"
    )


@app.command("score")
def score_cmd(run_id: str, scores_file: Path) -> None:
    """Apply the review agent's candidate scores (scores.json)."""
    from clipforge.pipeline import CandidateScore, apply_scores

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        stats = apply_scores(s, settings, run_id, _load_list(scores_file, CandidateScore))
    console.print(stats)


@app.command("render")
def render_cmd(
    run_id: str, max_clips: int = 8, profile: str = "universal", layout: str = "auto"
) -> None:
    """Render the best candidates of a run, run compliance, write review.json."""
    from clipforge.pipeline import run_dir, stage_render

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        rows = stage_render(
            s, settings, run_id, max_clips=max_clips, profile_name=profile, layout=layout
        )
    console.print(f"{len(rows)} clips rendered -> {run_dir(settings, run_id) / 'review.json'}")


@app.command("review")
def review_cmd(run_id: str, decisions_file: Path) -> None:
    """Apply the review agent's decisions (decisions.json): approve / reject + captions."""
    from clipforge.pipeline import ReviewDecision, apply_review

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        result = apply_review(s, settings, run_id, _load_list(decisions_file, ReviewDecision))
    console.print_json(json.dumps(result))


@app.command("run")
def run_cmd(
    file: Annotated[Path, typer.Option(help="Source video file")],
    campaign_json: Annotated[Path, typer.Option(help="Campaign spec JSON")],
    origin: Annotated[str, typer.Option(help="Origin URL of the file")] = "",
    license_note: str = "",
    scores: Annotated[Path | None, typer.Option(help="Agent scores.json to apply")] = None,
    max_clips: int = 8,
    layout: str = "auto",
) -> None:
    """End-to-end local run: ingest, transcribe, candidates, (scores), render, report."""
    from clipforge.campaigns.rules import CampaignSpec
    from clipforge.ingest.local import import_file
    from clipforge.pipeline import (
        CandidateScore,
        Timings,
        apply_scores,
        new_run_id,
        run_dir,
        run_summary,
        stage_candidates,
        stage_render,
        stage_transcribe,
        upsert_campaign,
    )  # fmt: skip

    settings = get_settings()
    timings = Timings()
    run_id = new_run_id()
    t0 = time.perf_counter()
    with db.session_scope(_engine(settings)) as s:
        campaign = upsert_campaign(s, CampaignSpec.load(campaign_json))
        with timings.stage("ingest"):
            asset = import_file(
                s,
                campaign,
                file,
                sources_dir=settings.paths.sources,
                origin_url=origin or file.resolve().as_uri(),
                license_note=license_note,
            )
        with timings.stage("transcribe"):
            transcript = stage_transcribe(s, settings, asset)
        with timings.stage("candidates"):
            rows, _ = stage_candidates(
                s, settings, campaign=campaign, asset=asset, transcript=transcript, run_id=run_id
            )
        s.commit()
        if scores is not None:
            with timings.stage("score"):
                apply_scores(s, settings, run_id, _load_list(scores, CandidateScore))
        with timings.stage("render"):
            rendered = stage_render(s, settings, run_id, max_clips=max_clips, layout=layout)
        s.commit()
        summary = run_summary(s, run_id)
    report = {
        "run_id": run_id,
        "campaign": campaign_json.name,
        "source": str(file),
        "candidates": len(rows),
        "rendered": len(rendered),
        "timings_s": {**timings.stages, "total": round(time.perf_counter() - t0, 2)},
        "llm_cost_usd": 0.0,
        "llm_note": "judgement steps run in the Claude agent session (plan usage, no API cost)",
        "clips": summary,
    }
    out = run_dir(settings, run_id) / "report.json"
    out.write_text(json.dumps(report, indent=2))
    table = Table(title=f"run {run_id}")
    for col_name in ("clip", "status", "span", "score", "compliance"):
        table.add_column(col_name)
    for c in summary:
        if c["video"]:
            table.add_row(
                str(c["clip_id"]),
                c["status"],
                f"{c['start']}-{c['end']}",
                str(c["llm_score"] or c["heuristic_score"]),
                "ok" if c["compliance_passed"] else "; ".join(c["hard_violations"]),
            )
    console.print(table)
    console.print(f"timings: {report['timings_s']}  LLM cost: $0.00  report: {out}")


# --------------------------------------------------------------------------- publishing


def _parse_hours(values: list[str]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for v in values:
        platform, _, hours = v.partition("=")
        out[platform] = [int(h) for h in hours.split(",") if h.strip()]
    return out


@app.command("publish-plan")
def publish_plan(
    best_hours: Annotated[
        list[str], typer.Option(help="Preferred hours per platform, e.g. instagram=18,20")
    ] = [],  # noqa: B006
    limit: Annotated[int, typer.Option(help="Max clips to plan in this run")] = 20,
) -> None:
    """Schedule approved clips: guards, rate-limit slots, R2 upload, Metricool requests."""
    from clipforge.publish.service import plan_posts, queue

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        result = plan_posts(s, settings, preferred_hours=_parse_hours(best_hours), limit=limit)
        pending = queue(s)
    path = settings.paths.data_dir / "publish_queue.json"
    path.write_text(json.dumps(pending, indent=2, ensure_ascii=False))
    console.print_json(json.dumps(result))
    console.print(f"{len(pending)} request(s) for the agent -> {path}")


@app.command("publish-queue")
def publish_queue() -> None:
    """Print pending Metricool requests (submit each via the Metricool connector)."""
    from clipforge.publish.service import queue

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        console.print_json(json.dumps(queue(s), ensure_ascii=False))


@app.command("publish-record")
def publish_record(
    post_id: int,
    ok: Annotated[bool, typer.Option("--ok/--failed")] = True,
    metricool_id: str = "",
    planner_url: str = "",
    error: str = "",
) -> None:
    """Record the Metricool connector's answer for a planned post."""
    from clipforge.publish.service import record_result

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        post = record_result(s, post_id, ok=ok, metricool_post_id=metricool_id or None,
                             planner_url=planner_url or None, error=error or None)  # fmt: skip
        console.print(f"post {post.id}: {post.status.value}")


@app.command("publish-published")
def publish_published(post_id: int, url: str) -> None:
    """Store the live post URL once Metricool has published it."""
    from clipforge.publish.service import mark_published

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        post = mark_published(s, post_id, url)
        console.print(f"post {post.id}: {post.status.value} {post.published_url}")


@app.command("publish-cancel")
def publish_cancel(post_id: int, reason: str = "cancelled") -> None:
    """Cancel a planned/scheduled post (also delete it in Metricool!)."""
    from clipforge.publish.service import cancel

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        post = cancel(s, post_id, reason)
        console.print(f"post {post.id}: {post.status.value}")


@app.command("publish-export")
def publish_export(post_id: int) -> None:
    """Manual fallback: export video + caption for posting by hand."""
    from clipforge.publish.service import export_manual

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        console.print(f"exported to {export_manual(s, settings, post_id)}")


@app.command("posts")
def posts_cmd() -> None:
    """List posts with status, time and URLs."""
    from sqlmodel import select as sql_select

    from clipforge.core.models import Post

    settings = get_settings()
    with db.session_scope(_engine(settings)) as s:
        for p in s.exec(sql_select(Post)).all():
            console.print(
                f"{p.id}: {p.status.value} {p.platform.value if p.platform else '-'} "
                f"{p.scheduled_at:%Y-%m-%d %H:%M}Z draft={p.draft} "
                f"{p.published_url or p.planner_url or p.error or ''}"
            )


if __name__ == "__main__":
    app()
