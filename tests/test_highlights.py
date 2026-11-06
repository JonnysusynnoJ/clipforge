from __future__ import annotations

import numpy as np

from clipforge.highlights import candidates as cand
from clipforge.highlights.features import Timeline
from clipforge.highlights.select import overlap, select, variants
from clipforge.transcribe.whisper import Segment, TranscriptResult, Word


def music_timeline(duration: float = 140.0) -> Timeline:
    rng = np.random.default_rng(0)
    n = int(duration / 0.1)
    rms = np.clip(0.5 + 0.3 * np.sin(np.arange(n) / 37) + rng.normal(0, 0.05, n), 0, 1)
    return Timeline(
        duration=duration,
        rms=rms.tolist(),
        flatness=[0.1] * n,
        onset=rng.random(n).tolist(),
        beats=[round(i * 0.5, 2) for i in range(int(duration * 2))],
        cuts=[round(i * 0.9, 2) for i in range(1, int(duration / 0.9))],  # fast montage
    )


EMPTY = TranscriptResult(language="en", model="x", backend="x")


def test_music_candidates_cover_whole_source() -> None:
    """Regression: dense cuts must not limit candidates to the beginning."""
    tl = music_timeline()
    pool = cand.generate(tl, EMPTY, min_seconds=10, max_seconds=30, max_windows=3000)
    assert pool
    assert max(c.end for c in pool) > 0.8 * tl.duration
    assert all(10 <= c.duration <= 30 for c in pool)
    assert all(0.0 <= c.score <= 1.0 for c in pool)


def test_speech_candidates_do_not_cut_words() -> None:
    words, segments, t = [], [], 0.0
    for s in range(30):
        seg_words = []
        for i in range(8):
            w = Word(w=f"word{i}" + ("." if i == 7 else ""), start=t, end=t + 0.4, conf=0.9)
            seg_words.append(w)
            t += 0.5
        segments.append(Segment(start=seg_words[0].start, end=seg_words[-1].end, text=f"s{s}"))
        words += seg_words
        t += 0.6
    tr = TranscriptResult(language="en", model="x", backend="x", words=words, segments=segments)
    tl = music_timeline(duration=t + 1)
    assert cand.speech_mode(tr, tl.duration)
    pool = cand.generate(tl, tr, min_seconds=8, max_seconds=20)
    assert pool
    for c in pool:
        assert not cand._mid_word(tr, c.start)
        assert not cand._mid_word(tr, c.end)
        assert c.features["clean_cut"] == 1.0


def test_selection_is_diverse() -> None:
    pool = cand.generate(music_timeline(), EMPTY, min_seconds=10, max_seconds=30)
    picked = select(pool, k=8, non_obvious_share=0.25)
    assert 1 < len(picked) <= 8
    for i, a in enumerate(picked):
        for b in picked[i + 1 :]:
            assert overlap(a, b) <= 0.5
    assert sum(c.features.get("non_obvious", 0) for c in picked) <= 2


def test_variants_stay_in_bounds() -> None:
    pool = cand.generate(music_timeline(), EMPTY, min_seconds=10, max_seconds=30)
    c = pool[len(pool) // 2]
    bounds = sorted({p for x in pool for p in (x.start, x.end)})
    for v in variants(c, bounds, n=3, min_s=10, max_s=30):
        assert v.end == c.end and v.start != c.start
        assert 10 <= v.duration <= 30
