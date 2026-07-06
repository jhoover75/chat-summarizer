"""
config.py — Load and validate config.yaml.

Expands ${ENV_VAR} references after loading .env via python-dotenv.
Returns a typed Config dataclass; raises ConfigError on missing required fields.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml
from dotenv import load_dotenv


class ConfigError(Exception):
    pass


# ── Sub-configs ──────────────────────────────────────────────────────────────

@dataclass
class RocketChatChannelConfig:
    name: str
    lookback_hours: Optional[int] = None

    @property
    def state_key(self) -> str:
        return f"rocketchat::{self.name}"


@dataclass
class RocketChatConfig:
    enabled: bool
    url: str
    user_id: str
    auth_token: str
    channels: list[RocketChatChannelConfig]
    max_messages_per_run: int = 500
    page_size: int = 100


@dataclass
class TeamsChannelConfig:
    channel_name: str
    channel_id: str
    team_name: str
    team_id: str

    @property
    def state_key(self) -> str:
        return f"teams::{self.team_name}::{self.channel_name}"


@dataclass
class TeamsConfig:
    enabled: bool
    tenant_id: str
    client_id: str
    client_secret: str
    teams_channels: list[TeamsChannelConfig]
    max_messages_per_run: int = 200
    page_size: int = 50


@dataclass
class OllamaOptions:
    temperature: float = 0.3
    num_predict: int = 1024
    top_p: float = 0.9


@dataclass
class OllamaConfig:
    url: str
    model: str
    prompt_template: str
    options: OllamaOptions = field(default_factory=OllamaOptions)
    timeout_seconds: int = 120


@dataclass
class OutputConfig:
    directory: str
    format: str
    lookback_hours: int = 24
    overwrite_existing: bool = True


@dataclass
class ArchiveConfig:
    enabled: bool
    host: str
    port: int
    database: str
    user: str
    password: str
    retention_days: int = 90


@dataclass
class StateConfig:
    state_file: str


@dataclass
class MeRocketChatConfig:
    user_id: str
    username: str


@dataclass
class MeTeamsConfig:
    user_id: str
    display_name: str


@dataclass
class MeConfig:
    rocketchat: MeRocketChatConfig
    teams: MeTeamsConfig


@dataclass
class TrackingConfig:
    thread_age_out_days: int = 90
    first_run_lookback_days: int = 90


@dataclass
class LoggingConfig:
    level: str = "info"
    log_file: Optional[str] = None


@dataclass
class Config:
    rocketchat: RocketChatConfig
    teams: TeamsConfig
    ollama: OllamaConfig
    output: OutputConfig
    archive: ArchiveConfig
    state: StateConfig
    me: MeConfig
    tracking: TrackingConfig
    logging: LoggingConfig


# ── Loader ───────────────────────────────────────────────────────────────────

def _expand(value: str) -> str:
    """Expand ${ENV_VAR} references in a string."""
    return os.path.expandvars(value)


def load_config(path: str) -> Config:
    load_dotenv()

    raw = Path(path).read_text()
    raw = os.path.expandvars(raw)
    data = yaml.safe_load(raw)

    # Rocket.Chat
    rc = data.get("rocketchat", {})
    rc_channels = [
        RocketChatChannelConfig(name=c["name"], lookback_hours=c.get("lookback_hours"))
        for c in rc.get("channels", [])
    ]
    rocketchat = RocketChatConfig(
        enabled=rc.get("enabled", False),
        url=rc.get("url", ""),
        user_id=rc.get("user_id", ""),
        auth_token=rc.get("auth_token", ""),
        channels=rc_channels,
        max_messages_per_run=rc.get("max_messages_per_run", 500),
        page_size=rc.get("page_size", 100),
    )

    # Teams
    t = data.get("teams", {})
    teams_channels = []
    for team in t.get("teams_channels", []):
        for ch in team.get("channels", []):
            teams_channels.append(TeamsChannelConfig(
                channel_name=ch["channel_name"],
                channel_id=ch["channel_id"],
                team_name=team["team_name"],
                team_id=team["team_id"],
            ))
    teams = TeamsConfig(
        enabled=t.get("enabled", False),
        tenant_id=t.get("tenant_id", ""),
        client_id=t.get("client_id", ""),
        client_secret=t.get("client_secret", ""),
        teams_channels=teams_channels,
        max_messages_per_run=t.get("max_messages_per_run", 200),
        page_size=t.get("page_size", 50),
    )

    # Ollama
    o = data.get("ollama", {})
    opts = o.get("options", {})
    ollama = OllamaConfig(
        url=o.get("url", "http://ollama:11434"),
        model=o.get("model", "llama3.1:8b"),
        prompt_template=o.get("prompt_template", ""),
        options=OllamaOptions(
            temperature=opts.get("temperature", 0.3),
            num_predict=opts.get("num_predict", 1024),
            top_p=opts.get("top_p", 0.9),
        ),
        timeout_seconds=o.get("timeout_seconds", 120),
    )

    # Output
    out = data.get("output", {})
    output = OutputConfig(
        directory=out.get("directory", "./summaries"),
        format=out.get("format", "markdown"),
        lookback_hours=out.get("lookback_hours", 24),
        overwrite_existing=out.get("overwrite_existing", True),
    )

    # Archive
    arch = data.get("archive", {})
    archive = ArchiveConfig(
        enabled=arch.get("enabled", True),
        host=arch.get("host", "localhost"),
        port=int(arch.get("port", 5432)),
        database=arch.get("database", "chat_summarizer"),
        user=arch.get("user", ""),
        password=arch.get("password", ""),
        retention_days=arch.get("retention_days", 90),
    )

    # State
    st = data.get("state", {})
    state = StateConfig(state_file=st.get("state_file", "./state.json"))

    # Me (user identity for eligibility detection)
    m = data.get("me", {})
    rc_me = m.get("rocketchat", {})
    teams_me = m.get("teams", {})
    me = MeConfig(
        rocketchat=MeRocketChatConfig(
            user_id=rc_me.get("user_id", ""),
            username=rc_me.get("username", ""),
        ),
        teams=MeTeamsConfig(
            user_id=teams_me.get("user_id", ""),
            display_name=teams_me.get("display_name", ""),
        ),
    )

    # Tracking behaviour
    tr = data.get("tracking", {})
    tracking = TrackingConfig(
        thread_age_out_days=int(tr.get("thread_age_out_days", 90)),
        first_run_lookback_days=int(tr.get("first_run_lookback_days", 90)),
    )

    # Logging
    lg = data.get("logging", {})
    logging_cfg = LoggingConfig(
        level=lg.get("level", "info"),
        log_file=lg.get("log_file"),
    )

    return Config(
        rocketchat=rocketchat,
        teams=teams,
        ollama=ollama,
        output=output,
        archive=archive,
        state=state,
        me=me,
        tracking=tracking,
        logging=logging_cfg,
    )
