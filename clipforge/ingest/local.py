"""Import of authorized local files into the source store."""

from __future__ import annotations

import shutil
from pathlib import Path

from sqlmodel import Session, select

from clipforge.campaigns.rules import ParsedRules
from clipforge.core.models import Campaign, SourceAsset
from clipforge.media import probe, sha256_file


class UnauthorizedSourceError(RuntimeError):
    pass


def authorize(rules: ParsedRules, origin_url: str, license_note: str) -> str:
    """Return the license note to store, or raise if the source is not authorized.

    A source is authorized if its origin matches the campaign's allowed sources, or if
    the owner explicitly approved it (non-empty `license_note`).
    """
    if rules.source_allowed(origin_url):
        return license_note or "listed in campaign allowed_sources"
    if license_note.strip():
        return f"manual approval: {license_note.strip()}"
    raise UnauthorizedSourceError(
        f"source {origin_url!r} is not in the campaign's allowed sources; "
        "pass an explicit license note to approve it manually"
    )


def import_file(
    session: Session,
    campaign: Campaign,
    file: Path,
    *,
    sources_dir: Path,
    origin_url: str,
    license_note: str = "",
    move: bool = False,
) -> SourceAsset:
    """Store `file` under sources/<campaign_id>/<sha>.<ext>; idempotent by content hash."""
    if campaign.id is None:
        raise ValueError("campaign must be persisted")
    rules = ParsedRules.model_validate(campaign.parsed_rules)
    note = authorize(rules, origin_url, license_note)

    digest = sha256_file(file)
    existing = session.exec(
        select(SourceAsset).where(
            SourceAsset.campaign_id == campaign.id, SourceAsset.sha256 == digest
        )
    ).first()
    if existing is not None and existing.local_path and Path(existing.local_path).exists():
        return existing

    target_dir = sources_dir / str(campaign.id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{digest[:16]}{file.suffix.lower()}"
    if file.resolve() != target.resolve():
        if move:
            shutil.move(str(file), target)
        else:
            shutil.copy2(file, target)
    info = probe(target)

    asset = existing or SourceAsset(campaign_id=campaign.id, origin_url=origin_url)
    asset.local_path = str(target)
    asset.sha256 = digest
    asset.duration = info.duration
    asset.license_note = note
    session.add(asset)
    session.flush()
    return asset
