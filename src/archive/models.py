"""
models.py — Python dataclasses for the archive layer.

Message: one chat message from any platform.
Summary: the result of an Ollama summarization run for one channel.
MessageGroup: a collection of messages belonging to the same thread.
TrackedThread: a thread the user cares about, persisted in Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class Message:
    message_id: str
    platform: str           # "rocketchat" or "teams"
    channel: str
    team: Optional[str]     # Teams only
    author_id: str
    author_name: str
    body: str
    timestamp: str          # ISO 8601 UTC string
    thread_id: Optional[str]
    raw_json: str


@dataclass
class MessageGroup:
    """A set of messages that belong to the same thread (or no thread)."""
    thread_id: Optional[str]
    messages: list[Message] = field(default_factory=list)


@dataclass
class Summary:
    run_id: str
    platform: str
    channel: str
    team: Optional[str]
    summary_date: str       # YYYY-MM-DD
    message_count: int
    summary_text: str       # Raw LLM output
    key_points: list[str]
    actions: list[str]
    message_ids: list[str]
    model: str
    generation_seconds: float
    success: bool
    error_message: Optional[str] = None
    output_path: Optional[str] = None


@dataclass
class TrackedThread:
    """
    A top-level thread the user is following.

    Persisted in the tracked_threads Postgres table.
    status: 'active' | 'aged_out'
    reason: 'started' | 'following' | 'mentioned' | 'replied'
    """
    platform: str                        # "rocketchat" or "teams"
    channel: str
    thread_id: str
    reason: str                          # 'started' | 'following' | 'mentioned' | 'replied'
    team: Optional[str] = None           # Teams only
    thread_subject: Optional[str] = None # First message body excerpt
    thread_url: Optional[str] = None     # Direct link to thread in the platform UI
    originated_at: Optional[datetime] = None  # Timestamp of the top-level message
    tracked_since: Optional[datetime] = None  # When we first began tracking it
    last_activity: Optional[datetime] = None  # Most recent message timestamp seen
    status: str = "active"               # 'active' | 'aged_out'
    needs_resummary: bool = True         # Set TRUE on new messages, FALSE after writing summary.md


def group_messages(messages: list[Message]) -> list[MessageGroup]:
    """
    Group messages by thread_id.
    Messages without a thread_id form a single "main channel" group (thread_id=None).
    """
    groups: dict[Optional[str], MessageGroup] = {}
    for msg in messages:
        key = msg.thread_id
        if key not in groups:
            groups[key] = MessageGroup(thread_id=key)
        groups[key].messages.append(msg)
    # Return main channel first, then threads sorted by earliest message timestamp
    ordered = sorted(groups.values(), key=lambda g: g.messages[0].timestamp)
    return ordered
