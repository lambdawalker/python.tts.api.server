"""Deployment entry point: explicit adapters, one process, optional MCP."""

import argparse
import importlib
import json
import os
from pathlib import Path

import uvicorn

from .adapter import Adapter
from .http import create_app
from .service import Settings


def build_parser():
    parser = argparse.ArgumentParser(description="Shared local TTS HTTP/MCP server")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--adapter",
        action="append",
        metavar="MODULE:FACTORY",
        help="Zero-argument factory returning an Adapter; repeat for more adapters",
    )
    source.add_argument("--fake", action="store_true", help="Test only: generate silent WAVs")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data-dir", type=Path, default=Path(".tts-data"))
    parser.add_argument("--default-model")
    parser.add_argument("--mcp", action="store_true")
    parser.add_argument("--max-queue", type=int, default=32)
    parser.add_argument("--max-upload-bytes", type=int, default=32 * 1024 * 1024)
    parser.add_argument("--asset-ttl", type=float, default=86400)
    parser.add_argument("--event-ttl", type=float, default=86400)
    parser.add_argument("--event-limit", type=int, default=1000)
    parser.add_argument("--idempotency-ttl", type=float, default=86400)
    return parser


def settings_from_args(args, environment):
    tokens = json.loads(environment.get("TTS_API_TOKENS", "{}"))
    if not isinstance(tokens, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in tokens.items()
    ):
        raise ValueError("TTS_API_TOKENS must map token strings to caller ID strings")
    if not tokens and args.host not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("Unauthenticated mode requires a loopback host. Set TTS_API_TOKENS.")
    return Settings(
        data_dir=args.data_dir,
        default_model=args.default_model,
        max_queue=args.max_queue,
        max_upload_bytes=args.max_upload_bytes,
        asset_ttl=args.asset_ttl,
        event_ttl=args.event_ttl,
        event_limit=args.event_limit,
        idempotency_ttl=args.idempotency_ttl,
        tokens=tokens,
    )


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        settings = settings_from_args(args, os.environ)
        if args.fake:
            from .fake import FakeAdapter

            adapters = [FakeAdapter()]
        else:
            adapters = []
            for target in args.adapter:
                module, separator, factory = target.partition(":")
                if not separator:
                    raise ValueError("--adapter must be MODULE:FACTORY")
                adapter = getattr(importlib.import_module(module), factory)()
                if not isinstance(adapter, Adapter):
                    raise ValueError("Adapter factories must return an Adapter instance")
                adapters.append(adapter)
        app = create_app(adapters, settings, enable_mcp=args.mcp)
    except (ValueError, ImportError, AttributeError, RuntimeError) as exc:
        parser.error(str(exc))
    uvicorn.run(app, host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
