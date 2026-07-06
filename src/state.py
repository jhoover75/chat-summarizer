"""
state.py — Read/write state.json.

state.json tracks the last-fetched message timestamp per channel:
  { "rocketchat::general": "2026-07-04T18:00:00Z", ... }

On first run the file does not exist; an empty dict is returned.
Writes are atomic (write to .tmp then os.replace).
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def load_state(path: str) -> dict[str, str]:
    """Return the state dict, or {} if the file does not exist."""
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text())


def save_state(path: str, state: dict[str, str]) -> None:
    """Atomically write the state dict to disk."""
    p = Path(path)
    tmp = str(p) + ".tmp"
    Path(tmp).write_text(json.dumps(state, indent=2))
    os.replace(tmp, path)
