"""
yaml_writer.py — Writes a Summary to a YAML file on disk.

Output path: <output_dir>/<platform>/<channel>/YYYY-MM-DD.yaml
Writes atomically via a .tmp file + os.replace().
"""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from src.archive.models import Summary
    from src.config import OutputConfig


class YamlWriter:

    def __init__(self, config: "OutputConfig"):
        self._config = config

    def write(self, summary: "Summary", channel_config) -> Path:
        platform = summary.platform
        channel = summary.channel
        team = summary.team

        if team:
            out_dir = Path(self._config.directory) / platform / team / channel
        else:
            out_dir = Path(self._config.directory) / platform / channel

        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{summary.summary_date}.yaml"

        if out_path.exists() and not self._config.overwrite_existing:
            return out_path

        data = asdict(summary)
        content = yaml.dump(data, default_flow_style=False, allow_unicode=True)

        tmp = str(out_path) + ".tmp"
        Path(tmp).write_text(content, encoding="utf-8")
        os.replace(tmp, out_path)

        return out_path
