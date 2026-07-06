"""
teams.py — Microsoft Teams source adapter via Microsoft Graph API.

Authentication: OAuth 2.0 Client Credentials (application permissions).
Required Graph permissions: ChannelMessage.Read.All, Channel.ReadBasic.All, Team.ReadBasic.All

Thread eligibility for Teams (no native "following" endpoint):
  'started'   — user authored the top-level post (from.user.id == me.teams.user_id)
  'mentioned' — me.teams.user_id appears in the message's `mentions` array or body text
  'replied'   — user has posted a reply (detected by scanning replies for top-level posts
                authored by others; expensive so limited to threads matching other signals)

Note: Teams does not expose a "following" API, so that reason is never emitted here.

See DESIGN.md §7, §13.9 for Azure AD app registration and Teams limitations.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

import httpx
import msal
from tenacity import retry, stop_after_attempt, wait_exponential

from src.archive.models import Message, TrackedThread
from src.sources.base import ChatSource

if TYPE_CHECKING:
    from src.config import TeamsConfig, TeamsChannelConfig, MeConfig

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Priority order — first match wins (no 'following' for Teams)
_REASON_PRIORITY = ("started", "mentioned", "replied")


class AuthError(Exception):
    pass


class TeamsSource(ChatSource):
    """Fetches messages from Microsoft Teams via Graph API."""

    PLATFORM = "teams"

    def __init__(self, config: "TeamsConfig", db=None):
        self._config = config
        self._db = db
        self._msal_app = msal.ConfidentialClientApplication(
            config.client_id,
            authority=f"https://login.microsoftonline.com/{config.tenant_id}",
            client_credential=config.client_secret,
        )
        self._scope = ["https://graph.microsoft.com/.default"]
        self._client = httpx.Client(timeout=30)

    # ── channel helpers ───────────────────────────────────────────────────────

    def channels(self):
        return self._config.teams_channels

    def _get_token(self) -> str:
        result = self._msal_app.acquire_token_for_client(scopes=self._scope)
        if "access_token" not in result:
            raise AuthError(
                f"MSAL token acquisition failed: {result.get('error_description')}"
            )
        return result["access_token"]

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    # ── legacy fetch (kept for backward compatibility) ────────────────────────

    def fetch(
        self, channel_config: "TeamsChannelConfig", since_ts: str | None
    ) -> list[Message]:
        return self.fetch_all(channel_config, since_ts)

    # ── in-memory fetch (no DB writes) ────────────────────────────────────────

    def fetch_all(
        self, channel_config: "TeamsChannelConfig", since_ts: str | None
    ) -> list[Message]:
        """
        Fetch top-level channel posts since since_ts into memory.
        Teams' channel messages API returns only top-level posts; replies are
        fetched separately via fetch_thread_messages().
        """
        if since_ts is None:
            since_dt = datetime.now(timezone.utc) - timedelta(hours=24)
        else:
            since_dt = datetime.fromisoformat(since_ts.replace("Z", "+00:00"))

        since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (
            f"{GRAPH_BASE}/teams/{channel_config.team_id}"
            f"/channels/{channel_config.channel_id}/messages"
            f"?$filter=lastModifiedDateTime ge {since_str}"
            f"&$top={self._config.page_size}"
        )

        messages: list[Message] = []
        fetched = 0

        while url and fetched < self._config.max_messages_per_run:
            data = self._graph_get(url)
            for raw in data.get("value", []):
                msg = self._parse_message(raw, channel_config)
                if msg:
                    messages.append(msg)
                    fetched += 1
            url = data.get("@odata.nextLink")

        return sorted(messages, key=lambda m: m.timestamp)

    # ── platform-native thread following ─────────────────────────────────────

    def fetch_followed_threads(
        self, channel_config: "TeamsChannelConfig"
    ) -> set[str]:
        """
        Teams has no public API to list threads a user is following.
        Always returns an empty set; eligibility is approximated by other signals.
        See DESIGN.md §13.9.
        """
        return set()

    # ── per-thread backfill ───────────────────────────────────────────────────

    def fetch_thread_messages(
        self, channel_config: "TeamsChannelConfig", thread_id: str
    ) -> list[Message]:
        """
        Fetch all replies for a thread (top-level post + its replies).
        Uses GET /teams/{tid}/channels/{cid}/messages/{thread_id}/replies.
        The top-level post itself is also fetched and prepended.
        """
        messages: list[Message] = []

        # 1. Fetch the top-level post
        try:
            root_url = (
                f"{GRAPH_BASE}/teams/{channel_config.team_id}"
                f"/channels/{channel_config.channel_id}/messages/{thread_id}"
            )
            root_data = self._graph_get(root_url)
            root_msg = self._parse_message(root_data, channel_config)
            if root_msg:
                messages.append(root_msg)
        except httpx.HTTPStatusError:
            pass  # Thread may have been deleted; proceed with replies only

        # 2. Fetch replies (paginated)
        url = (
            f"{GRAPH_BASE}/teams/{channel_config.team_id}"
            f"/channels/{channel_config.channel_id}/messages/{thread_id}/replies"
            f"?$top={self._config.page_size}"
        )
        while url:
            data = self._graph_get(url)
            for raw in data.get("value", []):
                msg = self._parse_message(raw, channel_config)
                if msg:
                    messages.append(msg)
            url = data.get("@odata.nextLink")

        return sorted(messages, key=lambda m: m.timestamp)

    # ── thread eligibility detection ──────────────────────────────────────────

    def discover_eligible_threads(
        self,
        channel_config: "TeamsChannelConfig",
        messages: list[Message],
        me_config: "MeConfig",
        followed_thread_ids: set[str],  # always empty for Teams; accepted for interface compat
    ) -> list[TrackedThread]:
        """
        Scan top-level channel posts for eligibility signals.

        Because fetch_all() returns only top-level posts for Teams, we can detect:
          - 'started': user authored the top-level post
          - 'mentioned': user appears in the message's mentions array or body text

        'replied' detection requires fetching replies — too expensive to do for every
        top-level post. Instead, we flag threads where the user is mentioned and
        additionally scan reply fetches from the backfill phase for 'replied' signals.
        """
        me_user_id = me_config.teams.user_id
        me_display_name = me_config.teams.display_name
        mention_re = _build_mention_re(me_user_id, me_display_name)

        reasons: dict[str, str] = {}
        top_level: dict[str, Message] = {}
        last_seen: dict[str, str] = {}

        for msg in messages:
            # All messages from fetch_all() are top-level posts; their message_id IS the thread_id
            thread_id = msg.message_id

            top_level[thread_id] = msg
            if msg.timestamp > last_seen.get(thread_id, ""):
                last_seen[thread_id] = msg.timestamp

            # 'started' — user authored this top-level post
            if msg.author_id == me_user_id:
                _set_reason(reasons, thread_id, "started")

            # 'mentioned' — check mentions array embedded in raw_json, then body text
            try:
                raw = json.loads(msg.raw_json) if isinstance(msg.raw_json, str) else {}
            except (ValueError, TypeError):
                raw = {}

            if _mentioned_in_raw(raw, me_user_id) or (
                mention_re and mention_re.search(msg.body)
            ):
                _set_reason(reasons, thread_id, "mentioned")

        # Build TrackedThread list
        tracked: list[TrackedThread] = []
        for thread_id, reason in reasons.items():
            root = top_level.get(thread_id)
            subject = (
                (root.body[:120] + "…") if root and len(root.body) > 120 else (root.body if root else None)
            )
            originated_dt = _parse_ts(root.timestamp) if root else None
            last_activity_dt = _parse_ts(last_seen.get(thread_id, ""))
            tracked.append(TrackedThread(
                platform=self.PLATFORM,
                channel=channel_config.channel_name,
                team=channel_config.team_name,
                thread_id=thread_id,
                thread_subject=subject,
                thread_url=self.build_thread_url(channel_config, thread_id),
                originated_at=originated_dt,
                last_activity=last_activity_dt,
                reason=reason,
                status="active",
                needs_resummary=True,
            ))

        return tracked

    # ── URL builders ──────────────────────────────────────────────────────────

    def build_deep_link(self, message: Message) -> str:
        return (
            f"https://teams.microsoft.com/l/message"
            f"/{message.channel}/{message.message_id}"
            f"?tenantId={self._config.tenant_id}"
        )

    def build_thread_url(
        self, channel_config: "TeamsChannelConfig", thread_id: str
    ) -> str:
        # Standard Teams deep link to a channel thread
        return (
            f"https://teams.microsoft.com/l/message"
            f"/{channel_config.channel_id}/{thread_id}"
            f"?tenantId={self._config.tenant_id}"
            f"&groupId={channel_config.team_id}"
            f"&channelName={channel_config.channel_name}"
        )

    # ── internal helpers ──────────────────────────────────────────────────────

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=2, max=60),
    )
    def _graph_get(self, url: str) -> dict:
        resp = self._client.get(url, headers=self._auth_headers())
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", "10"))
            time.sleep(retry_after)
            resp.raise_for_status()
        resp.raise_for_status()
        return resp.json()

    def _parse_message(
        self, raw: dict, channel_config: "TeamsChannelConfig"
    ) -> Message | None:
        # Skip system messages and deleted messages
        if raw.get("messageType") != "message":
            return None
        body_content = raw.get("body", {}).get("content", "").strip()
        if not body_content or body_content == "<deleted>":
            return None

        sender = raw.get("from", {}) or {}
        user = sender.get("user", {}) or {}

        # For replies, replyToId points to the top-level post — that is our thread_id
        reply_to = raw.get("replyToId")

        return Message(
            message_id=raw["id"],
            platform=self.PLATFORM,
            channel=channel_config.channel_name,
            team=channel_config.team_name,
            author_id=user.get("id", ""),
            author_name=user.get("displayName", "unknown"),
            body=body_content,
            timestamp=raw.get("createdDateTime", ""),
            thread_id=reply_to,  # None for top-level posts
            raw_json=json.dumps(raw, default=str),
        )


# ── module-level helpers ──────────────────────────────────────────────────────

def _reason_rank(reason: str) -> int:
    return _REASON_PRIORITY.index(reason) if reason in _REASON_PRIORITY else 99


def _set_reason(reasons: dict[str, str], thread_id: str, candidate: str) -> None:
    """Keep the highest-priority (lowest rank) reason for a thread."""
    existing = reasons.get(thread_id)
    if existing is None or _reason_rank(candidate) < _reason_rank(existing):
        reasons[thread_id] = candidate


def _parse_ts(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _mentioned_in_raw(raw: dict, me_user_id: str) -> bool:
    """Check the Graph API `mentions` array for the user's object ID."""
    if not me_user_id:
        return False
    for mention in raw.get("mentions", []):
        mentioned_user = (mention.get("mentioned", {}) or {}).get("user", {}) or {}
        if mentioned_user.get("id") == me_user_id:
            return True
    return False


def _build_mention_re(user_id: str, display_name: str) -> re.Pattern | None:
    """Build a regex to find the user by display name or ID in message body text."""
    parts = []
    if display_name:
        parts.append(re.escape(display_name))
    if user_id:
        parts.append(re.escape(user_id))
    if not parts:
        return None
    return re.compile("|".join(parts), re.IGNORECASE)
