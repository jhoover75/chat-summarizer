"""
rocketchat.py — Rocket.Chat REST API source adapter.

Authentication: Personal Access Token (X-Auth-Token / X-User-Id headers).
API docs: https://developer.rocket.chat/apidocs/

Key endpoints used:
  GET /api/v1/channels.messages       — all messages in a public channel (incl. replies)
  GET /api/v1/channels.info           — resolve roomName → roomId
  GET /api/v1/chat.getThreadsList     — threads the user is following (type=following)
  GET /api/v1/chat.getThreadMessages  — all messages in a specific thread

See DESIGN.md §4, §5, §8 for configuration and auth details.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.archive.models import Message, TrackedThread
from src.sources.base import ChatSource
from src.utils.rate_limiter import TokenBucketRateLimiter

if TYPE_CHECKING:
    from src.config import RocketChatConfig, RocketChatChannelConfig, MeConfig


# Priority order — first match wins
_REASON_PRIORITY = ("started", "following", "mentioned", "replied")


class RocketChatSource(ChatSource):
    """Fetches messages from a Rocket.Chat instance via REST API."""

    PLATFORM = "rocketchat"

    def __init__(self, config: "RocketChatConfig", db=None):
        self._config = config
        self._db = db
        # RC_CA_BUNDLE points at a self-signed cert to trust (set only by
        # docker-compose.yml's local testing profile, which fronts Rocket.Chat
        # with an HTTPS proxy using a generated cert). Absent in production,
        # where the standard trusted CA chain is used as normal.
        ca_bundle = os.environ.get("RC_CA_BUNDLE")
        verify: bool | str = True
        if ca_bundle and Path(ca_bundle).exists():
            verify = ca_bundle
        self._client = httpx.Client(
            base_url=config.url,
            headers={
                "X-Auth-Token": config.auth_token,
                "X-User-Id": config.user_id,
                "Content-Type": "application/json",
            },
            timeout=30,
            verify=verify,
        )
        # 2 requests/second — well within Rocket.Chat's default 200 req/60s limit
        self._limiter = TokenBucketRateLimiter(rate=2.0, capacity=5.0)
        # Cache: roomName → roomId (populated lazily)
        self._room_id_cache: dict[str, str] = {}

    # ── channel helpers ───────────────────────────────────────────────────────

    def channels(self):
        return self._config.channels

    def _resolve_room_id(self, room_name: str) -> str:
        """Return the roomId for a channel name, using a local cache."""
        if room_name not in self._room_id_cache:
            self._limiter.acquire()
            resp = self._client.get(
                "/api/v1/channels.info",
                params={"roomName": room_name},
            )
            resp.raise_for_status()
            self._room_id_cache[room_name] = resp.json()["channel"]["_id"]
        return self._room_id_cache[room_name]

    # ── legacy fetch (kept for backward compatibility) ────────────────────────

    def fetch(
        self, channel_config: "RocketChatChannelConfig", since_ts: str | None
    ) -> list[Message]:
        return self.fetch_all(channel_config, since_ts)

    # ── in-memory fetch (no DB writes) ────────────────────────────────────────

    def fetch_all(
        self, channel_config: "RocketChatChannelConfig", since_ts: str | None
    ) -> list[Message]:
        """
        Fetch all channel messages (top-level posts + replies) since since_ts.
        Returns messages in ascending timestamp order. Does not write to DB.
        """
        if since_ts is None:
            lookback = channel_config.lookback_hours or 24
            since_dt = datetime.now(timezone.utc) - timedelta(hours=lookback)
        else:
            since_dt = datetime.fromisoformat(since_ts.replace("Z", "+00:00"))

        messages: list[Message] = []
        offset = 0

        # channels.messages in this Rocket.Chat version rejects the documented
        # `oldest` param outright, and its `query` param is a no-op for
        # filtering (verified: even a query matching nothing returns
        # everything) — so date filtering happens client-side here instead.
        # Results come back newest-first, so once a page has a message older
        # than since_dt, every later page is older still and we can stop.
        while True:
            self._limiter.acquire()
            data = self._get_messages(channel_config.name, offset)
            batch = data.get("messages", [])
            if not batch:
                break

            reached_cutoff = False
            for raw in batch:
                msg = self._parse_message(raw, channel_config.name)
                if msg is None:
                    continue
                msg_dt = _parse_ts(msg.timestamp)
                if msg_dt is not None and msg_dt < since_dt:
                    reached_cutoff = True
                    continue
                messages.append(msg)

            offset += len(batch)
            if (
                reached_cutoff
                or offset >= self._config.max_messages_per_run
                or len(batch) < self._config.page_size
            ):
                break

        return sorted(messages, key=lambda m: m.timestamp)

    # ── platform-native thread following ─────────────────────────────────────

    def fetch_followed_threads(
        self, channel_config: "RocketChatChannelConfig"
    ) -> set[str]:
        """
        Return thread IDs the user has explicitly followed via Rocket.Chat UI.
        Uses GET /api/v1/chat.getThreadsList?type=following&rid=<roomId>.
        """
        rid = self._resolve_room_id(channel_config.name)
        thread_ids: set[str] = set()
        offset = 0

        while True:
            self._limiter.acquire()
            resp = self._client.get(
                "/api/v1/chat.getThreadsList",
                params={
                    "rid": rid,
                    "type": "following",
                    "count": self._config.page_size,
                    "offset": offset,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            threads = data.get("threads", [])
            if not threads:
                break
            for t in threads:
                thread_ids.add(t["_id"])
            offset += len(threads)
            if len(threads) < self._config.page_size:
                break

        return thread_ids

    # ── per-thread backfill ───────────────────────────────────────────────────

    def fetch_thread_messages(
        self, channel_config: "RocketChatChannelConfig", thread_id: str
    ) -> list[Message]:
        """
        Fetch the complete message history for a specific thread.
        Uses GET /api/v1/chat.getThreadMessages?tmid=<thread_id>.
        Returns messages in ascending timestamp order.
        """
        messages: list[Message] = []
        offset = 0

        while True:
            self._limiter.acquire()
            resp = self._client.get(
                "/api/v1/chat.getThreadMessages",
                params={
                    "tmid": thread_id,
                    "count": self._config.page_size,
                    "offset": offset,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            batch = data.get("messages", [])
            if not batch:
                break
            for raw in batch:
                msg = self._parse_message(raw, channel_config.name)
                if msg:
                    messages.append(msg)
            offset += len(batch)
            if len(batch) < self._config.page_size:
                break

        return sorted(messages, key=lambda m: m.timestamp)

    # ── thread eligibility detection ──────────────────────────────────────────

    def discover_eligible_threads(
        self,
        channel_config: "RocketChatChannelConfig",
        messages: list[Message],
        me_config: "MeConfig",
        followed_thread_ids: set[str],
    ) -> list[TrackedThread]:
        """
        Scan in-memory channel messages and return TrackedThread objects for any
        thread the user cares about. Each thread gets the highest-priority reason
        that applies: started > following > mentioned > replied.
        """
        me_user_id = me_config.rocketchat.user_id
        me_username = me_config.rocketchat.username
        # Compiled pattern for @username mentions (word-boundary safe)
        mention_re = re.compile(
            rf"(?:^|[\s,@])@?{re.escape(me_username)}(?:\b|$)", re.IGNORECASE
        )

        # thread_id → best reason so far
        reasons: dict[str, str] = {}
        # thread_id → top-level message (for metadata)
        top_level: dict[str, Message] = {}
        # thread_id → most recent message timestamp
        last_seen: dict[str, str] = {}

        for msg in messages:
            # Determine the thread_id for this message:
            # - If msg has a thread_id (tmid), it's a reply → parent is the thread root
            # - If msg has no thread_id, it IS a potential thread root if it has replies
            #   (We can't know that without the tcount field; we'll collect top-level msgs
            #    that we authored or that appear as thread_ids of replies)
            effective_thread_id = msg.thread_id or msg.message_id

            # Track the most recent activity per thread
            current_last = last_seen.get(effective_thread_id, "")
            if msg.timestamp > current_last:
                last_seen[effective_thread_id] = msg.timestamp

            # If this is a top-level post (no tmid), record it as the thread root
            if msg.thread_id is None:
                if effective_thread_id not in top_level:
                    top_level[effective_thread_id] = msg

            # 'started' — user authored the top-level post
            if msg.thread_id is None and msg.author_id == me_user_id:
                _set_reason(reasons, effective_thread_id, "started")

            # 'following' — platform-native signal
            if effective_thread_id in followed_thread_ids:
                _set_reason(reasons, effective_thread_id, "following")

            # 'mentioned' — @username or user ID appears anywhere in the message
            if me_username and mention_re.search(msg.body):
                _set_reason(reasons, effective_thread_id, "mentioned")
            elif me_user_id and me_user_id in msg.body:
                _set_reason(reasons, effective_thread_id, "mentioned")

            # 'replied' — user posted a reply (but didn't start the thread)
            if msg.thread_id is not None and msg.author_id == me_user_id:
                _set_reason(reasons, msg.thread_id, "replied")
                # Ensure the parent thread_id is represented in top_level tracking
                if msg.thread_id not in top_level:
                    top_level[msg.thread_id] = msg  # placeholder; backfill will replace

        # Build TrackedThread list
        tracked: list[TrackedThread] = []
        for thread_id, reason in reasons.items():
            root = top_level.get(thread_id)
            subject = (root.body[:120] + "…") if root and len(root.body) > 120 else (root.body if root else None)
            originated_dt = (
                _parse_ts(root.timestamp) if root and root.thread_id is None else None
            )
            last_activity_dt = _parse_ts(last_seen.get(thread_id, ""))
            tracked.append(TrackedThread(
                platform=self.PLATFORM,
                channel=channel_config.name,
                team=None,
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
        return f"{self._config.url}/channel/{message.channel}?msg={message.message_id}"

    def build_thread_url(
        self, channel_config: "RocketChatChannelConfig", thread_id: str
    ) -> str:
        return f"{self._config.url}/channel/{channel_config.name}?tmid={thread_id}"

    # ── internal helpers ──────────────────────────────────────────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=30))
    def _get_messages(self, room_name: str, offset: int) -> dict:
        room_id = self._resolve_room_id(room_name)
        resp = self._client.get(
            "/api/v1/channels.messages",
            params={
                "roomId": room_id,
                "count": self._config.page_size,
                "offset": offset,
            },
        )
        resp.raise_for_status()
        return resp.json()

    def _parse_message(self, raw: dict, channel: str) -> Message | None:
        body = raw.get("msg", "").strip()
        if not body:
            return None
        ts_field = raw.get("ts", "")
        # RC timestamps come as {"$date": <epoch_ms>} or ISO string
        if isinstance(ts_field, dict):
            epoch_ms = ts_field.get("$date", 0)
            timestamp = datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3] + "Z"
        else:
            timestamp = str(ts_field)

        return Message(
            message_id=raw["_id"],
            platform=self.PLATFORM,
            channel=channel,
            team=None,
            author_id=raw.get("u", {}).get("_id", ""),
            author_name=raw.get("u", {}).get("name") or raw.get("u", {}).get("username", "unknown"),
            body=body,
            timestamp=timestamp,
            thread_id=raw.get("tmid"),
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
