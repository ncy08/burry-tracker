"""Read new Burry Substack posts from Gmail.

Forward-only polling: returns posts strictly newer than `since_message_id`.
Decodes HTML preferentially via html2text, falling back to text/plain. The
Substack post URL is extracted from the body so downstream callers can
record post provenance independent of the Gmail Message-Id.
"""

from __future__ import annotations

import base64
import email.utils
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import html2text

from substack_trader.models import BurryPost

if TYPE_CHECKING:
    from googleapiclient.discovery import Resource

logger = logging.getLogger(__name__)

# Substack now sends most email links as open.substack.com/pub/<author>/p/<slug>
# rather than the legacy <author>.substack.com/p/<slug>. Match either form so
# the live pipeline picks up modern emails. Production bug fixed 2026-05-06:
# previously only the legacy form matched, which meant every recent Burry email
# returned an empty post_url.
POST_URL_RE = re.compile(
    r"https?://(?:michaeljburry\.substack\.com/p/|open\.substack\.com/pub/michaeljburry/p/)"
    r"[a-z0-9-]+"
)


def _decode_part(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    return base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", errors="replace")


def _walk_parts(payload: dict, mime_type: str) -> str:
    if payload.get("mimeType") == mime_type:
        return _decode_part(payload)
    for part in payload.get("parts", []) or []:
        found = _walk_parts(part, mime_type)
        if found:
            return found
    return ""


def _extract_body_text(payload: dict) -> str:
    html_body = _walk_parts(payload, "text/html")
    if html_body:
        h = html2text.HTML2Text()
        h.ignore_images = True
        h.ignore_emphasis = False
        h.body_width = 0
        return h.handle(html_body).strip()
    return _walk_parts(payload, "text/plain").strip()


def _header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _parse_date(raw: str) -> datetime:
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return datetime.now(tz=timezone.utc)


def fetch_new_posts(
    gmail_service: Resource,
    since_message_id: str | None,
    label: str,
    sender: str,
) -> list[BurryPost]:
    """Fetch Burry posts from Gmail newer than `since_message_id`.

    Build a Gmail search by sender and label (no date filter; dedup is
    by Message-Id). Iterate reverse-chrono and stop at `since_message_id`.
    First-run behavior: if `since_message_id is None`, return [] and let
    the user run `backfill` to seed history; live `run` is forward-only.
    """
    if since_message_id is None:
        logger.info("First-run: no since_message_id, returning empty list (run backfill to seed)")
        return []

    query = f"from:{sender} label:{label}"
    resp = gmail_service.users().messages().list(userId="me", q=query, maxResults=50).execute()
    messages = resp.get("messages", [])

    posts: list[BurryPost] = []
    for meta in messages:
        msg_id = meta["id"]
        if msg_id == since_message_id:
            break
        full = (
            gmail_service.users()
            .messages()
            .get(userId="me", id=msg_id, format="full")
            .execute()
        )
        payload = full.get("payload", {})
        headers = payload.get("headers", [])
        body = _extract_body_text(payload)
        url_match = POST_URL_RE.search(body)
        post_url = url_match.group(0) if url_match else ""
        posts.append(
            BurryPost(
                gmail_message_id=msg_id,
                post_url=post_url,
                title=_header(headers, "Subject"),
                pub_date=_parse_date(_header(headers, "Date")),
                body_text=body,
                legs=[],
            )
        )
    return posts
