"""Environment health checks for `clipforge doctor`."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from clipforge.core import db
from clipforge.core.settings import Settings

MIN_FREE_DISK_GB = 5.0


class Status(StrEnum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    return proc.stdout + proc.stderr


def check_python() -> CheckResult:
    v = sys.version_info
    ok = (v.major, v.minor) == (3, 12)
    return CheckResult("python", Status.OK if ok else Status.WARN, f"{v.major}.{v.minor}.{v.micro}")


def check_ffmpeg() -> list[CheckResult]:
    results: list[CheckResult] = []
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None:
        return [CheckResult("ffmpeg", Status.FAIL, "not found (macOS: brew install ffmpeg)")]
    version_out = _run([ffmpeg, "-hide_banner", "-version"])
    match = re.search(r"ffmpeg version (\S+)", version_out)
    results.append(CheckResult("ffmpeg", Status.OK, match.group(1) if match else "found"))
    results.append(
        CheckResult(
            "ffprobe",
            Status.OK if ffprobe else Status.FAIL,
            ffprobe or "not found",
        )
    )
    filters = _run([ffmpeg, "-hide_banner", "-filters"])
    for name, why in (("loudnorm", "loudness normalization"), ("ass", "burned-in captions")):
        present = re.search(rf"^\s*\S+\s+{name}\s", filters, re.MULTILINE) is not None
        results.append(
            CheckResult(
                f"ffmpeg filter '{name}'",
                Status.OK if present else Status.FAIL,
                why if present else f"missing ({why}); ffmpeg built without it",
            )
        )
    encoders = _run([ffmpeg, "-hide_banner", "-encoders"])
    has_x264 = "libx264" in encoders
    results.append(
        CheckResult(
            "encoder libx264",
            Status.OK if has_x264 else Status.WARN,
            "H.264 export" if has_x264 else "missing; will fall back to other H.264 encoder",
        )
    )
    return results


def check_dirs(settings: Settings) -> list[CheckResult]:
    results: list[CheckResult] = []
    for directory in settings.paths.all_dirs():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            writable = os.access(directory, os.W_OK)
        except OSError as exc:
            results.append(CheckResult(f"dir {directory}", Status.FAIL, str(exc)))
            continue
        results.append(
            CheckResult(
                f"dir {directory}",
                Status.OK if writable else Status.FAIL,
                "writable" if writable else "not writable",
            )
        )
    return results


def check_disk(path: Path) -> CheckResult:
    path.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(path).free / 1e9
    status = Status.OK if free_gb >= MIN_FREE_DISK_GB else Status.WARN
    return CheckResult("free disk", status, f"{free_gb:.1f} GB free at {path}")


def check_database(settings: Settings) -> CheckResult:
    if not settings.paths.db_file.is_file():
        return CheckResult("database", Status.FAIL, "not initialized; run `clipforge db upgrade`")
    try:
        engine = db.make_engine(settings.database_url)
        current = db.current_revision(engine)
        head = db.head_revision(settings.database_url)
    except Exception as exc:
        return CheckResult("database", Status.FAIL, f"error: {exc}")
    if current is None:
        return CheckResult("database", Status.FAIL, "not initialized; run `clipforge db upgrade`")
    if current != head:
        return CheckResult(
            "database", Status.WARN, f"at {current}, head is {head}; run `clipforge db upgrade`"
        )
    return CheckResult("database", Status.OK, f"migrations at head ({head})")


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def check_ml_backends(settings: Settings) -> list[CheckResult]:
    results: list[CheckResult] = []
    has_mlx = _module_available("mlx_whisper")
    has_fw = _module_available("faster_whisper")
    backend = settings.transcription.backend
    wanted_ok = {
        "auto": has_mlx or has_fw,
        "mlx": has_mlx,
        "faster_whisper": has_fw,
    }[backend]
    found = [n for n, ok in (("mlx-whisper", has_mlx), ("faster-whisper", has_fw)) if ok]
    results.append(
        CheckResult(
            "transcription backend",
            Status.OK if wanted_ok else Status.WARN,
            f"config={backend}, installed: {', '.join(found) or 'none (Phase 1)'}",
        )
    )
    has_mp = _module_available("mediapipe")
    results.append(
        CheckResult(
            "face tracking (mediapipe)",
            Status.OK if has_mp else Status.WARN,
            "installed" if has_mp else "not installed (Phase 1); center-crop fallback",
        )
    )
    return results


def check_secrets(settings: Settings) -> list[CheckResult]:
    results: list[CheckResult] = []
    sa_file = settings.google_service_account_file
    if sa_file is None:
        results.append(
            CheckResult("google service account", Status.WARN, "GOOGLE_SERVICE_ACCOUNT_FILE unset")
        )
    else:
        exists = sa_file.is_file()
        results.append(
            CheckResult(
                "google service account",
                Status.OK if exists else Status.FAIL,
                "key file found" if exists else f"file not found: {sa_file}",
            )
        )
    results.append(
        CheckResult(
            "apify token",
            Status.OK if settings.apify_token else Status.WARN,
            "set" if settings.apify_token else "unset (only needed as discovery fallback)",
        )
    )
    return results


def check_publishing(settings: Settings) -> CheckResult:
    p = settings.publishing
    limits = settings.rate_limits
    detail = (
        f"enabled={p.enabled}, approver={p.approver}, "
        f"limit={limits.max_posts_per_account_per_day}/day/account, "
        f"gap={limits.min_gap_minutes}min"
    )
    return CheckResult("publishing", Status.OK, detail)


def run_all(settings: Settings) -> list[CheckResult]:
    results: list[CheckResult] = [check_python()]
    results += check_ffmpeg()
    results += check_dirs(settings)
    results.append(check_disk(settings.paths.data_dir))
    results.append(check_database(settings))
    results += check_ml_backends(settings)
    results += check_secrets(settings)
    results.append(check_publishing(settings))
    return results
