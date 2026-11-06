from __future__ import annotations

import io
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session

from clipforge import daily
from clipforge.core.models import ClipStatus, Post, PostStatus
from clipforge.core.settings import Settings
from clipforge.publish import service
from clipforge.state_store import StateError, bundle_paths, extract_bundle, make_bundle
from tests.test_publish import NOW, FakeHost, add_account, approved_clip, make_campaign


class MemoryBucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, ctype: str = "") -> None:
        self.objects[key] = data

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def list(self, prefix: str) -> list[str]:
        return [k for k in self.objects if k.startswith(prefix)]


@pytest.fixture
def pub_settings(settings: Settings) -> Settings:
    settings.publishing.enabled = True
    settings.publishing.mode = "live"
    return settings


def scheduled_post(session: Session, settings: Settings, tmp: Path) -> Post:
    campaign = make_campaign(session)
    add_account(session)
    approved_clip(session, settings, campaign, tmp)
    out = service.plan_posts(session, settings, now=NOW, host=FakeHost())
    pid = out["planned"][0]["post_id"]
    service.record_result(
        session,
        pid,
        ok=True,
        metricool_post_id="111",
        planner_url="https://app.metricool.com/x?openWithPostUuid=999",
    )
    post = session.get(Post, pid)
    assert post is not None and not post.draft
    return post


def test_sync_keeps_pending_and_marks_published(
    session: Session, pub_settings: Settings, tmp_path: Path
) -> None:
    post = scheduled_post(session, pub_settings, tmp_path)
    at = post.scheduled_at.replace(tzinfo=UTC)
    pending = {"data": [{"id": 111, "uuid": "999", "providers": [{"status": "PENDING"}]}]}
    out = daily.sync_scheduled(session, pending, now=at + timedelta(hours=1))
    assert out["pending"] and post.status is PostStatus.SCHEDULED

    # gone from the scheduled list after its time -> published, URL filled in later
    out = daily.sync_scheduled(session, {"data": []}, now=at + timedelta(hours=1))
    assert out["published"] == [{"post_id": post.id, "url": None}]
    assert post.status is PostStatus.PUBLISHED
    assert daily.posts_missing_url(session)[0]["post_id"] == post.id


def test_sync_not_due_yet_stays_scheduled(
    session: Session, pub_settings: Settings, tmp_path: Path
) -> None:
    post = scheduled_post(session, pub_settings, tmp_path)
    at = post.scheduled_at.replace(tzinfo=UTC)
    out = daily.sync_scheduled(session, {"data": []}, now=at - timedelta(hours=1))
    assert out["pending"] and post.status is PostStatus.SCHEDULED


def test_sync_error_marks_failed_and_frees_clip(
    session: Session, pub_settings: Settings, tmp_path: Path
) -> None:
    post = scheduled_post(session, pub_settings, tmp_path)
    err = {"data": [{"uuid": "999", "providers": [{"status": "ERROR", "detailedStatus": "x"}]}]}
    out = daily.sync_scheduled(session, err, now=datetime.now(UTC))
    assert out["failed"] and post.status is PostStatus.FAILED
    clip = daily._clip(session, post)
    assert clip.status is ClipStatus.APPROVED  # can be planned again


def test_status_reports_backlog(session: Session, pub_settings: Settings, tmp_path: Path) -> None:
    campaign = make_campaign(session)
    add_account(session)
    for _ in range(3):
        approved_clip(session, pub_settings, campaign, tmp_path)
    st = daily.status(session, pub_settings, now=NOW)
    [c] = st["active_campaigns"]
    assert c["approved_unposted"] == 3 and c["produce_clips"] is False
    assert st["publishing"]["mode"] == "live" and st["brands_to_sync"] == ["7921004"]


def test_bundle_roundtrip_and_contents(
    session: Session, pub_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.chdir(root)
    data = Path("data")
    (root / "config.yaml").write_text("publishing:\n  enabled: true\n")
    (root / data / "campaigns/1").mkdir(parents=True)
    (root / data / "campaigns/1/rules.json").write_text("{}")
    (root / data / "clipforge.db").write_bytes(b"db")
    (root / data / "sources").mkdir()
    (root / data / "sources/big.mp4").write_bytes(b"x" * 10)  # never bundled
    campaign = make_campaign(session)
    clip_dir = root / data / "runs/r1/clips"
    clip_dir.mkdir(parents=True)
    clip = approved_clip(session, pub_settings, campaign, clip_dir)
    render = service.latest_render(session, clip)
    assert render is not None
    render.path = str(Path(render.path).relative_to(root))

    paths = bundle_paths(session, root, data)
    names = {str(p) for p in paths}
    assert {"config.yaml", "data/clipforge.db", "data/campaigns/1/rules.json"} <= names
    assert render.path in names and "data/sources/big.mp4" not in names

    blob = make_bundle(root, paths)
    restore = tmp_path / "fresh"
    restore.mkdir()
    extract_bundle(blob, restore)
    assert (restore / "data/campaigns/1/rules.json").read_text() == "{}"
    assert (restore / render.path).exists()


def test_extract_rejects_path_traversal(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("../evil.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(StateError):
        extract_bundle(buf.getvalue(), tmp_path / "root")


def test_push_pull_with_memory_bucket(
    session: Session, pub_settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clipforge import state_store

    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    (root / "config.yaml").write_text("x: 1\n")
    (root / "data/clipforge.db").write_bytes(b"db")
    bucket = MemoryBucket()
    for _ in range(9):
        state_store.push(bucket, session, root, Path("data"))  # type: ignore[arg-type]
        monkeypatch.setattr(
            state_store.time, "strftime", lambda *_a, n=[0]: f"t{n.append(0) or len(n):03d}"
        )
    assert "state/latest.tar.gz" in bucket.objects
    assert len(bucket.list("state/history/")) <= state_store.HISTORY_KEEP
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    assert state_store.pull(bucket, fresh)["restored"] == 2  # type: ignore[arg-type]
    assert (fresh / "config.yaml").read_text() == "x: 1\n"
