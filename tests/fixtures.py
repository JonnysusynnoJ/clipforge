"""Synthetic test media generated with ffmpeg (no third-party content in the repo)."""

from __future__ import annotations

import subprocess
from pathlib import Path


def make_video(out: Path, *, seconds: int = 24, width: int = 640, height: int = 360) -> Path:
    """Landscape video with a hard scene change every 4 s and loud bursts every 6 s."""
    out.parent.mkdir(parents=True, exist_ok=True)
    colors = ["red", "blue", "green", "yellow", "purple", "orange", "white", "cyan"]
    n = seconds // 4
    inputs: list[str] = []
    for i in range(n):
        inputs += ["-f", "lavfi", "-i",
                   f"color=c={colors[i % len(colors)]}:s={width}x{height}:d=4:r=25"]  # fmt: skip
    concat = "".join(f"[{i}:v]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]"
    audio = (
        f"sine=frequency=220:duration={seconds}:sample_rate=48000,"
        "volume='if(lt(mod(t,6),1.5),1.0,0.08)':eval=frame[a]"
    )
    cmd = [
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", *inputs,
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
        "-filter_complex", f"{concat};{audio}",
        "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out),
    ]  # fmt: skip
    subprocess.run(cmd, check=True)
    return out
