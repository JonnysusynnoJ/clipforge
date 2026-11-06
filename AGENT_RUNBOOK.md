# Agent runbook

Instructions for the Claude session that operates ClipForge (manually now, as a daily
scheduled cloud task from Phase 4 on). The Python CLI does the mechanical work; you make
the judgement calls and write them as JSON files. Never edit the database directly.

## Hard rules (also enforced in code)

- Only use material from the campaign's `allowed_sources`. Never download anything else.
- Every caption follows the campaign rules exactly: required mentions, disclosure hashtag
  (`#Ad` etc.) on its own line as the first hashtag, max extra hashtags.
- No made-up facts, stats or dates. A trailer is a trailer, never "gameplay".
- No rage bait, no exaggerated hooks. Hook text must match what is on screen.
- Skip moments with gore, NSFW or controversial content.
- Never interact with posts (no likes, comments, follows). Never create accounts.
- If compliance blocks a clip, fix the caption or reject it. Do not work around the check.

## Campaigns

```bash
uv run clipforge discover          # refresh public campaign data + scores
uv run clipforge campaign-select   # activate the best ready campaigns
```
For a campaign that needs a brief, the owner provides the brief link once:
```bash
uv run clipforge campaign-brief <id> --url <brief link>
```
Read `data/campaigns/<id>/rules_request.json` (brief text, schema, link candidates) and write
`rules.json`. Rules: copy mentions/hashtags/disclosure exactly; `allowed_sources` only the
official material links; `null` where the brief says nothing; never invent rules. Then
`uv run clipforge campaign-rules <id> rules.json`; on errors fix the file and retry (max 3).

## Steps for one campaign source

```bash
uv run clipforge campaign-add data/campaigns/<campaign>.json      # once per campaign
uv run clipforge ingest-mediasilo <campaign_id> <allowed share url>  # or: ingest
uv run clipforge candidates <asset_id>                              # prints run id
```

1. **Score.** Read `data/runs/<run>/candidates.json`. Open every `contact_sheet` image
   (6 frames of the candidate). Score 0–10: hook in the first 2 s, understandable
   without context, emotional/visual peak, clean ending, campaign fit. Prefer moments
   that show named content (map names, logos, title cards). Give each a `hook_text`
   based on the campaign's on-screen text direction that matches the frames. Write
   `scores.json`, then:
   ```bash
   uv run clipforge score <run> data/runs/<run>/scores.json
   uv run clipforge render <run> --max-clips 8
   ```
2. **Review.** Read `review.json`, open every `qa_sheet` (frames of the final 9:16
   render). Check: text readable and inside the frame, no black/blurry stretches, hook
   matches the picture. Write a caption per clip in your own words (never copy the
   campaign examples). Write `decisions.json`, then:
   ```bash
   uv run clipforge review <run> data/runs/<run>/decisions.json
   ```
   Clips below `publishing.min_agent_score` are rejected automatically; clips with
   compliance violations stay in `pending_review` and are listed under `blocked`.

## File formats

`scores.json`
```json
[{"id": 29, "score": 8.2, "decision": "render", "hook_text": "…", "reasoning": "…",
  "start": 2.1, "end": 16.7}]
```
`decision` is `render` or `skip`; `start`/`end` are optional and snapped to boundaries.

`decisions.json`
```json
[{"clip_id": 29, "decision": "approve", "score": 8.2, "platforms": ["instagram"],
  "reason": "…", "caption": "Your own words @mention\n#Ad\n#Tag1 #Tag2"}]
```
