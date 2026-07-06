"""Tests for config loading and validation."""

import textwrap
import pytest
from src.config import load_config


def test_load_minimal_config(tmp_path, monkeypatch):
    monkeypatch.setenv("RC_USER_ID", "rc_uid_123")
    monkeypatch.setenv("RC_AUTH_TOKEN", "test_token")
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_DB", "chat_summarizer")
    monkeypatch.setenv("POSTGRES_USER", "summarizer")
    monkeypatch.setenv("POSTGRES_PASSWORD", "secret")
    monkeypatch.setenv("TEAMS_ME_USER_ID", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(textwrap.dedent("""\
        rocketchat:
          enabled: true
          url: "https://rc.example.com"
          user_id: "${RC_USER_ID}"
          auth_token: "${RC_AUTH_TOKEN}"
          channels:
            - name: general
        teams:
          enabled: false
          tenant_id: ""
          client_id: ""
          client_secret: ""
          teams_channels: []
        ollama:
          url: "http://localhost:11434"
          model: "llama3.1:8b"
          prompt_template: "test prompt"
        output:
          directory: "/tmp/summaries"
        archive:
          enabled: true
          host: "${POSTGRES_HOST}"
          port: 5432
          database: "${POSTGRES_DB}"
          user: "${POSTGRES_USER}"
          password: "${POSTGRES_PASSWORD}"
        state:
          state_file: "/tmp/state.json"
        me:
          rocketchat:
            user_id: "${RC_USER_ID}"
            username: "alice"
          teams:
            user_id: "${TEAMS_ME_USER_ID}"
            display_name: "Alice Smith"
        tracking:
          thread_age_out_days: 60
          first_run_lookback_days: 30
    """))

    config = load_config(str(cfg_file))

    # Rocket.Chat
    assert config.rocketchat.enabled is True
    assert config.rocketchat.url == "https://rc.example.com"
    assert config.rocketchat.user_id == "rc_uid_123"
    assert config.rocketchat.channels[0].name == "general"

    # Archive / Postgres
    assert config.archive.host == "localhost"
    assert config.archive.database == "chat_summarizer"
    assert config.archive.port == 5432

    # Me identity
    assert config.me.rocketchat.user_id == "rc_uid_123"
    assert config.me.rocketchat.username == "alice"
    assert config.me.teams.user_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert config.me.teams.display_name == "Alice Smith"

    # Tracking
    assert config.tracking.thread_age_out_days == 60
    assert config.tracking.first_run_lookback_days == 30


def test_tracking_defaults(tmp_path, monkeypatch):
    """tracking section is optional; defaults apply when omitted."""
    monkeypatch.setenv("RC_USER_ID", "u1")
    monkeypatch.setenv("RC_AUTH_TOKEN", "tok")
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_DB", "db")
    monkeypatch.setenv("POSTGRES_USER", "usr")
    monkeypatch.setenv("POSTGRES_PASSWORD", "pw")
    monkeypatch.setenv("TEAMS_ME_USER_ID", "00000000-0000-0000-0000-000000000000")

    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(textwrap.dedent("""\
        rocketchat:
          enabled: false
          url: ""
          user_id: ""
          auth_token: ""
          channels: []
        teams:
          enabled: false
          tenant_id: ""
          client_id: ""
          client_secret: ""
          teams_channels: []
        ollama:
          url: "http://localhost:11434"
          model: "llama3.1:8b"
          prompt_template: ""
        output:
          directory: "/tmp/summaries"
        archive:
          enabled: false
          host: "${POSTGRES_HOST}"
          port: 5432
          database: "${POSTGRES_DB}"
          user: "${POSTGRES_USER}"
          password: "${POSTGRES_PASSWORD}"
        state:
          state_file: "/tmp/state.json"
        me:
          rocketchat:
            user_id: ""
            username: ""
          teams:
            user_id: ""
            display_name: ""
    """))

    config = load_config(str(cfg_file))
    assert config.tracking.thread_age_out_days == 90
    assert config.tracking.first_run_lookback_days == 90
