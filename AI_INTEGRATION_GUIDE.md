# Agent integration

Use the [HTTP client](https://github.com/lambdawalker/python.tts.api.client) or connect an
MCP host to the deployment's `/mcp` endpoint. Obtain credentials from connection configuration,
not tool arguments. This repository contains the shared server and an explicit silent test
adapter, not model weights or working Qwen/Fish/Chatterbox integrations.

1. List models and select a ready/loadable profile.
2. Read capabilities and feature guidance for that exact profile.
3. Upload reference audio with the HTTP client if needed; register asset IDs as a voice.
4. Author suitable native text/instructions. The service will not translate, strip or repair them.
5. Validate speech, then submit once with a stable idempotency key.
6. Poll the durable job or consume reconnectable SSE. Download result asset IDs through HTTP.
7. Only request cancellation deliberately; closing a connection leaves work running.

Refresh guidance and voice selections when changing deployment or model. IDs and embeddings
are deployment-local. See [contracts](docs/contracts.md), [deployment](docs/deployment.md),
[adapter integration](docs/adapters.md) and the
[central architecture](https://github.com/lambdawalker/design.ai/tree/main/tts).
