# Server development guidance

Architecture authority: https://github.com/lambdawalker/design.ai/tree/main/tts.
Client: https://github.com/lambdawalker/python.tts.api.client.

- HTTP and MCP must call the same Service; adapters must never own routes or queues.
- Preserve text, tags, instructions and transcripts exactly. Validate without repair.
- Keep Torch, CUDA and model weights out of this package. FakeAdapter produces silence only.
- Keep submission/idempotency/job/event changes atomic. Never rerun interrupted inference.
- Scope assets, voices, jobs and idempotency to the authenticated caller on both transports.
- Test cancellation, disconnects, crash recovery and native adapter failure, not just happy paths.
- Run `uv sync --extra mcp`, `uv run pytest`, `uv run ruff check .`,
  `uv run ruff format --check .`, `uv run python scripts/export_schemas.py --check`, and `uv build`.
- For SDK conformance, set TTS_CLIENT_SOURCE to a checkout of python.tts.api.client.
  CI pins a client commit; update intentionally when the shared contract changes.
- Do not claim GPU or real-model conformance from fake-adapter tests.
- Do not publish a release or choose a license without owner authorization.
