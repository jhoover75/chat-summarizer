"""
prompt_builder.py — Builds Ollama prompts from message groups.

Splits large message sets into chunks that fit within the token budget,
then renders each chunk using the Jinja2 prompt_template from config.

See DESIGN.md §13.4 for chunking strategy details.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from jinja2 import Environment

if TYPE_CHECKING:
    from src.archive.models import Message, MessageGroup
    from src.config import OllamaConfig

# Conservative: assume 4 chars per token, stay well under context window
MAX_CHARS = 6000 * 4


def format_message(msg: "Message") -> str:
    """Format a single message as a readable line for the prompt."""
    try:
        ts = datetime.fromisoformat(msg.timestamp.replace("Z", "+00:00"))
        ts_str = ts.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        ts_str = msg.timestamp
    return f"[{ts_str}] @{msg.author_name}: {msg.body}\n"


def build_prompts(
    ollama_config: "OllamaConfig",
    channel_config,
    groups: list["MessageGroup"],
) -> list[str]:
    """
    Return one or more rendered prompts for this channel's messages.
    If total message text fits in one prompt, returns a list of length 1.
    """
    all_messages: list["Message"] = []
    for group in groups:
        all_messages.extend(group.messages)

    if not all_messages:
        return []

    # Determine summary date from the messages
    try:
        first_ts = datetime.fromisoformat(all_messages[0].timestamp.replace("Z", "+00:00"))
        summary_date = first_ts.strftime("%Y-%m-%d")
    except Exception:
        summary_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Split into chunks
    chunks: list[list["Message"]] = []
    current: list["Message"] = []
    current_len = 0
    for msg in all_messages:
        line = format_message(msg)
        if current_len + len(line) > MAX_CHARS and current:
            chunks.append(current)
            current = [msg]
            current_len = len(line)
        else:
            current.append(msg)
            current_len += len(line)
    if current:
        chunks.append(current)

    env = Environment()
    template = env.from_string(ollama_config.prompt_template)

    prompts = []
    for chunk in chunks:
        messages_text = "".join(format_message(m) for m in chunk)
        rendered = template.render(
            channel=channel_config.channel_name if hasattr(channel_config, "channel_name") else channel_config.name,
            platform=all_messages[0].platform,
            date=summary_date,
            message_count=len(chunk),
            messages=messages_text,
        )
        prompts.append(rendered)

    return prompts


def build_thread_prompt(
    ollama_config: "OllamaConfig",
    thread,
    messages: list["Message"],
) -> str:
    """
    Build a single summarization prompt for a complete tracked thread.
    If the message text exceeds MAX_CHARS, the prompt includes as many messages
    as fit and notes the total count for context.
    """
    if not messages:
        return ""

    try:
        first_ts = datetime.fromisoformat(messages[0].timestamp.replace("Z", "+00:00"))
        summary_date = first_ts.strftime("%Y-%m-%d")
    except Exception:
        summary_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    messages_text = "".join(format_message(m) for m in messages)
    # Trim to fit context window
    if len(messages_text) > MAX_CHARS:
        messages_text = messages_text[:MAX_CHARS] + "\n[... truncated for context window ...]\n"

    env = Environment()
    template = env.from_string(ollama_config.prompt_template)

    channel = getattr(thread, "channel", "unknown")
    platform = getattr(thread, "platform", messages[0].platform if messages else "unknown")
    subject = getattr(thread, "thread_subject", "") or ""

    # Add subject line to messages_text if available
    preamble = f"Thread subject: {subject}\n\n" if subject else ""

    return template.render(
        channel=channel,
        platform=platform,
        date=summary_date,
        message_count=len(messages),
        messages=preamble + messages_text,
    )
