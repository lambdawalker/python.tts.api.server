# Validation and boundaries

Tests exercise real SQLite storage and encoded audio, HTTP through FastAPI, loopback
Uvicorn with the existing synchronous/asynchronous SDK, and the official MCP client.
A subprocess exits abruptly to verify recovery of interrupted persisted jobs.

Coverage includes request preservation, unknown fields, model/language/native controls,
voice reference requirements, asset ownership, voice snapshots, queue limits, duplicate
submissions/conflicts, cooperative cancellation, event replay/expiration, completed results
across restart, interrupted job failures, adapter errors, authentication, bounded uploads,
ETags, client downloads and MCP domain error parity.

Run all tests with the client checkout:

```bash
TTS_CLIENT_SOURCE=../python.tts.api.client uv run pytest
```

Without the client installed or this environment variable, only the SDK integration test
is skipped. CI explicitly supplies the pinned checkout. MCP tests require the `mcp` extra.
Tests do not download weights, validate DGX Spark/CUDA, benchmark performance, or prove
real speech quality. Engine adapters require their own actual-hardware smoke tests.

The initial server uses one worker and a small-deployment SQLite store, not a distributed
scheduler. It has no native streaming audio transport, conversational sessions, OAuth
server, model installer, fine-tuning endpoint, or automatic input transformation.

## Initial verification (2026-10-09)

26 tests passed on Python 3.12, including the existing client at commit
`2b9188940eb3d763ab7aeff100cc9af1a0d2a66b`. Ruff lint/format, generated schema checks,
wheel/sdist builds and Twine metadata checks passed. Installing the wheel into a fresh
environment confirmed HTTP works without MCP or Torch installed. The local test run
emitted one upstream Starlette/httpx TestClient deprecation warning.

Independent source review found and regression tests verified fixes for truncated FLAC
acceptance, a broken adapter error translator killing the worker, and conversion requests
incorrectly inheriting synthesis-only language requirements. Additional tests reject
nonfinite JSON/configuration values and verify mid-generation SSE disconnect/replay.
CI exercises Python 3.10 and 3.13; local verification does not substitute for those runs.
