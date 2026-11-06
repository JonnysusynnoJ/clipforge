"""Diverse selection: MMR over time overlap, non-obvious picks and cut variants."""

from __future__ import annotations

import random
from dataclasses import replace

from clipforge.highlights.candidates import Candidate


def overlap(a: Candidate, b: Candidate) -> float:
    """Intersection over the shorter clip (1.0 = one contains the other)."""
    inter = max(0.0, min(a.end, b.end) - max(a.start, b.start))
    return inter / max(min(a.duration, b.duration), 1e-6)


def mmr(pool: list[Candidate], k: int, lam: float = 0.7) -> list[Candidate]:
    """Maximal marginal relevance: trade off score against overlap with picks."""
    selected: list[Candidate] = []
    remaining = sorted(pool, key=lambda c: c.score, reverse=True)
    while remaining and len(selected) < k:
        best = max(
            remaining,
            key=lambda c: (
                lam * c.score - (1 - lam) * max((overlap(c, s) for s in selected), default=0.0)
            ),
        )
        if selected and max(overlap(best, s) for s in selected) > 0.5:
            remaining = [c for c in remaining if max(overlap(c, s) for s in selected) <= 0.5]
            continue
        selected.append(best)
        remaining.remove(best)
    return selected


def select(
    candidates: list[Candidate],
    *,
    k: int,
    non_obvious_share: float,
    seed: int = 7,
) -> list[Candidate]:
    """Pick `k` diverse candidates; a share comes from the mid score range."""
    if not candidates:
        return []
    n_non_obvious = round(k * non_obvious_share)
    top = mmr(candidates, k - n_non_obvious)
    ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
    lo, hi = int(len(ranked) * 0.3), int(len(ranked) * 0.7)
    middle = [c for c in ranked[lo:hi] if all(overlap(c, s) <= 0.3 for s in top)]
    rng = random.Random(seed)
    rng.shuffle(middle)
    extra = mmr(middle, n_non_obvious) if n_non_obvious else []
    for c in extra:
        c.features["non_obvious"] = 1.0
    return top + extra


def variants(
    c: Candidate, boundaries: list[float], n: int, min_s: float, max_s: float
) -> list[Candidate]:
    """Alternative cuts of the same moment: start one boundary earlier / later."""
    out: list[Candidate] = []
    earlier = [b for b in boundaries if b < c.start and c.end - b <= max_s]
    later = [b for b in boundaries if c.start < b and c.end - b >= min_s]
    for start in ([earlier[-1]] if earlier else []) + ([later[0]] if later else []):
        if len(out) >= n - 1:
            break
        out.append(replace(c, start=start, features=dict(c.features), variant_of=None))
    return out
