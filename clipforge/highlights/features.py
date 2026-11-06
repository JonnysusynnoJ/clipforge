"""Timeline features of a source: audio energy, beats, scene cuts, crowd noise."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from clipforge.media import SAMPLE_RATE, load_audio

HOP_SECONDS = 0.1


@dataclass
class Timeline:
    duration: float
    hop: float = HOP_SECONDS
    rms: list[float] = field(default_factory=list)  # normalized 0..1 per hop
    flatness: list[float] = field(default_factory=list)
    onset: list[float] = field(default_factory=list)  # normalized 0..1 per hop
    beats: list[float] = field(default_factory=list)  # seconds
    cuts: list[float] = field(default_factory=list)  # scene cut times, seconds

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Timeline:
        return cls(**data)

    def window(self, series: list[float], start: float, end: float) -> np.ndarray:
        a = max(0, int(start / self.hop))
        b = max(a + 1, int(end / self.hop))
        return np.asarray(series[a:b], dtype=np.float32)


def _normalize(x: np.ndarray) -> np.ndarray:
    if x.size == 0:
        return x
    lo, hi = np.percentile(x, 2), np.percentile(x, 98)
    if hi - lo < 1e-9:
        return np.zeros_like(x)
    return np.clip((x - lo) / (hi - lo), 0.0, 1.0)


def audio_features(audio: np.ndarray, sr: int = SAMPLE_RATE) -> dict[str, Any]:
    import librosa

    hop = int(sr * HOP_SECONDS)
    if audio.size < hop * 2:
        return {"rms": [], "flatness": [], "onset": [], "beats": []}
    rms = librosa.feature.rms(y=audio, frame_length=hop * 2, hop_length=hop)[0]
    flat = librosa.feature.spectral_flatness(y=audio, n_fft=hop * 2, hop_length=hop)[0]
    onset = librosa.onset.onset_strength(y=audio, sr=sr, hop_length=hop)
    _, beat_frames = librosa.beat.beat_track(onset_envelope=onset, sr=sr, hop_length=hop)
    beats = librosa.frames_to_time(beat_frames, sr=sr, hop_length=hop)
    rms_db = librosa.amplitude_to_db(rms + 1e-6)
    return {
        "rms": _normalize(rms_db).round(4).tolist(),
        "flatness": np.asarray(flat).round(4).tolist(),
        "onset": _normalize(onset).round(4).tolist(),
        "beats": np.asarray(beats).round(3).tolist(),
    }


def scene_cuts(path: Path, threshold: float = 27.0) -> list[float]:
    from scenedetect import ContentDetector, detect

    scenes = detect(str(path), ContentDetector(threshold=threshold, min_scene_len=8))
    return [round(start.seconds, 3) for start, _ in scenes[1:]]


def compute_timeline(path: Path, *, sha256: str, duration: float, cache_dir: Path) -> Timeline:
    cache_file = cache_dir / "features" / f"{sha256[:24]}_v1.json"
    if cache_file.exists():
        return Timeline.from_json(json.loads(cache_file.read_text()))
    feats = audio_features(load_audio(path))
    timeline = Timeline(duration=duration, cuts=scene_cuts(path), **feats)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(timeline.to_json()))
    return timeline
