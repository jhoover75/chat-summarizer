# Test Suite

## Test categories

The suite has two categories of tests, with different setup requirements:

**Unit tests** (`test_config.py`) require no external services. They use pytest's built-in `tmp_path` and `monkeypatch` fixtures to exercise config loading, env-var expansion, and default values entirely in-process. These always run.

**Integration tests** (`test_archive.py`) connect to a live PostgreSQL instance. If the database is unreachable the tests skip automatically rather than fail, so it is safe to run `pytest` without Postgres and only see the unit tests execute.

**Mock tests** (`test_rocketchat.py`, `test_teams.py`) are stubs pending implementation with an httpx mock library (`pytest-httpx` or `respx`). They contain no assertions yet and will be skipped or pass trivially until filled in.

---

## Prerequisites

Install the project dependencies and pytest into your environment:

```bash
pip install -r requirements.txt pytest
```

All commands below should be run from the **project root** (`chat-summarizer/`), not from inside `tests/`.

---

## Running the unit tests

No external services required:

```bash
pytest tests/test_config.py -v
```

---

## Running the integration tests

`test_archive.py` needs a PostgreSQL 16 instance with a database and user that the tests can connect to. The tests create and tear down their own rows within that database — they do **not** create or drop the database itself.

### Option A — start a throwaway Postgres container

The quickest path if you are not running Docker Compose:

```bash
docker run --rm -d \
  --name chat-summarizer-test-pg \
  -e POSTGRES_DB=test_chat_summarizer \
  -e POSTGRES_USER=summarizer \
  -e POSTGRES_PASSWORD=test \
  -p 5432:5432 \
  postgres:16-alpine
```

Wait a few seconds for Postgres to become ready, then run the tests:

```bash
POSTGRES_HOST=localhost \
POSTGRES_DB=test_chat_summarizer \
POSTGRES_USER=summarizer \
POSTGRES_PASSWORD=test \
pytest tests/test_archive.py -v
```

Stop and remove the container when done:

```bash
docker stop chat-summarizer-test-pg
```

### Option B — reuse the Docker Compose Postgres service

If you already have the project's normal `postgres` service running (`docker compose up -d postgres`), you can run the integration tests against it directly. The Compose service is exposed on `localhost:5432`.

Create a separate test database so test data is isolated from any real data:

```bash
docker compose exec postgres \
  psql -U "${POSTGRES_USER}" -c "CREATE DATABASE test_chat_summarizer;"
```

Then run the tests:

```bash
POSTGRES_HOST=localhost \
POSTGRES_DB=test_chat_summarizer \
POSTGRES_USER="${POSTGRES_USER}" \
POSTGRES_PASSWORD="${POSTGRES_PASSWORD}" \
pytest tests/test_archive.py -v
```

---

## Setup and teardown details

### Per-test cleanup

`test_archive.py` uses a `db` pytest fixture that:

1. Opens a connection and calls `init_db()` to apply the schema idempotently (`CREATE TABLE IF NOT EXISTS`).
2. Yields the connection to the test.
3. After the test returns, deletes all rows from `tracked_threads`, `summaries`, and `messages` and closes the connection.

Each test therefore starts with empty tables and leaves no residue. The schema itself (table definitions) persists for the lifetime of the database, which is fine — `init_db()` is safe to call on an already-initialised database.

### Database-level teardown

To fully remove the test database (e.g. to reset between major schema changes):

```bash
# If using Option A (standalone container): just stop the container — it was started with --rm
docker stop chat-summarizer-test-pg

# If using Option B (Compose Postgres):
docker compose exec postgres \
  psql -U "${POSTGRES_USER}" -c "DROP DATABASE test_chat_summarizer;"
```

---

## Running the full suite

Run all tests at once. Integration tests skip automatically if Postgres is not configured:

```bash
pytest tests/ -v
```

To run only tests that do **not** require Postgres (useful in CI without a database service):

```bash
pytest tests/ -v --ignore=tests/test_archive.py
```

To run a single test by name:

```bash
pytest tests/test_archive.py::test_flag_aged_out_threads -v
```

---

## Environment variable reference

| Variable | Default used by tests | Purpose |
|---|---|---|
| `POSTGRES_HOST` | `localhost` | Hostname of the test Postgres instance |
| `POSTGRES_PORT` | `5432` | Port |
| `POSTGRES_DB` | `test_chat_summarizer` | Database name |
| `POSTGRES_USER` | `summarizer` | Database user |
| `POSTGRES_PASSWORD` | `test` | Database password |

These can be set in your shell, in a `.env.test` file sourced before running pytest, or passed inline as shown in the examples above.

---

## Future: Rocket.Chat and Teams mock tests

`test_rocketchat.py` and `test_teams.py` are currently stubs. They will be implemented using an httpx-compatible mock library so the source adapters can be tested without a live server. Fixture data (representative API responses) already lives in `tests/fixtures/`:

- `fixtures/rocketchat_messages.json` — sample `channels.messages` response
- `fixtures/teams_messages.json` — sample Graph API channel messages response

To add the mock library when implementing these tests:

```bash
pip install pytest-httpx   # or: pip install respx
```
