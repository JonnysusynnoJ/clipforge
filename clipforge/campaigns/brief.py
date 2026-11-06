"""Fetch campaign briefs from public links and prepare them for agent rule parsing."""

from __future__ import annotations

import html
import json
import re
import urllib.request

from pydantic import ValidationError

from clipforge.campaigns.rules import ParsedRules

USER_AGENT = "clipforge/0.1 (campaign brief reader)"

SOURCE_HOST_HINTS = (
    "mediasilo.com", "drive.google.com", "dropbox.com", "frame.io", "wetransfer.com",
    "box.com", "youtube.com", "youtu.be", "vimeo.com", "docs.google.com/spreadsheets",
)  # fmt: skip


def _google_doc_id(url: str) -> str | None:
    m = re.search(r"docs\.google\.com/document/d/([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def html_to_text(page: str) -> tuple[str, list[str]]:
    page = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", page, flags=re.S)
    links = [html.unescape(x) for x in re.findall(r'href="([^"]+)"', page)]
    # unwrap google redirect links
    links = [
        re.sub(r"^https://www\.google\.com/url\?q=([^&]+).*$", r"\1", x)
        for x in links
        if x.startswith("http")
    ]
    text = re.sub(r"<br\s*/?>|</p>|</li>|</h\d>|</tr>|</div>", "\n", page)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text, links


def fetch_brief(url: str, timeout: float = 60) -> tuple[str, list[str]]:
    """Return (plain text, links) of a public brief. Google Docs use the public mobile view."""
    doc_id = _google_doc_id(url)
    target = f"https://docs.google.com/document/d/{doc_id}/mobilebasic" if doc_id else url
    req = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        page = resp.read().decode("utf-8", errors="replace")
    return html_to_text(page)


def source_link_candidates(text: str, links: list[str]) -> list[str]:
    found = set(re.findall(r"https?://[^\s<>\"')]+", text)) | set(links)
    return sorted(u.rstrip(".,") for u in found if any(h in u for h in SOURCE_HOST_HINTS))


def rules_request(*, campaign_id: int, title: str, platforms: list[str], text: str,
                  links: list[str]) -> dict[str, object]:  # fmt: skip
    """The JSON handed to the agent to produce a ParsedRules document."""
    return {
        "campaign_id": campaign_id,
        "title": title,
        "campaign_platforms": platforms,
        "instructions": (
            "Extract the campaign rules into rules.json following `schema` exactly. "
            "allowed_sources: only links the brief names as official material. "
            "Copy required mentions/hashtags/disclosures exactly as written. Use null when "
            "the brief says nothing. Do not invent rules. Then run "
            "`clipforge campaign-rules <id> rules.json`; on validation errors fix and retry."
        ),
        "source_link_candidates": source_link_candidates(text, links),
        "schema": ParsedRules.model_json_schema(),
        "brief_text": text,
    }


def validate_rules(raw: str) -> tuple[ParsedRules | None, str | None]:
    """Parse and validate the agent's rules JSON; return a readable error for retry."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON: {exc}"
    try:
        return ParsedRules.model_validate(data), None
    except ValidationError as exc:
        return None, "\n".join(
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors()
        )
