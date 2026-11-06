"""Thin wrappers around ffmpeg / ffprobe."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16_000


class MediaError(RuntimeError):
    pass


@dataclass(frozen=True)
class MediaInfo:
    duration: float
    width: int | None
    height: int | None
    fps: float | None
    has_audio: bool


def _bin(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise MediaError(f"{name} not found on PATH")
    return path


def run_ffmpeg(args: list[str], *, timeout: float = 3600) -> str:
    cmd = [_bin("ffmpeg"), "-hide_banner", "-nostdin", "-y", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise MediaError(f"ffmpeg failed ({proc.returncode}):\n{tail}")
    return proc.stderr


def probe(path: Path) -> MediaInfo:
    cmd = [
        _bin("ffprobe"), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0:
        raise MediaError(f"ffprobe failed for {path}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    fps = None
    if video and video.get("avg_frame_rate", "0/0") != "0/0":
        num, den = video["avg_frame_rate"].split("/")
        fps = float(num) / float(den) if float(den) else None
    return MediaInfo(
        duration=float(data["format"]["duration"]),
        width=int(video["width"]) if video else None,
        height=int(video["height"]) if video else None,
        fps=fps,
        has_audio=has_audio,
    )


def load_audio(path: Path, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Decode audio to mono float32 at `sample_rate` using ffmpeg."""
    cmd = [
        _bin("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-i", str(path),
        "-vn", "-ac", "1", "-ar", str(sample_rate), "-f", "s16le", "-",
    ]  # fmt: skip
    proc = subprocess.run(cmd, capture_output=True, timeout=3600, check=False)
    if proc.returncode != 0:
        raise MediaError(f"audio decode failed: {proc.stderr.decode(errors='replace')[-500:]}")
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def extract_frame(path: Path, t: float, out: Path, width: int = 480) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    run_ffmpeg(
        ["-ss", f"{t:.3f}", "-i", str(path), "-frames:v", "1",
         "-vf", f"scale={width}:-2", "-q:v", "3", str(out)]
    )  # fmt: skip
    return out


def contact_sheet(path: Path, start: float, end: float, out: Path, n: int = 6) -> Path:
    """Tile `n` evenly spaced frames of [start, end] into one image (3 columns)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    duration = max(end - start, 0.1)
    fps = n / duration
    rows = (n + 2) // 3
    run_ffmpeg(
        ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(path),
         "-vf", f"fps={fps:.5f},scale=360:-2,tile=3x{rows}:padding=4:color=black",
         "-frames:v", "1", "-q:v", "3", str(out)]
    )  # fmt: skip
    return out


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()
