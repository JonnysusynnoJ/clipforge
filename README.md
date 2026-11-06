# ClipForge

Clipping pipeline for Whop Content Rewards campaigns: authorized long-form material →
transcript → highlight candidates → rendered 9:16 clips → agent review → Metricool →
Whop submission → view tracking.

Status: **Phase 2** (campaign discovery, rule parsing, scoring, compliance) on top of Phase 1
(local clip generation with agent scoring/review). Operating instructions for the agent: `AGENT_RUNBOOK.md`.
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
| `clipforge discover` | Reads public contentrewards.com discover + detail pages, scores campaigns. |
| `clipforge campaigns` | Ranked list: score, CPM, budget left, runway, readiness, flags. |
| `clipforge campaign-add-url URL` | Adds one campaign by its public URL. |
| `clipforge campaign-brief ID --url LINK` | Fetches a public brief (Google Doc) → `rules_request.json` for the agent. |
| `clipforge campaign-rules ID rules.json` | Validates the agent's parsed rules (errors → fix and retry) and stores them. |
| `clipforge campaign-show ID` | Raw brief next to parsed rules and score report. |
| `clipforge campaign-select` | Activates the best ready campaigns (`campaigns.max_active`). |
| `clipforge account-add PLATFORM HANDLE` | Registers a posting account (platform fit for scoring). |
| `clipforge campaign-add spec.json` | Creates/updates a campaign from a spec file (rules + raw text). |
| `clipforge ingest <campaign> <file> --origin URL` | Imports an authorized file (hash-deduplicated). |
| `clipforge ingest-mediasilo <campaign> <url>` | Downloads videos from a public MediaSilo share listed in the campaign. |
| `clipforge transcribe <asset>` | Whisper with word timestamps (cached by hash). |
| `clipforge candidates <asset>` | Features, candidates, diverse selection, contact sheets → `candidates.json`. |
| `clipforge score <run> scores.json` | Applies the agent's candidate scores. |
| `clipforge render <run>` | Renders 9:16 clips, runs compliance → `review.json`. |
| `clipforge review <run> decisions.json` | Applies the agent's approve/reject + captions. |
| `clipforge run --file F --campaign-json C` | End-to-end run with heuristic ranking (optionally `--scores`), prints timings and report. |

### Campaign discovery and scoring

- Only public pages are read (`/c/discover/clipping`, `/c/campaigns/<id>`); `/api/` is
  disallowed by robots.txt and never called. Nothing behind a login is accessed.
- The full brief (rules + material links) is only visible when logged in. The owner pastes the
  brief link once per campaign (`campaign-brief --url`); the agent parses it.
- Score = expected net $ per clip at `analytics.default_median_views` on the platforms of the
  registered accounts (CPM, minimum payout per clip, cap, campaign fee), times a runway factor
  (budget burn rate). Penalties: application required, other content language, sensitive
  topics, low budget. Blocked (score 0): gambling/adult categories and keywords, no matching
  account platform, inactive campaign.
- A campaign is *ready* only with a brief and at least one authorized material source.

### Pipeline notes

- **Highlights** work for speech (windows snap to sentence boundaries) and for music-driven
  material like trailers (windows snap to scene cuts and beats). Selection uses MMR over
  time overlap, a share of non-obvious mid-score picks and cut variants per moment.
- **Rendering:** 1080×1920, 30 fps, H.264/AAC, two-pass `loudnorm` to −14 LUFS, word-timed
  ASS captions (bundled Inter font), hook text and credit overlays inside the platform safe
  zones. Layouts: `blur_fit` (default for 16:9), `center_crop`, `passthrough` for 9:16.
- **Campaign specs** live in `data/campaigns/` (not committed); see `CampaignSpec` in
  `clipforge/campaigns/rules.py` for the format.

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
