"""Whisper transcription with word timestamps and a content-hash cache."""

from __future__ import annotations

import importlib.util
import json
import platform
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from clipforge.media import SAMPLE_RATE, load_audio


@dataclass
class Word:
    w: str
    start: float
    end: float
    conf: float


@dataclass
class Segment:
    start: float
    end: float
    text: str


@dataclass
class TranscriptResult:
    language: str | None
    model: str
    backend: str
    words: list[Word] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)

    @property
    def speech_seconds(self) -> float:
        return sum(s.end - s.start for s in self.segments)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TranscriptResult:
        return cls(
            language=data.get("language"),
            model=data["model"],
            backend=data["backend"],
            words=[Word(**w) for w in data.get("words", [])],
            segments=[Segment(**s) for s in data.get("segments", [])],
        )


def resolve_backend(preference: str) -> str:
    has_mlx = importlib.util.find_spec("mlx_whisper") is not None
    is_apple = platform.system() == "Darwin" and platform.machine() == "arm64"
    if preference == "mlx" or (preference == "auto" and has_mlx and is_apple):
        return "mlx"
    return "faster_whisper"


def _transcribe_faster_whisper(path: Path, model_name: str) -> TranscriptResult:
    from faster_whisper import WhisperModel

    audio = load_audio(path, SAMPLE_RATE)
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments_iter, info = model.transcribe(audio, word_timestamps=True, vad_filter=True)
    result = TranscriptResult(language=info.language, model=model_name, backend="faster_whisper")
    for seg in segments_iter:
        result.segments.append(Segment(start=seg.start, end=seg.end, text=seg.text.strip()))
        for w in seg.words or []:
            result.words.append(
                Word(w=w.word.strip(), start=w.start, end=w.end, conf=float(w.probability))
            )
    return result


def _transcribe_mlx(path: Path, model_name: str) -> TranscriptResult:  # pragma: no cover
    import mlx_whisper

    repo = model_name if "/" in model_name else f"mlx-community/whisper-{model_name}-mlx"
    out = mlx_whisper.transcribe(str(path), path_or_hf_repo=repo, word_timestamps=True)
    result = TranscriptResult(language=out.get("language"), model=repo, backend="mlx")
    for seg in out.get("segments", []):
        result.segments.append(Segment(seg["start"], seg["end"], seg["text"].strip()))
        for w in seg.get("words", []):
            result.words.append(
                Word(w["word"].strip(), w["start"], w["end"], float(w.get("probability", 1)))
            )
    return result


def transcribe(
    path: Path, *, sha256: str, model_name: str, backend: str, cache_dir: Path
) -> TranscriptResult:
    """Transcribe `path`, reusing the cached result for the same content + model."""
    resolved = resolve_backend(backend)
    cache_file = cache_dir / "transcripts" / f"{sha256[:24]}_{resolved}_{model_name}.json"
    if cache_file.exists():
        return TranscriptResult.from_json(json.loads(cache_file.read_text()))
    if resolved == "mlx":
        result = _transcribe_mlx(path, model_name)
    else:
        result = _transcribe_faster_whisper(path, model_name)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(result.to_json()))
    return result
