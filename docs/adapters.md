# Writing an engine adapter

The public adapter API is in [`adapter.py`](../src/tts_api_server/adapter.py).
See [`FakeAdapter`](../src/tts_api_server/fake.py) for a runnable transport fixture.
It generates silence; it must never stand in for a claimed model integration.

Implement an `Adapter` with these methods:

| Method | Contract |
|---|---|
| `profiles() -> list[Profile]` | Exact checkpoint/revision/mode, versions, readiness, limits and controls; no weight load |
| `guidance(profile, feature) -> dict` | Caller-facing input rules, examples, limitations and authoritative sources |
| `validate(operation, profile, request, voice)` | Validate native combinations; raise `DomainError` before queueing |
| `load(profile)` / `unload(profile)` | Serialized resource lifetime; switch/unload before loading another profile |
| `execute(operation, profile, request, voice, context) -> list[AudioOutput]` | Execute and return encoded containers with native provenance |
| `translate_error(exception) -> DomainError` | Classify native failures without exposing filesystem paths or credentials |

`validate` may run concurrently with inference; it must not load weights or mutate
shared model state. `voice_registration` is also passed to it. Other operations are
`tts`, `voice_design`, `voice_conversion`. Inputs are copied; preserve all caller-authored
content when invoking native inference. The server resolves voice selectors and stores
a reference snapshot before queueing. `Voice` may be a preset or registered reference.

`ExecutionContext` provides:

- `emit(stage, progress=None)` for actual stages and measured progress in 0–100.
- `cancelled()` for cooperative cancellation requests.
- `asset_path(asset_id)` for only the assets captured in that accepted job.

Return `AudioOutput(data=encoded_bytes, format="wav", provenance={...})`. Record native
parameters (including resolved defaults), supported seed, inference mode, output encoding
and any technical decoding/resampling. The server adds checkpoint/adapter/guidance revisions
and validates the returned container and sample rate. Do not return bare float arrays.

Profiles declare languages exactly as accepted on the wire. Map documented language codes
to native enums without changing language meaning. `inline_tags` and `feature_combinations`
describe controls; `validate` must enforce native combinations. Namespaced extensions use
JSON Schema Draft 2020-12; close object schemas with `additionalProperties: false`, including
nested objects. No arbitrary keyword forwarding. Advertise only tested controls.

The profile's reference transcript flag expresses an unconditional requirement. For
mode-dependent rules (e.g. Qwen ICL versus x-vector-only), document combinations and enforce
in native validation at synthesis. Reference registration can store an optional transcript;
it does not run ASR or prepare expensive embeddings. Cache embeddings inside the adapter
using model revision, asset identity, transcript and native mode as the cache key.

Initial output containers are WAV and FLAC. Only advertise formats the server can validate.
`native_incremental_audio` must remain false: this initial execution interface returns
complete audio, with lifecycle SSE delivered independently.

## Qwen integration mapping

A future adapter in [dgxspark.qwen3TTS](https://github.com/lambdawalker/dgxspark.qwen3TTS)
should supply separate Base 0.6B/1.7B, CustomVoice 0.6B/1.7B and VoiceDesign 1.7B profiles.
Map speech to `generate_voice_clone` or `generate_custom_voice`; map design to
`generate_voice_design`. Reject instructions for 0.6B CustomVoice and Base. Keep
voice conversion unsupported. A design candidate becomes reusable only after explicit
registration as a Base reference voice. This server does not implement that adapter.

Create a zero-argument factory in your installed engine package and launch:

```bash
tts-api-server --adapter your_package.adapter:create_adapter --default-model your-profile
```

Use the [central adapter design](https://github.com/lambdawalker/design.ai/blob/main/tts/adapters.md)
and this package's tests as the conformance baseline. Test inference on the actual hardware
before claiming model readiness, cancellation boundaries or acoustic quality.
