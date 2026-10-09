# Shared TTS API server

An engine-independent HTTP and optional MCP server for local text-to-speech deployments.
It implements the [central TTS design](https://github.com/lambdawalker/design.ai/tree/main/tts)
and works with the existing [Python HTTP client](https://github.com/lambdawalker/python.tts.api.client).

**Status:** initial implementation. The included test adapter generates silent WAV audio;
real Qwen, Fish and Chatterbox adapters belong in their engine repositories and are not
bundled or GPU-tested here. This repository is not evidence of a published PyPI release.

## Run from source

Python 3.10+; use the Python version required by your engine deployment (the Qwen Spark
fork uses Python 3.13). This package does not install Torch, CUDA or model weights.

```bash
git clone https://github.com/lambdawalker/python.tts.api.server.git
cd python.tts.api.server
uv sync --extra mcp
uv run tts-api-server --fake --mcp --data-dir .tts-data
```

HTTP documentation: http://127.0.0.1:8000/docs. OpenAPI: `/openapi.json`.
MCP Streamable HTTP: `http://127.0.0.1:8000/mcp` when enabled.
The fake adapter is opt-in and is for testing only.

```bash
curl http://127.0.0.1:8000/v1/capabilities
curl -X POST http://127.0.0.1:8000/v1/speech \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: example-1' \
  -d '{"text":"Hello","model":"fake-v1"}'
# Use the returned status_url and events_url; results contain downloadable asset IDs.
```

Install a built wheel into a model environment and load its adapter factory:

```bash
uv build
# In the engine environment, install the resulting wheel with uv pip install.
tts-api-server --adapter my_engine.tts_adapter:create_adapter --default-model my-profile --mcp
```

`my_engine` is an illustrative adapter, not an installed integration. A factory returns
an `Adapter` instance. Multiple `--adapter` arguments may register disjoint profile IDs.

## What is implemented

- All `/v1` discovery, guidance, asset, voice, validation, synthesis, design, conversion,
  job, event and cancellation routes. Unsupported operations return `unsupported_feature`.
- Strict request schemas shared by HTTP and MCP; no input rewriting or silent control removal.
- SQLite job/event/idempotency persistence and atomic admission; one inference worker.
- Reconnectable SSE (`Last-Event-ID`), independent jobs, cooperative cancellation, explicit restart failures.
- Caller-scoped bearer authentication across HTTP/MCP/assets; bounded request and upload sizes.
- WAV/FLAC asset validation, result metadata, expiry, bounded event retention and ETags.
- Optional MCP tools with the same validation and errors; no internal HTTP round trips.

## Client example

In an environment containing the client package:

```python
from tts_api_client import TTSClient

with TTSClient("http://127.0.0.1:8000") as client:
    print(client.capabilities())
    print(client.guidance("tts"))
    job = client.generate_speech(text="Hello", idempotency_key="example-2")
    result = job.wait(timeout=60)
    client.download_asset(result.results[0].asset_id, "output.wav")
```

This produces silence with `--fake`. Choose a real adapter for speech.
Do not reuse an idempotency key for a different request.

## Documentation

- [Deployment, authentication and retention](docs/deployment.md)
- [Adapter authoring](docs/adapters.md)
- [HTTP and MCP contract](docs/contracts.md)
- [Implementation and validation evidence](docs/testing.md)
- [Agent integration guide](AI_INTEGRATION_GUIDE.md)
- [Generated schemas](schemas/)

## Development

```bash
uv sync --extra mcp
uv run pytest
# Point at a client checkout for real sync/async SDK integration tests:
TTS_CLIENT_SOURCE=../python.tts.api.client uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run python scripts/export_schemas.py --check
uv build
uv run twine check dist/*
```

No license or automatic PyPI publishing has been selected for this new repository.
