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
