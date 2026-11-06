"""Pipeline stages. Each stage is idempotent and callable on its own via the CLI.

Judgement steps are done by the Claude agent through JSON files in the run directory:

    candidates.json  (written by `candidates`)  ->  scores.json  (written by the agent)
    review.json      (written by `render`)      ->  decisions.json (written by the agent)
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from sqlmodel import Session, col, select

from clipforge.campaigns.rules import CampaignSpec, ParsedRules
from clipforge.compliance.check import PostMetadata, check
from clipforge.core.compliance_report import ComplianceReport
from clipforge.core.logging import get_logger
from clipforge.core.models import (
    Actor,
    Campaign,
    CampaignStatus,
    ClipCandidate,
    ClipStatus,
    Platform,
    RenderedClip,
    SourceAsset,
    Transcript,
)
from clipforge.core.settings import Settings
from clipforge.core.state import TransitionError, latest_render, transition
from clipforge.highlights import candidates as cand
from clipforge.highlights.features import Timeline, compute_timeline
from clipforge.highlights.select import select as select_diverse
from clipforge.highlights.select import variants
from clipforge.media import contact_sheet
from clipforge.render.profiles import get_profile
from clipforge.render.render import RenderSpec, render
from clipforge.transcribe.whisper import TranscriptResult, Word, transcribe

log = get_logger(__name__)


# --------------------------------------------------------------------------- helpers


@dataclass
class Timings:
    stages: dict[str, float] = field(default_factory=dict)

    @contextmanager
    def stage(self, name: str):  # type: ignore[no-untyped-def]
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = round(self.stages.get(name, 0) + time.perf_counter() - t0, 2)


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def run_dir(settings: Settings, run_id: str) -> Path:
    d = settings.paths.data_dir / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def effective_lengths(settings: Settings, rules: ParsedRules) -> tuple[float, float]:
    return (
        rules.min_seconds or settings.clips.min_seconds,
        rules.max_seconds or settings.clips.max_seconds,
    )


def campaign_rules(campaign: Campaign) -> ParsedRules:
    return ParsedRules.model_validate(campaign.parsed_rules)


# --------------------------------------------------------------------------- campaign


def upsert_campaign(session: Session, spec: CampaignSpec) -> Campaign:
    campaign = session.exec(select(Campaign).where(Campaign.whop_url == spec.whop_url)).first()
    campaign = campaign or Campaign(whop_url=spec.whop_url, brand=spec.brand)
    campaign.brand = spec.brand
    campaign.category = spec.category
    campaign.cpm_usd = spec.cpm_usd
    campaign.budget_total = spec.budget_total
    campaign.budget_left = spec.budget_left
    campaign.platforms = [p.value for p in spec.rules.allowed_platforms]
    campaign.geo_restrictions = spec.rules.geo_restrictions
    campaign.raw_rules_text = spec.raw_rules_text
    campaign.parsed_rules = spec.rules.model_dump(mode="json")
    if campaign.status is CampaignStatus.DISCOVERED:
        campaign.status = CampaignStatus.ACTIVE
    session.add(campaign)
    session.flush()
    return campaign


# --------------------------------------------------------------------------- transcribe


def stage_transcribe(session: Session, settings: Settings, asset: SourceAsset) -> TranscriptResult:
    assert asset.local_path and asset.sha256
    result = transcribe(
        Path(asset.local_path),
        sha256=asset.sha256,
        model_name=settings.transcription.model,
        backend=settings.transcription.backend,
        cache_dir=settings.paths.cache,
    )
    existing = session.exec(select(Transcript).where(Transcript.asset_id == asset.id)).first()
    if existing is None:
        data = result.to_json()
        session.add(
            Transcript(
                asset_id=asset.id or 0,
                language=result.language,
                model=f"{result.backend}:{result.model}",
                words=data["words"],
                segments=data["segments"],
            )
        )
        session.flush()
    return result


# --------------------------------------------------------------------------- candidates


def stage_candidates(
    session: Session,
    settings: Settings,
    *,
    campaign: Campaign,
    asset: SourceAsset,
    transcript: TranscriptResult,
    run_id: str,
) -> tuple[list[ClipCandidate], Timeline]:
    assert asset.local_path and asset.sha256 and asset.duration
    source = Path(asset.local_path)
    rules = campaign_rules(campaign)
    min_s, max_s = effective_lengths(settings, rules)
    timeline = compute_timeline(
        source, sha256=asset.sha256, duration=asset.duration, cache_dir=settings.paths.cache
    )
    pool = cand.generate(timeline, transcript, min_seconds=min_s, max_seconds=max_s)
    hl = settings.highlights
    picked = select_diverse(pool, k=hl.top_n_for_review, non_obvious_share=hl.non_obvious_share)
    use_speech = cand.speech_mode(transcript, timeline.duration)
    bounds = cand.boundaries(timeline, transcript, use_speech)

    out_dir = run_dir(settings, run_id)
    rows: list[ClipCandidate] = []
    export: list[dict[str, Any]] = []
    for moment in picked:
        group = [moment, *variants(moment, bounds, hl.variants_per_moment, min_s, max_s)]
        parent_id: int | None = None
        for c in group:
            feats = cand.window_features(c, timeline, transcript, use_speech)
            feats["heuristic_score"] = cand.heuristic_score(feats, use_speech)
            feats["non_obvious"] = moment.features.get("non_obvious", 0.0)
            row = ClipCandidate(
                asset_id=asset.id or 0,
                start=c.start,
                end=c.end,
                features={**feats, "variant_of": parent_id},
                run_id=run_id,
            )
            session.add(row)
            session.flush()
            parent_id = parent_id or row.id
            sheet = contact_sheet(source, c.start, c.end, out_dir / "sheets" / f"cand_{row.id}.jpg")
            rows.append(row)
            export.append(
                {
                    "id": row.id,
                    "variant_of": row.features["variant_of"],
                    "start": round(c.start, 2),
                    "end": round(c.end, 2),
                    "duration": round(c.end - c.start, 2),
                    "heuristic_score": round(feats["heuristic_score"], 3),
                    "non_obvious": bool(feats["non_obvious"]),
                    "features": {k: round(v, 3) for k, v in feats.items() if isinstance(v, float)},
                    "transcript": " ".join(
                        w.w for w in transcript.words if c.start <= w.start < c.end
                    ),
                    "contact_sheet": str(sheet),
                }
            )
    doc = {
        "run_id": run_id,
        "campaign": {"brand": campaign.brand, "whop_url": campaign.whop_url},
        "source": {"asset_id": asset.id, "duration": asset.duration, "speech_mode": use_speech},
        "rules": rules.model_dump(mode="json"),
        "instructions": (
            "Score every candidate 0-10 for: hook strength in the first 2 s, understandable "
            "without context, emotional/visual peak, clean ending, fit to the campaign. "
            "Look at each contact_sheet. Write scores.json as a list of "
            '{"id", "score", "reasoning", "hook_text", "start", "end", "decision"} with '
            'decision "render" or "skip"; start/end optional (snapped to boundaries).'
        ),
        "boundaries": bounds,
        "candidates": export,
    }
    (out_dir / "candidates.json").write_text(json.dumps(doc, indent=2))
    return rows, timeline


class CandidateScore(BaseModel):
    id: int
    score: float = Field(ge=0, le=10)
    reasoning: str = ""
    hook_text: str | None = None
    start: float | None = None
    end: float | None = None
    decision: str = "render"


def _snap(t: float, points: list[float]) -> float:
    return min(points, key=lambda p: abs(p - t)) if points else t


def apply_scores(
    session: Session, settings: Settings, run_id: str, scores: list[CandidateScore]
) -> dict[str, int]:
    doc = json.loads((run_dir(settings, run_id) / "candidates.json").read_text())
    bounds: list[float] = doc["boundaries"]
    min_s, max_s = effective_lengths(settings, ParsedRules.model_validate(doc["rules"]))
    stats = {"scored": 0, "skipped": 0}
    for s in scores:
        row = session.get(ClipCandidate, s.id)
        if row is None or row.run_id != run_id:
            raise ValueError(f"candidate {s.id} not in run {run_id}")
        row.llm_score = s.score
        row.llm_reasoning = s.reasoning
        row.hook_text = s.hook_text
        if s.start is not None or s.end is not None:
            start = _snap(s.start if s.start is not None else row.start, bounds)
            end = _snap(s.end if s.end is not None else row.end, bounds)
            if min_s - 0.05 <= end - start <= max_s + 0.05:
                row.start, row.end = start, end
        session.add(row)
        stats["scored"] += 1
        if s.decision == "skip" and row.status is ClipStatus.CANDIDATE:
            transition(
                session,
                row,
                ClipStatus.REJECTED,
                actor=Actor.AGENT,
                reason=f"agent skip: {s.reasoning or 'low score'}",
            )
            stats["skipped"] += 1
    session.flush()
    return stats


# --------------------------------------------------------------------------- render + compliance


def compliance_for(
    session: Session, settings: Settings, clip: ClipCandidate, render_row: RenderedClip
) -> ComplianceReport:
    asset = session.get(SourceAsset, clip.asset_id)
    assert asset is not None
    campaign = session.get(Campaign, asset.campaign_id)
    assert campaign is not None
    meta = PostMetadata.model_validate(render_row.post_metadata or {})
    report = check(
        rules=campaign_rules(campaign),
        duration=clip.end - clip.start,
        origin_url=asset.origin_url,
        license_note=asset.license_note,
        metadata=meta,
        overlays=list(render_row.overlay_config.get("credits", [])),
        disclosure_default=settings.disclosure.for_language(settings.language),
    )
    render_row.compliance_report = report.model_dump(mode="json")
    session.add(render_row)
    return report


def stage_render(
    session: Session,
    settings: Settings,
    run_id: str,
    *,
    max_clips: int,
    profile_name: str = "universal",
    layout: str = "auto",
) -> list[RenderedClip]:
    rows = session.exec(
        select(ClipCandidate).where(
            ClipCandidate.run_id == run_id, ClipCandidate.status == ClipStatus.CANDIDATE
        )
    ).all()

    if any(c.llm_score is not None for c in rows):
        # the agent has scored this run: render only what it scored (and did not skip)
        rows = [c for c in rows if c.llm_score is not None]

    def rank(c: ClipCandidate) -> float:
        return c.llm_score if c.llm_score is not None else 10 * c.features.get("heuristic_score", 0)

    chosen: list[ClipCandidate] = []
    for c in sorted(rows, key=rank, reverse=True):
        # one variant per moment unless the agent explicitly scored several
        group = c.features.get("variant_of") or c.id
        if c.llm_score is None and any(
            (x.features.get("variant_of") or x.id) == group for x in chosen
        ):
            continue
        chosen.append(c)
        if len(chosen) >= max_clips:
            break

    out_dir = run_dir(settings, run_id) / "clips"
    profile = get_profile(profile_name)
    results: list[RenderedClip] = []
    for clip in chosen:
        asset = session.get(SourceAsset, clip.asset_id)
        assert asset is not None and asset.local_path
        campaign = session.get(Campaign, asset.campaign_id)
        assert campaign is not None
        rules = campaign_rules(campaign)
        transcript_row = session.exec(
            select(Transcript).where(Transcript.asset_id == asset.id)
        ).first()
        words = [Word(**w) for w in (transcript_row.words if transcript_row else [])]
        out = out_dir / f"clip_{clip.id}.mp4"
        try:
            res = render(
                RenderSpec(
                    source=Path(asset.local_path),
                    start=clip.start,
                    end=clip.end,
                    out=out,
                    profile=profile,
                    layout=layout,  # type: ignore[arg-type]
                    words=words,
                    hook_text=clip.hook_text,
                    credits=rules.required_overlays,
                )
            )
            qa = contact_sheet(out, 0, res.duration, out.with_name(f"clip_{clip.id}_qa.jpg"))
        except Exception as exc:  # isolate failures per clip
            log.error("render_failed", clip_id=clip.id, error=str(exc))
            transition(session, clip, ClipStatus.FAILED, actor=Actor.SYSTEM, reason=str(exc)[:500])
            continue
        row = RenderedClip(
            candidate_id=clip.id or 0,
            path=str(out),
            platform_profile=profile.name,
            overlay_config={
                "layout": res.layout,
                "hook_text": clip.hook_text,
                "credits": rules.required_overlays,
                "thumbnail": str(res.thumbnail),
                "qa_sheet": str(qa),
                "loudness_input_i": res.loudness.get("input_i"),
            },
            post_metadata=PostMetadata(
                platforms=rules.allowed_platforms, hook_text=clip.hook_text
            ).model_dump(mode="json"),
            run_id=run_id,
        )
        session.add(row)
        session.flush()
        transition(session, clip, ClipStatus.RENDERED, actor=Actor.SYSTEM)
        compliance_for(session, settings, clip, row)
        transition(session, clip, ClipStatus.PENDING_REVIEW, actor=Actor.SYSTEM)
        results.append(row)
    write_review_file(session, settings, run_id)
    return results


def write_review_file(session: Session, settings: Settings, run_id: str) -> Path:
    clips = session.exec(
        select(ClipCandidate).where(
            ClipCandidate.run_id == run_id, ClipCandidate.status == ClipStatus.PENDING_REVIEW
        )
    ).all()
    items = []
    for c in clips:
        r = latest_render(session, c)
        if r is None:
            continue
        items.append(
            {
                "clip_id": c.id,
                "video": r.path,
                "qa_sheet": r.overlay_config.get("qa_sheet"),
                "start": round(c.start, 2),
                "end": round(c.end, 2),
                "score": c.llm_score,
                "reasoning": c.llm_reasoning,
                "hook_text": c.hook_text,
                "post_metadata": r.post_metadata,
                "compliance": r.compliance_report,
            }
        )
    path = run_dir(settings, run_id) / "review.json"
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "instructions": (
                    "Review each clip (look at qa_sheet). Write decisions.json as a list of "
                    '{"clip_id", "decision": "approve"|"reject", "score", "reason", "caption", '
                    '"platforms"}. The caption must satisfy the campaign rules exactly; '
                    "approval is refused while compliance has hard violations."
                ),
                "clips": items,
            },
            indent=2,
        )
    )
    return path


class ReviewDecision(BaseModel):
    clip_id: int
    decision: str  # approve | reject
    score: float = Field(default=0, ge=0, le=10)
    reason: str = ""
    caption: str | None = None
    platforms: list[Platform] | None = None


def apply_review(
    session: Session, settings: Settings, run_id: str, decisions: list[ReviewDecision]
) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"approved": [], "rejected": [], "blocked": []}
    min_score = settings.publishing.min_agent_score
    for d in decisions:
        clip = session.get(ClipCandidate, d.clip_id)
        if clip is None or clip.run_id != run_id:
            raise ValueError(f"clip {d.clip_id} not in run {run_id}")
        render_row = latest_render(session, clip)
        if render_row is None or clip.status is not ClipStatus.PENDING_REVIEW:
            out["blocked"].append(
                {"clip_id": clip.id, "why": f"not reviewable (status {clip.status.value})"}
            )
            continue
        meta = PostMetadata.model_validate(render_row.post_metadata or {})
        if d.caption is not None:
            meta.caption = d.caption
        if d.platforms is not None:
            meta.platforms = d.platforms
        render_row.post_metadata = meta.model_dump(mode="json")
        report = compliance_for(session, settings, clip, render_row)
        decision = d.decision
        if decision == "approve" and d.score < min_score:
            decision = "reject"
            d.reason = f"score {d.score} below threshold {min_score}. {d.reason}".strip()
        render_row.agent_review = {"decision": decision, "score": d.score, "reason": d.reason}
        session.add(render_row)
        session.flush()
        if decision == "approve":
            try:
                transition(session, clip, ClipStatus.APPROVED, actor=Actor.AGENT, reason=d.reason)
                out["approved"].append({"clip_id": clip.id})
            except TransitionError as exc:
                out["blocked"].append(
                    {
                        "clip_id": clip.id,
                        "why": str(exc),
                        "violations": [v.message for v in report.hard_violations],
                    }
                )
        else:
            transition(
                session,
                clip,
                ClipStatus.REJECTED,
                actor=Actor.AGENT,
                reason=d.reason or "rejected by review agent",
            )
            out["rejected"].append({"clip_id": clip.id, "reason": d.reason})
    return out


def run_summary(session: Session, run_id: str) -> list[dict[str, Any]]:
    clips = session.exec(
        select(ClipCandidate).where(ClipCandidate.run_id == run_id).order_by(col(ClipCandidate.id))
    ).all()
    rows = []
    for c in clips:
        r = latest_render(session, c)
        report = (
            ComplianceReport.model_validate(r.compliance_report)
            if r and r.compliance_report
            else None
        )
        rows.append(
            {
                "clip_id": c.id,
                "status": c.status.value,
                "start": round(c.start, 2),
                "end": round(c.end, 2),
                "llm_score": c.llm_score,
                "heuristic_score": round(c.features.get("heuristic_score", 0), 3),
                "video": r.path if r else None,
                "compliance_passed": report.passed if report else None,
                "hard_violations": [v.message for v in report.hard_violations] if report else [],
                "error": c.error,
            }
        )
    return rows
