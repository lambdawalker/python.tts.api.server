"""Real loopback HTTP and official MCP protocol tests; no inference framework required."""

import asyncio
import os
import sys
from pathlib import Path

import pytest
from test_service import wav

from tts_api_server.fake import FakeAdapter
from tts_api_server.http import create_app
from tts_api_server.service import Settings


def test_existing_sync_and_async_client(tmp_path, live_server):
    source = os.environ.get("TTS_CLIENT_SOURCE")
    if source:
        sys.path.insert(0, str(Path(source) / "src"))
    sdk = pytest.importorskip("tts_api_client")
    app = create_app(
        [FakeAdapter()], Settings(data_dir=tmp_path / "server", tokens={"token": "alice"})
    )
    with live_server(app) as url:
        with sdk.TTSClient(url, api_key="token") as client:
            assert client.list_models()[0].id == "fake-v1"
            assert client.capabilities().model == "fake-v1"
            assert client.capabilities().model == "fake-v1"  # 304 cache path
            assert client.guidance("tts").feature == "tts"
            path = tmp_path / "reference.wav"
            path.write_bytes(wav())
            asset = client.upload_asset(path)
            voice = client.register_voice(alias="narrator", references=[{"asset_id": asset.id}])
            assert client.validate_speech(text=" hi ", voice={"id": voice.id}).valid
            job = client.generate_speech(
                text=" hi ", voice={"alias": "narrator"}, idempotency_key="sdk"
            )
            done = job.wait(timeout=3, poll_interval=0.01)
            assert done.status == "succeeded"
            events = list(client.events(job.id))
            assert any(e.event == "status" for e in events)
            output = client.download_asset(done.results[0].asset_id, tmp_path / "output.wav")
            assert output.read_bytes()[:4] == b"RIFF"
            assert (
                client.generate_speech(
                    text=" hi ", voice={"alias": "narrator"}, idempotency_key="sdk"
                ).id
                == job.id
            )
            client.delete_voice(voice.id)

        async def async_flow():
            async with sdk.AsyncTTSClient(url, api_key="token") as client:
                assert (await client.capabilities()).api_version == "1.0"
                job = await client.design_voice(description="test voice", preview_text="hi")
                result = await job.wait(timeout=3, poll_interval=0.01)
                assert result.status == "succeeded"
                events = [event async for event in client.events(job.id)]
                assert events

        asyncio.run(async_flow())


def test_mcp_protocol_auth_and_structured_errors(tmp_path, live_server):
    import httpx
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    app = create_app(
        [FakeAdapter()],
        Settings(data_dir=tmp_path, tokens={"token-a": "alice", "token-b": "bob"}),
        enable_mcp=True,
    )
    with live_server(app) as url:

        async def call(token, name, args):
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer " + token}, trust_env=False
            ) as http:
                async with streamable_http_client(url + "/mcp", http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as session:
                        await session.initialize()
                        names = {t.name for t in (await session.list_tools()).tools}
                        assert "generate_speech" in names
                        return await session.call_tool(name, args)

        result = asyncio.run(
            call("token-a", "generate_speech", {"text": "hello", "idempotency_key": "one"})
        )
        assert not result.isError
        identifier = result.structuredContent["id"]
        denied = asyncio.run(call("token-b", "get_job", {"job_id": identifier}))
        assert denied.isError
        assert denied.structuredContent["error"]["code"] == "job_not_found"
        invalid = asyncio.run(call("token-a", "validate_speech", {"text": "hi", "language": "xx"}))
        assert invalid.isError
        assert invalid.structuredContent["error"]["code"] == "unsupported_language"


def test_disconnect_does_not_cancel_and_sse_replays(tmp_path, live_server):
    import threading
    import time

    import httpx

    entered, release = threading.Event(), threading.Event()

    class Block(FakeAdapter):
        def execute(self, *args):
            entered.set()
            assert release.wait(3)
            return super().execute(*args)

    app = create_app([Block()], Settings(data_dir=tmp_path))
    with live_server(app) as url:
        try:
            with httpx.Client(base_url=url, trust_env=False) as c:
                job = c.post("/v1/speech", json={"text": "hello"}).json()
                assert entered.wait(2)
                with c.stream("GET", job["events_url"]) as response:
                    first_id = next(
                        line.split(": ", 1)[1]
                        for line in response.iter_lines()
                        if line.startswith("id: ")
                    )
                assert c.get(job["status_url"]).json()["status"] == "running"
            release.set()
            with httpx.Client(base_url=url, trust_env=False) as other:
                deadline = time.monotonic() + 3
                while other.get(job["status_url"]).json()["status"] != "succeeded":
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                events = other.get(job["events_url"], headers={"Last-Event-ID": first_id}).text
                ids = [
                    int(line.split(": ", 1)[1])
                    for line in events.splitlines()
                    if line.startswith("id: ")
                ]
                assert ids and min(ids) > int(first_id)
                assert "succeeded" in events
        finally:
            release.set()
