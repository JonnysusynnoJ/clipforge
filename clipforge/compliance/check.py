"""Automatic compliance check of a rendered clip + post metadata against campaign rules."""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, Field

from clipforge.campaigns.rules import ParsedRules
from clipforge.core.compliance_report import ComplianceReport, Severity, Violation
from clipforge.core.models import Platform

HASHTAG = re.compile(r"(?<!\w)#\w+")
MENTION = re.compile(r"(?<!\w)@[\w.]+")


class PostMetadata(BaseModel):
    """Caption text exactly as it will be posted (disclosure and hashtags included)."""

    caption: str = ""
    platforms: list[Platform] = Field(default_factory=list)
    hook_text: str | None = None


def _norm(tags: Iterable[str]) -> set[str]:
    return {t.lower() for t in tags}


def check(
    *,
    rules: ParsedRules,
    duration: float,
    origin_url: str,
    license_note: str,
    metadata: PostMetadata,
    overlays: list[str],
    disclosure_default: str,
) -> ComplianceReport:
    report = ComplianceReport()
    v = report.violations

    def run(name: str) -> None:
        report.checks_run.append(name)

    run("length")
    if rules.min_seconds and duration < rules.min_seconds - 0.05:
        v.append(
            Violation(
                rule="length",
                severity=Severity.HARD,
                message=f"{duration:.1f}s < min {rules.min_seconds}s",
            )
        )
    if rules.max_seconds and duration > rules.max_seconds + 0.05:
        v.append(
            Violation(
                rule="length",
                severity=Severity.HARD,
                message=f"{duration:.1f}s > max {rules.max_seconds}s",
            )
        )

    run("source")
    if not (rules.source_allowed(origin_url) or license_note.startswith("manual approval")):
        v.append(
            Violation(
                rule="source",
                severity=Severity.HARD,
                message=f"source not authorized: {origin_url}",
            )
        )

    run("platform")
    if not metadata.platforms:
        v.append(Violation(rule="platform", severity=Severity.HARD, message="no target platform"))
    for p in metadata.platforms:
        if rules.allowed_platforms and p not in rules.allowed_platforms:
            v.append(
                Violation(
                    rule="platform",
                    severity=Severity.HARD,
                    message=f"{p.value} not allowed by campaign",
                )
            )

    run("overlays")
    overlay_text = " ".join(overlays).lower()
    for req in rules.required_overlays:
        if req.lower() not in overlay_text:
            v.append(
                Violation(
                    rule="overlays",
                    severity=Severity.HARD,
                    message=f"required overlay missing: {req}",
                )
            )

    caption = metadata.caption.strip()
    run("caption")
    if not caption:
        v.append(
            Violation(
                rule="caption",
                severity=Severity.HARD,
                message="caption missing (written by the review agent)",
            )
        )
        return report

    tags = HASHTAG.findall(caption)
    mentions = _norm(MENTION.findall(caption))

    run("disclosure")
    options = _norm(rules.disclosure_options or [disclosure_default])
    disclosures = [t for t in tags if t.lower() in options]
    if not disclosures:
        v.append(
            Violation(
                rule="disclosure",
                severity=Severity.HARD,
                message=f"no disclosure hashtag ({', '.join(sorted(options))})",
            )
        )
    elif rules.disclosure_own_line_first:
        if tags[0].lower() not in options:
            v.append(
                Violation(
                    rule="disclosure",
                    severity=Severity.HARD,
                    message="disclosure must be the first hashtag",
                )
            )
        line = next(ln for ln in caption.splitlines() if disclosures[0] in ln)
        if line.strip() != disclosures[0]:
            v.append(
                Violation(
                    rule="disclosure",
                    severity=Severity.HARD,
                    message="disclosure must be on its own line",
                )
            )

    run("mentions")
    for m in rules.required_mentions:
        if m.lower() not in mentions:
            v.append(
                Violation(
                    rule="mentions",
                    severity=Severity.HARD,
                    message=f"required mention missing: {m}",
                )
            )

    run("hashtags")
    tag_set = _norm(tags)
    for t in rules.required_hashtags:
        if t.lower() not in tag_set:
            v.append(
                Violation(
                    rule="hashtags",
                    severity=Severity.HARD,
                    message=f"required hashtag missing: {t}",
                )
            )
    if rules.max_extra_hashtags is not None:
        extra = [
            t
            for t in tags
            if t.lower() not in options and t.lower() not in _norm(rules.required_hashtags)
        ]
        if len(extra) > rules.max_extra_hashtags:
            v.append(
                Violation(
                    rule="hashtags",
                    severity=Severity.HARD,
                    message=f"{len(extra)} extra hashtags > {rules.max_extra_hashtags}",
                )
            )

    run("forbidden_terms")
    text = f"{caption} {metadata.hook_text or ''}".lower()
    for term in rules.forbidden_caption_terms:
        if re.search(rf"\b{re.escape(term.lower())}\b", text):
            v.append(
                Violation(
                    rule="forbidden_terms",
                    severity=Severity.HARD,
                    message=f"forbidden term in caption/hook: {term!r}",
                )
            )

    run("caption_copy")
    for example in rules.caption_examples:
        if example.strip().lower() and example.strip().lower() in caption.lower():
            v.append(
                Violation(
                    rule="caption_copy",
                    severity=Severity.HARD,
                    message="caption copies a campaign example verbatim",
                )
            )
    return report
