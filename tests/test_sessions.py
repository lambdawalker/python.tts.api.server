import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_service import wav

from tts_api_server.cli import build_parser, settings_from_args
from tts_api_server.fake import FakeAdapter
from tts_api_server.http import create_app
from tts_api_server.service import Settings


def issue(client):
    response = client.post("/v1/sessions")
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def bearer(session):
    return {"Authorization": "Bearer " + session["access_token"]}


def test_sessions_isolate_all_resources_and_idempotency(tmp_path):
    app = create_app([FakeAdapter()], Settings(data_dir=tmp_path, anonymous_sessions=True))
    with TestClient(app) as client:
        a, b = issue(client), issue(client)
        ha, hb = bearer(a), bearer(b)
        assert a["session_id"] != b["session_id"]
        assert client.get("/v1/models").status_code == 401
        assert (
            client.get(
                "/v1/models", headers={"Authorization": "Bearer " + a["session_id"]}
            ).status_code
            == 401
        )
        asset = client.post(
            "/v1/assets", headers=ha, files={"file": ("ref.wav", wav(), "audio/wav")}
        ).json()
        voice = client.post(
            "/v1/voices", headers=ha, json={"references": [{"asset_id": asset["id"]}]}
        ).json()
        job = client.post(
            "/v1/speech", headers={**ha, "Idempotency-Key": "same"}, json={"text": "hello"}
        ).json()
        for path, method in [
            (job["status_url"], "get"),
            (job["events_url"], "get"),
            (job["status_url"] + "/cancel", "post"),
            ("/v1/assets/" + asset["id"], "get"),
            ("/v1/voices/" + voice["id"], "delete"),
        ]:
            assert getattr(client, method)(path, headers=hb).status_code == 404
        assert client.get("/v1/voices", headers=hb).json()["voices"] == []
        forbidden = client.post(
            "/v1/speech", headers=hb, json={"text": "hi", "voice": {"id": voice["id"]}}
        )
        assert forbidden.status_code == 404
        other = client.post(
            "/v1/speech", headers={**hb, "Idempotency-Key": "same"}, json={"text": "hello"}
        ).json()
        assert other["id"] != job["id"]
        assert client.get(job["status_url"], headers=ha).status_code == 200
        response = client.delete("/v1/sessions/current", headers=ha)
        assert response.status_code == 204 and response.headers["cache-control"] == "no-store"
        assert client.get(job["status_url"], headers=ha).status_code == 401
        assert client.get("/v1/models", headers=hb).status_code == 200
        rows = app.state.service.store.db.execute("SELECT * FROM sessions").fetchall()
        assert b["access_token"] not in json.dumps(rows)


def test_restart_expiration_and_admission_limit(tmp_path):
    now = [1000.0]
    settings = Settings(
        data_dir=tmp_path,
        anonymous_sessions=True,
        session_ttl=10,
        max_sessions=1,
        clock=lambda: now[0],
    )
    with TestClient(create_app([FakeAdapter()], settings)) as client:
        a = issue(client)
        assert client.post("/v1/sessions").status_code == 429
        assert client.post("/v1/sessions", json={"owner": "local"}).status_code == 422
    with TestClient(create_app([FakeAdapter()], settings)) as client:
        assert client.get("/v1/models", headers=bearer(a)).status_code == 200
        now[0] += 10
        assert client.get("/v1/models", headers=bearer(a)).status_code == 401
        b = issue(client)
        assert b["session_id"] != a["session_id"]


def test_cli_session_modes_and_settings():
    parser = build_parser()
    args = parser.parse_args(["--fake", "--host", "0.0.0.0", "--anonymous-sessions"])
    assert settings_from_args(args, {}).anonymous_sessions
    with pytest.raises(ValueError, match="static tokens"):
        settings_from_args(args, {"TTS_API_TOKENS": '{"secret":"alice"}'})
    with pytest.raises(SystemExit):
        parser.parse_args(["--fake", "--anonymous-sessions", "--no-auth"])
    with pytest.raises(ValueError):
        Settings(session_ttl=0)
    with pytest.raises(ValueError):
        Settings(max_sessions=0)


def test_session_endpoint_disabled_in_existing_modes(tmp_path):
    with TestClient(create_app([FakeAdapter()], Settings(data_dir=tmp_path))) as client:
        assert client.post("/v1/sessions").json()["error"]["code"] == "unsupported_feature"


def test_mcp_session_isolation(tmp_path, live_server):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    app = create_app(
        [FakeAdapter()], Settings(data_dir=tmp_path, anonymous_sessions=True), enable_mcp=True
    )
    with live_server(app) as url:
        with httpx.Client(base_url=url, trust_env=False) as http:
            a = http.post("/v1/sessions").json()
            b = http.post("/v1/sessions").json()

        async def call(session, tool, arguments):
            async with httpx.AsyncClient(headers=bearer(session), trust_env=False) as http:
                async with streamable_http_client(url + "/mcp", http_client=http) as streams:
                    async with ClientSession(streams[0], streams[1]) as mcp:
                        await mcp.initialize()
                        return await mcp.call_tool(tool, arguments)

        job = asyncio.run(call(a, "generate_speech", {"text": "hi"}))
        assert not job.isError
        denied = asyncio.run(call(b, "get_job", {"job_id": job.structuredContent["id"]}))
        assert denied.isError and denied.structuredContent["error"]["code"] == "job_not_found"


def test_python_clients_create_resume_and_isolate_sessions(tmp_path, live_server):
    import os
    import sys
    from pathlib import Path

    source = os.environ.get("TTS_CLIENT_SOURCE")
    if source:
        sys.path.insert(0, str(Path(source) / "src"))
    sdk = pytest.importorskip("tts_api_client")
    app = create_app([FakeAdapter()], Settings(data_dir=tmp_path, anonymous_sessions=True))
    with live_server(app) as url:
        with sdk.TTSClient(url, anonymous_session=True) as a:
            job = a.generate_speech(text="hello")
            done = job.wait(timeout=3, poll_interval=0.01)
            token = a.session_token
            assert list(a.events(job.id))
            a.download_asset(done.results[0].asset_id, tmp_path / "result.wav")
        with sdk.TTSClient(url, session_token=token) as resumed:
            assert resumed.get_job(job.id).status == "succeeded"

        async def check():
            async with sdk.AsyncTTSClient(url, anonymous_session=True) as b:
                with pytest.raises(sdk.APIError) as error:
                    await b.get_job(job.id)
                assert error.value.status_code == 404
                own = await b.generate_speech(text="other")
                await own.wait(timeout=3, poll_interval=0.01)
                assert [e async for e in b.events(own.id)]
                await b.revoke_session()

        asyncio.run(check())


@pytest.mark.parametrize("action", ["expire", "revoke"])
def test_open_sse_closes_when_session_loses_access(tmp_path, live_server, action):
    import threading

    entered, release = threading.Event(), threading.Event()
    now = [1000.0]

    class Blocking(FakeAdapter):
        def execute(self, *args):
            entered.set()
            assert release.wait(5)
            return super().execute(*args)

    app = create_app(
        [Blocking()],
        Settings(data_dir=tmp_path, anonymous_sessions=True, session_ttl=10, clock=lambda: now[0]),
    )
    with live_server(app) as url:
        try:
            with httpx.Client(base_url=url, trust_env=False, timeout=3) as client:
                session = client.post("/v1/sessions").json()
                headers = bearer(session)
                job = client.post("/v1/speech", headers=headers, json={"text": "hi"}).json()
                assert entered.wait(2)
                with client.stream("GET", job["events_url"], headers=headers) as response:
                    lines = response.iter_lines()
                    assert next(lines).startswith("id:")
                    if action == "expire":
                        now[0] += 10
                    else:
                        assert (
                            client.delete("/v1/sessions/current", headers=headers).status_code
                            == 204
                        )
                    list(lines)  # Must terminate despite the job still running.
                assert client.get(job["status_url"], headers=headers).status_code == 401
        finally:
            release.set()
