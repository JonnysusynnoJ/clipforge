"""Persist ClipForge state between ephemeral cloud sessions in a private R2 bucket.

Layout in the state bucket:
    state/latest.tar.gz          DB, config.yaml, campaign files, caches, run metadata,
                                 renders of clips that are not posted yet
    state/history/<ts>.tar.gz    last snapshots (backup)
    sources/<sha256>.<ext>       source videos (uploaded once, fetched on demand)
"""

from __future__ import annotations

import io
import json
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from clipforge.core.models import ClipCandidate, ClipStatus, RenderedClip, SourceAsset
from clipforge.publish.hosting import API, PLACEHOLDER_AUTH, USER_AGENT

KEEP_RENDERS_FOR = {
    ClipStatus.RENDERED,
    ClipStatus.PENDING_REVIEW,
    ClipStatus.APPROVED,
    ClipStatus.SCHEDULED,
}
HISTORY_KEEP = 7
MAX_SINGLE_PUT = 250 * 1024 * 1024


class StateError(RuntimeError):
    pass


@dataclass(frozen=True)
class R2Bucket:
    account_id: str
    bucket: str
    timeout: float = 900

    def _url(self, key: str) -> str:
        return f"{API}/accounts/{self.account_id}/r2/buckets/{self.bucket}/objects/{key}"

    def _req(
        self,
        method: str,
        url: str,
        data: bytes | None = None,
        ctype: str = "application/octet-stream",
    ) -> urllib.request.Request:
        headers = {"Authorization": PLACEHOLDER_AUTH, "User-Agent": USER_AGENT}
        if data is not None:
            headers["Content-Type"] = ctype
        return urllib.request.Request(url, data=data, method=method, headers=headers)

    def put(self, key: str, data: bytes, ctype: str = "application/octet-stream") -> None:
        if len(data) > MAX_SINGLE_PUT:
            raise StateError(f"{key}: {len(data) / 1e6:.0f} MB exceeds single upload limit")
        try:
            with urllib.request.urlopen(
                self._req("PUT", self._url(key), data, ctype), timeout=self.timeout
            ) as resp:
                body = json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise StateError(f"PUT {key} failed ({exc.code}): {exc.read()[:200]!r}") from exc
        if not body.get("success", False):
            raise StateError(f"PUT {key} failed: {body.get('errors')}")

    def get(self, key: str) -> bytes | None:
        try:
            with urllib.request.urlopen(
                self._req("GET", self._url(key)), timeout=self.timeout
            ) as resp:
                data: bytes = resp.read()
                return data
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise StateError(f"GET {key} failed ({exc.code})") from exc

    def delete(self, key: str) -> None:
        try:
            urllib.request.urlopen(self._req("DELETE", self._url(key)), timeout=60).close()
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise StateError(f"DELETE {key} failed ({exc.code})") from exc

    def list(self, prefix: str) -> list[str]:
        url = (
            f"{API}/accounts/{self.account_id}/r2/buckets/{self.bucket}/objects"
            f"?prefix={prefix}&per_page=1000"
        )
        with urllib.request.urlopen(self._req("GET", url), timeout=60) as resp:
            body = json.loads(resp.read())
        return [o["key"] for o in body.get("result", [])]


# --------------------------------------------------------------------------- bundle


def bundle_paths(session: Session, root: Path, data_dir: Path) -> list[Path]:
    """Files that make up the state (relative to `root`)."""
    paths: list[Path] = []
    for name in ("config.yaml",):
        if (root / name).exists():
            paths.append(Path(name))
    rel_data = data_dir if not data_dir.is_absolute() else data_dir.relative_to(root)
    db = rel_data / "clipforge.db"
    if (root / db).exists():
        paths.append(db)
    for sub in ("campaigns", "cache"):
        base = root / rel_data / sub
        if base.exists():
            paths += [p.relative_to(root) for p in base.rglob("*") if p.is_file()]
    runs = root / rel_data / "runs"
    if runs.exists():  # small run metadata only
        paths += [p.relative_to(root) for p in runs.glob("*/*.json")]
    keep = [c.id for c in session.exec(select(ClipCandidate)).all() if c.status in KEEP_RENDERS_FOR]
    for r in session.exec(select(RenderedClip)).all():
        if r.candidate_id in keep:
            p = Path(r.path)
            for f in (p, p.with_suffix(".jpg"), p.with_name(p.stem + "_qa.jpg")):
                if (root / f).exists():
                    paths.append(f)
    return sorted(set(paths))


def make_bundle(root: Path, paths: list[Path]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in paths:
            tar.add(root / p, arcname=str(p))
    return buf.getvalue()


def extract_bundle(data: bytes, root: Path) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = []
        for m in tar.getmembers():
            target = (root / m.name).resolve()
            if not str(target).startswith(str(root.resolve())) or m.issym() or m.islnk():
                raise StateError(f"unsafe path in state bundle: {m.name}")
            names.append(m.name)
        tar.extractall(root, filter="data")
    return names


def push(bucket: R2Bucket, session: Session, root: Path, data_dir: Path) -> dict[str, object]:
    paths = bundle_paths(session, root, data_dir)
    blob = make_bundle(root, paths)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    bucket.put("state/latest.tar.gz", blob, "application/gzip")
    bucket.put(f"state/history/{stamp}.tar.gz", blob, "application/gzip")
    history = sorted(bucket.list("state/history/"))
    for old in history[:-HISTORY_KEEP]:
        bucket.delete(old)
    uploaded = push_sources(bucket, session, root)
    return {
        "files": len(paths),
        "bytes": len(blob),
        "snapshot": stamp,
        "sources_uploaded": uploaded,
    }


def pull(bucket: R2Bucket, root: Path) -> dict[str, object]:
    blob = bucket.get("state/latest.tar.gz")
    if blob is None:
        return {"restored": 0, "note": "no state in bucket yet (first run)"}
    names = extract_bundle(blob, root)
    return {"restored": len(names), "bytes": len(blob)}


# --------------------------------------------------------------------------- sources


def _source_key(asset: SourceAsset) -> str:
    suffix = Path(asset.local_path or "x.mp4").suffix or ".mp4"
    return f"sources/{asset.sha256}{suffix}"


def push_sources(bucket: R2Bucket, session: Session, root: Path) -> int:
    existing = set(bucket.list("sources/"))
    n = 0
    for a in session.exec(select(SourceAsset)).all():
        if not (a.sha256 and a.local_path) or _source_key(a) in existing:
            continue
        p = root / a.local_path
        if p.exists():
            bucket.put(_source_key(a), p.read_bytes(), "video/mp4")
            n += 1
    return n


def ensure_source(bucket: R2Bucket, asset: SourceAsset, root: Path) -> Path:
    """Make sure the asset's file exists locally, fetching it from the state bucket."""
    if not asset.local_path:
        raise StateError(f"asset {asset.id} has no local path")
    p = root / asset.local_path
    if p.exists():
        return p
    data = bucket.get(_source_key(asset))
    if data is None:
        raise StateError(f"source of asset {asset.id} is neither local nor in the state bucket")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p
