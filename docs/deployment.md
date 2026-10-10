# Deployment and operations

Run one server process per data directory and inference device. A file lock rejects a
second process using the same state. Do not use multiple Uvicorn workers. Different
engines should run in separate environments and use separate data directories/ports.
The server imports engine code only through explicitly configured adapter factories.

## Authentication

The CLI defaults to unauthenticated loopback development. In that mode every request
belongs to caller `local`. To bind elsewhere with authentication, configure bearer tokens:

```bash
export TTS_API_TOKENS='{"replace-with-a-strong-secret":"application-a"}'
uv run tts-api-server --adapter my_engine.tts_adapter:create_adapter --host 0.0.0.0 --mcp
```

Use a protected environment/secret manager; the literal above is a placeholder. The
map is token -> stable caller ID. Rotating a token while retaining its caller ID keeps
access to existing resources. Multiple tokens can identify the same caller. Restart
to reload configuration. Tokens are never part of model request schemas or provenance.

This is a static bearer deployment profile, not an OAuth authorization server. MCP
hosts must support explicitly configured bearer headers. HTTP and MCP enforce the
same caller ownership, including result download links. Jobs, assets and voices owned
by another caller return 404. Discovery and preset voices are common to the deployment.

Use TLS at a reverse proxy for remote deployment. Forward authorization headers,
disable proxy buffering for SSE, and allow long SSE connections and 15-second
heartbeats. Do not forward caller credentials to other hosts. `create_app` embedders
are responsible for bind/TLS choices; the loopback guard applies to the CLI.

## Storage and retention

The data directory contains `state.sqlite3`, its WAL files, `server.lock`, and opaque
asset files. Back up the directory with SQLite-consistent tooling or while stopped.
Never edit live database records or replace asset files from outside the service.

Defaults:

| Setting | Default | CLI option |
|---|---|---|
| Waiting queue | 32 jobs (plus one running) | `--max-queue` |
| Uploaded/output audio | 32 MiB per asset | `--max-upload-bytes` |
| JSON/MCP request body | 1 MiB | `Settings.max_request_bytes` |
| WAV/FLAC duration | 600 seconds; at most 8 channels | `Settings.max_audio_duration` |
| Asset lifetime | 24 hours | `--asset-ttl` |
| Event lifetime | 24 hours | `--event-ttl` |
| Event count | Last 1,000 per job | `--event-limit` |
| Idempotency key lifetime | 24 hours | `--idempotency-ttl` |

Capabilities publish configured retention. Asset metadata/results include `expires_at`.
Expired asset reads return 410 `asset_expired`. Active jobs retain access to the exact
references accepted at submission even if those assets expire or their voice is deleted.
Registration does not extend the source asset lifetime; upload/register fresh references
when needed. Idle cleanup deletes expired unpinned binary assets and old events/keys.
Job metadata and asset tombstones are retained; operators must monitor database/disk
usage. This version has no administrative purge API or per-caller storage quota.

Restart preserves completed results but marks queued/running jobs failed with
`server_restarted`; it does not resume or resubmit inference. Graceful shutdown waits
for the running native operation and leaves queued work to the documented restart policy.
A forcibly terminated native process is recovered on the next startup.

## Cancellation and progress

Queued jobs cancel immediately. Running jobs expose `cancellation_requested=true` and
remain running until the adapter returns at a safe boundary. A late cancellation of a
terminal job returns its existing state. Adapters can inspect cancellation signals.
Uninterruptible native inference may delay cancellation and graceful shutdown indefinitely.

SSE reports lifecycle stages, not audio chunks. Progress is null unless an adapter emits
a measured percentage. This first adapter API returns complete encoded audio; incremental
audio transport and duplex sessions are not implemented and must not be advertised.

## Resource boundaries

Request bodies are bounded before multipart parsing. Uploads are multipart field `file`;
container content is inspected rather than trusting filename/MIME. Public APIs do not
accept server paths, remote audio URLs, embeddings or serialized Python objects.
Decode/resample inside an adapter only when declared, and record transformations.
There is no built-in ASR, reference editing, automatic model download or voice fallback.

## Token-free LAN access

Explicitly pass `--no-auth` to allow requests without tokens on a network interface:

```bash
tts-api-server --adapter your_package.adapter:create_adapter --host 0.0.0.0 --no-auth --mcp
```

This flag overrides `TTS_API_TOKENS`, even if it is set. HTTP, MCP, events and audio
downloads all use the shared `local` caller. Anyone who can reach the listening port
can use the API and access that caller's jobs, voices and assets. The flag does not
restrict connections to private IP addresses; network reachability determines access.
Remove the flag and configure tokens to restore authentication. Without the flag,
the existing loopback-only rule for token-free startup still applies.

## Anonymous sessions

Run with `--anonymous-sessions` for public admission with isolated resources:

```bash
tts-api-server --adapter your_package.adapter:create_adapter --host 0.0.0.0 --anonymous-sessions --mcp
```

Unset `TTS_API_TOKENS` in this mode. It cannot be combined with `--no-auth` or static
tokens. POST `/v1/sessions` without credentials, then send the returned `access_token`
as a bearer credential on all other HTTP/MCP requests. DELETE `/v1/sessions/current`
revokes that credential. The token is returned only at creation; save it privately
if you need to resume after a client restart. Only token hashes are stored server-side.

`--session-ttl` sets an absolute lifetime in seconds (default 86400).
`--max-sessions` limits active sessions (default 10000); admission at capacity returns
429. Restarting the server preserves unexpired sessions. Expiration/revocation prevents
new requests and closes job SSE, but does not cancel accepted jobs or ongoing downloads.
All jobs, voices, assets and idempotency keys are scoped to the session. Losing the token
or letting it expire loses access; a newly created session cannot recover old resources.

See the [central session contract](https://github.com/lambdawalker/design.ai/blob/docs/tts-implementation-sources/tts/sessions.md).
