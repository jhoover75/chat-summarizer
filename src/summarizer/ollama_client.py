"""
ollama_client.py — Wraps the Ollama REST API for summarization.

Sends one or more prompts (chunks) to Ollama and parses the structured
response into a Summary dataclass. If multiple chunks are sent, a final
"merge" prompt combines them.

See DESIGN.md §5 Step 7 and §13.4 for details.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

from src.archive.models import Summary

if TYPE_CHECKING:
    from src.config import OllamaConfig

MERGE_PROMPT = """You are given multiple partial summaries of the same chat channel.
Merge them into a single coherent summary using exactly this structure:

## Summary
<paragraph>

## Key Points
- <point>

## Actions & Decisions
- [ACTION/DECISION] <item>

Partial summaries:
{parts}
"""


class SummarizationError(Exception):
    pass


class OllamaClient:

    def __init__(self, config: "OllamaConfig"):
        self._config = config
        self._client = httpx.Client(
            base_url=config.url,
            timeout=config.timeout_seconds,
        )

    def summarize(self, prompts: list[str], channel_config, run_id: str) -> Summary:
        if not prompts:
            raise SummarizationError("No prompts to summarize")

        start = time.monotonic()
        platform = channel_config.platform if hasattr(channel_config, "platform") else "unknown"
        channel = channel_config.channel_name if hasattr(channel_config, "channel_name") else channel_config.name
        team = getattr(channel_config, "team_name", None)
        summary_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        try:
            if len(prompts) == 1:
                raw_response = self._generate(prompts[0])
            else:
                # Summarize each chunk, then merge
                partial_summaries = [self._generate(p) for p in prompts]
                merge_prompt = MERGE_PROMPT.format(
                    parts="\n\n---\n\n".join(partial_summaries)
                )
                raw_response = self._generate(merge_prompt)

            elapsed = time.monotonic() - start
            key_points, actions = self._parse_response(raw_response)

            return Summary(
                run_id=run_id,
                platform=platform,
                channel=channel,
                team=team,
                summary_date=summary_date,
                message_count=0,  # filled in by main.py
                summary_text=raw_response,
                key_points=key_points,
                actions=actions,
                message_ids=[],  # filled in by main.py
                model=self._config.model,
                generation_seconds=elapsed,
                success=True,
            )

        except Exception as exc:
            elapsed = time.monotonic() - start
            return Summary(
                run_id=run_id,
                platform=platform,
                channel=channel,
                team=team,
                summary_date=summary_date,
                message_count=0,
                summary_text="",
                key_points=[],
                actions=[],
                message_ids=[],
                model=self._config.model,
                generation_seconds=elapsed,
                success=False,
                error_message=str(exc),
            )

    def _generate(self, prompt: str) -> str:
        resp = self._client.post(
            "/api/generate",
            json={
                "model": self._config.model,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": self._config.options.temperature,
                    "num_predict": self._config.options.num_predict,
                    "top_p": self._config.options.top_p,
                },
            },
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()

    def _parse_response(self, text: str) -> tuple[list[str], list[str]]:
        """Extract key points and actions from the structured LLM response."""
        key_points: list[str] = []
        actions: list[str] = []

        in_key_points = False
        in_actions = False

        for line in text.splitlines():
            stripped = line.strip()
            if stripped == "## Key Points":
                in_key_points = True
                in_actions = False
            elif stripped == "## Actions & Decisions":
                in_actions = True
                in_key_points = False
            elif stripped.startswith("## "):
                in_key_points = False
                in_actions = False
            elif stripped.startswith("- "):
                content = stripped[2:]
                if in_key_points:
                    key_points.append(content)
                elif in_actions:
                    actions.append(content)

        return key_points, actions
