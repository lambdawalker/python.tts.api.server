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


@pytest.mark.parametrize("configured_tokens", ["{}", '{"secret":"alice"}', "invalid-json"])
def test_explicit_no_auth_allows_lan_and_overrides_environment(configured_tokens):
    from tempfile import TemporaryDirectory

    from fastapi.testclient import TestClient

    from tts_api_server.cli import build_parser, settings_from_args
    from tts_api_server.fake import FakeAdapter
    from tts_api_server.http import create_app

    with TemporaryDirectory() as directory:
        args = build_parser().parse_args(
            ["--fake", "--host", "0.0.0.0", "--no-auth", "--data-dir", directory]
        )
        settings = settings_from_args(args, {"TTS_API_TOKENS": configured_tokens})
        assert settings.tokens == {}
        with TestClient(create_app([FakeAdapter()], settings)) as client:
            assert client.get("/v1/models").status_code == 200
            assert (
                client.get("/v1/models", headers={"Authorization": "Bearer ignored"}).status_code
                == 200
            )
