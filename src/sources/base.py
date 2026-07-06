"""
base.py — Abstract base class for chat sources.

All source adapters (Rocket.Chat, Teams, future platforms) must implement
ChatSource. This ensures main.py can iterate sources uniformly.

Thread-tracking pipeline (used by main.py):
  1. fetch_followed_threads() — platform-native "following" thread IDs
  2. fetch_all()              — all channel messages into memory (no DB write)
  3. discover_eligible_threads() — scan in-memory messages for eligibility signals
  4. fetch_thread_messages()  — backfill a specific thread's full history on first discovery
  5. build_thread_url()       — canonical link to a thread in the platform UI
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.archive.models import Message, TrackedThread
    from src.config import MeConfig


class ChatSource(ABC):

    @abstractmethod
    def channels(self):
        """Return an iterable of channel config objects for this source."""
        ...

    @abstractmethod
    def fetch(self, channel_config, since_ts: str | None) -> list["Message"]:
        """
        Legacy fetch: retrieve messages newer than since_ts for the given channel.
        If since_ts is None, fall back to the config lookback_hours.
        Returns messages sorted ascending by timestamp.
        """
        ...

    @abstractmethod
    def fetch_all(
        self, channel_config, since_ts: str | None
    ) -> list["Message"]:
        """
        Fetch all channel messages since since_ts into memory.
        In Rocket.Chat this includes both top-level posts and replies.
        In Teams this returns only top-level posts (replies require separate calls).
        Does NOT write anything to the database.
        """
        ...

    @abstractmethod
    def fetch_followed_threads(self, channel_config) -> set[str]:
        """
        Return the set of thread IDs the user has platform-natively followed.
        Returns an empty set for platforms that don't expose a native follow API.
        """
        ...

    @abstractmethod
    def fetch_thread_messages(
        self, channel_config, thread_id: str
    ) -> list["Message"]:
        """
        Fetch the complete message history for a specific thread.
        Used for initial backfill when a thread is first discovered.
        Returns messages sorted ascending by timestamp.
        """
        ...

    @abstractmethod
    def discover_eligible_threads(
        self,
        channel_config,
        messages: list["Message"],
        me_config: "MeConfig",
        followed_thread_ids: set[str],
    ) -> list["TrackedThread"]:
        """
        From a batch of in-memory channel messages, identify threads the user
        cares about. Returns TrackedThread objects (eligibility detected, not yet
        persisted to DB). The caller is responsible for DB upserts.

        Eligibility reasons (mutually exclusive priority order):
          'started'   — user authored the top-level message
          'following' — in followed_thread_ids (platform-native signal)
          'mentioned' — user's name/handle appears in a message in the thread
          'replied'   — user has posted a reply in the thread
        """
        ...

    @abstractmethod
    def build_deep_link(self, message: "Message") -> str:
        """Return a URL that opens the message in the platform's UI."""
        ...

    @abstractmethod
    def build_thread_url(self, channel_config, thread_id: str) -> str:
        """Return a direct URL to the top-level thread in the platform's UI."""
        ...
