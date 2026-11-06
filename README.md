# ClipForge

Clipping pipeline for Whop Content Rewards campaigns: authorized long-form material →
transcript → highlight candidates → rendered 9:16 clips → agent review → Metricool →
Whop submission → view tracking.

Status: **Phase 5** (views, earnings, Whop submission list, weekly report) on top of
**Phase 4** (publishing via Metricool with R2 media hosting and rate limits) on top of
Phase 2 (campaign discovery, rules, scoring) and Phase 1 (clip generation with agent review).
Phase 3 (dashboard) is reduced to a later control view; see `docs/DECISIONS.md`. Operating instructions for the agent: `AGENT_RUNBOOK.md`.
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
| `clipforge publish-plan [--limit N]` | Approved clips → guards, rate-limited slots, R2 upload, Metricool requests (`data/publish_queue.json`). |
| `clipforge publish-record ID --ok/--failed` | Records the Metricool connector result for a planned post. |
| `clipforge publish-published ID URL` | Stores the live post URL. |
| `clipforge publish-cancel ID` / `publish-export ID` | Cancel a post / manual export (video + caption). |
| `clipforge posts` | Lists posts. |
| `clipforge metrics-spec` / `metrics-import NET FILE` | Metricool analytics ids per network / import views per post. |
| `clipforge earnings` | Estimated earnings per post (CPM, min payout, cap, fee) and engagement check. |
| `clipforge submit-list` / `submit-mark ID` | Posts to submit on Whop (with campaign link) / record a submission. |
| `clipforge report` | Weekly markdown report (`data/reports/weekly_<year>-W<week>.md`). |
| `clipforge state pull/push` | Restore/save state in the private R2 bucket (cloud sessions). |
| `clipforge daily-status` | Backlog per active campaign, open posts, brands to reconcile. |
| `clipforge publish-sync file.json` | Reconcile posts with Metricool `getScheduledPosts` output. |
| `clipforge fetch-sources` | Fetch source videos of active campaigns from the state bucket. |
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

### Publishing

- Media is uploaded to Cloudflare R2 (`hosting.*` in config.yaml) and checked for a public
  200 before a post is created; Metricool copies the file into its own storage on creation.
  The Cloudflare token is a network secret of the cloud environment (never in code or `.env`).
- Hard guards before every post: kill switch (`publishing.enabled`), clip approved,
  campaign active, compliance re-run passes, disclosure in caption, account active + public +
  linked in Whop + has a Metricool brand, one post per clip, rate limits (default 1 post per
  account per day, 120 min gap, 9–23 h local window).
- `publishing.mode: draft` creates Metricool drafts only; `live` publishes automatically.
- ClipForge does not call Metricool itself: the agent submits each queued request through the
  Metricool connector and records the answer.

### Daily run

A scheduled cloud session runs `DAILY_RUN.md` every morning: restore state, reconcile posts,
discover/select campaigns, produce and review clips, plan and submit posts, save state, report.

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
