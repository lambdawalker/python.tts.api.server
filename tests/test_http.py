import importlib.util

from test_service import wav


def test_http_module_exists():
    assert importlib.util.find_spec("tts_api_server.http") is not None


def app(tmp_path, **kwargs):
    from tts_api_server.fake import FakeAdapter
    from tts_api_server.http import create_app
    from tts_api_server.service import Settings

    return create_app([FakeAdapter()], Settings(data_dir=tmp_path, **kwargs))


def test_http_roundtrip_and_errors(tmp_path):
    from fastapi.testclient import TestClient

    with TestClient(app(tmp_path)) as c:
        assert c.get("/v1/models").json()["models"][0]["id"] == "fake-v1"
        r = c.get("/v1/capabilities")
        assert (
            c.get("/v1/capabilities", headers={"If-None-Match": r.headers["etag"]}).status_code
            == 304
        )
        assert c.get("/v1/guidance/tts").json()["revision"] == "1"
        assert c.post("/v1/speech/validate", json={"text": "hello"}).json()["valid"]
        error = c.post("/v1/speech", json={"text": "hello", "unknown": 1})
        assert error.status_code == 422
        assert error.json()["error"]["code"] == "invalid_request"
        assert (
            c.post("/v1/speech", content="{", headers={"Content-Type": "application/json"}).json()[
                "error"
            ]["code"]
            == "malformed_request"
        )
        a = c.post("/v1/assets", files={"file": ("ref.wav", wav())})
        assert a.status_code == 201
        v = c.post("/v1/voices", json={"alias": "a", "references": [{"asset_id": a.json()["id"]}]})
        assert v.status_code == 201
        r = c.post(
            "/v1/speech",
            json={"text": "hello", "voice": {"alias": "a"}},
            headers={"Idempotency-Key": "test"},
        )
        assert r.status_code == 202
        job = r.json()
        with c.stream("GET", job["events_url"]) as events:
            body = "".join(events.iter_text())
        assert "event: status" in body and "succeeded" in body
        done = c.get(job["status_url"]).json()
        assert done["status"] == "succeeded"
        assert c.get("/v1/assets/" + done["results"][0]["asset_id"]).content[:4] == b"RIFF"
        assert c.post(job["status_url"] + "/cancel").json()["status"] == "succeeded"
        assert c.delete("/v1/voices/" + v.json()["id"]).status_code == 204
        assert (
            c.post(
                "/v1/voice-conversions",
                json={"source_asset_id": a.json()["id"], "voice": {"id": "x"}},
            ).json()["error"]["code"]
            == "unsupported_feature"
        )
        assert c.get("/openapi.json").json()["paths"]["/v1/speech"]["post"]["requestBody"]


def test_auth_and_body_limits(tmp_path):
    from fastapi.testclient import TestClient

    with TestClient(
        app(
            tmp_path,
            tokens={"token-a": "alice", "token-b": "bob"},
            max_upload_bytes=1024,
            max_request_bytes=128,
        )
    ) as c:
        assert c.get("/v1/models").status_code == 401
        a = {"Authorization": "Bearer token-a"}
        b = {"Authorization": "Bearer token-b"}
        result = c.post("/v1/speech", json={"text": "hi"}, headers=a)
        assert result.status_code == 202
        assert c.get(result.json()["status_url"], headers=b).status_code == 404
        assert c.post("/v1/speech", json={"text": "x" * 200}, headers=a).status_code == 413
        assert (
            c.post("/v1/assets", files={"file": ("bad.wav", b"x" * 2048)}, headers=a).status_code
            == 413
        )
        assert (
            c.post("/v1/assets", files={"file": ("bad.wav", b"bad")}, headers=a).status_code == 422
        )
