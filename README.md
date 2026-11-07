# Notegram

Python logging → HTTPS ingestion → durable SQLite storage → Telegram.

Notegram 2 keeps the Python logging interface and moves database access entirely
onto the server. The client and server use Python 3.11+ and have **no runtime
third-party dependencies**. PostgreSQL is needed only to import a version 1 database.

The implementation is split into two subpackages in one distribution:

- `notegram.client`: logging handler and client configuration. Importing it never
  loads server code, SQLite, PostgreSQL, or Telegram libraries.
- `notegram.server`: ingestion API, database, Telegram bot, CLI, and migration.

`pip install notegram` installs no runtime dependencies. Neither subpackage needs
`python-telegram-bot`; the server talks to Telegram over its HTTP API using the
standard library. Only the optional PostgreSQL migration extra adds a dependency.
Configuration discovery, HTTP transport, and protocol limits are shared modules.

## Install

```sh
pip install --upgrade git+https://github.com/hiaoxui/notegram.git
```

For development in this repository:

```sh
.venv/bin/python -m pip install -e .
```

## Client

Set the server base URL and the shared authentication token:

```sh
export NOTEGRAM_URL=https://logs.example.com
export NOTEGRAM_TOKEN=your-shared-token
```

Then use ordinary Python logging:

```python
import logging
from notegram import NotegramHandler

handler = NotegramHandler()
logger = logging.getLogger("myapp")
logger.setLevel(logging.INFO)
logger.addHandler(handler)

logger.warning("Disk space is low: %s%%", 5)
try:
    raise RuntimeError("example")
except RuntimeError:
    logger.exception("Job failed")  # Includes the traceback.

handler.flush()  # Bounded wait; returns False if work is still pending.
handler.close()  # Call when shutting down, especially in short-lived programs.
```

`from notegram.client import NotegramHandler` is the explicit client import.
The original top-level import and `from notegram.handler import NotegramHandler`
remain supported for existing applications and logging configurations.

`NotegramHandler(cfg_path=None)` and its positional config-directory argument
remain supported. An explicit file path works too. Constructor keyword arguments
(`url`, `token`, `queue_size`, `timeout`, `retries`, `flush_timeout`, `level`) override
configuration. Standard handler methods such as `setLevel()` and `addFilter()` work.
The logger name remains the subscription domain.

Plain text is the default; `<`, `>`, and `&` are sent literally. For Markdown:

```python
logger.warning("*Something happened*", extra={"notegram_format": "markdown"})
logger.warning("MARKDOWN *Something happened*")  # Legacy syntax still works.
```

Markdown uses Telegram's legacy `Markdown` mode. If Telegram rejects it, the
message is retried as plain text. Long messages are split conservatively by UTF-16
length. Formatting that crosses chunk boundaries may fall back to plain text.
Messages include a domain/level header so multiple subscriptions are distinguishable.

### Client behavior

- `emit()` snapshots the message and traceback into a bounded queue; it performs
  no network I/O. One sender thread per handler performs HTTP requests.
- Default queue size: 1,000. When full, the newest record is dropped.
- Default request timeout: 5 seconds; 3 retries after the initial attempt, with
  exponential backoff. Timeouts, connection failures, HTTP 408/425/429 and 5xx are
  retryable. Authentication and other permanent request failures are not retried.
- The same event ID is reused on every retry. The server acknowledges only after
  committing, and deduplicates retries against persisted event contents.
- `handler.sent`, `handler.failed`, `handler.dropped`, and `handler.last_error`
  expose local outcomes. Failures do not log through the handler itself.
  `sent` means stored by Notegram, not delivered by Telegram.
- `flush()` waits up to `flush_timeout` (default 5 seconds). `close()` stops accepting
  records, performs a bounded flush, and asks the daemon sender to stop. An in-flight
  request can continue until its request timeout. Remaining queued work is discarded.
- The client queue is in memory: process crashes, exhausted retries, queue overflow,
  and shutdown deadlines can lose events. This is best-effort client transport.
- Without a token the handler is disabled and emits a Python warning. Invalid
  configuration raises `ConfigError` instead of silently hiding a typo.
- HTTPS certificates are verified using Python's system trust store. HTTP is
  accepted only for loopback endpoints. Redirects are rejected so bearer credentials
  cannot be forwarded to a different endpoint.
- Create handlers after forking a process; each process needs its own sender thread.
  Importing the library never creates log files or configures application logging.

## Configuration

A client TOML file can contain just:

```toml
[notegram]
url = "https://logs.example.com"
token = "your-shared-token"
```

Configuration tables merge in this order, from lowest to highest priority:

1. `/etc/notegram.toml` and `/etc/notegram/*notegram*.toml`.
2. `~/walless.config.d/*notegram*.toml` (legacy).
3. `~/.config/*notegram*.toml` (legacy), then
   `$XDG_CONFIG_HOME/notegram/*notegram*.toml` (default `~/.config/notegram`).
4. `$WALLESS_ROOT/walless.config.d` and `$WALLESS_CONFIG`, when set (legacy).
5. `*notegram*.toml` in the current directory.
6. The file or directory selected by `NOTEGRAM_CONFIG`, or `cfg_path` / `--config`.
7. Environment variables.
8. Explicit client constructor arguments.

Files within a directory merge in alphabetical order. Unrelated TOML files and
`*.example.toml` / `*.sample.toml` templates are ignored during discovery;
configuration belongs under `[notegram]`. Relative database and certificate
paths resolve against the process working directory. An explicitly selected missing
path is an error. See [notegram.example.toml](notegram.example.toml) for all settings.

| Environment variable | TOML setting |
| --- | --- |
| `NOTEGRAM_CONFIG` | Selected configuration file or directory |
| `NOTEGRAM_URL` | `notegram.url` |
| `NOTEGRAM_TOKEN` | `notegram.token` |
| `NOTEGRAM_HOST` | `notegram.server.host` |
| `NOTEGRAM_PORT` | `notegram.server.port` |
| `NOTEGRAM_DATABASE` | `notegram.server.database` |
| `NOTEGRAM_TLS_CERT` | `notegram.server.tls_cert` |
| `NOTEGRAM_TLS_KEY` | `notegram.server.tls_key` |
| `NOTEGRAM_TELEGRAM_TOKEN` | `notegram.telegram.token` |

There is **one Notegram authentication token**, shared by clients and `/link`.
It grants access to every domain; this is a trusted single-owner service, not a
multi-tenant authorization system. The server additionally needs the Telegram bot
credential issued by BotFather. That credential never belongs on clients.

## Run the server

Create a bot using BotFather, then create a server config:

```toml
[notegram]
token = "your-shared-token"

[notegram.server]
host = "127.0.0.1"
port = 8765
database = "/path/to/notegram.sqlite3"

[notegram.telegram]
token = "your-botfather-token"
```

Generate a shared token with:

```sh
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Start ingestion, Telegram command polling, and the delivery worker together:

```sh
.venv/bin/python -m notegram server --config /path/to/notegram.toml
```

The server also has its own entry points:

```sh
.venv/bin/python -m notegram.server --config /path/to/notegram.toml
notegram-server --config /path/to/notegram.toml
```

The legacy `python -m notegram bot` command starts the same complete service.
`notegram server --api-only` runs ingestion without Telegram, useful for local tests.
Without a Telegram bot token the server also runs ingestion alone. A shared Notegram
token is always required. SIGINT and SIGTERM stop the service gracefully.

Run **one server process per database**. SQLite connections are short-lived and
transactions end before Telegram network requests begin. WAL mode lets HTTP
requests and the two bot workers share the database. The delivery worker is designed
for one process; it does not coordinate competing workers or machines.

### HTTPS deployment

The default listener is loopback HTTP. Put a reverse proxy such as Caddy in front
for remote clients, with public DNS pointing at the server:

```caddyfile
logs.example.com {
    request_body {
        max_size 1MB
    }
    reverse_proxy 127.0.0.1:8765
}
```

The proxy should terminate TLS and enforce connection/rate limits; the standard
library listener is intended to sit behind it. Alternatively, configure both
`notegram.server.tls_cert` and `tls_key` and an appropriate listening host for TLS
in the Python process. Do not expose the default plaintext listener publicly.

[python-notegram.service](python-notegram.service) is a systemd template for this
checkout. Adjust the user, interpreter, working directory, config, and database
paths for your deployment. Installing the service is a separate deployment step.

### Telegram commands

The bot provides a private inline-button management panel. In a private conversation:

```text
/auth YOUR_SHARED_TOKEN
/links
```

The panel lets you:

1. Choose a chat, with pagination when needed. Existing links appear even if the
   bot has not yet recorded that chat's name.
2. View its linked loggers and minimum severity levels.
3. Open a logger to see pending/failed delivery counts, then select DEBUG, INFO,
   WARNING, ERROR, or CRITICAL for future events.
4. Choose **Add logger**, select a known logger, and confirm linking at WARNING.
   Known loggers come from stored events and existing links. Send a first event or
   use the legacy `/link` command to introduce a new logger name.
5. Choose **Unlink…** and confirm. Unlinking deletes queued deliveries for this
   chat/logger pair; stored history remains. Cancel leaves the link intact.

To manage a group, add the bot and send `/links` there. This registers its chat ID
and title; manage its routing from the private panel without posting your token to
the group. Adding a bot alone does not register a group: it must receive a command.
The bot registers common commands in Telegram's command menu when polling starts.

Anyone who authenticates with the shared token can manage **all known chats**.
Authentication is tied to the Telegram user, survives server restarts, and can be
revoked with `/logout` or by rotating the shared token and restarting the server.
The bot attempts to delete `/auth` messages containing credentials; deletion can
fail, so keep this exchange private. Credentials are never echoed in replies.
Menu buttons are bound to their authenticated user and private chat, contain only
opaque references, and expire after 15 minutes or a server restart. Open `/links`
again if an old menu expires. Menus edit the same message as you navigate.

The text commands remain available:

Start a private conversation with your bot, then subscribe using the exact logger name:

```text
/link myapp YOUR_SHARED_TOKEN
/link myapp YOUR_SHARED_TOKEN error
```

The default threshold is warning (30). New subscriptions receive future events;
use `/pull` to retrieve earlier history. Repeating `/link` updates the threshold.
Keep `/link` private because the token grants access to all domains. Linking a group
is supported, but places the token in that group's command history.

| Command | Behavior |
| --- | --- |
| `/ping` | Reply `pong` |
| `/help` | Show command syntax |
| `/auth TOKEN` | Authenticate for global link management, in private only |
| `/links`, `/chats` | Open the private chat/logger management panel |
| `/logout` | Revoke this user's management access without changing links |
| `/subscriptions` | List this chat's domains and thresholds |
| `/level DOMAIN LEVEL` | Update an existing subscription |
| `/unlink DOMAIN` | Remove this chat's subscription and queued deliveries |
| `/pull [SECONDS]` | Retrieve history for this chat's subscribed domains |
| `/debug`, `/info`, `/warning`, `/error`, `/critical [SECONDS]` | Retrieve one level |

Levels accept names or integers from 0 to 100. History defaults to the last hour;
lookback is limited to 30 days and results to the latest 100 events, returned in
chronological order. History requests do not alter automatic delivery state.
In groups, `/level` and `/unlink` require an authenticated management user; other
members can view the logs already linked to that group. In private chats, the
legacy commands still control that chat's own subscriptions.

### Delivery and maintenance

Each accepted event creates a persisted delivery for each matching subscription.
No recent-time window or process-local dictionary is used to decide what to send.
Restarts and long downtime retain pending events. Each successful message chunk is
checkpointed before the next chunk is sent.

Sending observes a shared limit and per-chat spacing, including more conservative
group pacing. Telegram's `retry_after` delays and temporary failures are retried
with persisted backoff. Permanent failures (for example, a blocked bot) are retained
as failed rows for inspection. Polling runs separately and stores its update offset
after processing a command. A permanent command-reply failure is skipped so one
blocked chat cannot stop commands from every other chat.

Telegram delivery is **at least once for retryable failures**, with a possible duplicate
if the process crashes after Telegram accepts a chunk but before its checkpoint commits.
Permanent Telegram failures require intervention. Command replies/history are not
persisted as deliveries; a failed history command may replay already sent chunks.

After fixing a permanent failure, requeue failed deliveries:

```sh
notegram retry-failed --config /path/to/notegram.toml
```

Retention is explicit, with no automatic deletion:

```sh
notegram prune --days 30 --config /path/to/notegram.toml
```

Pruning removes old events only if they have no pending or failed deliveries.
Back up the SQLite database with SQLite's backup API or while the service is stopped;
copying only the main file during WAL writes can miss committed events.

## Upgrade from Notegram 1

1. Stop the old bot. Preserve PostgreSQL and its configuration.
2. Install version 2 and optionally `pip install 'notegram[migration]'`.
3. Keep the existing `notegram.toml` for import. Its `[notegram.db]` table is used
   only by the explicit import command. Version 2 does not write to PostgreSQL.
4. Select a server SQLite database under `[notegram.server]`, then run:

   ```sh
   notegram import-postgres --config /path/to/notegram.toml
   ```

   Import uses a read-only PostgreSQL snapshot and one SQLite transaction. Both
   legacy `domain` and `domain_id` foreign-key column variants are supported.
   Events use deterministic IDs, so repeating an import does not duplicate history.
   Subscriptions are copied; historical messages are not automatically delivered.
   A repeat import reapplies legacy subscription thresholds.
5. Configure `[notegram].token` or `NOTEGRAM_TOKEN`. When neither is present,
   legacy `[notegram.telegram].secret` is accepted as the single shared token.
6. Start the new server. Upgrade clients and give them its URL and shared token.
   Existing `NotegramHandler()` calls continue to work. Without a URL, upgraded
   clients default to `http://127.0.0.1:8765`; remote clients must set their server URL.
7. Once migration is verified, remove database credentials and the Telegram bot
   credential from client machines. Keep the bot credential only on the server.

This is a major version: the Python logger interface is preserved, but the old
wire/storage protocol is replaced. Unupgraded clients still write PostgreSQL;
coordinate their upgrade and a final import before retiring that database.
`Storage` is now a server-internal SQLite class, not a public client interface.
The destructive version 1 `dbinit.sql` is no longer used.

## Ingestion API

`POST /v1/events` with `Authorization: Bearer TOKEN` and JSON:

```json
{
  "id": "client-generated-unique-id",
  "timestamp": 1793977200.0,
  "domain": "myapp",
  "level": 30,
  "message": "Disk space is low",
  "format": "plain"
}
```

`timestamp` is a finite Unix timestamp in seconds. `id` and `domain` are 1–100
characters without control characters. `level` is 0–100, the message is nonempty
and at most 128 KiB in UTF-8, and `format` is `plain` (default) or `markdown`.
Request bodies are capped at 1 MiB, including JSON escape overhead. Unknown fields
are rejected. Event IDs must be unique across all clients.

- `201`: committed; `{"id": "...", "duplicate": false}`.
- `200`: identical event already committed; `duplicate` is `true`.
- `400`: malformed event; `401`: wrong/missing token; `409`: ID/content conflict.
- `413`: body limit; `415`: wrong content type; `503`: storage unavailable.

`GET /healthz` checks database availability without exposing event contents or config.
A healthy ingestion endpoint does not imply Telegram is reachable or delivery is current.

## Tests

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests use temporary SQLite databases, a loopback HTTP server, and fake Telegram
responses. They load the repository's local `notegram.toml` when present to verify
legacy configuration compatibility, without printing credentials or contacting its
PostgreSQL/Telegram services. They cover auth, duplicate/conflicting events, logging
tracebacks, queue overflow, retries, durable delivery, chunk restart recovery,
Telegram rate limits, command authorization, and persisted polling offsets.

`test_notifier.py` is a manual smoke check that sends a real notification only when
run explicitly. It is not run as part of the automated test suite.

### Troubleshoot a remote client

Copying the server TOML to a client does not set a remote endpoint. The client reads
`[notegram].url`, not `[notegram.server].host`, and defaults to
`http://127.0.0.1:8765` when no URL is set. For a remote server, add its HTTPS base URL:

```toml
[notegram]
url = "https://logs.example.com"
token = "your-shared-token"
```

`~/.config/notegram.toml` is supported. A TOML in the current directory overrides
settings it also contains; environment variables take precedence over both.
You can explicitly select a config or override the endpoint for the smoke check:

```sh
python test_notifier.py --config ~/.config/notegram.toml
NOTEGRAM_URL=https://logs.example.com python test_notifier.py
```

The smoke check prints the resolved endpoint, makes one attempt, and distinguishes
HTTP errors, connection refusal, DNS failure, timeout, and TLS verification failure.
Acceptance confirms storage on the server; Telegram delivery additionally requires
linking the `notegram_test` logger to a chat.

An HTTP 403 may come from a reverse proxy or access-control layer: Notegram's
ingestion endpoint returns 401 for an invalid token and does not generate 403.
Cloudflare error 1010 can reject Python urllib's default User-Agent. The transport
sends `User-Agent: notegram/2.0` to identify the application, following
[Cloudflare's guidance](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-1xxx-errors/error-1010/).
If an updated client is still blocked, inspect Cloudflare Security Events and the
proxy configuration. The upstream must match the server's actual listening port.

If the server only has the default loopback HTTP listener, use an SSH tunnel until
you configure an HTTPS reverse proxy:

```sh
ssh -N -L 8765:127.0.0.1:8765 USER@SERVER
```

Keep that tunnel running and use `NOTEGRAM_URL=http://127.0.0.1:8765` in another
terminal. The remote server's loopback listener is reached through SSH.
