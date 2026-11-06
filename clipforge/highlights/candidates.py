"""Cheap candidate generation and heuristic scoring.

Works for speech material (windows snap to sentence boundaries) and for music-driven
material such as trailers (windows snap to scene cuts and beats).
"""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from clipforge.highlights.features import Timeline
from clipforge.transcribe.whisper import TranscriptResult

QUESTION_WORDS = {"why", "how", "what", "who", "when", "where", "which", "would", "should"}
CONTROVERSY = {
    "never", "always", "worst", "best", "insane", "crazy", "secret", "truth",
    "wrong", "hate", "love", "nobody", "everyone", "actually", "honestly",
}  # fmt: skip


@dataclass
class Candidate:
    start: float
    end: float
    features: dict[str, float] = field(default_factory=dict)
    text: str = ""
    score: float = 0.0
    variant_of: int | None = None

    @property
    def duration(self) -> float:
        return self.end - self.start

    def to_json(self) -> dict[str, Any]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "score": round(self.score, 4),
            "features": {k: round(v, 4) for k, v in self.features.items()},
            "text": self.text,
            "variant_of": self.variant_of,
        }


def speech_mode(transcript: TranscriptResult, duration: float) -> bool:
    return duration > 0 and transcript.speech_seconds / duration > 0.25


def boundaries(timeline: Timeline, transcript: TranscriptResult, use_speech: bool) -> list[float]:
    if use_speech:
        points = [s.start for s in transcript.segments] + [s.end for s in transcript.segments]
        # sentence ends inside segments
        for i, w in enumerate(transcript.words[:-1]):
            if w.w.endswith((".", "?", "!")):
                points += [w.end, transcript.words[i + 1].start]
    else:
        points = [0.0, *timeline.cuts, *timeline.beats[::4]]
    return sorted({round(p, 3) for p in points if 0 <= p <= timeline.duration})


def _words_between(transcript: TranscriptResult, start: float, end: float) -> list[str]:
    words = transcript.words
    if not words:
        return []
    starts = [w.start for w in words]  # words are time-ordered
    lo = bisect_left(starts, start - 0.05)
    hi = bisect_left(starts, end + 0.05)
    return [w.w for w in words[lo:hi] if w.end <= end + 0.05]


def _cuts_inside(cuts: list[float], start: float, end: float) -> int:
    return bisect_left(cuts, end) - bisect_left(cuts, start)


def _mid_word(transcript: TranscriptResult, t: float) -> bool:
    words = transcript.words
    i = bisect_left([w.start for w in words], t) - 1
    return 0 <= i < len(words) and words[i].start + 0.05 < t < words[i].end - 0.05


def window_features(
    c: Candidate, timeline: Timeline, transcript: TranscriptResult, use_speech: bool
) -> dict[str, float]:
    rms = timeline.window(timeline.rms, c.start, c.end)
    onset = timeline.window(timeline.onset, c.start, c.end)
    flat = timeline.window(timeline.flatness, c.start, c.end)
    hook = timeline.window(timeline.rms, c.start, min(c.end, c.start + 2.0))
    hook_onset = timeline.window(timeline.onset, c.start, min(c.end, c.start + 2.0))
    tail = timeline.window(timeline.rms, max(c.start, c.end - 1.0), c.end)
    words = _words_between(transcript, c.start, c.end)
    lower = [re.sub(r"[^a-z']", "", w.lower()) for w in words]
    speech_s = sum(
        min(s.end, c.end) - max(s.start, c.start)
        for s in transcript.segments
        if s.end > c.start and s.start < c.end
    )
    mean = float(rms.mean()) if rms.size else 0.0
    return {
        "energy_mean": mean,
        "energy_peak": float(rms.max()) if rms.size else 0.0,
        "hook_energy": float(hook.mean()) if hook.size else 0.0,
        "hook_onset": float(hook_onset.mean()) if hook_onset.size else 0.0,
        "onset_mean": float(onset.mean()) if onset.size else 0.0,
        "end_drop": float(max(0.0, mean - float(tail.mean()))) if tail.size else 0.0,
        "cut_rate": _cuts_inside(timeline.cuts, c.start, c.end) / max(c.duration, 1e-6),
        "crowd_noise": float(((flat > 0.3) & (rms > 0.6)).mean()) if flat.size else 0.0,
        "speech_ratio": max(0.0, speech_s) / max(c.duration, 1e-6),
        "words_per_sec": len(words) / max(c.duration, 1e-6),
        "question_words": float(sum(w in QUESTION_WORDS for w in lower)),
        "controversy_markers": float(sum(w in CONTROVERSY for w in lower)),
        "clean_cut": float(
            not use_speech or not (_mid_word(transcript, c.start) or _mid_word(transcript, c.end))
        ),
    }


def heuristic_score(f: dict[str, float], use_speech: bool) -> float:
    """Weighted sum of features in roughly 0..1. Will later be replaced by a learned model."""
    if use_speech:
        score = (
            0.25 * f["hook_energy"]
            + 0.15 * f["energy_peak"]
            + 0.15 * min(f["words_per_sec"] / 3.0, 1.0)
            + 0.10 * min(f["question_words"] / 2.0, 1.0)
            + 0.10 * min(f["controversy_markers"] / 2.0, 1.0)
            + 0.10 * f["crowd_noise"]
            + 0.15 * f["clean_cut"]
        )
    else:
        score = (
            0.30 * f["hook_energy"]
            + 0.15 * f["hook_onset"]
            + 0.20 * f["energy_mean"]
            + 0.10 * f["energy_peak"]
            + 0.15 * min(f["cut_rate"] / 1.5, 1.0)
            + 0.10 * min(f["end_drop"] * 3.0, 1.0)
        )
    return float(np.clip(score, 0.0, 1.0))


def generate(
    timeline: Timeline,
    transcript: TranscriptResult,
    *,
    min_seconds: float,
    max_seconds: float,
    max_windows: int = 20_000,
) -> list[Candidate]:
    """All windows between boundaries with a length in [min, max].

    If there would be more than `max_windows`, start points are thinned evenly so the
    whole source stays covered (never just the beginning).
    """
    use_speech = speech_mode(transcript, timeline.duration)
    pts = boundaries(timeline, transcript, use_speech)
    avg_ends = max(1.0, len(pts) * (max_seconds - min_seconds) / max(timeline.duration, 1.0))
    stride = max(1, int(len(pts) * avg_ends / max_windows) + 1)
    out: list[Candidate] = []
    for i, start in enumerate(pts):
        if i % stride:
            continue
        for end in pts[i + 1 :]:
            length = end - start
            if length < min_seconds:
                continue
            if length > max_seconds:
                break
            c = Candidate(start=start, end=end)
            c.features = window_features(c, timeline, transcript, use_speech)
            c.score = heuristic_score(c.features, use_speech)
            c.text = " ".join(_words_between(transcript, start, end))
            out.append(c)
    if not out and timeline.duration >= min_seconds:  # no usable boundaries: fixed windows
        length = min(max_seconds, max(min_seconds, timeline.duration / 4))
        t = 0.0
        while t + length <= timeline.duration:
            c = Candidate(start=t, end=t + length)
            c.features = window_features(c, timeline, transcript, use_speech)
            c.score = heuristic_score(c.features, use_speech)
            out.append(c)
            t += length / 2
    return out
