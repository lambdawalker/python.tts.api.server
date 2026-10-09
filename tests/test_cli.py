import importlib.util

import pytest


def test_cli_exists():
    assert importlib.util.find_spec("tts_api_server.cli") is not None


def test_requires_explicit_adapter_and_protects_unauthenticated_bind():
    from tts_api_server.cli import build_parser, settings_from_args

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    with pytest.raises(ValueError, match="loopback"):
        settings_from_args(parser.parse_args(["--fake", "--host", "0.0.0.0"]), {})
    settings = settings_from_args(parser.parse_args(["--fake"]), {})
    assert settings.tokens == {}
    assert settings_from_args(
        parser.parse_args(["--fake", "--host", "0.0.0.0"]), {"TTS_API_TOKENS": '{"abc":"alice"}'}
    ).tokens == {"abc": "alice"}
