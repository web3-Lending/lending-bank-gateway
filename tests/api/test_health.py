from fastapi.testclient import TestClient


def test_healthz_ok(app) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(app)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["success"] is True and r.json()["trace_id"]


def test_trace_id_echo(app) -> None:  # type: ignore[no-untyped-def]
    client = TestClient(app)
    r = client.get("/healthz", headers={"X-Trace-Id": "trc-echo"})
    assert r.json()["trace_id"] == "trc-echo"
