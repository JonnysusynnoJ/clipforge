"""Render a clip: cut, reframe to 9:16, burn captions/overlays, normalize loudness."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal, Protocol

from clipforge.media import MediaInfo, extract_frame, probe, run_ffmpeg
from clipforge.render.captions import FONT_DIR, PRESETS, build_ass
from clipforge.render.profiles import ExportProfile
from clipforge.transcribe.whisper import Word

Layout = Literal["auto", "blur_fit", "center_crop", "face_crop", "passthrough"]

TARGET_LUFS = -14.0
TRUE_PEAK = -1.5
LRA = 11.0


class FaceDetector(Protocol):
    """Returns the horizontal face center (0..1) for a frame image, or None."""

    def center_x(self, frame: Path) -> float | None: ...


@dataclass
class RenderSpec:
    source: Path
    start: float
    end: float
    out: Path
    profile: ExportProfile
    layout: Layout = "auto"
    fg_aspect: float = 4 / 3  # blur_fit: aspect of the sharp foreground crop
    crop_x: float | None = None  # center_crop / face_crop: horizontal center 0..1
    words: list[Word] = field(default_factory=list)  # absolute source times
    caption_style: str = "default"
    hook_text: str | None = None
    hook_seconds: float = 3.0
    credits: list[str] = field(default_factory=list)
    burn_captions: bool = True
    fps: float | None = None  # None: profile fps
    words_per_line: int | None = None
    end_card: Path | None = None  # image or video appended after the clip
    end_card_seconds: float = 2.5


@dataclass
class RenderResult:
    path: Path
    thumbnail: Path
    duration: float
    layout: str
    loudness: dict[str, float]


def _filter_path(p: Path) -> str:
    s = str(p.resolve())
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def choose_layout(spec: RenderSpec, info: MediaInfo) -> str:
    if spec.layout != "auto":
        if spec.layout == "face_crop" and spec.crop_x is None:
            return "blur_fit"  # no face information: fall back
        return spec.layout
    if info.width and info.height and info.height / info.width >= 1.6:
        return "passthrough"
    return "blur_fit"


def video_filter(layout: str, spec: RenderSpec, info: MediaInfo) -> str:
    w, h = spec.profile.width, spec.profile.height
    if layout == "passthrough":
        return f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
    if layout in ("center_crop", "face_crop"):
        cx = spec.crop_x if spec.crop_x is not None else 0.5
        return f"scale=-2:{h},crop={w}:{h}:'max(0,min(iw-{w},iw*{cx:.4f}-{w}/2))':0"
    # blur_fit: blurred, darkened fill + sharp foreground crop in the middle
    a = spec.fg_aspect
    fg_h = int(round(w / a / 2) * 2)
    return (
        f"split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
        f"gblur=sigma=40,eq=brightness=-0.12:saturation=0.9[bg];"
        f"[fgsrc]crop='min(iw,ih*{a:.4f})':ih,scale={w}:{fg_h}[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2"
    )


def measure_loudness(source: Path, start: float, duration: float) -> dict[str, float]:
    stderr = run_ffmpeg(
        ["-ss", f"{start:.3f}", "-t", f"{duration:.3f}", "-i", str(source), "-vn",
         "-af", f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:LRA={LRA}:print_format=json",
         "-f", "null", "-"]
    )  # fmt: skip
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.S)
    if not match:
        raise RuntimeError("could not parse loudnorm measurement")
    return {k: float(v) for k, v in json.loads(match.group(0)).items() if k != "normalization_type"}


def _encoder() -> list[str]:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    encoders = subprocess.run(
        [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, check=False
    ).stdout
    if "libx264" in encoders:
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high"]
    if "h264_videotoolbox" in encoders:
        return ["-c:v", "h264_videotoolbox", "-b:v", "8M"]
    raise RuntimeError("no H.264 encoder available")


_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")


def append_end_card(
    main: Path, card: Path, out: Path, spec: RenderSpec, fps: float, has_audio: bool
) -> None:
    """Concatenate `card` (image or video, scaled with blurred fill) after `main`."""
    if not card.exists():
        raise FileNotFoundError(f"end card not found: {card}")
    w, h, n = spec.profile.width, spec.profile.height, spec.end_card_seconds
    is_image = card.suffix.lower() in _IMAGE_EXT
    card_in = ["-loop", "1", "-t", f"{n:.3f}", "-i", str(card)] if is_image else [
        "-t", f"{n:.3f}", "-i", str(card)
    ]  # fmt: skip
    card_has_audio = (not is_image) and probe(card).has_audio
    graph = (
        f"[1:v]split=2[cb][cf];"
        f"[cb]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
        f"gblur=sigma=40,eq=brightness=-0.12[cbg];"
        f"[cf]scale={w}:{h}:force_original_aspect_ratio=decrease[cfg];"
        f"[cbg][cfg]overlay=(W-w)/2:(H-h)/2,fps={fps:.3f},format=yuv420p,setsar=1,"
        f"trim=duration={n:.3f},setpts=PTS-STARTPTS[v1];"
    )
    inputs = ["-i", str(main), *card_in]
    if card_has_audio:
        graph += f"[1:a]aresample=48000,atrim=duration={n:.3f},asetpts=PTS-STARTPTS[a1];"
    else:
        inputs += ["-f", "lavfi", "-t", f"{n:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        graph += "[2:a]asetpts=PTS-STARTPTS[a1];"
    if has_audio:
        graph += "[0:v][0:a][v1][a1]concat=n=2:v=1:a=1[v][a]"
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        graph += "[0:v][v1]concat=n=2:v=1:a=0[v]"
        maps = ["-map", "[v]"]
    run_ffmpeg([
        *inputs, "-filter_complex", graph, *maps, *_encoder(),
        "-c:a", "aac", "-b:a", spec.profile.audio_bitrate,
        "-maxrate", spec.profile.max_bitrate, "-bufsize", "16M",
        "-movflags", "+faststart", str(out),
    ])  # fmt: skip


def render(spec: RenderSpec) -> RenderResult:
    info = probe(spec.source)
    duration = spec.end - spec.start
    if duration <= 0:
        raise ValueError("clip end must be after start")
    if duration > spec.profile.max_seconds:
        raise ValueError(f"clip is {duration:.1f}s, profile max is {spec.profile.max_seconds}s")
    layout = choose_layout(spec, info)
    spec.out.parent.mkdir(parents=True, exist_ok=True)

    rel_words = [
        Word(w=w.w, start=w.start - spec.start, end=w.end - spec.start, conf=w.conf)
        for w in spec.words
        if w.start >= spec.start - 0.05 and w.end <= spec.end + 0.05
    ]
    fg_bottom = None
    if layout == "blur_fit":
        fg_h = spec.profile.width / spec.fg_aspect
        fg_bottom = int((spec.profile.height + fg_h) / 2)
    style = PRESETS[spec.caption_style]
    if spec.words_per_line:
        style = replace(style, words_per_line=spec.words_per_line)
    fps = spec.fps or spec.profile.fps
    ass_text = build_ass(
        profile=spec.profile,
        duration=duration,
        words=rel_words if spec.burn_captions else [],
        style=style,
        hook_text=spec.hook_text,
        hook_seconds=spec.hook_seconds,
        credits=spec.credits,
        caption_center_y=(fg_bottom + 110) if fg_bottom else None,
    )
    ass_file = spec.out.with_suffix(".ass")
    ass_file.write_text(ass_text, encoding="utf-8")

    vf = video_filter(layout, spec, info)
    fade = "" if spec.end_card else f"fade=t=out:st={max(0.0, duration - 0.25):.3f}:d=0.25,"
    vf += (
        f",fps={fps:.3f},"
        f"ass='{_filter_path(ass_file)}':fontsdir='{_filter_path(FONT_DIR)}',"
        f"{fade}format=yuv420p,setsar=1"
    )
    main_out = spec.out.with_name(spec.out.stem + "_main.mp4") if spec.end_card else spec.out
    args = ["-ss", f"{spec.start:.3f}", "-t", f"{duration:.3f}", "-i", str(spec.source)]
    loud: dict[str, float] = {}
    if info.has_audio:
        loud = measure_loudness(spec.source, spec.start, duration)
        af = (
            f"loudnorm=I={TARGET_LUFS}:TP={TRUE_PEAK}:LRA={LRA}:"
            f"measured_I={loud['input_i']}:measured_TP={loud['input_tp']}:"
            f"measured_LRA={loud['input_lra']}:measured_thresh={loud['input_thresh']}:"
            f"offset={loud['target_offset']}:linear=true,"
            f"afade=t=in:d=0.05,afade=t=out:st={max(0.0, duration - 0.3):.3f}:d=0.3,"
            "aresample=48000"
        )
        args += ["-af", af, "-c:a", "aac", "-b:a", spec.profile.audio_bitrate]
    args += [
        "-filter_complex" if ";" in vf else "-vf", vf,
        *_encoder(),
        "-maxrate", spec.profile.max_bitrate, "-bufsize", "16M",
        "-movflags", "+faststart", str(main_out),
    ]  # fmt: skip
    if ";" in vf:  # filter_complex needs explicit input label
        idx = args.index("-filter_complex")
        args[idx + 1] = "[0:v]" + vf
    run_ffmpeg(args)
    if spec.end_card:
        append_end_card(main_out, spec.end_card, spec.out, spec, fps, info.has_audio)
        main_out.unlink(missing_ok=True)

    thumb = extract_frame(spec.out, min(1.0, duration / 2), spec.out.with_suffix(".jpg"), width=540)
    out_info = probe(spec.out)
    return RenderResult(
        path=spec.out, thumbnail=thumb, duration=out_info.duration, layout=layout, loudness=loud
    )
