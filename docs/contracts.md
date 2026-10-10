# Wire contract

Architecture authority: [design.ai/tts](https://github.com/lambdawalker/design.ai/tree/main/tts).
Concrete inputs/responses: [generated OpenAPI](../schemas/openapi.json) and
[request JSON Schemas](../schemas/requests.json).
Consumer: [python.tts.api.client](https://github.com/lambdawalker/python.tts.api.client).

| Method | Path | Result |
|---|---|---|
| GET | `/v1/models` | `{api_version, default, models}` |
| GET | `/v1/capabilities?model=...` | Effective profile capabilities; ETag |
| GET | `/v1/guidance/{feature}?model=...` | Revisioned guidance; ETag |
| POST | `/v1/assets` | 201 asset metadata; multipart `file` |
| GET | `/v1/assets/{id}` | Authorized audio download |
| GET | `/v1/voices?model=...` | `{voices}` |
| POST | `/v1/voices` | 201 registered voice |
| DELETE | `/v1/voices/{id}` | 204; retains shared assets |
| POST | `/v1/speech/validate` | `{valid, model, guidance_revision}` |
| POST | `/v1/speech` | 202 job |
| POST | `/v1/voice-designs` | 202 job with candidate audio results |
| POST | `/v1/voice-conversions` | 202 job or unsupported-feature error |
| GET | `/v1/jobs/{id}` | Job snapshot |
| GET | `/v1/jobs/{id}/events` | SSE with numeric per-job IDs |
| POST | `/v1/jobs/{id}/cancel` | Current job state and cancellation flag |

All routes exist even when an adapter does not support an operation. Known unsupported
features return 422 `unsupported_feature`. Unknown routes return 404. Execution failure
is stored on the job; retrieving that failed job still returns HTTP 200.

## Stable behavior

Inputs forbid unknown fields, use strict types and retain caller text exactly. Defaults
match the client: model `default`, WAV output, empty extensions. A voice selector contains
exactly one nonempty `id` or `alias`. Model profile IDs may contain slashes in JSON/query
parameters. Server-generated resource IDs are safe opaque path segments.

Capabilities and guidance ETags include resolved model identity and revision. Supplying a
stale speech `guidance_revision` fails before queueing. Unsupported languages, instructions,
formats, sample rates, extension namespaces and native constraints fail before admission.
Validation checks requests without synthesis; it does not predict acoustic adherence.

Submission may include `Idempotency-Key` (1–256 visible ASCII characters without spaces).
Keys are scoped by caller and operation. Defaults and omitted nulls are canonicalized.
The same normalized request returns the original job, including after a server restart,
voice deletion, or default model change, while the key remains retained. Changed payloads
return 409 `idempotency_conflict`. Never automatically generate with a new key after a
network failure. After the advertised retention period, a key may create new work.

Job states: queued, running, succeeded, failed, cancelled. Stage and nullable progress are
separate. Event payloads are public job snapshots; IDs increase monotonically per job.
Reconnect with `Last-Event-ID` to receive only later retained events. A stale cursor gets
409 `event_history_expired` and a message containing the status URL. Streams end after
terminal events; poll status if a network connection ends unexpectedly. Disconnecting
HTTP, SSE or MCP never requests cancellation.

Result entries contain `asset_id`, MIME type, sample rate, channels, duration, size,
expiry and provenance. Audio is downloaded separately and never returned as routine
base64 tool content. Ownership checks apply identically to every route.

Errors use `{error: {code, message, field?, retryable, guidance_url?}}`.
The server adds `alias_conflict`, `server_unavailable`, `inference_failed` and
`internal_error` to the central design's representative codes.

## MCP

Install the `mcp` extra and enable `--mcp`. The official Python MCP SDK 1.x supplies
stateless Streamable HTTP at `/mcp`. The tools are:

`list_models`, `get_capabilities`, `get_guidance`, `list_voices`, `clone_voice`,
`delete_voice`, `validate_speech`, `generate_speech`, `design_voice`, `convert_voice`,
`get_job`, `cancel_job`.

Inputs are flat fields from the same request schemas; submission tools add
`idempotency_key`. Resource tools use `job_id` and `voice_id`. `clone_voice` registers
references; it does not fine-tune or synthesize. Upload references over HTTP first.
Domain failures use `isError=true` with the same structured error payload. Essential
constraints are in tool schemas; full model guidance is retrieved on demand. MCP tools
call Service in-process, not the public HTTP API.

## Anonymous sessions

In `--anonymous-sessions` mode, unauthenticated `POST /v1/sessions` (empty body or `{}`)
returns 201 with `session_id`, secret `access_token`, `token_type: "Bearer"` and ISO
`expires_at`. Responses carry `Cache-Control: no-store`. All other requests require
that bearer token, including `/mcp`. `DELETE /v1/sessions/current` returns 204 and
revokes the current session. Unknown/expired/revoked tokens receive 401; cross-session
resources return the existing not-found errors. Session IDs are not credentials.
