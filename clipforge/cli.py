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


if __name__ == "__main__":
    app()
