# ClipForge

Clipping pipeline for Whop Content Rewards campaigns: authorized long-form material →
transcript → highlight candidates → rendered 9:16 clips → agent review → Metricool →
Whop submission → view tracking.

Status: **Phase 0** (setup, data model, state machine, `doctor`).
See `docs/DECISIONS.md` for owner decisions that amend the original brief.

## Setup

### macOS (Apple Silicon)

```bash
brew install ffmpeg uv
uv python install 3.12
uv sync
cp config.example.yaml config.yaml
cp .env.example .env        # fill in secrets
uv run clipforge db upgrade
uv run clipforge doctor
```

### Linux / cloud session

Same steps; install ffmpeg with `apt-get install -y ffmpeg`.

## Commands

| Command | Purpose |
|---|---|
| `clipforge doctor` | Checks ffmpeg (+ `loudnorm`, `ass`), folders, disk, DB migrations, ML backends, secrets, publishing config. Exit code 1 on failures. |
| `clipforge db upgrade` | Applies Alembic migrations to `data/clipforge.db`. |

## Development

```bash
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

## Compliance rules (enforced in code)

1. **Review gate.** Every clip passes `pending_review`. Approval is done by the Claude review
   agent (or a human); the pipeline itself (`Actor.SYSTEM`) can never approve. Agent
   approval needs a recorded `approve` decision.
2. **Compliance blocks approval.** No approval while the latest compliance report has hard
   violations or no checks were run.
3. **Authorized material only.** Sources are always linked to a campaign and must be listed
   in its allowed sources.
4. **No fake engagement**, no automated likes/comments/follows, no account creation, no
   scraping behind logins, no UI automation of Whop/TikTok/Instagram/YouTube.
5. **Disclosure.** Paid-campaign posts carry `#ad` (configurable) and the platform's
   commercial-content flag; no disclosure, no publish.
6. **Rate limits** per account (default during testing: 1 post/day, 120 min gap).
7. **Kill switch.** `publishing.enabled: false` stops all publishing.

All status changes go through `clipforge.core.state.transition`, which validates the
transition and writes an audit log entry.
