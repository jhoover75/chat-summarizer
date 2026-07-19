# Chat Summarization System — Software Architecture Design Document

**Version:** 1.2  
**Date:** 2026-07-05  
**Status:** Draft  

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Component Inventory](#2-component-inventory)
3. [Directory Structure](#3-directory-structure)
4. [config.yaml Schema](#4-configyaml-schema)
5. [Data Flow](#5-data-flow)
6. [Output File Format](#6-output-file-format)
7. [Microsoft Teams Auth Setup](#7-microsoft-teams-auth-setup)
8. [Rocket.Chat Auth Setup](#8-rocketchat-auth-setup)
9. [Docker Compose File](#9-docker-compose-file)
10. [Dockerfile](#10-dockerfile)
11. [Cron Setup](#11-cron-setup)
12. [Database Schema (PostgreSQL)](#12-database-schema-postgresql)
13. [Key Implementation Notes](#13-key-implementation-notes)

---

## 1. System Overview

`chat-summarizer` is a self-hosted, cron-schedulable CLI tool that monitors Rocket.Chat and Microsoft Teams for chat threads the user cares about — threads they started, are following on the platform, or have been mentioned in — and uses a locally-running Ollama LLM to generate per-thread summaries. The system tracks eligible threads in a PostgreSQL `tracked_threads` table and stores only those threads' messages. Each run fetches all new channel activity, identifies newly eligible threads, appends new messages to a cumulative per-thread `raw.md` transcript, and re-summarizes any thread with new activity. Output is one directory per thread containing `raw.md` (the complete verbatim chat history) and `summary.md` (the latest LLM summary with deep links back to the source). Threads age out after a configurable period of inactivity; deletion requires an explicit `--prune` command so the user can review before anything is removed. All components carry MIT, Apache 2.0, or LGPL licenses, the entire stack runs in Docker Compose on Linux, and no message content ever leaves the host.

---

## 2. Component Inventory

| Component | License | Version (recommended) | Purpose |
|---|---|---|---|
| Python | PSF (permissive) | 3.12 | Application runtime |
| httpx | BSD | 0.27+ | Async HTTP client for Rocket.Chat and Graph API calls |
| PyYAML | MIT | 6.x | Parsing `config.yaml` and writing YAML output |
| Jinja2 | BSD | 3.x | Templating Markdown output files |
| ollama (Python SDK) | MIT | 0.3+ | Client library for local Ollama REST API |
| PostgreSQL | PostgreSQL License (permissive) | 16+ | Persistent message archive and deduplication store |
| psycopg2-binary | LGPL v3 | 2.9+ | PostgreSQL driver for Python; LGPL is permissive for application use |
| python-dateutil | Apache 2.0 | 2.x | Robust datetime parsing from API responses |
| msal | MIT | 1.x | Microsoft Authentication Library — handles OAuth2 token acquisition for Graph API |
| tenacity | Apache 2.0 | 8.x | Retry logic with exponential backoff for API calls |
| structlog | MIT | 24.x | Structured logging (JSON-safe, easy to pipe to files) |
| Ollama (server) | MIT | 0.3+ | Local LLM inference runner; exposes OpenAI-compatible REST API on port 11434 |
| Llama 3.1 8B (default model) | Meta Llama 3 Community License* | — | Recommended default LLM; fits in 8 GB VRAM / 16 GB RAM with 4-bit quant |
| Mistral 7B (alternative) | Apache 2.0 | — | Apache 2.0 alternative; similar quality, slightly lower RAM use |
| Docker Engine | Apache 2.0 | 25+ | Container runtime |
| Docker Compose | Apache 2.0 | v2 | Multi-container orchestration |

> **Note on Llama 3.1 license:** Meta's Llama 3 Community License is not OSI-approved but permits free commercial and research use for deployments under 700M monthly active users. If your organisation requires a fully Apache-licensed model, use `mistral:7b` instead — quality is comparable and the license is clean Apache 2.0.

> **Note on psycopg2-binary license:** psycopg2-binary is LGPL v3. LGPL requires that modifications to the library itself be open-sourced, but using it as a dependency in your application imposes no restrictions on your own code. This is standard practice for commercial and private projects. If a stricter license is required, `pg8000` (BSD) is a pure-Python alternative, though it is less commonly used.

### LLM Model Selection Guidance

| Model | Ollama tag | VRAM required | Notes |
|---|---|---|---|
| Llama 3.1 8B (Q4_K_M) | `llama3.1:8b` | ~5 GB | Best overall quality for summarization tasks at this size |
| Mistral 7B (Q4_K_M) | `mistral:7b` | ~4.5 GB | Apache 2.0, strong instruction following |
| Phi-3 Mini (Q4) | `phi3:mini` | ~2.5 GB | Works on CPU-only hosts; noticeably lower quality |

If no GPU is available, any of these will run on CPU — expect 30–120 seconds per channel summary rather than 3–8 seconds.

---

## 3. Directory Structure

```
chat-summarizer/
│
├── docker-compose.yml          # Orchestrates app + Ollama containers
├── Dockerfile                  # Python app image
├── .env.example                # Template for secrets (never commit .env)
├── .env                        # Actual secrets (gitignored)
├── .gitignore
├── README.md
│
├── config.yaml                 # Main configuration file (see §4)
├── state.json                  # Runtime state: last-fetched timestamps per channel
│                               #   (auto-created on first run, gitignored)
│
├── src/
│   ├── __init__.py
│   ├── main.py                 # CLI entry point; orchestrates the full pipeline
│   │
│   ├── config.py               # Loads + validates config.yaml; returns typed dataclasses
│   ├── state.py                # Reads/writes state.json (last-run timestamps)
│   │
│   ├── sources/
│   │   ├── __init__.py
│   │   ├── base.py             # Abstract base class: ChatSource(config) → list[Message]
│   │   ├── rocketchat.py       # Rocket.Chat REST API client
│   │   └── teams.py            # Microsoft Graph API client (uses msal for auth)
│   │
│   ├── archive/
│   │   ├── __init__.py
│   │   ├── db.py               # PostgreSQL connection, schema migration, upsert helpers
│   │   └── models.py           # Python dataclasses: Message, Summary, SourceRef
│   │
│   ├── summarizer/
│   │   ├── __init__.py
│   │   ├── ollama_client.py    # Wraps Ollama Python SDK; sends prompt, parses response
│   │   └── prompt_builder.py   # Builds prompt from messages + Jinja2 template
│   │
│   ├── output/
│   │   ├── __init__.py
│   │   ├── markdown_writer.py  # Renders summary → Markdown file on disk
│   │   ├── yaml_writer.py      # Renders summary → YAML file on disk
│   │   └── templates/
│   │       └── summary.md.j2   # Jinja2 template for Markdown output
│   │
│   └── utils/
│       ├── __init__.py
│       ├── logging.py          # structlog configuration
│       └── rate_limiter.py     # Token-bucket rate limiter for API calls
│
├── summaries/                  # Output directory (gitignored, bind-mounted in Docker)
│   └── rocketchat/
│       └── general/
│           └── 2026-06-15/     # Date thread originated (YYYY-MM-DD)
│               └── aoBRcAkpbPMDLDKFb/   # Parent message ID (thread ID)
│                   ├── raw.md           # Cumulative verbatim transcript (append-only)
│                   └── summary.md       # Latest LLM summary (overwritten on new activity)
│   └── teams/
│       └── Engineering/
│           └── general/
│               └── 2026-06-20/
│                   └── 1686812345678/
│                       ├── raw.md
│                       └── summary.md
│
├── ollama-models/              # Ollama model weights (bind-mounted into Ollama container)
│
├── tests/
│   ├── __init__.py
│   ├── test_config.py
│   ├── test_rocketchat.py
│   ├── test_teams.py
│   ├── test_archive.py
│   └── fixtures/
│       ├── rocketchat_messages.json
│       └── teams_messages.json
│
└── requirements.txt
```

---

## 4. config.yaml Schema

```yaml
# chat-summarizer/config.yaml
# Full annotated configuration reference.
# Secrets should be loaded from environment variables (see .env.example).
# Use ${ENV_VAR} syntax — PyYAML alone does NOT expand env vars;
# use python-dotenv + os.path.expandvars() in config.py to resolve them.

# ---------------------------------------------------------------------------
# Rocket.Chat source
# ---------------------------------------------------------------------------
rocketchat:
  enabled: true

  # Base URL of your Rocket.Chat instance (no trailing slash). Set RC_URL
  # (scheme + host, no port) and RC_PORT in .env.
  url: "${RC_URL}:${RC_PORT}"

  # Personal Access Token credentials.
  # Generate in RC: My Account → Security → Personal Access Tokens
  user_id: "${RC_USER_ID}"          # Rocket.Chat user ID (not username)
  auth_token: "${RC_AUTH_TOKEN}"    # Personal access token value

  # List of channels to monitor.
  # Use the channel *name* (without #). The fetcher will resolve to room ID.
  channels:
    - name: "general"
      # Optional: override the global lookback_hours for this channel only
      lookback_hours: 24
    - name: "engineering"
      lookback_hours: 8
    - name: "incidents"
      lookback_hours: 4

  # Maximum messages to fetch per channel per run (guards against runaway API calls
  # on first run or after a long gap). Set to 0 for unlimited.
  max_messages_per_run: 500

  # Rocket.Chat REST API page size (max 100 per RC docs)
  page_size: 100

# ---------------------------------------------------------------------------
# Microsoft Teams source
# ---------------------------------------------------------------------------
teams:
  enabled: true

  # Azure AD / Entra ID tenant ID (Directory ID in Azure portal)
  tenant_id: "${TEAMS_TENANT_ID}"

  # App registration client ID (Application ID)
  client_id: "${TEAMS_CLIENT_ID}"

  # App registration client secret
  client_secret: "${TEAMS_CLIENT_SECRET}"

  # List of Teams + channels to monitor.
  # team_id and channel_id are GUIDs from Graph API (see §7 for how to find them).
  teams_channels:
    - team_name: "Engineering"           # Human-readable label (used in output filenames)
      team_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
      channels:
        - channel_name: "general"
          channel_id: "19:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx@thread.tacv2"
        - channel_name: "deployments"
          channel_id: "19:yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy@thread.tacv2"
    - team_name: "Product"
      team_id: "yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy"
      channels:
        - channel_name: "roadmap"
          channel_id: "19:zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz@thread.tacv2"

  # Maximum messages to fetch per channel per run
  max_messages_per_run: 200

  # Graph API $top page size (max 50 for channel messages)
  page_size: 50

# ---------------------------------------------------------------------------
# Ollama (local LLM)
# ---------------------------------------------------------------------------
ollama:
  # URL of the Ollama server. When running via Docker Compose, use the
  # service name as hostname: http://ollama:11434
  url: "http://ollama:11434"

  # Model to use. Run `ollama pull llama3.1:8b` (or mistral:7b) first.
  model: "llama3.1:8b"

  # Ollama generation options (passed through to the API)
  options:
    temperature: 0.3        # Lower = more deterministic summaries
    num_predict: 1024       # Max tokens in response
    top_p: 0.9

  # Prompt template. Use Jinja2 syntax.
  # Available variables:
  #   {{ channel }}         — channel name
  #   {{ platform }}        — "rocketchat" or "teams"
  #   {{ date }}            — date of messages (YYYY-MM-DD)
  #   {{ message_count }}   — number of messages
  #   {{ messages }}        — formatted message block (see prompt_builder.py)
  prompt_template: |
    You are an expert at summarizing workplace chat conversations.
    
    Below are {{ message_count }} messages from the #{{ channel }} channel
    on {{ platform }} for {{ date }}.
    
    Your task:
    1. Write a concise paragraph summary (3-5 sentences) of the main topics discussed.
    2. List 3-7 key points as bullet points, each starting with "- ".
    3. Note any action items or decisions made, prefixed with "[ACTION]" or "[DECISION]".
    
    Important: Do not include any personally identifying information beyond
    what is in the messages. Be neutral and factual.
    
    Messages:
    {{ messages }}
    
    ---
    Respond with exactly this structure (no extra commentary):
    
    ## Summary
    <paragraph>
    
    ## Key Points
    - <point>
    
    ## Actions & Decisions
    - [ACTION/DECISION] <item>

  # Timeout in seconds for a single Ollama request. Long for large message batches.
  timeout_seconds: 120

# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------
output:
  # Root directory for all generated output files.
  # Structure: <directory>/<platform>/<channel>/<YYYY-MM-DD>/<thread_id>/
  #   raw.md       — cumulative verbatim transcript (append-only)
  #   summary.md   — latest LLM summary (overwritten on new activity)
  directory: "./summaries"

# ---------------------------------------------------------------------------
# Who am I — used for thread eligibility detection
# ---------------------------------------------------------------------------
me:
  rocketchat:
    # Your Rocket.Chat user ID (same value as RC_USER_ID credential above).
    # Used to identify threads you started.
    user_id: "${RC_USER_ID}"
    # Your plain username without @. Used to scan message text for mentions
    # when the platform mention metadata is unavailable.
    username: "alice"

  teams:
    # Your Azure AD / Entra object ID (not the same as TEAMS_CLIENT_ID).
    # Find it at: https://portal.azure.com → Azure AD → Users → your account → Object ID
    user_id: "${TEAMS_ME_USER_ID}"
    # Your Teams display name. Used as a fallback for mention text-scanning.
    display_name: "Alice Smith"

# ---------------------------------------------------------------------------
# Thread tracking behaviour
# ---------------------------------------------------------------------------
tracking:
  # Threads with no new messages for this many days are flagged 'aged_out'.
  # They are NOT deleted automatically — run with --prune to review and delete.
  thread_age_out_days: 90

  # On the very first run for a channel (no prior state), look back this many
  # days for threads the user started. Subsequent runs use state.json.
  first_run_lookback_days: 90

# ---------------------------------------------------------------------------
# Archive (PostgreSQL)
# ---------------------------------------------------------------------------
archive:
  enabled: true

  # PostgreSQL connection parameters.
  # When running via Docker Compose, use the service name as host: "postgres"
  host: "${POSTGRES_HOST}"        # e.g. "postgres" inside Docker, "localhost" outside
  port: 5432
  database: "${POSTGRES_DB}"      # e.g. "chat_summarizer"
  user: "${POSTGRES_USER}"        # e.g. "summarizer"
  password: "${POSTGRES_PASSWORD}"

  # Retain raw messages for this many days. Older messages are deleted on
  # each run to keep the DB from growing unbounded. Set to 0 to keep forever.
  retention_days: 90

# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------
state:
  # Path to JSON file that tracks the last-fetched timestamp per channel.
  # Format: { "rocketchat::general": "2026-07-04T18:00:00Z", ... }
  # This file is created automatically on first run.
  state_file: "./state.json"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging:
  # "debug", "info", "warning", "error"
  level: "info"

  # Write logs to a file in addition to stdout. Set to null to disable.
  log_file: "./logs/summarizer.log"
```

### Environment Variable File (`.env.example`)

```dotenv
# Copy to .env and fill in real values. Never commit .env to git.

# Rocket.Chat credentials
RC_USER_ID=your_rocketchat_user_id
RC_AUTH_TOKEN=your_personal_access_token

# Microsoft Teams / Azure credentials
TEAMS_TENANT_ID=xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
TEAMS_CLIENT_ID=yyyyyyyy-yyyy-yyyy-yyyy-yyyyyyyyyyyy
TEAMS_CLIENT_SECRET=your_client_secret_value

# Your personal Teams/Azure AD user object ID (not the app client ID above).
# Find at: Azure Portal → Azure AD → Users → your account → Object ID
TEAMS_ME_USER_ID=zzzzzzzz-zzzz-zzzz-zzzz-zzzzzzzzzzzz

# PostgreSQL credentials (matches docker-compose.yml postgres service)
POSTGRES_HOST=postgres
POSTGRES_DB=chat_summarizer
POSTGRES_USER=summarizer
POSTGRES_PASSWORD=change_me_in_production
```

---

## 5. Data Flow

The following sequence describes what happens when the script is invoked. There are two modes: a normal summarization run and a `--prune` run (manual, interactive).

### Normal run: `docker compose run --rm app`

#### Step 1 — Load Configuration

```
main.py
  └── config.py::load_config("config.yaml")
        - Reads config.yaml with PyYAML
        - Expands ${ENV_VAR} references via os.path.expandvars()
          (after loading .env with python-dotenv if present)
        - Validates required fields (raises ConfigError on missing secrets)
        - Returns a Config dataclass (typed, immutable)
```

#### Step 2 — Initialize Database

```
main.py
  └── archive/db.py::init_db(config.archive)
        - Opens a psycopg2 connection using host/port/database/user/password
        - Runs CREATE TABLE IF NOT EXISTS for messages, summaries,
          tracked_threads, and schema_migrations tables (see §12)
        - Returns a psycopg2 connection used throughout the run
```

#### Step 3 — Load State File

```
main.py
  └── state.py::load_state(config.state.state_file)
        - Reads state.json from disk (creates {} if file doesn't exist)
        - Returns dict mapping channel keys → last-fetched ISO 8601 timestamps
        - Key format: "rocketchat::general", "teams::Engineering::general"
```

#### Step 4 — Flag Aged-Out Threads

```
main.py
  └── archive/db.py::flag_aged_out_threads(conn, age_out_days)
        - UPDATE tracked_threads SET status = 'aged_out'
          WHERE status = 'active'
            AND last_activity < NOW() - INTERVAL '<age_out_days> days'
        - Returns list of newly aged-out TrackedThread objects
        - If any were flagged: logs a warning with count and instructions
          "N threads aged out — run with --prune to review and delete"
        - Does NOT delete anything
```

#### Step 5 — For Each Platform + Channel: Fetch All New Messages Into Memory

```
main.py
  └── sources/rocketchat.py::RocketChatSource.fetch_all(channel, since_ts)
        OR
        sources/teams.py::TeamsSource.fetch_all(team_channel, since_ts)

  - Fetches all new messages since last run into a temporary in-memory list
  - These messages are NOT written to Postgres yet
  - If no prior state: since_ts = now() - first_run_lookback_days (first run)
                        OR now() - lookback_hours (channel-level override)

  Rocket.Chat API:
    GET /api/v1/channels.messages
      ?roomName=general&oldest=<since_ts>&count=100&offset=0
    Headers: X-Auth-Token, X-User-Id

  Graph API:
    GET /teams/{team_id}/channels/{channel_id}/messages
      ?$filter=lastModifiedDateTime ge <since_ts>&$top=50
    Headers: Authorization: Bearer <token>
```

#### Step 6 — Discover Eligible Threads

```
main.py
  └── sources/<platform>.py::discover_eligible_threads(
          all_messages, channel_config, me_config, conn)

  For each message in the fetched batch:

    A. Is the message a top-level post (not a reply) authored by me?
         Rocket.Chat: msg.tmid is None AND msg.author_id == me.rocketchat.user_id
         Teams:       msg.replyToId is None AND msg.author_id == me.teams.user_id
       → Reason: 'started'

    B. Does the message text or mentions field reference my username?
         Rocket.Chat: msg.body contains '@<me.username>'
                      OR msg.raw_json['mentions'][*]['username'] == me.username
         Teams:       msg.raw_json['mentions'][*]['mentioned']['user']['id'] == me.user_id
                      OR msg.body contains me.display_name
       → Reason: 'mentioned'

    C. Is this a reply authored by me (proxy for 'I engaged with this thread')?
         msg.thread_id is not None AND msg.author_id == me.user_id
       → Reason: 'replied'

  Additionally, once per channel per run, query the platform for threads
  the user is explicitly following:

    Rocket.Chat:
      GET /api/v1/chat.getThreadsList?rid=<roomId>&type=following
      → Returns thread parent message IDs the authenticated user follows
      → Reason: 'following'

    Teams: (no direct "list followed threads" API — see §13.9)
      Covered by 'mentioned' and 'replied' checks above.

  For each eligible thread ID not already in tracked_threads:
    → INSERT INTO tracked_threads (new row, status='active')
    → This thread's messages will now be stored
```

#### Step 7 — Filter Messages to Tracked Threads Only

```
main.py
  - From the full in-memory message batch, retain only messages whose
    thread_id (or message_id for top-level posts) appears in tracked_threads
    with status = 'active'
  - Discard everything else from memory — it is never written to Postgres
```

#### Step 8 — Store Tracked Messages in Postgres

```
main.py
  └── archive/db.py::upsert_messages(conn, tracked_messages)
        INSERT INTO messages (...) VALUES (...)
        ON CONFLICT (platform, message_id) DO NOTHING

  For each tracked thread that received new messages:
    └── archive/db.py::update_thread_activity(conn, thread)
          UPDATE tracked_threads
             SET last_activity = <newest message timestamp>,
                 needs_resummary = TRUE
           WHERE platform = %s AND channel = %s AND thread_id = %s
```

#### Step 9 — Append New Messages to raw.md

```
main.py
  └── output/markdown_writer.py::append_raw(thread, new_messages, output_dir)

  Output path:
    <output_dir>/<platform>/<channel>/<YYYY-MM-DD>/<thread_id>/raw.md
    where YYYY-MM-DD is thread.originated_at date

  - If raw.md does not exist: create it with a thread header block, then
    write all messages for this thread from Postgres (full history)
  - If raw.md already exists: append only the new messages to the bottom
  - Format: one block per message (see §6 for example)
  - File is append-only and grows over time — it is never overwritten
```

#### Step 10 — Re-Summarize Threads with New Activity

```
main.py — for each tracked thread where needs_resummary = TRUE:

  └── archive/db.py::get_thread_messages(conn, platform, channel, thread_id)
        SELECT * FROM messages
         WHERE platform = %s AND channel = %s AND thread_id = %s
         ORDER BY timestamp ASC
        → Returns the FULL thread history (not just new messages)

  └── summarizer/prompt_builder.py::build_prompts(ollama_config, thread, messages)
        - Formats all messages into prompt text
        - Splits into chunks if needed (see §13.4)
        - Returns list of rendered prompt strings

  └── summarizer/ollama_client.py::summarize(prompts, thread, run_id)
        POST http://ollama:11434/api/generate
        → Returns a Summary dataclass

  └── output/markdown_writer.py::write_summary(thread, summary, output_dir)
        - Path: <output_dir>/.../<thread_id>/summary.md
        - Always overwrites — this is always a complete, current summary
        - Includes thread_url deep link, key points, actions, source citations

  └── archive/db.py::insert_summary(conn, summary)
        INSERT INTO summaries (...)

  └── archive/db.py::mark_summarized(conn, platform, channel, thread_id)
        UPDATE tracked_threads SET needs_resummary = FALSE
         WHERE platform = %s AND channel = %s AND thread_id = %s
```

#### Step 11 — Update State File and Exit

```
main.py
  └── state.py::save_state(state_file, updated_state)
        - For each successfully processed channel:
            state[channel_key] = timestamp of newest fetched message
        - Writes atomically to state.json

  └── Logs run summary: channels scanned, threads newly tracked,
      messages stored, summaries written, threads aged out
  └── Exits with code 0 on success, code 1 if any channel errored
```

---

### Prune run: `docker compose run --rm app --prune`

This is the only way aged-out threads and their data are ever deleted. It must be run interactively (never via cron).

```
Step 1: Load config + connect to Postgres (same as normal run)

Step 2: Query aged-out threads
  SELECT * FROM tracked_threads WHERE status = 'aged_out' ORDER BY last_activity

Step 3: Print a summary table to stdout:
  Platform | Channel | Subject | Last Activity | Files on Disk
  ---------------------------------------------------------------
  rocketchat | general | "Has anyone looked at..." | 2026-04-01 | raw.md, summary.md

Step 4: Prompt: "Delete N aged-out threads and their files? [y/N]: "
  - If N or no input: exit without deleting
  - If Y: proceed

Step 5: For each aged-out thread:
  - DELETE FROM messages WHERE platform=%s AND channel=%s AND thread_id=%s
  - DELETE FROM summaries WHERE platform=%s AND channel=%s AND thread_id=%s (where applicable)
  - DELETE FROM tracked_threads WHERE platform=%s AND channel=%s AND thread_id=%s
  - Remove the thread directory from disk:
      rm -rf <output_dir>/<platform>/<channel>/<date>/<thread_id>/

Step 6: Report: "Deleted N threads, freed ~X MB"
Step 7: Exit 0
```

---

## 6. Output File Format

Each tracked thread produces exactly two files under:
`summaries/<platform>/<channel>/<YYYY-MM-DD>/<thread_id>/`

Where `YYYY-MM-DD` is the date the top-level thread message was originally posted.

---

### `raw.md` — Cumulative verbatim transcript

This file is **append-only**. On first discovery of a thread it is created with a header and the full message history to date. On each subsequent run where new messages arrive, they are appended to the bottom. It is never overwritten or truncated. This is your audit trail for verifying summary correctness.

**Example:** `summaries/rocketchat/engineering/2026-06-15/aoBRcAkpbPMDLDKFb/raw.md`

````markdown
---
platform: rocketchat
channel: engineering
thread_id: aoBRcAkpbPMDLDKFb
thread_url: "https://chat.example.com/channel/engineering?msg=aoBRcAkpbPMDLDKFb"
originated_at: "2026-06-15T09:14:00Z"
tracked_since: "2026-06-15T09:30:01Z"
reason: started
---

# Thread: #engineering — "I'm seeing intermittent 500 errors in production..."

[View original thread ↗](https://chat.example.com/channel/engineering?msg=aoBRcAkpbPMDLDKFb)

---

**@alice** · 2026-06-15 09:14 UTC
I'm seeing intermittent 500 errors in production. Investigating now.

---

**@bob** · 2026-06-15 09:20 UTC
Could be the connection pool — let me check the metrics.

---

**@alice** · 2026-06-15 09:35 UTC
Found it. `connect_timeout` is not set in the SQLAlchemy config.

---

**@bob** · 2026-06-15 14:22 UTC
Hotfix deployed. Error rate is back to zero.

---

<!-- appended 2026-06-16T07:30:01Z -->

**@carol** · 2026-06-16 08:45 UTC
Great catch @alice — should we add this to the runbook?

---

**@alice** · 2026-06-16 09:02 UTC
Good idea — I'll add it today.

---
````

---

### `summary.md` — Latest LLM summary

This file is **always overwritten** when `needs_resummary = TRUE`. It reflects the complete current state of the thread, not just what's new since the last run. It links back to both the original thread URL and the local `raw.md` for verification.

**Example:** `summaries/rocketchat/engineering/2026-06-15/aoBRcAkpbPMDLDKFb/summary.md`

````markdown
---
generated_at: "2026-06-16T07:30:05Z"
platform: rocketchat
channel: engineering
thread_id: aoBRcAkpbPMDLDKFb
thread_url: "https://chat.example.com/channel/engineering?msg=aoBRcAkpbPMDLDKFb"
originated_at: "2026-06-15T09:14:00Z"
message_count: 6
model: llama3.1:8b
run_id: "a3f8c2d1-4e92-4b1a-8f5e-9c3b7a2e6d04"
reason: started
---

# Thread Summary — #engineering

> **[View original thread ↗](https://chat.example.com/channel/engineering?msg=aoBRcAkpbPMDLDKFb)**  
> **[View raw transcript →](./raw.md)**  
> Started by @alice · 2026-06-15 09:14 UTC · 6 messages

## Summary

Alice reported intermittent 500 errors in production on June 15th. Bob and Alice
investigated and identified the root cause as a missing `connect_timeout` parameter
in the SQLAlchemy connection pool configuration. Bob deployed a hotfix that same
afternoon, resolving the issue. The following day, Carol suggested documenting the
fix in the runbook, which Alice agreed to add.

## Key Points

- Production 500 errors traced to missing `connect_timeout` in SQLAlchemy pool config
- Hotfix deployed 2026-06-15 14:22 UTC; error rate immediately returned to zero
- Team agreed to document the fix in the runbook

## Actions & Decisions

- [ACTION] @alice to add `connect_timeout` fix to the runbook

---

*Generated by chat-summarizer v1.2 · Model: llama3.1:8b · Run ID: a3f8c2d1*
````

---

### Deep Link Formats

**Rocket.Chat:** `https://<rc_url>/channel/<channel_name>?msg=<thread_id>`

**Microsoft Teams:**
```
https://teams.microsoft.com/l/message/<channel_id>/<thread_id>?tenantId=<tenant_id>&groupId=<team_id>
```

The `thread_url` is constructed once when the thread is first added to `tracked_threads` and stored there permanently. Both output files reference it from that stored value.

---

## 7. Microsoft Teams Auth Setup

This section covers the one-time Azure configuration that must be completed before the summarizer can read Teams messages. The summarizer uses **application permissions** (daemon/service auth, no user login required), which is appropriate for a scheduled background job.

### 7.1 Register an Azure AD Application

1. Sign in to the [Azure portal](https://portal.azure.com) as a Global Administrator or Application Administrator.
2. Navigate to **Azure Active Directory → App registrations → New registration**.
3. Fill in:
   - **Name:** `chat-summarizer` (or any name you choose)
   - **Supported account types:** *Accounts in this organizational directory only*
   - **Redirect URI:** Leave blank (not needed for client credentials flow)
4. Click **Register**.
5. On the app overview page, copy and save:
   - **Application (client) ID** → `TEAMS_CLIENT_ID`
   - **Directory (tenant) ID** → `TEAMS_TENANT_ID`

### 7.2 Create a Client Secret

1. In the app registration, go to **Certificates & secrets → Client secrets → New client secret**.
2. Set a description (`chat-summarizer-secret`) and expiry (recommend 12 or 24 months — calendar a rotation reminder).
3. Click **Add**.
4. **Copy the secret Value immediately** — it will not be shown again.
5. Save as `TEAMS_CLIENT_SECRET`.

### 7.3 Add API Permissions

1. Go to **API permissions → Add a permission → Microsoft Graph → Application permissions**.
2. Search for and add the following permissions:

| Permission | Why |
|---|---|
| `ChannelMessage.Read.All` | Read messages in all channels |
| `Channel.ReadBasic.All` | List channels within a team |
| `Team.ReadBasic.All` | List teams the app has access to |

3. Click **Grant admin consent for \<your tenant\>** and confirm.
   - This requires Global Administrator role.
   - After granting, all three permissions should show a green checkmark under "Status".

### 7.4 Find Team and Channel IDs

You need the GUID identifiers for each team and channel to put in `config.yaml`. The easiest method:

**Option A — Graph Explorer (browser, one-time)**
1. Go to [Graph Explorer](https://developer.microsoft.com/en-us/graph/graph-explorer).
2. Sign in with your Teams account.
3. Run: `GET https://graph.microsoft.com/v1.0/me/joinedTeams`
4. Copy the `id` values for your teams.
5. For channels: `GET https://graph.microsoft.com/v1.0/teams/{team_id}/channels`
6. Copy the `id` values for channels.

**Option B — Teams desktop app**
1. In Teams, right-click a channel → **Get link to channel**.
2. The URL contains encoded team and channel IDs that can be decoded.

**Option C — Run a helper script**
```python
# tools/list_teams_channels.py — run once to enumerate your teams/channels
import msal, httpx, json, os

app = msal.ConfidentialClientApplication(
    os.environ["TEAMS_CLIENT_ID"],
    authority=f"https://login.microsoftonline.com/{os.environ['TEAMS_TENANT_ID']}",
    client_credential=os.environ["TEAMS_CLIENT_SECRET"],
)
token = app.acquire_token_for_client(["https://graph.microsoft.com/.default"])
headers = {"Authorization": f"Bearer {token['access_token']}"}

teams = httpx.get("https://graph.microsoft.com/v1.0/teams", headers=headers).json()
for team in teams.get("value", []):
    print(f"\nTeam: {team['displayName']} | ID: {team['id']}")
    channels = httpx.get(
        f"https://graph.microsoft.com/v1.0/teams/{team['id']}/channels",
        headers=headers,
    ).json()
    for ch in channels.get("value", []):
        print(f"  Channel: {ch['displayName']} | ID: {ch['id']}")
```

### 7.5 Token Acquisition (Runtime)

The summarizer uses MSAL's `ConfidentialClientApplication` with `acquire_token_for_client()`, which implements the OAuth 2.0 Client Credentials grant. MSAL caches the token in memory and automatically refreshes it before expiry. Each run acquires a fresh token since there is no persistent process.

```python
# src/sources/teams.py (excerpt)
import msal

class TeamsSource:
    def __init__(self, config: TeamsConfig):
        self._msal_app = msal.ConfidentialClientApplication(
            config.client_id,
            authority=f"https://login.microsoftonline.com/{config.tenant_id}",
            client_credential=config.client_secret,
        )
        self._scope = ["https://graph.microsoft.com/.default"]

    def _get_token(self) -> str:
        result = self._msal_app.acquire_token_for_client(scopes=self._scope)
        if "access_token" not in result:
            raise AuthError(f"MSAL token acquisition failed: {result.get('error_description')}")
        return result["access_token"]
```

---

## 8. Rocket.Chat Auth Setup

Rocket.Chat uses Personal Access Tokens for authentication. These tokens do not expire unless manually revoked or the account password is changed.

### 8.1 Generate a Personal Access Token

1. Log in to your Rocket.Chat instance.
2. Click your avatar (top-left or top-right) → **My Account**.
3. Scroll to **Security → Personal Access Tokens**.
4. Click **Add**.
5. Enter a token name: `chat-summarizer`.
6. Optionally enable **Require Two Factor Authentication** if your account has 2FA enabled (this adds a TOTP step — for automation, it is easier to leave this unchecked or use a dedicated bot account).
7. Click **Add**.
8. **Copy the Token value** — shown only once.
9. Also copy the **User ID** shown on the same page.
10. Save both as `RC_AUTH_TOKEN` and `RC_USER_ID` in `.env`.

### 8.2 Dedicated Bot Account (Recommended for Production)

Rather than using your personal account, create a dedicated Rocket.Chat user for the summarizer:

1. Admin Panel → Users → New User.
2. Set username (`summarizer-bot`), email, password.
3. Assign **bot** role (read-only, cannot send messages) — reduces blast radius if credentials are leaked.
4. Add the bot to each channel you want to monitor (or use a public channel).
5. Log in as the bot account and generate a Personal Access Token as above.

### 8.3 API Authentication Headers

All Rocket.Chat REST API requests require these headers:
```
X-Auth-Token: <auth_token>
X-User-Id: <user_id>
Content-Type: application/json
```

### 8.4 Finding Channel Room IDs

The `channels.messages` endpoint accepts `roomName` (channel name) or `roomId`. Using `roomName` is simpler for config. For private rooms (`groups`), use the `groups.messages` endpoint with `roomId` instead. The source adapter detects room type and calls the correct endpoint.

---

## 9. Docker Compose File

```yaml
# docker-compose.yml
version: "3.9"

services:

  # -----------------------------------------------------------------------
  # PostgreSQL — message archive database
  # -----------------------------------------------------------------------
  postgres:
    image: postgres:16-alpine        # PostgreSQL License (permissive)
    container_name: chat-summarizer-postgres
    restart: unless-stopped          # Postgres is a persistent service; keep it running
    ports:
      - "5432:5432"                  # Expose for local psql/pgAdmin access
    volumes:
      - postgres-data:/var/lib/postgresql/data   # Named volume — survives container restarts
    environment:
      POSTGRES_DB: "${POSTGRES_DB}"
      POSTGRES_USER: "${POSTGRES_USER}"
      POSTGRES_PASSWORD: "${POSTGRES_PASSWORD}"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER} -d ${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 20s

  # -----------------------------------------------------------------------
  # Ollama — local LLM inference server
  # -----------------------------------------------------------------------
  ollama:
    image: ollama/ollama:latest      # Apache 2.0 / MIT — https://github.com/ollama/ollama
    container_name: chat-summarizer-ollama
    restart: "no"                    # No daemon; starts fresh each compose run
    ports:
      - "11434:11434"                # Expose locally for debugging; not required for app→ollama
    volumes:
      - ./ollama-models:/root/.ollama  # Persist downloaded model weights between runs
    environment:
      - OLLAMA_KEEP_ALIVE=0          # Unload model from VRAM immediately after inference
    # GPU support (NVIDIA) — remove this deploy section if running CPU-only
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:11434/api/tags"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 30s

  # -----------------------------------------------------------------------
  # chat-summarizer — the Python application
  # -----------------------------------------------------------------------
  app:
    build:
      context: .
      dockerfile: Dockerfile
    container_name: chat-summarizer-app
    restart: "no"
    depends_on:
      postgres:
        condition: service_healthy   # Wait until Postgres is ready
      ollama:
        condition: service_healthy   # Wait until Ollama is ready
    volumes:
      - ./config.yaml:/app/config.yaml:ro          # Config (read-only)
      - ./state.json:/app/state.json               # State file (read-write)
      - ./summaries:/app/summaries                 # Output summaries
      - ./logs:/app/logs                           # Log files
    env_file:
      - .env                                       # Injects secrets as environment variables
    environment:
      - PYTHONUNBUFFERED=1
    # The app exits 0 on success; docker-compose run exits with the container's exit code
    # Use `docker compose run --rm app` for cron-invoked runs

volumes:
  postgres-data:    # Persists Postgres data across container restarts and rebuilds
```

### Usage Notes

**First run — start Postgres and pull Ollama model:**
```bash
# Start Postgres in the background (persistent service)
docker compose up -d postgres

# Pull the Ollama model weight once (stored in ./ollama-models/)
docker compose run --rm ollama ollama pull llama3.1:8b
```

**Manual run:**
```bash
docker compose run --rm app
```

**Prune aged-out threads (interactive — never put in cron):**
```bash
docker compose run --rm -it app --prune
```

**Check exit code (for cron error alerting):**
```bash
docker compose run --rm app; echo "Exit code: $?"
```

**View logs:**
```bash
cat logs/summarizer.log
# or live during a run:
docker compose logs -f app
```

---

## 10. Dockerfile

```dockerfile
# Dockerfile
# Multi-stage build — keeps final image lean

# ---------------------------------------------------------------------------
# Stage 1: Build dependencies
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

# Install build tools needed for some Python packages (libpq-dev required for psycopg2)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies into a prefix we can copy to final stage
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install --prefix=/install --no-cache-dir -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2: Runtime image
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY src/ ./src/

# Create directories that will be bind-mounted (keeps permissions correct)
RUN mkdir -p /app/summaries /app/data /app/logs

# Run as non-root for security
RUN useradd -r -u 1001 -g root summarizer \
    && chown -R summarizer:root /app
USER summarizer

# Default command — runs the summarizer once and exits
CMD ["python", "-m", "src.main", "--config", "/app/config.yaml"]
```

### `requirements.txt`

```
httpx==0.27.0
PyYAML==6.0.2
Jinja2==3.1.4
ollama==0.3.3
python-dateutil==2.9.0
msal==1.29.0
tenacity==8.3.0
structlog==24.2.0
python-dotenv==1.0.1
psycopg2-binary==2.9.9
```

---

## 11. Cron Setup

The summarizer is designed to be invoked by cron as a one-shot Docker Compose run. There is no daemon — each run is self-contained.

### Crontab Entries

```crontab
# /etc/cron.d/chat-summarizer
# (or add to the user's crontab with `crontab -e`)

# ── Daily digest at 07:00 UTC ──────────────────────────────────────────────
# Generates a summary of the past 24 hours for all channels.
0 7 * * * cd /opt/chat-summarizer && docker compose run --rm app >> /var/log/chat-summarizer-cron.log 2>&1

# ── Hourly summaries for incident channels ────────────────────────────────
# If your config has incident channels with lookback_hours: 1, run hourly.
# Uses a separate config file that only lists incident channels.
0 * * * * cd /opt/chat-summarizer && docker compose run --rm -e CONFIG=/app/config-incidents.yaml app >> /var/log/chat-summarizer-cron.log 2>&1

# ── Weekly deep-dive summary (Monday 08:00 UTC) ───────────────────────────
# Runs with lookback_hours: 168 (one week) using a separate config.
0 8 * * 1 cd /opt/chat-summarizer && docker compose run --rm -e LOOKBACK_HOURS=168 app >> /var/log/chat-summarizer-cron.log 2>&1
```

### Cron Lockfile (Prevent Overlapping Runs)

For hourly cron jobs, add `flock` to prevent overlap if a previous run is still active (e.g., if Ollama is slow on a large backlog):

```crontab
0 * * * * flock -n /tmp/chat-summarizer.lock -c "cd /opt/chat-summarizer && docker compose run --rm app" >> /var/log/chat-summarizer-cron.log 2>&1
```

### Cron Error Alerting

To receive an email when the summarizer fails:

```crontab
MAILTO=ops@example.com
0 7 * * * cd /opt/chat-summarizer && docker compose run --rm app || echo "chat-summarizer failed — check /var/log/chat-summarizer-cron.log"
```

Or use a monitoring script:

```bash
#!/bin/bash
# /opt/chat-summarizer/run_and_alert.sh
cd /opt/chat-summarizer
docker compose run --rm app
EXIT_CODE=$?
if [ $EXIT_CODE -ne 0 ]; then
    echo "chat-summarizer exited with code $EXIT_CODE at $(date -u)" \
    | mail -s "chat-summarizer FAILED" ops@example.com
fi
exit $EXIT_CODE
```

---

## 12. Database Schema (PostgreSQL)

```sql
-- schema.sql
-- Applied via archive/db.py on startup (CREATE TABLE IF NOT EXISTS is idempotent).
-- PostgreSQL-specific syntax: SERIAL, NOW(), TIMESTAMPTZ, ON CONFLICT DO NOTHING.

-- ────────────────────────────────────────────────────────────────────────────
-- tracked_threads — threads the user is following, started, or was mentioned in
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tracked_threads (
    id              BIGSERIAL       PRIMARY KEY,

    -- Source platform
    platform        TEXT            NOT NULL CHECK (platform IN ('rocketchat', 'teams')),

    -- Channel name (display name)
    channel         TEXT            NOT NULL,

    -- Teams only: team display name. NULL for Rocket.Chat.
    team            TEXT,

    -- The parent message ID of the thread (top-level post)
    thread_id       TEXT            NOT NULL,

    -- First ~120 characters of the parent message body — human-readable label
    thread_subject  TEXT,

    -- Direct URL to browse the original thread on the platform.
    -- Rocket.Chat: https://<url>/channel/<channel>?msg=<thread_id>
    -- Teams:       https://teams.microsoft.com/l/message/<channel_id>/<thread_id>?...
    thread_url      TEXT            NOT NULL,

    -- When the top-level (parent) message was originally posted
    originated_at   TIMESTAMPTZ     NOT NULL,

    -- When this system first started tracking this thread
    tracked_since   TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    -- Timestamp of the most recently seen message in this thread.
    -- Used by the age-out check: if this is older than thread_age_out_days, flag aged_out.
    last_activity   TIMESTAMPTZ,

    -- Why this thread was added to tracking.
    -- 'started'   — user authored the top-level message
    -- 'following' — user explicitly follows this thread on the platform (RC only)
    -- 'mentioned' — user's username/display name appeared in a message
    -- 'replied'   — user replied to this thread (Teams proxy for engagement)
    reason          TEXT            NOT NULL
                    CHECK (reason IN ('started', 'following', 'mentioned', 'replied')),

    -- 'active'    — thread is actively monitored
    -- 'aged_out'  — no activity for thread_age_out_days; awaiting --prune confirmation
    status          TEXT            NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'aged_out')),

    -- Set to TRUE when new messages arrive; set to FALSE after summary.md is written.
    -- Only threads with needs_resummary = TRUE are sent to Ollama on each run.
    needs_resummary BOOLEAN         NOT NULL DEFAULT FALSE,

    UNIQUE (platform, channel, thread_id)
);

CREATE INDEX IF NOT EXISTS idx_tracked_threads_status
    ON tracked_threads (status);

CREATE INDEX IF NOT EXISTS idx_tracked_threads_last_activity
    ON tracked_threads (last_activity);

CREATE INDEX IF NOT EXISTS idx_tracked_threads_platform_channel
    ON tracked_threads (platform, channel);

-- ────────────────────────────────────────────────────────────────────────────
-- messages — tracked thread messages only (never stores untracked messages)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS messages (
    -- Unique message identifier from source platform.
    -- Rocket.Chat: the _id field (e.g. "aoBRcAkpbPMDLDKFb")
    -- Teams: the message id GUID
    message_id      TEXT            NOT NULL,

    -- Source platform identifier: "rocketchat" or "teams"
    platform        TEXT            NOT NULL CHECK (platform IN ('rocketchat', 'teams')),

    -- Rocket.Chat: channel name (e.g. "engineering")
    -- Teams: channel display name (e.g. "general")
    channel         TEXT            NOT NULL,

    -- Teams only: team display name (e.g. "Engineering"). NULL for Rocket.Chat.
    team            TEXT,

    -- Internal user ID from the source platform
    author_id       TEXT            NOT NULL,

    -- Display name of the message author
    author_name     TEXT            NOT NULL,

    -- Message body text (HTML stripped, plain text only)
    body            TEXT            NOT NULL DEFAULT '',

    -- Message timestamp as ISO 8601 UTC string (e.g. "2026-07-05T09:14:32.000Z")
    -- Stored as TEXT to preserve the exact format returned by each API.
    timestamp       TEXT            NOT NULL,

    -- Thread/reply parent message ID, if applicable.
    -- Rocket.Chat: tmid field. Teams: replyToId field. NULL for top-level messages.
    thread_id       TEXT,

    -- Full raw JSON response from the API, stored as JSONB for queryability.
    -- Allows replaying or re-processing without re-fetching.
    raw_json        JSONB           NOT NULL DEFAULT '{}',

    -- When this row was inserted into the archive (UTC)
    archived_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    PRIMARY KEY (platform, message_id)
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_messages_platform_channel_ts
    ON messages (platform, channel, timestamp);

CREATE INDEX IF NOT EXISTS idx_messages_thread
    ON messages (platform, thread_id)
    WHERE thread_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_messages_ts
    ON messages (timestamp);

-- GIN index on raw_json for ad-hoc JSON queries
CREATE INDEX IF NOT EXISTS idx_messages_raw_json
    ON messages USING GIN (raw_json);

-- ────────────────────────────────────────────────────────────────────────────
-- summaries — record of every generated summary
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS summaries (
    id                  BIGSERIAL       PRIMARY KEY,

    -- UUID generated at the start of each run; groups all summaries from one invocation
    run_id              TEXT            NOT NULL,

    -- Source platform
    platform            TEXT            NOT NULL CHECK (platform IN ('rocketchat', 'teams')),

    -- Channel summarized
    channel             TEXT            NOT NULL,

    -- Team (Teams only)
    team                TEXT,

    -- Date this summary covers (YYYY-MM-DD, UTC)
    summary_date        DATE            NOT NULL,

    -- Number of messages included in this summary
    message_count       INTEGER         NOT NULL DEFAULT 0,

    -- The full summary text as returned by Ollama (raw LLM output)
    summary_text        TEXT            NOT NULL DEFAULT '',

    -- Structured extracted sections stored as JSONB
    -- { "key_points": ["...", "..."], "actions": ["..."] }
    structured_json     JSONB           NOT NULL DEFAULT '{}',

    -- Array of message_ids included in this summary (stored as JSONB array)
    message_ids         JSONB           NOT NULL DEFAULT '[]',

    -- Relative path to the output file, e.g. "summaries/rocketchat/engineering/2026-07-05.md"
    output_path         TEXT,

    -- LLM model used
    model               TEXT            NOT NULL,

    -- Ollama generation time in seconds
    generation_seconds  REAL,

    -- Whether the LLM call succeeded
    success             BOOLEAN         NOT NULL DEFAULT TRUE,

    -- Error message if success = false
    error_message       TEXT,

    -- When this summary was created (UTC)
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_summaries_platform_channel_date
    ON summaries (platform, channel, summary_date);

CREATE INDEX IF NOT EXISTS idx_summaries_run_id
    ON summaries (run_id);

-- ────────────────────────────────────────────────────────────────────────────
-- schema_migrations — tracks applied migrations (for future schema changes)
-- ────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS schema_migrations (
    version         INTEGER         PRIMARY KEY,
    applied_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    description     TEXT
);

INSERT INTO schema_migrations (version, description)
    VALUES (1, 'Initial schema: messages and summaries tables')
    ON CONFLICT (version) DO NOTHING;
```

### Useful Query Examples

```sql
-- Find all summaries for a channel in the last 7 days
SELECT summary_date, message_count, left(summary_text, 200)
FROM summaries
WHERE platform = 'rocketchat' AND channel = 'engineering'
  AND summary_date >= CURRENT_DATE - INTERVAL '7 days'
ORDER BY summary_date DESC;

-- Find all messages in a thread
SELECT timestamp, author_name, body
FROM messages
WHERE platform = 'rocketchat' AND thread_id = 'abc123'
ORDER BY timestamp;

-- Count messages per channel per day (useful for monitoring ingestion)
SELECT platform, channel, timestamp::date AS day, COUNT(*) AS msg_count
FROM messages
GROUP BY platform, channel, day
ORDER BY day DESC, msg_count DESC;

-- Query the raw JSONB directly (Postgres advantage over SQLite)
SELECT message_id, raw_json->>'u'->>'name' AS author
FROM messages
WHERE platform = 'rocketchat' AND raw_json ? 'attachments';
```

---

## 13. Key Implementation Notes

### 13.1 Idempotency — Never Re-Summarize Already Processed Messages

The system achieves idempotency at two levels:

**Level 1 — PostgreSQL deduplication (archival):**
`INSERT INTO messages ... ON CONFLICT (platform, message_id) DO NOTHING` uses the composite primary key. Re-running after a crash or partial failure will skip already-inserted messages silently.

**Level 2 — Summary deduplication (summarization):**
Before calling Ollama, `main.py` checks the `summaries` table:
```python
existing = db.query(
    "SELECT id FROM summaries WHERE platform=%s AND channel=%s AND summary_date=%s AND success=TRUE",
    (platform, channel, today)
)
if existing and not config.output.overwrite_existing:
    logger.info("Summary already exists for %s/%s/%s — skipping", platform, channel, today)
    continue
```
This prevents burning Ollama inference time re-generating summaries that already exist on disk.

**Level 3 — State file prevents re-fetching:**
The state file stores the `timestamp` of the newest successfully processed message per channel. On next run, the API query uses `oldest=<that timestamp>`, so the API itself returns only new messages. This is the primary defense against redundant work.

### 13.2 Rate Limiting

**Rocket.Chat:**
The default Rocket.Chat rate limit is 200 requests per 60 seconds per IP. The source adapter uses a token bucket rate limiter (`utils/rate_limiter.py`) configured to stay well under this: 2 requests/second. With page_size=100, a channel with 500 messages requires 5 API calls — comfortably within limits.

```python
# src/utils/rate_limiter.py
import time
from threading import Lock

class TokenBucketRateLimiter:
    """Thread-safe token bucket rate limiter."""
    def __init__(self, rate: float, capacity: float):
        self.rate = rate          # tokens per second
        self.capacity = capacity  # max burst
        self._tokens = capacity
        self._last_check = time.monotonic()
        self._lock = Lock()

    def acquire(self):
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_check
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)
            self._last_check = now
            if self._tokens >= 1:
                self._tokens -= 1
                return
        time.sleep(1.0 / self.rate)  # Wait for a token
        self.acquire()
```

**Microsoft Graph API:**
The Graph API enforces per-app throttling. The response headers `Retry-After` and status code 429 indicate throttling. The `tenacity` library handles retries:

```python
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

@retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    retry=retry_if_exception(lambda e: getattr(e, "status_code", 0) == 429),
    reraise=True,
)
def _graph_get(self, url: str) -> dict:
    resp = self._client.get(url, headers=self._auth_headers())
    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", "10"))
        time.sleep(retry_after)
        resp.raise_for_status()
    resp.raise_for_status()
    return resp.json()
```

### 13.3 Error Handling Strategy

The summarizer follows a **fail-soft per channel** strategy: an error fetching or summarizing one channel does not abort the entire run. All other channels continue processing.

```python
# src/main.py (simplified)
failed_channels = []
for source_config in all_channels:
    try:
        messages = source.fetch(source_config, since=state.get(source_config.key))
        db.upsert_messages(messages)
        summary = summarizer.summarize(messages, source_config)
        writer.write(summary, source_config)
        state.update(source_config.key, messages[-1].timestamp)
    except FetchError as e:
        logger.error("Fetch failed for %s: %s", source_config.key, e)
        failed_channels.append(source_config.key)
        # Do NOT update state — next run will retry from same since_ts
    except SummarizationError as e:
        logger.error("Summarization failed for %s: %s", source_config.key, e)
        failed_channels.append(source_config.key)
        db.insert_summary(failed_summary_record(source_config, e))
        # Still update state — messages were archived, just not summarized
        state.update(source_config.key, messages[-1].timestamp)

# Exit 1 if any channel failed (useful for cron alerting)
sys.exit(1 if failed_channels else 0)
```

**Key rule:** The state file is only updated for a channel if its messages were successfully archived in PostgreSQL. If the API fetch fails midway, the state is NOT updated, so the next run retries from the last known-good timestamp.

### 13.4 Handling Large Message Volumes (Chunking)

If a channel has more messages than the LLM context window can handle in one prompt (e.g., after a long gap or first run), the prompt builder splits messages into chunks:

```python
# src/summarizer/prompt_builder.py
MAX_PROMPT_TOKENS = 6000  # Conservative limit; Llama 3.1 8B has 128K context
APPROX_CHARS_PER_TOKEN = 4

def build_prompts(config, channel, messages) -> list[str]:
    """Returns one or more prompts, each within the token budget."""
    chunks = []
    current_chunk = []
    current_len = 0
    for msg in messages:
        line = format_message(msg)
        if current_len + len(line) > MAX_PROMPT_TOKENS * APPROX_CHARS_PER_TOKEN:
            chunks.append(current_chunk)
            current_chunk = [msg]
            current_len = len(line)
        else:
            current_chunk.append(msg)
            current_len += len(line)
    if current_chunk:
        chunks.append(current_chunk)
    return [render_template(config, channel, chunk) for chunk in chunks]
```

When multiple chunks exist, each gets a separate Ollama call. The output writer then combines the partial summaries with a final "combine" prompt:

```
You are given multiple partial summaries of the same chat channel.
Merge them into a single coherent summary following the same format.

Partial summary 1:
{{ summary_1 }}

Partial summary 2:
{{ summary_2 }}
```

### 13.5 First-Run Bootstrap

On first run, there is no state for any channel, so the fetcher uses `lookback_hours` from config. For channels that have existed for years, this may return 0 messages (if `lookback_hours` is 24 and the channel was silent). This is intentional — the system is a forward-looking summarizer, not a historical importer. To backfill historical data, temporarily set `lookback_hours: 720` (30 days) in `config.yaml` and run once manually.

### 13.6 Teams Reply Chain Reconstruction

Microsoft Graph returns reply messages with `replyToId` set to the parent message's ID. However, when using `$filter=lastModifiedDateTime ge ...`, replies to old threads appear in the results even if the parent message is older than the lookback window. The Teams source adapter fetches the parent message if it's not in the current batch:

```python
# src/sources/teams.py (excerpt)
def _enrich_with_parent(self, messages: list[Message]) -> list[Message]:
    """Fetch parent messages for threads whose parent is not in current batch."""
    seen_ids = {m.message_id for m in messages}
    parents_needed = {
        m.thread_id for m in messages
        if m.thread_id and m.thread_id not in seen_ids
    }
    for parent_id in parents_needed:
        parent = self._db.get_message("teams", parent_id)  # Check archive first
        if not parent:
            parent = self._fetch_single_message(parent_id)
            self._db.upsert_messages([parent])
    return messages
```

### 13.7 Security Considerations

- **Secrets in environment variables only.** The `.env` file is gitignored. The `config.yaml` uses `${VAR}` references that are never expanded if the env var is missing (raises `ConfigError`).
- **Docker bind mounts are volume-only.** No secrets are baked into the Docker image.
- **PostgreSQL credentials are injected via environment variables only.** The Postgres password lives in `.env` (gitignored) and is passed to both the `postgres` and `app` containers via `env_file`. Never hardcode it in `config.yaml` or `docker-compose.yml`. The named Docker volume (`postgres-data`) is owned by the Postgres container and is not directly accessible on the host filesystem.
- **Ollama runs without auth by default.** If the host is multi-user, set `OLLAMA_HOST=127.0.0.1` so Ollama only binds localhost and is not accessible on the Docker bridge network from other containers.
- **LLM output is untrusted.** The Markdown writer uses Jinja2's `autoescape=False` since we want raw Markdown from the LLM. However, any user-provided data (message bodies) injected into the template uses `{{ message | e }}` (HTML-escaped) to prevent injection if the output is later rendered as HTML.
- **Rocket.Chat tokens do not expire** — rotate them quarterly and revoke immediately if the server logs show unexpected API activity.
- **Teams client secrets expire** — set a calendar reminder to rotate them before the configured expiry date (default 12–24 months in Azure).

### 13.9 Teams "Following" API Limitation

Microsoft's Graph API does not expose a "list threads I'm following in this channel" endpoint equivalent to Rocket.Chat's `chat.getThreadsList?type=following`. Teams' notification subscription model is webhook-based, not queryable. As a result, the Teams source adapter approximates "threads I care about" through three detectable signals:

- **Started:** top-level messages where `from.user.id == me.teams.user_id`
- **Mentioned:** messages whose `mentions` array contains the user's object ID, or whose body text contains `me.display_name`
- **Replied:** messages where `from.user.id == me.teams.user_id` and `replyToId` is set — indicating the user actively engaged in the thread

The practical effect is that Teams thread discovery may lag one run behind: a reply to a new thread will be seen in the same fetch that discovers the thread, but the thread isn't added to `tracked_threads` until eligibility is evaluated. The next run will then include it fully. This is a minor inconvenience, not a data loss risk — all messages are fetched into memory; the filtering step simply decides what to keep.

### 13.10 Thread Eligibility: Whole Thread, Not Just the Triggering Message

When any message in a thread triggers eligibility (a mention, a reply, a follow), the **entire thread** is tracked — including messages that predate the trigger. On first discovery, the adapter fetches the full thread history from the platform API:

- **Rocket.Chat:** `GET /api/v1/chat.getThreadMessages?tmid=<thread_id>` returns all messages in the thread
- **Teams:** `GET /teams/{teamId}/channels/{channelId}/messages/{thread_id}/replies` returns all replies; the parent is fetched separately

All historical messages are stored and included in the first `raw.md` and `summary.md`.

### 13.11 Cumulative `raw.md` and Append Strategy

`raw.md` is opened in append mode (`'a'`) when new messages arrive. A comment line is inserted before each new batch to record the date it was appended:

```markdown
<!-- appended 2026-06-16T07:30:01Z -->
```

This makes it easy to see the history of when the summarizer ran and what it saw. The file must never be truncated or overwritten — it is the source of truth for audit purposes. `summary.md`, by contrast, is always overwritten with a complete re-summary of the full thread (sourced from Postgres, not from `raw.md`).

### 13.12 `needs_resummary` Lifecycle

```
Thread first tracked:          needs_resummary = FALSE (no messages yet)
New messages stored:           needs_resummary = TRUE
Ollama summarization succeeds: needs_resummary = FALSE
Ollama summarization fails:    needs_resummary = TRUE  (retries next run)
Thread aged out:               needs_resummary unchanged (skipped in summarization)
```

This means a thread that was waiting for re-summarization when it aged out will still have `needs_resummary = TRUE`. The `--prune` deletion step clears it as part of removing the entire row.

### 13.13 Age-Out and the --prune Command

The age-out mechanism is deliberately conservative. The `flag_aged_out_threads()` function only changes `status` — it never deletes data. This means:

- Cron runs are always safe: no data loss can occur from an automated run
- Aged-out threads appear in the warning log but continue to occupy disk and DB space until `--prune` is run
- `--prune` requires a TTY (run with `docker compose run --rm -it app --prune`) and prints the full list of threads to be deleted before asking for confirmation
- If the user answers N (or presses Ctrl-C), nothing is deleted

To re-activate an aged-out thread manually (e.g., it went quiet and then came back to life):
```sql
UPDATE tracked_threads
   SET status = 'active', last_activity = NOW()
 WHERE platform = 'rocketchat' AND thread_id = '<id>';
```
The next normal run will pick it up again.

### 13.14 Extending to Additional Sources

The `ChatSource` abstract base class makes it straightforward to add new platforms:

```python
# src/sources/base.py
from abc import ABC, abstractmethod
from archive.models import Message

class ChatSource(ABC):
    @abstractmethod
    def fetch(self, channel_config, since_ts: str | None) -> list[Message]:
        """Fetch messages newer than since_ts. Return sorted ascending."""
        ...

    @abstractmethod
    def build_deep_link(self, message: Message) -> str:
        """Return a URL that opens the message in the platform's UI."""
        ...
```

Adding Slack, Discord, or Matrix would involve creating `src/sources/slack.py` implementing this interface, adding a `slack:` section to `config.yaml`, and registering the source in `main.py`'s source registry.

---

*End of document.*
