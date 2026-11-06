# Decisions and amendments to PROJECT_BRIEF.md

Owner decisions taken after the initial brief (2026-11-06).

## D1 – Unattended publishing with agent review (amends hard limit 1)

The owner explicitly wants to test a fully unattended daily run. The human approval
step is replaced by a **Claude review agent**. The state machine allows the
`pending_review -> approved` transition for `Actor.AGENT` and `Actor.HUMAN`, never for
`Actor.SYSTEM`. Approval is still impossible while the compliance report has hard
violations.

All other hard limits stay as code-level guards the agent cannot bypass: authorized
sources only, no fake engagement, no scraping behind logins, mandatory disclosure,
per-account rate limits, campaign rules.

Safeguards for the test phase:
- `publishing.enabled` kill switch (default `false`).
- Conservative default: 1 post per account per day, 120 min gap.
- Daily report of everything posted and rejected, with reasons.

## D2 – Claude agent session instead of the Anthropic SDK

Judgement steps (campaign choice, rule parsing, highlight scoring, clip QA, captions)
are done by the Claude Code agent session that runs the daily job. The Python tool does
the mechanical work and exchanges JSON files with the agent. No API key required.

## D3 – Runs in the cloud

The daily job runs as a scheduled Claude Code cloud session, so the Mac can be off.
Consequences: CPU-only transcription (`faster-whisper`), limited disk (sources are
deleted after processing), ephemeral container (state is persisted outside, see D5).

## D4 – Metricool via the Claude connector

Publishing uses the Metricool MCP connector (no Metricool API plan or keys). Media is
passed as public / Google Drive URLs. TikTok posts set `commercialContentThirdParty`
for paid campaigns, in addition to the `#ad` text disclosure.

## D5 – Google Drive for source material, renders and state

Campaign material comes from Google Drive shares. Rendered clips and the SQLite state
are stored in a Google Drive folder via a service account.

## D6 – Language

Content is English: English Whisper model, English captions, `#ad` disclosure.

## D7 – Campaign discovery

Try the public Whop directory first; fall back to an Apify actor if that fails.

## D8 – Face tracking deferred, mediapipe dropped

mediapipe 1.x no longer ships the legacy face-detection solution and its task models must be
downloaded from Google storage; OpenCV 5 wheels no longer include Haar cascades. The first
campaign (MW4 trailer) is music-driven game footage without faces to track, so the default
16:9 layout is `blur_fit`. `render.FaceDetector` is the hook for a detector later (needed for
talking-head podcasts).

## D9 – Campaign material from MediaSilo

The first campaign links an official public MediaSilo share instead of Google Drive.
`ingest/mediasilo.py` reads only the public share page (no login) and only if the share URL
is in the campaign's `allowed_sources`.

## D10 – Discovery from public contentrewards.com pages

Campaigns live on contentrewards.com (Whop Content Rewards). The public discover page and
campaign pages embed structured data (budget, spend, CPM per platform, minimum/maximum payout
per clip, fee, status, Whop submission link), so no Apify actor is needed. `/api/` is
disallowed by robots.txt and is never called; requests are rate-limited (1.5 s).
The full brief is only shown to logged-in users, so the owner provides the brief link once
per campaign (hard limit 4: no scraping behind logins).

## D11 – Fee and payout threshold per campaign

Earnings use the campaign's own fee (`configuration.fee`, basis points; MW4: 8 %) instead of
the 7 % default, and respect the per-clip minimum payout (MW4 Instagram: $4 → 2,963 views)
and cap.

## D12 – Adult and skin-gambling campaigns blocked by default

Besides gambling/casino, adult platforms (e.g. Sinparty) and skin/case gambling (e.g.
Flip.gg) are blocked via `campaigns.blocked_keywords`. Health/finance topics are only flagged.

## D13 – Phase 3 reduced

With unattended publishing (D1) the human review queue is not needed. Phase 3 shrinks to a
later read-only control view (campaigns, clips with agent reasoning, posts, kill switch).

## D14 – Media hosting on Cloudflare R2

Metricool needs public media URLs. Google service accounts have no storage quota for personal
Gmail accounts, so clips are hosted on Cloudflare R2 (public r2.dev URL). Upload uses the
Cloudflare REST API with a token stored as a network secret of the cloud environment.

## D15 – Metricool via connector: findings

- `createScheduledPost` with `autoPublish: true` publishes directly via the API for Instagram,
  TikTok and YouTube; `autoPublish: false` only sends a push notification for manual posting.
  `draft: true` creates a draft that never publishes.
- TikTok supports `commercialContentThirdParty` (paid-partnership disclosure), set for every
  campaign post. Custom thumbnails are ignored on personal TikTok accounts.
- Instagram's paid-partnership label is not available through the API; disclosure is the
  `#Ad` line in the caption (required by the campaigns anyway).
- Media passed by URL is copied into Metricool's storage at creation time.
- Live TikTok publishing is untested until a TikTok account is connected.

## D16 – Live mode and daily cloud run

The owner approved the first draft and switched to `publishing.mode: live`. The daily run is a
scheduled cloud task that starts a fresh session (Mac can be off) and follows `DAILY_RUN.md`.
State (DB, config, campaign files, caches, unposted renders) is persisted in the private R2
bucket `clipforge-state` (`clipforge state pull/push`, 7 snapshots kept); source videos are
stored there once and fetched on demand. New candidate runs never re-offer moments that were
already used (max 30 % overlap).

## D17 – Whop submission stays semi-automatic

Research (Nov 2026): Content Rewards documents no creator API for submissions; only the
dashboard flow. Whop's beta Bounty Submissions API (`/api/v1/bounty_submissions`) needs a
personal user token and nothing documents that Content Rewards campaigns are bounties. Per the
brief no `WhopApiSubmitter` is built. ClipForge prepares the list (submit link + URLs) in the
daily report; the owner submits; `submit-mark` records it. Content Rewards terms ban botting
and timing tricks that make automation look organic; ClipForge schedules openly via Metricool
and never randomizes timing to disguise automation.

## D18 – Views from Metricool analytics

Instagram Reels: IGRE06 (url), IGRE23 (views), IGRE10 likes, IGRE07 comments, IGRE21 shares,
IGRE24 avg watch time. TikTok: TKPO03/07/08/09/10/15. Metricool's YouTube video metrics are
marked deprecated; YouTube views are not imported yet. Snapshots are matched by normalized
post URL; earnings use the latest snapshot. Engagement below 0.5 % (likes/views, from 200
views) is flagged because campaigns like MW4 reject such posts.
