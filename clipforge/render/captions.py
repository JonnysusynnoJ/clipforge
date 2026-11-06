"""ASS subtitle generation: word-timed captions, hook text and credit overlays."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from clipforge.render.profiles import ExportProfile
from clipforge.transcribe.whisper import Word

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"


@dataclass(frozen=True)
class CaptionStyle:
    name: str
    font: str = "Inter"
    size: int = 78
    primary: str = "&H00FFFFFF"  # white  (ASS: &HAABBGGRR)
    highlight: str = "&H0000E5FF"  # yellow
    outline: str = "&H00000000"
    outline_w: int = 6
    shadow: int = 0
    bold: bool = True
    words_per_line: int = 3
    uppercase: bool = True


PRESETS: dict[str, CaptionStyle] = {
    "default": CaptionStyle(name="default"),
    "clean": CaptionStyle(
        name="clean", size=66, highlight="&H00FFFFFF", outline_w=4, uppercase=False
    ),
    "bold_green": CaptionStyle(name="bold_green", highlight="&H0066FF33"),
}


def _ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")").replace("\n", "\\N")


def build_ass(
    *,
    profile: ExportProfile,
    duration: float,
    words: list[Word],
    style: CaptionStyle,
    hook_text: str | None = None,
    hook_seconds: float = 3.0,
    credits: list[str] | None = None,
    caption_center_y: int | None = None,
) -> str:
    """Return an ASS document. Word times must be relative to the clip start."""
    w, h, safe = profile.width, profile.height, profile.safe
    bold = -1 if style.bold else 0
    cap_margin_v = h - (caption_center_y or int(h * 0.68))
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{style.font},{style.size},{style.primary},{style.highlight},{style.outline},&H64000000,{bold},0,0,0,100,100,0,0,1,{style.outline_w},{style.shadow},2,{safe.left},{safe.right},{cap_margin_v},1
Style: Hook,{style.font},70,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,3,18,0,8,{safe.left + 40},{safe.right + 40},{safe.top + 40},1
Style: Credit,{style.font},40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,3,0,2,{safe.left},{safe.right},{safe.bottom + 20},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""  # noqa: E501
    lines: list[str] = []
    if hook_text:
        end = min(hook_seconds, duration)
        lines.append(
            f"Dialogue: 2,{_ts(0)},{_ts(end)},Hook,,0,0,0,,{{\\fad(120,200)}}{_escape(hook_text)}"
        )
    for credit in credits or []:
        lines.append(f"Dialogue: 1,{_ts(0)},{_ts(duration)},Credit,,0,0,0,,{_escape(credit)}")
    # word-timed captions: groups of N words, current word highlighted
    group: list[Word] = []
    groups: list[list[Word]] = []
    for word in words:
        group.append(word)
        if len(group) >= style.words_per_line or word.w.endswith((".", "?", "!", ",")):
            groups.append(group)
            group = []
    if group:
        groups.append(group)
    for g in groups:
        g_end = g[-1].end
        for i, word in enumerate(g):
            start = word.start
            end = g[i + 1].start if i + 1 < len(g) else g_end
            parts = []
            for j, other in enumerate(g):
                text = other.w.upper() if style.uppercase else other.w
                text = _escape(text)
                parts.append(
                    f"{{\\c{style.highlight}}}{text}{{\\c{style.primary}}}" if j == i else text
                )
            if end > start:
                lines.append(
                    f"Dialogue: 0,{_ts(start)},{_ts(end)},Caption,,0,0,0,,{' '.join(parts)}"
                )
    return header + "\n".join(lines) + "\n"
